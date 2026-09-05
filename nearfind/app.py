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


def json_response(success=True, data=None, error=None, status=200):
    payload = {"success": success}
    if success:
        payload["data"] = data or {}
    else:
        payload["error"] = error or "Something went wrong"
    return jsonify(payload), status


def wants_json():
    return request.path.startswith(("/query/", "/upload/", "/user/"))


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
        return json_response(False, error="Invalid or missing CSRF token.", status=403)
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
    return value[:max_len]


def safe_supabase_message(error):
    if error.status_code == 429:
        return "Supabase Auth rate limit exceeded. Please wait and try again."
    if 400 <= error.status_code < 500:
        return error.message or "Unable to complete the request. Please check your input."
    return "Unable to complete the request. Please try again."


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            if wants_json():
                return json_response(False, error="Login required", status=401)
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def seeker_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") != "seeker":
            if wants_json():
                return json_response(False, error="Seeker access required", status=403)
            return redirect(url_for("index"))
        return fn(*args, **kwargs)

    return wrapper


def provider_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") != "provider":
            if wants_json():
                return json_response(False, error="Provider access required", status=403)
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
        table("queries").update({"status": "expired"}).in_("id", ids).execute()


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


def provider_query_eligibility(query, provider):
    if not query:
        return False, "Request not found.", 404
    if query.get("status") != "open":
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


@app.errorhandler(Exception)
def handle_error(error):
    if isinstance(error, SupabaseRequestError):
        status = error.status_code if 400 <= error.status_code < 500 else 502
        message = safe_supabase_message(error)
        log_message = error.message
    else:
        status = 400 if isinstance(error, ValueError) else error.code if isinstance(error, HTTPException) else 500
        message = error.description if isinstance(error, HTTPException) else str(error)
        log_message = message
    if status >= 500:
        app.logger.exception("Request failed: %s", log_message)
    else:
        app.logger.warning("Request failed: %s", log_message)
    if wants_json():
        return json_response(False, error="Internal server error" if status == 500 else message, status=status)
    return render_template("error.html", message="Something went wrong." if status == 500 else message), status


@app.get("/favicon.ico")
def favicon():
    return "", 204


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
    table("users").insert(profile).execute()
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
    session.update(
        user_id=user.id,
        role=profile["role"],
        email=profile["email"],
        lat=profile.get("lat"),
        lng=profile.get("lng"),
        name=profile.get("name"),
    )
    return redirect(url_for("seeker_dashboard" if profile["role"] == "seeker" else "provider_dashboard"))


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
        if row.get("lat") is None or row.get("lng") is None:
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
    return render_template("provider/dashboard.html", profile=profile, summary=summary)


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
    image_url = data.get("existing_image_url") or None
    if request.files.get("image") and request.files["image"].filename:
        image_url = upload_public_image("response-images", request.files["image"], session["user_id"])["image_url"]
    record = {
        "message": clean_text(data.get("message"), 500),
        "price": clean_text(data.get("price"), 80),
        "image_url": image_url,
        "status": "available",
    }
    existing = (
        table("responses")
        .select("id")
        .eq("query_id", query_id)
        .eq("provider_id", session["user_id"])
        .maybe_single()
        .execute()
        .data
    )
    if existing:
        table("responses").update(record).eq("id", existing["id"]).execute()
        response_id = existing["id"]
    else:
        record.update({"id": str(uuid.uuid4()), "query_id": query_id, "provider_id": session["user_id"]})
        table("responses").insert(record).execute()
        response_id = record["id"]
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
        .select("*, users:provider_id(id,name,business_name,provider_type,phone,lat,lng)")
        .eq("query_id", query_id)
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    responses = []
    for row in rows:
        provider = row.get("users") or {}
        selected = row.get("status") == "selected" or query.get("status") in {"matched", "resolved"}
        plat, plng = public_provider_location(provider, selected=selected)
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
            }
        )
    return json_response(data={"query": query, "responses": responses})


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
    if not response or response.get("status") != "available":
        return json_response(False, error="Provider response is not available", status=409)
    get_supabase(admin=True).rpc(
        "select_provider_for_query",
        {"p_query_id": query_id, "p_response_id": response_id, "p_seeker_id": session["user_id"]},
    ).execute()
    return json_response(data={"message": "Provider selected."})


@app.post("/query/resolve")
@seeker_required
def resolve_query():
    data = parse_json_or_form()
    query = require_query_owner(data.get("query_id"))
    if not query:
        return json_response(False, error="Request not found or access denied", status=404)
    if query.get("status") != "matched":
        return json_response(False, error="Only matched requests can be resolved", status=409)
    table("queries").update({"status": "resolved"}).eq("id", query["id"]).execute()
    return json_response(data={"message": "Request resolved."})


if __name__ == "__main__":
    app.run(debug=True)
