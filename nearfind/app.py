import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from hmac import compare_digest

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from config import ALLOWED_PROVIDER_TYPES, ALLOWED_ROLES, ALLOWED_URGENCY, Config
from services.location_service import coerce_float, haversine, public_provider_location
from services.storage_service import upload_public_image
from services.supabase_service import SupabaseRequestError, get_supabase, table

app = Flask(__name__)
app.config.from_object(Config)
logging.basicConfig(level=logging.INFO)


def json_response(success=True, data=None, error=None, status=200, preserve_none=False, error_code=None):
    payload = {"success": success}
    if success:
        payload["data"] = data if preserve_none else (data or {})
    else:
        payload["error"] = {"code": error_code or f"http_{status}", "message": error or "Something went wrong"}
    return jsonify(payload), status


def wants_json():
    if request.is_json:
        return True
    api_paths = (
        "/query/list",
        "/query/responses/",
        "/query/select-provider",
        "/query/resolve",
        "/upload/",
        "/user/",
        "/location/",
        "/notifications",
    )
    if request.path.startswith(api_paths) or request.path == "/query/respond" and request.headers.get("Accept", "").startswith("application/json") or re.match(r"^/query/[^/]+/provider-location$", request.path):
        return True
    return request.path.startswith("/chat/") and request.path.count("/") >= 3


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


def csrf_error():
    if wants_json():
        # Preserve the established CSRF response for existing clients.
        return jsonify({"success": False, "error": "Invalid or missing CSRF token."}), 403
    return render_template("error.html", message="Invalid or missing CSRF token."), 403


@app.before_request
def protect_state_changing_requests():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    expected = session.get("_csrf_token")
    supplied = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
    if not expected or not supplied or not compare_digest(expected, supplied):
        return csrf_error()
    return None


def require_supabase_ready():
    if not Config.SUPABASE_URL or not Config.SUPABASE_ANON_KEY or not Config.SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase environment variables are not fully configured")


def valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def parse_json_or_form():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def clean_text(value, max_len=500, required=True):
    value = (value or "").strip()
    if required and not value:
        raise ValueError("Required field is missing")
    if len(value) > max_len:
        raise ValueError(f"Field must be {max_len} characters or fewer")
    return value


def safe_supabase_message(error):
    if error.status_code == 429:
        return "Supabase Auth rate limit exceeded. Please wait and try again."
    if error.status_code in {400, 422}:
        return "Unable to complete the request. Please check your input."
    if error.status_code in {401, 403}:
        return "Authentication failed or access was denied."
    if 400 <= error.status_code < 500:
        return "Unable to complete the request."
    return "Unable to complete the request. Please try again."


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            if wants_json():
                return json_response(False, error="Your session has expired. Please log in again.", status=401, error_code="authentication_required")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def seeker_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") != "seeker":
            if wants_json():
                return json_response(False, error="Seeker access required", status=403, error_code="forbidden")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)

    return wrapper


def provider_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") != "provider":
            if wants_json():
                return json_response(False, error="Provider access required", status=403, error_code="forbidden")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)

    return wrapper


def current_profile():
    user_id = session.get("user_id")
    if not user_id:
        return None
    result = table("users").select("*").eq("id", user_id).maybe_single().execute()
    return result.data


def require_query_owner(query_id):
    if not valid_uuid(query_id):
        return None
    result = (
        table("queries")
        .select("*")
        .eq("id", query_id)
        .eq("seeker_id", session["user_id"])
        .maybe_single()
        .execute()
    )
    return result.data


def get_query(query_id):
    if not valid_uuid(query_id):
        return None
    result = table("queries").select("*").eq("id", query_id).maybe_single().execute()
    return result.data


def parse_expiration(value):
    hours = int(value or 6)
    if hours not in {2, 6, 12, 24}:
        raise ValueError("Expiration must be 2, 6, 12 or 24 hours")
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def query_is_expired(query):
    expires_at = (query or {}).get("expires_at")
    if not expires_at:
        return False
    try:
        expiration = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    return expiration <= datetime.now(timezone.utc)


def expire_open_queries():
    now = datetime.now(timezone.utc).isoformat()
    expired = (
        table("queries")
        .select("id")
        .eq("status", "open")
        .lt("expires_at", now)
        .execute()
        .data
        or []
    )
    if expired:
        ids = [row["id"] for row in expired]
        table("queries").update({"status": "expired"}).in_("id", ids).eq("status", "open").execute()


def query_response_count(query_id):
    rows = table("responses").select("id").eq("query_id", query_id).execute().data or []
    return len(rows)


def provider_response_summary(provider_id):
    rows = table("responses").select("status").eq("provider_id", provider_id).execute().data or []
    summary = {"total": len(rows), "available": 0, "selected": 0, "rejected": 0, "sold": 0}
    for row in rows:
        status = row.get("status")
        if status in summary:
            summary[status] += 1
    return summary


def selected_provider_responses(provider_id):
    try:
        return (
            table("responses")
            .select("id,query_id,status,created_at,queries(item_name)")
            .eq("provider_id", provider_id)
            .eq("status", "selected")
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        app.logger.exception("Selected response lookup failed")
        return []


def provider_query_eligibility(query, provider):
    if not query:
        return False, "Request not found.", 404
    if query.get("status") != "open" or query_is_expired(query):
        return False, "This request is no longer open.", 409
    if not provider or provider.get("is_active") is not True:
        return False, "Provider account is inactive.", 403
    if not provider or provider.get("lat") is None or provider.get("lng") is None:
        return False, "Update your location before responding to requests.", 403
    if query.get("lat") is None or query.get("lng") is None:
        return False, "This request does not have a valid location.", 409
    distance = haversine(float(provider["lat"]), float(provider["lng"]), float(query["lat"]), float(query["lng"]))
    if distance > float(query.get("radius_km") or 0):
        return False, "This request is outside your service radius.", 403
    return True, distance, 200


def is_response_conflict(error):
    if not isinstance(error, SupabaseRequestError) or error.status_code != 409:
        return False
    detail = str(error.message).lower()
    return "responses_one_active_per_provider_query" in detail or "duplicate key" in detail


NOTIFICATION_NAMESPACE = uuid.UUID("6c7e8d6f-6c8b-4f4d-9e31-1f0cf9a8d4b5")


def notification_id(user_id, notification_type, query_id, response_id=None, identity_id=None):
    identity = ":".join(str(value or "") for value in (user_id, notification_type, query_id, response_id, identity_id or response_id))
    return str(uuid.uuid5(NOTIFICATION_NAMESPACE, identity))


def create_notification(user_id, notification_type, title, message, query_id, response_id=None, identity_id=None):
    record = {
        "id": notification_id(user_id, notification_type, query_id, response_id, identity_id),
        "user_id": user_id,
        "type": notification_type,
        "title": title,
        "message": message,
        "query_id": query_id,
        "response_id": response_id,
        "is_read": False,
    }
    try:
        table("notifications").insert(record).execute()
        return True
    except SupabaseRequestError as error:
        if error.status_code == 409:
            return False
        app.logger.warning("Notification creation failed: %s", error.message)
    except Exception:
        app.logger.exception("Notification creation failed")
    return False


def ensure_conversation(query_id, response_id, seeker_id, provider_id):
    conversation_id = str(uuid.uuid5(NOTIFICATION_NAMESPACE, f"conversation:{response_id}"))
    try:
        existing = table("conversations").select("*").eq("response_id", response_id).maybe_single().execute().data
        if existing:
            return existing
        record = {
            "id": conversation_id,
            "query_id": query_id,
            "response_id": response_id,
            "seeker_id": seeker_id,
            "provider_id": provider_id,
        }
        table("conversations").insert(record).execute()
        return record
    except Exception:
        app.logger.exception("Conversation creation failed for response %s", response_id)
        return None


def get_conversation_member(conversation_id):
    if not valid_uuid(conversation_id):
        return None
    conversation = table("conversations").select("*").eq("id", conversation_id).maybe_single().execute().data
    if not conversation:
        return None
    user_id = session.get("user_id")
    if user_id not in {conversation.get("seeker_id"), conversation.get("provider_id")}:
        return None
    return conversation


def notify_nearby_providers(query):
    try:
        providers = (
            table("users")
            .select("id,role,is_active,lat,lng")
            .eq("role", "provider")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
    except Exception:
        app.logger.exception("Nearby provider notification lookup failed")
        return
    for provider in providers:
        if provider.get("role") != "provider" or provider.get("is_active") is not True:
            continue
        if provider.get("lat") is None or provider.get("lng") is None:
            continue
        distance = haversine(float(query["lat"]), float(query["lng"]), float(provider["lat"]), float(provider["lng"]))
        if distance <= float(query.get("radius_km") or 0):
            create_notification(
                provider["id"],
                "new_request",
                "New nearby request",
                f"{query['item_name']} is needed nearby.",
                query["id"],
            )


def notify_provider_for_response(query, response):
    if not query.get("seeker_id") or not response.get("id"):
        return
    create_notification(
        query["seeker_id"],
        "provider_response",
        "New provider response",
        f"A provider responded to {query['item_name']}.",
        query["id"],
        response["id"],
    )


def error_code_for(status):
    return {
        400: "bad_request",
        401: "authentication_required",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        429: "rate_limited",
        500: "internal_error",
    }.get(status, f"http_{status}")


def render_request_error(status, message, log_message=None, error_code=None):
    safe_message = message
    user_id = session.get("user_id") or "anonymous"
    log_message = log_message or safe_message
    if status >= 500:
        app.logger.exception("Request failed route=%s user_id=%s error=%s", request.path, user_id, log_message)
    else:
        app.logger.warning("Request rejected route=%s user_id=%s status=%s error=%s", request.path, user_id, status, log_message)
    if wants_json():
        return jsonify({"success": False, "error": {"code": error_code or error_code_for(status), "message": safe_message}}), status
    return render_template("error.html", message=safe_message), status


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(429)
def handle_http_error(error):
    status = error.code
    message = {
        400: "The request could not be understood.",
        401: "Your session has expired. Please log in again.",
        403: "You do not have permission to access this resource.",
        404: "The requested page or resource was not found.",
        409: "The request conflicts with the current state.",
        429: "Too many requests. Please wait and try again.",
    }.get(status, "Unable to complete the request.")
    return render_request_error(status, message, log_message=error.description, error_code=error_code_for(status))


@app.errorhandler(Exception)
def handle_error(error):
    if isinstance(error, HTTPException):
        return handle_http_error(error)
    if isinstance(error, SupabaseRequestError):
        status = error.status_code if 400 <= error.status_code < 500 else 502
        message = safe_supabase_message(error)
        log_message = error.message
    else:
        status = 400 if isinstance(error, ValueError) else 500
        message = "The request could not be processed." if status == 400 else "Something went wrong. Please try again."
        log_message = repr(error)
    return render_request_error(status, message, log_message=log_message, error_code=error_code_for(status))


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/healthz")
def healthz():
    configured = all(
        (
            Config.SUPABASE_URL,
            Config.SUPABASE_ANON_KEY,
            Config.SUPABASE_SERVICE_ROLE_KEY,
        )
    )
    return jsonify({"status": "ok" if configured else "not_ready"}), 200 if configured else 503


@app.route("/")
def index():
    if session.get("role") == "seeker":
        return redirect(url_for("seeker_dashboard"))
    if session.get("role") == "provider":
        return redirect(url_for("provider_dashboard"))
    return render_template("index.html")


@app.route("/signup", methods=["GET"])
def signup():
    return render_template("signup.html")


@app.route("/login", methods=["GET"])
def login():
    return render_template("login.html")


@app.post("/auth/signup")
def auth_signup():
    require_supabase_ready()
    data = parse_json_or_form()
    email = clean_text(data.get("email"), 255).lower()
    password = data.get("password") or ""
    role = clean_text(data.get("role"), 20)
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("A valid email is required")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters")
    if role not in ALLOWED_ROLES:
        raise ValueError("Invalid role")
    provider_type = data.get("provider_type") or None
    if role == "provider" and provider_type not in ALLOWED_PROVIDER_TYPES:
        raise ValueError("Provider type must be shop or individual")
    if role == "seeker":
        provider_type = None
    lat = data.get("lat")
    lng = data.get("lng")
    lat = coerce_float(lat, "Latitude", -90, 90) if lat not in {None, ""} else None
    lng = coerce_float(lng, "Longitude", -180, 180) if lng not in {None, ""} else None

    auth_client = get_supabase(admin=False)
    auth_result = auth_client.auth.sign_up({"email": email, "password": password})
    user = auth_result.user
    if not user:
        raise RuntimeError("Supabase did not create the user")
    profile = {
        "id": user.id,
        "email": email,
        "role": role,
        "provider_type": provider_type,
        "name": clean_text(data.get("name"), 120),
        "business_name": clean_text(data.get("business_name"), 160, required=False) or None,
        "phone": clean_text(data.get("phone"), 30),
        "address": clean_text(data.get("address"), 300),
        "lat": lat,
        "lng": lng,
        "last_location_update": datetime.now(timezone.utc).isoformat() if lat and lng else None,
    }
    try:
        table("users").insert(profile).execute()
    except Exception:
        session.clear()
        app.logger.exception("Signup profile creation failed for Supabase user %s", user.id)
        return render_template("error.html", message="Your account could not be completed. Please try again."), 502
    session.clear()
    session.permanent = True
    session.update(
        user_id=user.id,
        role=role,
        email=email,
        lat=lat,
        lng=lng,
        name=profile["name"],
    )
    return redirect(url_for("seeker_dashboard" if role == "seeker" else "provider_dashboard"))


@app.post("/auth/login")
def auth_login():
    require_supabase_ready()
    data = parse_json_or_form()
    email = clean_text(data.get("email"), 255).lower()
    password = data.get("password") or ""
    auth_result = get_supabase(admin=False).auth.sign_in_with_password({"email": email, "password": password})
    user = auth_result.user
    if not user:
        raise ValueError("Invalid email or password")
    profile = table("users").select("*").eq("id", user.id).maybe_single().execute().data
    if not profile or not profile.get("is_active", True):
        raise ValueError("User profile is inactive or missing")
    session.clear()
    session.permanent = True
    session.update(
        user_id=user.id,
        role=profile["role"],
        email=profile["email"],
        lat=profile.get("lat"),
        lng=profile.get("lng"),
        name=profile.get("name"),
    )
    return redirect(url_for("seeker_dashboard" if profile["role"] == "seeker" else "provider_dashboard"))


@app.get("/notifications")
@login_required
def notifications():
    rows = (
        table("notifications")
        .select("id,type,title,message,query_id,response_id,is_read,created_at")
        .eq("user_id", session["user_id"])
        .order("created_at", desc=True)
        .limit(50)
        .execute()
        .data
        or []
    )
    return json_response(data={"notifications": rows, "unread_count": sum(not row.get("is_read") for row in rows)})


@app.post("/notifications/<notification_id>/read")
@login_required
def mark_notification_read(notification_id):
    if not valid_uuid(notification_id):
        return json_response(False, error="Invalid notification id", status=400)
    result = (
        table("notifications")
        .update({"is_read": True})
        .eq("id", notification_id)
        .eq("user_id", session["user_id"])
        .execute()
    )
    if not result.data:
        return json_response(False, error="Notification not found", status=404)
    return json_response(data={"message": "Notification marked as read."})


@app.post("/auth/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.post("/user/update-location")
@login_required
def update_location():
    data = parse_json_or_form()
    lat = coerce_float(data.get("lat"), "Latitude", -90, 90)
    lng = coerce_float(data.get("lng"), "Longitude", -180, 180)
    table("users").update(
        {"lat": lat, "lng": lng, "last_location_update": datetime.now(timezone.utc).isoformat()}
    ).eq("id", session["user_id"]).execute()
    session["lat"] = lat
    session["lng"] = lng
    return json_response(data={"lat": lat, "lng": lng})


def parse_sharing_enabled(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    raise ValueError("sharing_enabled must be true or false")


def validate_accuracy(value):
    if value in {None, ""}:
        return None
    return coerce_float(value, "Accuracy", 0, 100000)


@app.post("/location/update")
@provider_required
def update_provider_location():
    provider = current_profile()
    if not provider or provider.get("role") != "provider" or provider.get("is_active") is not True:
        return json_response(False, error="Provider account is inactive", status=403)
    data = parse_json_or_form()
    lat = coerce_float(data.get("lat"), "Latitude", -90, 90)
    lng = coerce_float(data.get("lng"), "Longitude", -180, 180)
    accuracy_m = validate_accuracy(data.get("accuracy_m"))
    sharing_enabled = parse_sharing_enabled(data.get("sharing_enabled"))
    record = {
        "provider_id": session["user_id"],
        "lat": lat,
        "lng": lng,
        "accuracy_m": accuracy_m,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sharing_enabled": sharing_enabled,
    }
    existing = table("provider_locations").select("provider_id").eq("provider_id", session["user_id"]).maybe_single().execute().data
    if existing:
        table("provider_locations").update(record).eq("provider_id", session["user_id"]).execute()
    else:
        table("provider_locations").insert(record).execute()
    return json_response(data=record)


@app.post("/location/stop")
@provider_required
def stop_provider_location():
    table("provider_locations").update(
        {"sharing_enabled": False, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("provider_id", session["user_id"]).execute()
    return json_response(data={"sharing_enabled": False})


@app.get("/seeker/dashboard")
@seeker_required
def seeker_dashboard():
    expire_open_queries()
    queries = (
        table("queries")
        .select("*")
        .eq("seeker_id", session["user_id"])
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    counts = {q["id"]: query_response_count(q["id"]) for q in queries}
    buckets = {status: [q for q in queries if q.get("status") == status] for status in ["open", "matched", "resolved", "expired"]}
    return render_template("seeker/dashboard.html", buckets=buckets, counts=counts)


@app.get("/seeker/post")
@seeker_required
def post_query_page():
    return render_template("seeker/post_query.html")


@app.post("/query/post")
@seeker_required
def post_query():
    data = request.form.to_dict()
    lat = coerce_float(data.get("lat") or session.get("lat"), "Latitude", -90, 90)
    lng = coerce_float(data.get("lng") or session.get("lng"), "Longitude", -180, 180)
    radius = coerce_float(data.get("radius_km") or 5, "Radius", 0.5, 50)
    urgency = clean_text(data.get("urgency"), 20)
    if urgency not in ALLOWED_URGENCY:
        raise ValueError("Invalid urgency")
    image_url = None
    if request.files.get("image") and request.files["image"].filename:
        image_url = upload_public_image("query-images", request.files["image"], session["user_id"])["image_url"]
    record = {
        "id": str(uuid.uuid4()),
        "seeker_id": session["user_id"],
        "item_name": clean_text(data.get("item_name"), 160),
        "category": clean_text(data.get("category"), 80),
        "description": clean_text(data.get("description"), 1000),
        "location": clean_text(data.get("location"), 250),
        "lat": lat,
        "lng": lng,
        "radius_km": radius,
        "urgency": urgency,
        "status": "open",
        "image_url": image_url,
        "expires_at": parse_expiration(data.get("expiration")),
    }
    table("queries").insert(record).execute()
    notify_nearby_providers(record)
    flash("Request posted successfully.", "success")
    return redirect(url_for("seeker_responses_page", query_id=record["id"]))


@app.post("/upload/query-image")
@seeker_required
def upload_query_image():
    file_storage = request.files.get("image")
    result = upload_public_image("query-images", file_storage, session["user_id"])
    return json_response(data={"image_url": result["image_url"]}, status=201)


@app.get("/query/list")
@provider_required
def list_queries():
    expire_open_queries()
    provider = current_profile()
    if not provider or provider.get("is_active") is not True:
        return json_response(False, error="Provider account is inactive", status=403)
    if not provider or provider.get("lat") is None or provider.get("lng") is None:
        return json_response(False, error="Update your location to discover nearby requests", status=400)
    radius = coerce_float(request.args.get("radius_km") or 5, "Radius", 0.5, 50)
    category = (request.args.get("category") or "").strip().lower()
    sort = request.args.get("sort") or "distance"
    rows = table("queries").select("*, users:seeker_id(name)").eq("status", "open").execute().data or []
    priority = {"very_urgent": 0, "urgent": 1, "normal": 2}
    items = []
    for row in rows:
        if row.get("status") != "open":
            continue
        if row.get("lat") is None or row.get("lng") is None:
            continue
        if query_is_expired(row):
            continue
        if category and category not in (row.get("category") or "").lower():
            continue
        distance = haversine(float(provider["lat"]), float(provider["lng"]), float(row["lat"]), float(row["lng"]))
        if distance <= min(radius, float(row.get("radius_km") or radius)):
            items.append(
                {
                    "id": row["id"],
                    "item_name": row["item_name"],
                    "category": row["category"],
                    "description": row["description"],
                    "seeker_name": (row.get("users") or {}).get("name", "Seeker"),
                    "location": row.get("location"),
                    "lat": row["lat"],
                    "lng": row["lng"],
                    "distance": round(distance, 2),
                    "radius_km": row.get("radius_km"),
                    "urgency": row.get("urgency"),
                    "status": row.get("status"),
                    "image_url": row.get("image_url"),
                    "created_at": row.get("created_at"),
                    "expires_at": row.get("expires_at"),
                }
            )
    if sort == "urgency":
        items.sort(key=lambda q: (priority.get(q["urgency"], 3), q["distance"]))
    else:
        items.sort(key=lambda q: (q["distance"], priority.get(q["urgency"], 3)))
    return json_response(data={"queries": items, "provider": {"lat": provider["lat"], "lng": provider["lng"]}})


@app.get("/provider/dashboard")
@provider_required
def provider_dashboard():
    profile = current_profile()
    summary = provider_response_summary(session["user_id"])
    selected_responses = selected_provider_responses(session["user_id"])
    location = (
        table("provider_locations")
        .select("sharing_enabled")
        .eq("provider_id", session["user_id"])
        .maybe_single()
        .execute()
        .data
        or {}
    )
    return render_template(
        "provider/dashboard.html",
        profile=profile,
        summary=summary,
        selected_responses=selected_responses,
        location_sharing=location.get("sharing_enabled") is True,
    )


@app.get("/provider/respond/<query_id>")
@provider_required
def provider_respond_page(query_id):
    expire_open_queries()
    query = get_query(query_id)
    provider = current_profile()
    allowed, detail, status = provider_query_eligibility(query, provider)
    if not allowed:
        return render_template("error.html", message=detail), status
    distance = round(detail, 2)
    existing = (
        table("responses")
        .select("*")
        .eq("query_id", query_id)
        .eq("provider_id", session["user_id"])
        .maybe_single()
        .execute()
        .data
    )
    return render_template("provider/respond.html", query=query, existing=existing, distance=distance)


@app.post("/query/respond")
@provider_required
def query_respond():
    expire_open_queries()
    data = request.form.to_dict()
    query_id = data.get("query_id")
    if not valid_uuid(query_id):
        raise ValueError("Invalid request id")
    query = get_query(query_id)
    provider = current_profile()
    allowed, detail, status = provider_query_eligibility(query, provider)
    if not allowed:
        return json_response(False, error=detail, status=status)
    existing = (
        table("responses")
        .select("id,image_url")
        .eq("query_id", query_id)
        .eq("provider_id", session["user_id"])
        .maybe_single()
        .execute()
        .data
    )
    image_url = existing.get("image_url") if existing else None
    if request.files.get("image") and request.files["image"].filename:
        image_url = upload_public_image("response-images", request.files["image"], session["user_id"])["image_url"]
    record = {
        "message": clean_text(data.get("message"), 500),
        "price": clean_text(data.get("price"), 80),
        "image_url": image_url,
        "status": "available",
    }
    if existing:
        table("responses").update(record).eq("id", existing["id"]).execute()
        response_id = existing["id"]
    else:
        record.update({"id": str(uuid.uuid4()), "query_id": query_id, "provider_id": session["user_id"]})
        try:
            table("responses").insert(record).execute()
            response_id = record["id"]
            notify_provider_for_response(query, record)
        except SupabaseRequestError as error:
            if not is_response_conflict(error):
                raise
            existing = (
                table("responses")
                .select("id")
                .eq("query_id", query_id)
                .eq("provider_id", session["user_id"])
                .maybe_single()
                .execute()
                .data
            )
            if not existing:
                raise
            table("responses").update(record).eq("id", existing["id"]).execute()
            response_id = existing["id"]
    if request.headers.get("Accept", "").startswith("application/json"):
        return json_response(data={"response_id": response_id}, status=201)
    flash("Provider response received.", "success")
    return redirect(url_for("provider_dashboard"))


@app.post("/upload/response-image")
@provider_required
def upload_response_image():
    result = upload_public_image("response-images", request.files.get("image"), session["user_id"])
    return json_response(data={"image_url": result["image_url"]}, status=201)


@app.get("/seeker/responses/<query_id>")
@seeker_required
def seeker_responses_page(query_id):
    query = require_query_owner(query_id)
    if not query:
        return render_template("error.html", message="Request not found or access denied."), 404
    return render_template("seeker/responses.html", query=query)


@app.get("/query/responses/<query_id>")
@seeker_required
def query_responses(query_id):
    query = require_query_owner(query_id)
    if not query:
        return json_response(False, error="Request not found or access denied", status=404)
    rows = (
        table("responses")
        .select("*, users!responses_provider_id_fkey(id,name,business_name,provider_type,phone,lat,lng)")
        .eq("query_id", query_id)
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    responses = []
    for row in rows:
        provider = row.get("users") or {}
        selected = row.get("status") == "selected"
        plat, plng = public_provider_location(provider, selected=selected)
        if row.get("status") == "rejected":
            plat, plng = None, None
        distance = None
        if plat is not None and plng is not None and query.get("lat") is not None and query.get("lng") is not None:
            distance = round(haversine(float(query["lat"]), float(query["lng"]), plat, plng), 2)
        responses.append(
            {
                "id": row["id"],
                "provider_id": row["provider_id"],
                "provider_name": provider.get("name"),
                "business_name": provider.get("business_name"),
                "provider_type": provider.get("provider_type"),
                "provider_lat": plat,
                "provider_lng": plng,
                "distance": distance,
                "message": row.get("message"),
                "price": row.get("price"),
                "image_url": row.get("image_url"),
                "status": row.get("status"),
                "phone": provider.get("phone") if selected or row.get("status") == "available" else None,
                "created_at": row.get("created_at"),
                "location_available": bool(plat is not None and plng is not None),
            }
        )
    return json_response(data={"query": query, "responses": responses})


@app.get("/query/<query_id>/provider-location")
@seeker_required
def selected_provider_location(query_id):
    query = require_query_owner(query_id)
    if not query or query.get("status") != "matched":
        return json_response(data=None, preserve_none=True)
    selected = (
        table("responses")
        .select("provider_id")
        .eq("query_id", query_id)
        .eq("status", "selected")
        .maybe_single()
        .execute()
        .data
    )
    if not selected:
        return json_response(data=None, preserve_none=True)
    location = (
        table("provider_locations")
        .select("lat,lng,accuracy_m,updated_at,sharing_enabled")
        .eq("provider_id", selected["provider_id"])
        .maybe_single()
        .execute()
        .data
    )
    if not location or location.get("sharing_enabled") is not True:
        return json_response(data=None, preserve_none=True)
    distance = None
    if query.get("lat") is not None and query.get("lng") is not None:
        distance = round(haversine(float(query["lat"]), float(query["lng"]), float(location["lat"]), float(location["lng"])), 2)
    location["distance_km"] = distance
    return json_response(data=location)


@app.post("/query/select-provider")
@seeker_required
def select_provider():
    data = parse_json_or_form()
    query_id = data.get("query_id")
    response_id = data.get("response_id")
    if not valid_uuid(response_id):
        return json_response(False, error="Invalid provider response id", status=400)
    query = require_query_owner(query_id)
    if not query:
        return json_response(False, error="Request not found or access denied", status=404)
    if query.get("status") != "open":
        return json_response(False, error="Only open requests can be matched", status=409)
    response = table("responses").select("*").eq("id", response_id).eq("query_id", query_id).single().execute().data
    if not response or response.get("query_id") != query_id or response.get("status") != "available":
        return json_response(False, error="Provider response is not available", status=409)
    rpc_result = get_supabase(admin=True).rpc(
        "select_provider_for_query",
        {"p_query_id": query_id, "p_response_id": response_id, "p_seeker_id": session["user_id"]},
    ).execute()
    if (rpc_result.data or {}).get("status") == "expired":
        return json_response(False, error="Only open requests can be matched", status=409)
    if response.get("provider_id"):
        ensure_conversation(query_id, response_id, session["user_id"], response["provider_id"])
        create_notification(
            response["provider_id"],
            "provider_selected",
            "Your response was selected",
            f"Your response for {query.get('item_name', 'your request')} was selected.",
            query_id,
            response_id,
        )
    return json_response(data={"message": "Provider selected."})


@app.get("/chat/response/<response_id>")
@login_required
def chat_for_response(response_id):
    if not valid_uuid(response_id):
        return render_template("error.html", message="Conversation not found."), 404
    conversation = table("conversations").select("*").eq("response_id", response_id).maybe_single().execute().data
    if not conversation or session["user_id"] not in {conversation.get("seeker_id"), conversation.get("provider_id")}:
        return render_template("error.html", message="Conversation not found or access denied."), 404
    return redirect(url_for("chat_page", conversation_id=conversation["id"]))


@app.get("/chat/<conversation_id>")
@login_required
def chat_page(conversation_id):
    conversation = get_conversation_member(conversation_id)
    if not conversation:
        return render_template("error.html", message="Conversation not found or access denied."), 404
    query = get_query(conversation["query_id"]) or {}
    other_id = conversation["provider_id"] if session["user_id"] == conversation["seeker_id"] else conversation["seeker_id"]
    other = table("users").select("name,business_name,role").eq("id", other_id).maybe_single().execute().data or {}
    return render_template("chat.html", conversation=conversation, other=other, query=query)


@app.get("/chat/<conversation_id>/messages")
@login_required
def chat_messages(conversation_id):
    conversation = get_conversation_member(conversation_id)
    if not conversation:
        return json_response(False, error="Conversation not found or access denied", status=404)
    rows = (
        table("messages")
        .select("id,conversation_id,sender_id,message,is_read,created_at")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    query = get_query(conversation["query_id"]) or {}
    return json_response(data={"conversation": conversation, "messages": rows, "query_status": query.get("status")})


@app.post("/chat/<conversation_id>/messages")
@login_required
def send_chat_message(conversation_id):
    conversation = get_conversation_member(conversation_id)
    if not conversation:
        return json_response(False, error="Conversation not found or access denied", status=404)
    data = parse_json_or_form()
    message = clean_text(data.get("message"), 2000)
    message_id = str(uuid.uuid4())
    record = {
        "id": message_id,
        "conversation_id": conversation_id,
        "sender_id": session["user_id"],
        "message": message,
        "is_read": False,
    }
    table("messages").insert(record).execute()
    recipient_id = conversation["provider_id"] if session["user_id"] == conversation["seeker_id"] else conversation["seeker_id"]
    try:
        create_notification(
            recipient_id,
            "chat_message",
            "New message",
            f"You have a new message from {session.get('name') or 'your contact'}.",
            conversation["query_id"],
            conversation["response_id"],
            identity_id=message_id,
        )
    except Exception:
        app.logger.exception("Chat notification failed for message %s", message_id)
    return json_response(data={"message": record}, status=201)


@app.post("/chat/<conversation_id>/read")
@login_required
def mark_chat_read(conversation_id):
    conversation = get_conversation_member(conversation_id)
    if not conversation:
        return json_response(False, error="Conversation not found or access denied", status=404)
    other_id = conversation["provider_id"] if session["user_id"] == conversation["seeker_id"] else conversation["seeker_id"]
    table("messages").update({"is_read": True}).eq("conversation_id", conversation_id).eq("sender_id", other_id).eq("is_read", False).execute()
    return json_response(data={"message": "Messages marked as read."})


@app.post("/query/resolve")
@seeker_required
def resolve_query():
    data = parse_json_or_form()
    query = require_query_owner(data.get("query_id"))
    if not query:
        return json_response(False, error="Request not found or access denied", status=404)
    if query.get("status") != "matched":
        return json_response(False, error="Only matched requests can be resolved", status=409)
    selected_response = (
        table("responses")
        .select("id,provider_id")
        .eq("query_id", query["id"])
        .eq("status", "selected")
        .maybe_single()
        .execute()
        .data
    )
    result = (
        table("queries")
        .update({"status": "resolved"})
        .eq("id", query["id"])
        .eq("seeker_id", session["user_id"])
        .eq("status", "matched")
        .execute()
    )
    if not result.data:
        return json_response(False, error="Only matched requests can be resolved", status=409)
    if selected_response:
        create_notification(
            selected_response["provider_id"],
            "request_resolved",
            "Request resolved",
            f"The request for {query['item_name']} has been resolved.",
            query["id"],
            selected_response["id"],
        )
    return json_response(data={"message": "Request resolved."})


if __name__ == "__main__":
    app.run(debug=Config.DEBUG)
