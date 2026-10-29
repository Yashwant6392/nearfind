import concurrent.futures
import logging
import math
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import wraps
from hmac import compare_digest
from types import SimpleNamespace

from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from config import ALLOWED_PROVIDER_TYPES, ALLOWED_ROLES, ALLOWED_URGENCY, Config
from services.location_service import (
    coerce_float,
    format_distance,
    haversine,
    public_provider_location,
    valid_coordinates,
)
from services.similarity_service import DISCLAIMER, compare_images
from services.storage_service import (
    create_signed_verification_url,
    upload_private_verification_document,
    upload_public_image,
    validate_image,
    validate_verification_document,
)
from services.supabase_service import (
    SupabaseConnectionError,
    SupabaseConfigurationError,
    SupabaseRequestError,
    get_supabase,
    rpc,
    supabase_auth_request,
    table,
)

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
    if request.headers.get("Accept", "").startswith("application/json"):
        return True
    api_paths = (
        "/query/list",
        "/query/responses/",
        "/query/select-provider",
        "/query/resolve",
        "/upload/",
        "/user/",
        "/location/",
        "/provider/driver-location",
        "/provider/request/",
        "/notifications",
        "/hospitals/search",
        "/seeker/active-reservations",
    )
    if (
        request.path.startswith(api_paths)
        or request.path == "/query/respond" and request.headers.get("Accept", "").startswith("application/json")
        or re.match(r"^/query/[^/]+/provider-location$", request.path)
        or re.match(r"^/query/[^/]+/response/[^/]+/similarity$", request.path)
        or re.match(r"^/seeker/driver-requests?/[^/]+/driver-location$", request.path)
    ):
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
    missing = [
        name
        for name, value in (
            ("SUPABASE_URL", Config.SUPABASE_URL),
            ("SUPABASE_ANON_KEY", Config.SUPABASE_ANON_KEY),
            ("SUPABASE_SERVICE_ROLE_KEY", Config.SUPABASE_SERVICE_ROLE_KEY),
        )
        if not value
    ]
    if missing:
        raise SupabaseConfigurationError(f"Required server settings are missing: {', '.join(missing)}.")


def valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def parse_nonnegative_count(value, name):
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")
    if count < 0:
        raise ValueError(f"{name} cannot be negative")
    return count


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


def safe_signup_supabase_error(error):
    code = str(getattr(error, "code", None) or "").casefold()
    detail = str(getattr(error, "message", None) or "").casefold()
    status_code = getattr(error, "status_code", 400)
    disabled_codes = {"signup_disabled", "signups_disabled", "signup_not_enabled", "signup_not_allowed"}
    disabled_phrases = (
        "sign-ups are currently paused",
        "signups are currently paused",
        "signups are disabled",
        "sign ups are disabled",
        "signups not allowed",
        "signups are not allowed",
        "signup is disabled",
    )
    if code in disabled_codes or any(phrase in detail for phrase in disabled_phrases):
        return (
            403,
            "Sign-ups are currently disabled for this Supabase project. Contact the project administrator to enable them in Auth settings.",
            "signup_disabled",
        )
    if (
        status_code == 429
        or code in {"over_request_rate_limit", "over_email_send_rate_limit"}
        or "rate limit" in detail
        or "too many requests" in detail
    ):
        return 429, "Sign-up rate limit reached. Please wait and try again later.", "rate_limited"
    if code in {"invalid_api_key", "bad_jwt"} or "invalid api key" in detail:
        return (
            503,
            "Supabase Auth rejected the configured public API key. The project administrator should verify SUPABASE_URL and SUPABASE_ANON_KEY.",
            "supabase_configuration_error",
        )
    if (
        code in {"user_already_exists", "email_exists", "user_already_registered"}
        or "already registered" in detail
        or "already exists" in detail
        or "duplicate" in detail
    ):
        return (
            409,
            "An account with this email address already exists. Please sign in instead.",
            "user_already_exists",
        )
    if "password" in detail or code in {"weak_password", "invalid_password"}:
        return (
            400,
            "Password does not meet the security requirements. It must be at least 6 characters.",
            "invalid_password",
        )
    if status_code in {400, 401, 403, 422}:
        return (
            status_code,
            "Supabase Auth rejected this sign-up request. Check the form requirements and ask the project administrator to verify sign-up settings and the public API key.",
            "signup_rejected",
        )
    return (
        502,
        "Supabase Auth could not complete sign-up. Please try again later or contact the project administrator.",
        "supabase_auth_error",
    )


def safe_login_supabase_error(error):
    code = str(getattr(error, "code", None) or "").casefold()
    detail = str(getattr(error, "message", None) or "").casefold()
    status_code = getattr(error, "status_code", 400)
    if (
        status_code == 429
        or code in {"over_request_rate_limit", "over_email_send_rate_limit"}
        or "rate limit" in detail
        or "too many requests" in detail
    ):
        return 429, "Too many login attempts. Please wait and try again later.", "rate_limited"
    if (
        code in {"email_not_confirmed", "email_provider_disabled"}
        or "email not confirmed" in detail
        or "confirm your email" in detail
        or "unconfirmed" in detail
    ):
        return (
            403,
            "Please verify your email address before signing in. Check your inbox for the confirmation link.",
            "email_not_confirmed",
        )
    if code in {"user_banned", "user_inactive", "account_disabled"} or "banned" in detail or "disabled" in detail:
        return 403, "Your account is inactive or disabled. Please contact support.", "account_disabled"
    if code in {"invalid_api_key", "bad_jwt"} or "invalid api key" in detail:
        return (
            503,
            "Supabase Auth rejected the configured public API key. The project administrator should verify SUPABASE_URL and SUPABASE_ANON_KEY.",
            "supabase_configuration_error",
        )
    if (
        status_code in {400, 401}
        or code in {"invalid_credentials", "invalid_grant", "bad_credentials"}
        or "invalid login credentials" in detail
        or "invalid credentials" in detail
    ):
        return 400, "Invalid email or password.", "invalid_credentials"
    if 400 <= status_code < 500:
        return status_code, "Invalid email or password.", "invalid_credentials"
    return (
        502,
        "Unable to sign in right now. Please try again.",
        "supabase_auth_error",
    )


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
    try:
        if hasattr(g, "current_profile") and g.current_profile and g.current_profile.get("id") == user_id:
            return g.current_profile
    except RuntimeError:
        pass
    try:
        result = table("users").select("*").eq("id", user_id).maybe_single().execute()
        data = result.data if result else None
    except Exception as exc:
        app.logger.warning("Could not load user profile for %s: %s", user_id, exc)
        data = None
    try:
        g.current_profile = data
    except RuntimeError:
        pass
    return data


def current_hospital():
    user_id = session.get("user_id")
    if not user_id:
        return None
    profile = current_profile()
    if not profile or profile.get("is_hospital_manager") is not True:
        return None
    try:
        return (
            table("hospitals")
            .select("*")
            .eq("manager_id", user_id)
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        app.logger.warning("Hospital profile query failed")
        return None


HOSPITAL_BED_TYPES = {
    "general": ("General", "general_available"),
    "icu": ("ICU", "icu_available"),
    "oxygen": ("Oxygen-supported", "oxygen_available"),
    "emergency": ("Emergency", "emergency_available"),
    "isolation": ("Isolation", "isolation_available"),
}
HOSPITAL_RADIUS_OPTIONS = {2, 5, 10, 20, 50}


def hospital_freshness(updated_at, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {"state": "stale", "label": "Availability may be outdated", "minutes_ago": None}
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    minutes = max(0, int((now - updated).total_seconds() // 60))
    if minutes <= 30:
        state, label = "fresh", "Updated recently"
    elif minutes <= 120:
        state, label = "aging", "Updated some time ago"
    else:
        state, label = "stale", "Availability may be outdated"
    return {"state": state, "label": label, "minutes_ago": minutes}


def hospital_search_results(latitude, longitude, radius, bed_type):
    bed_label, bed_field = HOSPITAL_BED_TYPES[bed_type]
    latitude_delta = radius / 111.0
    longitude_delta = radius / max(111.0 * math.cos(math.radians(latitude)), 0.01)
    try:
        hospitals = (
            table("hospitals")
            .select("id,name,address,phone,lat,lng,is_verified")
            .eq("is_verified", True)
            .gte("lat", max(-90, latitude - latitude_delta))
            .lte("lat", min(90, latitude + latitude_delta))
            .gte("lng", max(-180, longitude - longitude_delta))
            .lte("lng", min(180, longitude + longitude_delta))
            .execute()
            .data
            or []
        )
    except Exception:
        app.logger.warning("Hospitals table query failed")
        hospitals = []
    hospital_ids = [hospital.get("id") for hospital in hospitals if hospital.get("id")]
    availability_rows = []
    if hospital_ids:
        try:
            availability_rows = (
                table("hospital_bed_availability")
                .select("hospital_id,general_available,icu_available,oxygen_available,emergency_available,isolation_available,updated_at,verified_at")
                .in_("hospital_id", hospital_ids)
                .execute()
                .data
                or []
            )
        except Exception:
            app.logger.warning("Hospital bed availability table query failed")
            availability_rows = []
    availability_by_hospital = {row.get("hospital_id"): row for row in availability_rows}
    results = []
    for hospital in hospitals:
        if hospital.get("lat") is None or hospital.get("lng") is None:
            continue
        availability = availability_by_hospital.get(hospital.get("id"))
        if not availability:
            continue
        distance = haversine(latitude, longitude, float(hospital["lat"]), float(hospital["lng"]))
        if distance > radius:
            continue
        freshness = hospital_freshness(availability.get("updated_at"))
        requested_count = int(availability.get(bed_field) or 0)
        results.append(
            {
                "hospital_id": hospital["id"],
                "name": hospital.get("name"),
                "address": hospital.get("address"),
                "phone": hospital.get("phone"),
                "lat": hospital["lat"],
                "lng": hospital["lng"],
                "is_verified": hospital.get("is_verified") is True,
                "distance_km": round(distance, 2),
                "bed_type": bed_type,
                "bed_label": bed_label,
                "requested_count": requested_count,
                "availability_status": "available" if requested_count > 0 else "full",
                "availability": {
                    "general": int(availability.get("general_available") or 0),
                    "icu": int(availability.get("icu_available") or 0),
                    "oxygen": int(availability.get("oxygen_available") or 0),
                    "emergency": int(availability.get("emergency_available") or 0),
                    "isolation": int(availability.get("isolation_available") or 0),
                },
                "updated_at": availability.get("updated_at"),
                "freshness_state": freshness["state"],
                "freshness_label": freshness["label"],
                "updated_minutes_ago": freshness["minutes_ago"],
            }
        )
    freshness_order = {"fresh": 0, "aging": 1, "stale": 2}
    results.sort(
        key=lambda result: (
            result["availability_status"] != "available",
            freshness_order.get(result["freshness_state"], 3),
            result["distance_km"],
        )
    )
    return results


def hospital_manager_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        profile = current_profile()
        if not profile or profile.get("is_hospital_manager") is not True:
            if wants_json():
                return json_response(False, error="Hospital manager access required", status=403, error_code="forbidden")
            return render_template("error.html", message="Hospital manager access required."), 403
        if not current_hospital() and request.endpoint != "hospital_onboarding":
            return redirect(url_for("hospital_onboarding"))
        return fn(*args, **kwargs)

    return wrapper


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


_last_expire_queries_ts = 0.0


def expire_open_queries(force=False):
    global _last_expire_queries_ts
    now_ts = time.time()
    if not force and not app.testing and (now_ts - _last_expire_queries_ts) < 60.0:
        return
    _last_expire_queries_ts = now_ts
    now = datetime.now(timezone.utc).isoformat()
    try:
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
    except Exception as exc:
        app.logger.warning("Query expiration check failed: %s", exc)


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
            .select("id,query_id,status,created_at,queries(id,item_name,description,location,lat,lng,status,users:seeker_id(phone))")
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


def get_query_effective_coordinates(query):
    """
    Determines effective coordinates for a query/request:
    1. Primary: query['lat'] and query['lng'] from the queries table.
    2. Fallback: if query coordinates are missing or invalid (legacy request),
       falls back to the seeker profile coordinates in the users table.
    3. Returns (lat, lng, source) or (None, None, 'missing').
    """
    if query:
        qlat = query.get("lat")
        qlng = query.get("lng")
        if valid_coordinates(qlat, qlng):
            return float(qlat), float(qlng), "request"
        seeker_id = query.get("seeker_id")
        if seeker_id:
            try:
                user = table("users").select("lat,lng").eq("id", seeker_id).maybe_single().execute().data
                if user and valid_coordinates(user.get("lat"), user.get("lng")):
                    return float(user["lat"]), float(user["lng"]), "profile_fallback"
            except Exception:
                pass
    return None, None, "missing"


def get_provider_effective_location(provider):
    """
    Determines effective coordinates for a provider:
    1. If provider has provider_locations with sharing_enabled=True and valid coordinates,
       uses the live shared location.
    2. Otherwise, uses saved profile location (users.lat, users.lng).
    3. Returns (lat, lng, source) or (None, None, 'missing').
    """
    if not provider:
        return None, None, "missing"
    provider_id = provider.get("id") or provider.get("provider_id")
    if provider_id:
        try:
            loc = (
                table("provider_locations")
                .select("lat,lng,sharing_enabled")
                .eq("provider_id", provider_id)
                .maybe_single()
                .execute()
                .data
            )
            if isinstance(loc, dict) and loc.get("sharing_enabled") and valid_coordinates(loc.get("lat"), loc.get("lng")):
                return float(loc["lat"]), float(loc["lng"]), "live_shared"
        except Exception:
            pass
    plat = provider.get("lat")
    plng = provider.get("lng")
    if valid_coordinates(plat, plng):
        return float(plat), float(plng), "saved_profile"
    try:
        from flask import has_request_context
        if has_request_context() and valid_coordinates(session.get("lat"), session.get("lng")):
            return float(session["lat"]), float(session["lng"]), "session_fallback"
    except Exception:
        pass
    return None, None, "missing"


def provider_query_eligibility(query, provider):
    if not query:
        return False, "Request not found.", 404
    if query.get("status") != "open" or query_is_expired(query):
        return False, "This request is no longer open.", 409
    if not provider or provider.get("is_active") is not True:
        return False, "Provider account is inactive.", 403
    plat, plng, _ = get_provider_effective_location(provider)
    if plat is None or plng is None:
        return False, "Update your location before responding to requests.", 403
    qlat, qlng, _ = get_query_effective_coordinates(query)
    if qlat is None or qlng is None:
        return False, "This request does not have a valid location.", 409
    try:
        distance = haversine(plat, plng, qlat, qlng)
    except (ValueError, TypeError):
        return False, "Invalid location coordinates.", 400
    query_radius = float(query.get("radius_km") or 5.0)
    provider_radius = None
    if provider.get("service_radius_km"):
        try:
            provider_radius = float(provider["service_radius_km"])
        except (ValueError, TypeError):
            pass
    max_radius = min(query_radius, provider_radius) if provider_radius else query_radius
    if distance > max_radius:
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


def create_notification(user_id, notification_type, title, message, query_id=None, response_id=None, identity_id=None):
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
    qlat, qlng, _ = get_query_effective_coordinates(query)
    if qlat is None or qlng is None:
        return
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
        plat, plng, _ = get_provider_effective_location(provider)
        if plat is None or plng is None:
            continue
        distance = haversine(qlat, qlng, plat, plng)
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


def render_request_error(status, message, log_message=None, error_code=None, log_stack=True):
    safe_message = message
    user_id = session.get("user_id") or "anonymous"
    log_message = log_message or safe_message
    if status >= 500:
        if log_stack:
            app.logger.exception("Request failed route=%s user_id=%s error=%s", request.path, user_id, log_message)
        else:
            app.logger.error("Request failed route=%s user_id=%s error=%s", request.path, user_id, log_message)
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
    if isinstance(error, SupabaseConfigurationError):
        return render_request_error(
            503,
            error.public_message,
            log_message=f"Supabase configuration unavailable: {error.public_message}",
            error_code="supabase_configuration_error",
            log_stack=False,
        )
    if isinstance(error, SupabaseConnectionError):
        return render_request_error(
            503,
            "The authentication service is temporarily unavailable. Please try again shortly.",
            log_message=(
                "Supabase Auth transport failure "
                f"exception_types={'>'.join(error.exception_types)} "
                f"diagnostic={error.diagnostic}"
            ),
            error_code="service_unavailable",
            log_stack=False,
        )
    if isinstance(error, SupabaseRequestError):
        status = error.status_code if 400 <= error.status_code < 500 else 502
        message = safe_supabase_message(error)
        log_message = error.message
    else:
        status = 400 if isinstance(error, ValueError) else 500
        message = str(error) if isinstance(error, ValueError) and str(error) else ("Something went wrong. Please try again." if status == 500 else "The request could not be processed.")
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


@app.get("/driver-demo")
def driver_demo_page():
    return render_template("driver/demo.html")


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
    if (lat is None) != (lng is None):
        raise ValueError("Both latitude and longitude are required if coordinates are provided")

    session_user_id = session.get("user_id")
    session_email = (session.get("email") or "").lower()
    user = None
    if session_user_id and session_email == email:
        user = SimpleNamespace(id=session_user_id, email=email)
    else:
        try:
            auth_result = supabase_auth_request(
                get_supabase(admin=False).auth,
                "sign_up",
                {"email": email, "password": password},
            )
            user = auth_result.user
        except SupabaseRequestError as error:
            status, message, error_code = safe_signup_supabase_error(error)
            app.logger.warning("Supabase signup rejected with HTTP status=%s error_code=%s", error.status_code, error_code)
            return render_request_error(
                status,
                message,
                log_message=f"Supabase signup rejected status={error.status_code}",
                error_code=error_code,
                log_stack=False,
            )
    if not user:
        raise RuntimeError("Supabase did not create the user")
    identities = getattr(user, "identities", None)
    if identities is not None and len(identities) == 0:
        session.clear()
        app.logger.info("Signup attempted for existing Supabase account with email %s (empty identities)", email)
        return render_request_error(
            409,
            "An account with this email address already exists. Please sign in instead.",
            error_code="user_already_exists",
        )
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
    existing_profile = None
    try:
        existing_profile = table("users").select("*").eq("id", user.id).maybe_single().execute().data
    except Exception as exc:
        app.logger.warning("Profile check prior to insert failed: %s", exc)
        existing_profile = None

    if existing_profile:
        existing_email = (existing_profile.get("email") or "").strip().lower()
        if existing_email and existing_email != email:
            session.clear()
            app.logger.error("Signup profile conflict: user ID %s has email %s but existing profile has %s", user.id, email, existing_email)
            return render_request_error(
                409,
                "Your account could not be completed due to a profile conflict. Please contact support.",
                error_code="account_conflict",
            )

        update_data = {}
        for key in ("name", "phone", "address"):
            if not existing_profile.get(key) and profile.get(key):
                update_data[key] = profile[key]
        if not existing_profile.get("role"):
            update_data["role"] = role
        target_role = existing_profile.get("role") or role
        if target_role == "provider":
            if not existing_profile.get("provider_type") and profile.get("provider_type"):
                update_data["provider_type"] = profile["provider_type"]
            if not existing_profile.get("business_name") and profile.get("business_name"):
                update_data["business_name"] = profile["business_name"]
        if existing_profile.get("lat") is None and profile.get("lat") is not None:
            update_data["lat"] = profile["lat"]
            update_data["lng"] = profile["lng"]
            update_data["last_location_update"] = profile["last_location_update"]

        if update_data:
            try:
                table("users").update(update_data).eq("id", user.id).execute()
                existing_profile.update(update_data)
            except Exception:
                session.clear()
                app.logger.exception("Failed to update existing stub profile for Supabase user %s", user.id)
                return render_request_error(
                    502,
                    "Your account could not be completed. Please try again.",
                    error_code="profile_update_failed",
                )

        final_role = existing_profile.get("role") or role
        final_name = existing_profile.get("name") or profile["name"]
        final_lat = existing_profile.get("lat") if existing_profile.get("lat") is not None else lat
        final_lng = existing_profile.get("lng") if existing_profile.get("lng") is not None else lng
        is_hospital_mgr = bool(existing_profile.get("is_hospital_manager", False))
    else:
        try:
            table("users").insert(profile).execute()
            final_role = role
            final_name = profile["name"]
            final_lat = lat
            final_lng = lng
            is_hospital_mgr = False
        except SupabaseRequestError as error:
            err_text = f"{error.code} {error.message}".lower()
            is_users_fk_violation = "users_id_fkey" in err_text or (
                (getattr(error, "code", None) == "23503" or "23503" in err_text)
                and ("users_id_fkey" in err_text or "auth.users" in err_text)
            )
            if is_users_fk_violation:
                session.clear()
                app.logger.warning(
                    "Signup profile insert failed due to unpersisted auth user (users_id_fkey / 23503) for user ID %s email %s: code=%s message=%s",
                    user.id,
                    email,
                    getattr(error, "code", None),
                    getattr(error, "message", None),
                )
                return render_request_error(
                    409,
                    "An account with this email address already exists. Please sign in instead.",
                    error_code="user_already_exists",
                )
            if error.status_code == 409 or "duplicate key" in err_text or "users_pkey" in err_text:
                recheck = None
                try:
                    recheck = table("users").select("*").eq("id", user.id).maybe_single().execute().data
                except Exception:
                    recheck = None
                if recheck:
                    recheck_email = (recheck.get("email") or "").strip().lower()
                    if recheck_email and recheck_email != email:
                        session.clear()
                        app.logger.error("Signup profile conflict on 409: user ID %s has email %s but existing profile has %s", user.id, email, recheck_email)
                        return render_request_error(
                            409,
                            "Your account could not be completed due to a profile conflict. Please contact support.",
                            error_code="account_conflict",
                        )
                    update_data = {}
                    for key in ("name", "phone", "address"):
                        if not recheck.get(key) and profile.get(key):
                            update_data[key] = profile[key]
                    if not recheck.get("role"):
                        update_data["role"] = role
                    target_role = recheck.get("role") or role
                    if target_role == "provider":
                        if not recheck.get("provider_type") and profile.get("provider_type"):
                            update_data["provider_type"] = profile["provider_type"]
                        if not recheck.get("business_name") and profile.get("business_name"):
                            update_data["business_name"] = profile["business_name"]
                    if recheck.get("lat") is None and profile.get("lat") is not None:
                        update_data["lat"] = profile["lat"]
                        update_data["lng"] = profile["lng"]
                        update_data["last_location_update"] = profile["last_location_update"]
                    if update_data:
                        try:
                            table("users").update(update_data).eq("id", user.id).execute()
                            recheck.update(update_data)
                        except Exception:
                            session.clear()
                            app.logger.exception("Failed to update reconciled stub profile for Supabase user %s", user.id)
                            return render_request_error(
                                502,
                                "Your account could not be completed. Please try again.",
                                error_code="profile_update_failed",
                            )
                    final_role = recheck.get("role") or role
                    final_name = recheck.get("name") or profile["name"]
                    final_lat = recheck.get("lat") if recheck.get("lat") is not None else lat
                    final_lng = recheck.get("lng") if recheck.get("lng") is not None else lng
                    is_hospital_mgr = bool(recheck.get("is_hospital_manager", False))
                else:
                    session.clear()
                    app.logger.exception("Signup profile creation failed with 409 for Supabase user %s", user.id)
                    return render_request_error(
                        502,
                        "Your account could not be completed. Please try again.",
                        error_code="profile_creation_conflict",
                    )
            else:
                session.clear()
                app.logger.exception("Signup profile creation failed for Supabase user %s status=%s", user.id, error.status_code)
                return render_request_error(
                    502,
                    "Your account could not be completed. Please try again.",
                    error_code="profile_creation_failed",
                )
        except Exception as exc:
            exc_text = str(exc).lower()
            if "users_id_fkey" in exc_text:
                session.clear()
                app.logger.warning(
                    "Signup profile insert failed due to unpersisted auth user (users_id_fkey) for user ID %s email %s: %s",
                    user.id,
                    email,
                    exc,
                )
                return render_request_error(
                    409,
                    "An account with this email address already exists. Please sign in instead.",
                    error_code="user_already_exists",
                )
            session.clear()
            app.logger.exception("Unexpected exception during signup profile creation for user %s", user.id)
            return render_request_error(
                502,
                "Your account could not be completed. Please try again.",
                error_code="profile_creation_failed",
            )

    session.clear()
    session.permanent = True
    session.update(
        user_id=user.id,
        role=final_role,
        email=email,
        lat=final_lat,
        lng=final_lng,
        name=final_name,
        is_hospital_manager=is_hospital_mgr,
    )
    return redirect(url_for("seeker_dashboard" if final_role == "seeker" else "provider_dashboard"))


@app.post("/auth/login")
def auth_login():
    require_supabase_ready()
    data = parse_json_or_form()
    email = clean_text(data.get("email"), 255).lower()
    password = data.get("password") or ""
    if not email or not password:
        return render_request_error(
            400,
            "Invalid email or password.",
            log_message="Missing email or password in login",
            error_code="invalid_credentials",
            log_stack=False,
        )

    try:
        auth_result = supabase_auth_request(
            get_supabase(admin=False).auth,
            "sign_in_with_password",
            {"email": email, "password": password},
        )
        user = auth_result.user
    except SupabaseConnectionError:
        raise
    except SupabaseRequestError as error:
        status, message, error_code = safe_login_supabase_error(error)
        app.logger.warning("Supabase login rejected for %s status=%s code=%s error_code=%s", email, error.status_code, error.code, error_code)
        return render_request_error(
            status,
            message,
            log_message=f"Supabase login failed ({error_code})",
            error_code=error_code,
            log_stack=False,
        )
    except Exception as error:
        app.logger.exception("Supabase login failed for %s", email)
        return render_request_error(
            502,
            "Unable to sign in right now. Please try again.",
            log_message="Unexpected error during Supabase login",
            error_code="bad_gateway",
            log_stack=False,
        )

    if not user:
        return render_request_error(
            400,
            "Invalid email or password.",
            log_message="No user returned from Supabase sign in",
            error_code="invalid_credentials",
            log_stack=False,
        )

    try:
        profile = table("users").select("*").eq("id", user.id).maybe_single().execute().data
    except Exception as error:
        app.logger.exception("Failed to load user profile during login for %s", user.id)
        return render_request_error(
            502,
            "Unable to load your profile. Please try again shortly.",
            log_message=f"Profile fetch failed for user {user.id}",
            error_code="profile_load_failed",
            log_stack=False,
        )

    if not profile:
        # Allow a user to sign in if they exist in auth but have not completed their
        # profile yet; redirect to account setup.
        session.clear()
        session.permanent = True
        session.update(user_id=user.id, email=email, role="seeker", is_hospital_manager=False, pending_profile=True)
        return redirect(url_for("signup"))

    if not profile.get("is_active", True):
        return render_request_error(
            403,
            "Your account is inactive. Please contact support.",
            log_message=f"Inactive account login attempt for user {user.id}",
            error_code="account_inactive",
            log_stack=False,
        )

    is_reviewer = False
    try:
        rev = table("driver_reviewers").select("user_id").eq("user_id", user.id).eq("is_active", True).maybe_single().execute().data
        is_reviewer = bool(rev and isinstance(rev, dict) and rev.get("user_id") == user.id)
    except Exception:
        is_reviewer = False

    session.clear()
    session.permanent = True
    session.update(
        user_id=user.id,
        role=profile["role"],
        email=profile["email"],
        lat=profile.get("lat"),
        lng=profile.get("lng"),
        name=profile.get("name"),
        is_hospital_manager=bool(profile.get("is_hospital_manager")),
        is_reviewer=is_reviewer,
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
    if hasattr(g, "current_profile") and g.current_profile:
        g.current_profile["lat"] = lat
        g.current_profile["lng"] = lng
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


@app.route("/hospital/onboarding", methods=["GET", "POST"])
@login_required
def hospital_onboarding():
    existing = None
    existing_lookup_failed = False
    try:
        existing = (
            table("hospitals")
            .select("*")
            .eq("manager_id", session["user_id"])
            .maybe_single()
            .execute()
            .data
        )
    except Exception as exc:
        app.logger.warning("Could not check existing hospital for user %s: %s", session.get("user_id"), exc)
        existing = None
        existing_lookup_failed = True

    if request.method == "GET":
        if existing:
            return redirect(url_for("hospital_dashboard"))
        if existing_lookup_failed:
            flash("Unable to verify existing hospital registration. If you have already registered, please try again shortly.", "warning")
        profile = current_profile() or {}
        return render_template("hospital/onboarding.html", profile=profile)

    if existing_lookup_failed:
        app.logger.error("Hospital onboarding aborted because existing status check failed for user %s", session.get("user_id"))
        if wants_json():
            return json_response(False, error="Unable to verify registration status. Please try again.", status=502)
        flash("Unable to verify registration status due to a database connection issue. Please try again.", "error")
        return redirect(url_for("hospital_onboarding"))

    data = parse_json_or_form()
    name = clean_text(data.get("name"), 160)
    address = clean_text(data.get("address"), 300)
    phone = clean_text(data.get("phone"), 30, required=False) or None
    lat = coerce_float(data.get("lat"), "Latitude", -90, 90)
    lng = coerce_float(data.get("lng"), "Longitude", -180, 180)

    general_available = parse_nonnegative_count(data.get("general_available", 0), "General beds")
    icu_available = parse_nonnegative_count(data.get("icu_available", 0), "ICU beds")
    oxygen_available = parse_nonnegative_count(data.get("oxygen_available", 0), "Oxygen beds")
    emergency_available = parse_nonnegative_count(data.get("emergency_available", 0), "Emergency beds")
    isolation_available = parse_nonnegative_count(data.get("isolation_available", 0), "Isolation beds")

    hospital_record = {
        "manager_id": session["user_id"],
        "name": name,
        "address": address,
        "phone": phone,
        "lat": lat,
        "lng": lng,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    hospital_id = None
    try:
        if existing:
            hospital_id = existing.get("id")
            table("hospitals").update(hospital_record).eq("id", hospital_id).execute()
        else:
            hospital_record["id"] = str(uuid.uuid4())
            hospital_record["is_verified"] = False
            hospital_record["created_at"] = hospital_record["updated_at"]
            hospital_res = table("hospitals").insert(hospital_record).execute()
            created_hospital = hospital_res.data[0] if (hospital_res.data and len(hospital_res.data) > 0) else None
            hospital_id = (created_hospital.get("id") if created_hospital else None) or hospital_record["id"]
    except Exception as exc:
        app.logger.error("Hospital record insert/update failed for user %s: %s", session.get("user_id"), exc)
        err_str = str(exc).lower()
        if "duplicate key" in err_str or "unique" in err_str or "23505" in err_str:
            try:
                fetch_res = table("hospitals").select("id").eq("manager_id", session["user_id"]).maybe_single().execute().data
                if fetch_res and fetch_res.get("id"):
                    hospital_id = fetch_res["id"]
                    table("hospitals").update(hospital_record).eq("id", hospital_id).execute()
                else:
                    raise exc
            except Exception as retry_exc:
                app.logger.error("Failed to recover from unique constraint race on hospitals: %s", retry_exc)
                if wants_json():
                    return json_response(False, error="Hospital registration could not be saved. Please try again.", status=502)
                flash("Hospital registration could not be saved. Please try again.", "error")
                return redirect(url_for("hospital_onboarding"))
        else:
            if wants_json():
                return json_response(False, error="Hospital registration could not be saved. Please try again.", status=502)
            flash("Hospital registration could not be saved. Please try again.", "error")
            return redirect(url_for("hospital_onboarding"))

    if not hospital_id:
        app.logger.error("Hospital onboarding failed to establish a valid hospital_id for user %s", session.get("user_id"))
        if wants_json():
            return json_response(False, error="Hospital registration failed. Please try again.", status=502)
        flash("Hospital registration failed. Please try again.", "error")
        return redirect(url_for("hospital_onboarding"))

    # Initialize or update bed availability inventory
    try:
        existing_avail = None
        try:
            existing_avail = (
                table("hospital_bed_availability")
                .select("hospital_id")
                .eq("hospital_id", hospital_id)
                .maybe_single()
                .execute()
                .data
            )
        except Exception:
            existing_avail = None

        avail_record = {
            "hospital_id": hospital_id,
            "general_available": general_available,
            "icu_available": icu_available,
            "oxygen_available": oxygen_available,
            "emergency_available": emergency_available,
            "isolation_available": isolation_available,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "verified_at": None,
        }
        if existing_avail:
            table("hospital_bed_availability").update(avail_record).eq("hospital_id", hospital_id).execute()
        else:
            table("hospital_bed_availability").insert(avail_record).execute()
    except Exception as exc:
        app.logger.error("Failed to save bed availability for hospital %s: %s", hospital_id, exc)
        if wants_json():
            return json_response(False, error="Failed to save hospital bed availability. Please try again.", status=502)
        flash("Failed to save hospital bed availability. Please try again.", "error")
        return redirect(url_for("hospital_onboarding"))

    # Only persist is_hospital_manager on users table after hospital is successfully stored
    try:
        table("users").update({"is_hospital_manager": True}).eq("id", session["user_id"]).execute()
        session["is_hospital_manager"] = True
    except Exception as exc:
        app.logger.warning("Could not persist is_hospital_manager on users table: %s", exc)
        session["is_hospital_manager"] = True

    if wants_json():
        return json_response(True, data={"hospital_id": hospital_id, "message": "Hospital onboarding completed successfully."}, status=201)

    flash("Hospital onboarding submitted successfully. Bed inventory is active.", "success")
    return redirect(url_for("hospital_dashboard"))


@app.get("/hospital/dashboard")
@hospital_manager_required
def hospital_dashboard():
    hospital = current_hospital()
    if not hospital:
        return redirect(url_for("hospital_onboarding"))
    availability = {}
    try:
        availability = (
            table("hospital_bed_availability")
            .select("*")
            .eq("hospital_id", hospital["id"])
            .maybe_single()
            .execute()
            .data
            or {}
        )
    except Exception:
        app.logger.warning("Could not query hospital_bed_availability")

    holds = []
    try:
        holds = (
            table("hospital_bed_holds")
            .select("*, users!hospital_bed_holds_seeker_id_fkey(name,phone)")
            .eq("hospital_id", hospital["id"])
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        try:
            holds = (
                table("hospital_bed_holds")
                .select("*")
                .eq("hospital_id", hospital["id"])
                .order("created_at", desc=True)
                .execute()
                .data
                or []
            )
            for h in holds:
                if h.get("seeker_id"):
                    try:
                        u = table("users").select("name,phone").eq("id", h["seeker_id"]).maybe_single().execute().data
                        h["users"] = u
                    except Exception:
                        pass
        except Exception:
            holds = []

    return render_template("hospital/dashboard.html", hospital=hospital, availability=availability, holds=holds)


@app.post("/hospital/availability")
@hospital_manager_required
def update_hospital_availability():
    hospital = current_hospital()
    if not hospital:
        return redirect(url_for("hospital_onboarding"))
    data = parse_json_or_form()
    availability = {
        "hospital_id": hospital["id"],
        "general_available": parse_nonnegative_count(data.get("general_available"), "General availability"),
        "icu_available": parse_nonnegative_count(data.get("icu_available"), "ICU availability"),
        "oxygen_available": parse_nonnegative_count(data.get("oxygen_available"), "Oxygen availability"),
        "emergency_available": parse_nonnegative_count(data.get("emergency_available"), "Emergency availability"),
        "isolation_available": parse_nonnegative_count(data.get("isolation_available"), "Isolation availability"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    existing = None
    try:
        existing = (
            table("hospital_bed_availability")
            .select("hospital_id")
            .eq("hospital_id", hospital["id"])
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        existing = None
    if existing:
        table("hospital_bed_availability").update(availability).eq("hospital_id", hospital["id"]).execute()
    else:
        table("hospital_bed_availability").insert(availability).execute()
    try:
        table("hospitals").update({"updated_at": availability["updated_at"]}).eq("id", hospital["id"]).eq("manager_id", session["user_id"]).execute()
    except Exception:
        pass
    flash("Hospital availability updated.", "success")
    return redirect(url_for("hospital_dashboard"))


@app.post("/hospitals/<hospital_id>/hold-bed")
@seeker_required
def hold_hospital_bed(hospital_id):
    if not valid_uuid(hospital_id):
        raise ValueError("Invalid hospital id")
    data = parse_json_or_form()
    bed_type = clean_text(data.get("bed_type", "general"), 30).lower()
    if bed_type not in HOSPITAL_BED_TYPES:
        raise ValueError(f"Invalid bed type: {bed_type}")
    col_name = HOSPITAL_BED_TYPES[bed_type][1]

    hospital = table("hospitals").select("id,name,manager_id").eq("id", hospital_id).maybe_single().execute().data
    if not hospital:
        raise ValueError("Hospital not found")

    # Prevent duplicate active holds by the same seeker
    existing_hold = None
    try:
        existing_hold = (
            table("hospital_bed_holds")
            .select("id,hospital_id,bed_type,expires_at")
            .eq("seeker_id", session["user_id"])
            .eq("status", "held")
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        existing_hold = None

    if existing_hold:
        msg = "You already have an active emergency bed hold. Please proceed directly to the hospital or cancel your existing hold."
        if wants_json():
            return json_response(False, error=msg, status=409, error_code="duplicate_hold")
        flash(msg, "error")
        return redirect(url_for("emergency_bed_page"))

    # Attempt atomic RPC first
    rpc_hold = None
    try:
        rpc_hold = rpc(
            "hold_hospital_bed",
            {
                "p_hospital_id": hospital_id,
                "p_seeker_id": session["user_id"],
                "p_bed_type": bed_type,
                "p_notes": clean_text(data.get("notes"), 500, required=False) or None,
            },
        )
    except Exception as error:
        app.logger.info("hold_hospital_bed RPC not available or failed (%s), using direct table transaction fallback", error)
        rpc_hold = None

    if rpc_hold:
        new_count = None
        try:
            avail = table("hospital_bed_availability").select(col_name).eq("hospital_id", hospital_id).maybe_single().execute().data
            new_count = avail.get(col_name) if avail else None
        except Exception:
            pass
    else:
        # Fallback table-level check and update
        avail = (
            table("hospital_bed_availability")
            .select("*")
            .eq("hospital_id", hospital_id)
            .maybe_single()
            .execute()
            .data
            or {}
        )
        current_count = int(avail.get(col_name) or 0)
        if current_count <= 0:
            msg = f"No {HOSPITAL_BED_TYPES[bed_type][0]} beds currently available at this hospital."
            if wants_json():
                return json_response(False, error=msg, status=409, error_code="capacity_exceeded")
            flash(msg, "error")
            return redirect(url_for("emergency_bed_page"))

        new_count = current_count - 1
        table("hospital_bed_availability").update({
            col_name: new_count,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("hospital_id", hospital_id).execute()

        try:
            table("hospital_bed_holds").insert({
                "hospital_id": hospital_id,
                "seeker_id": session["user_id"],
                "bed_type": bed_type,
                "status": "held",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            }).execute()
        except Exception:
            app.logger.warning("Could not record into hospital_bed_holds")

    # Audit notifications
    if hospital.get("manager_id"):
        create_notification(
            user_id=hospital["manager_id"],
            notification_type="bed_hold",
            title="Emergency Bed Hold Requested",
            message=f"A seeker has placed a temporary hold on 1 {HOSPITAL_BED_TYPES[bed_type][0]} bed.",
        )
    create_notification(
        user_id=session["user_id"],
        notification_type="bed_hold_confirmation",
        title="Emergency Bed Hold Confirmed",
        message=f"1 {HOSPITAL_BED_TYPES[bed_type][0]} bed held at {hospital['name']} for 2 hours.",
    )

    msg = f"Bed hold confirmed for 1 {HOSPITAL_BED_TYPES[bed_type][0]} bed at {hospital['name']}. Please proceed directly to the facility."
    if wants_json():
        return json_response(True, data={
            "hospital_id": hospital_id,
            "hospital_name": hospital["name"],
            "bed_type": bed_type,
            "remaining_available": new_count,
            "expires_in_minutes": 120,
            "message": msg,
        })
    flash(msg, "success")
    return redirect(url_for("emergency_bed_page"))


@app.post("/hospitals/holds/<hold_id>/cancel")
@login_required
def cancel_hospital_bed_hold(hold_id):
    if not valid_uuid(hold_id):
        raise ValueError("Invalid hold id")

    hold = None
    try:
        hold = table("hospital_bed_holds").select("*").eq("id", hold_id).maybe_single().execute().data
    except Exception:
        hold = None

    if not hold or hold.get("status") != "held":
        raise ValueError("Active bed hold not found")

    # User must be the seeker who held it OR the hospital manager
    user_id = session["user_id"]
    is_seeker_owner = (hold.get("seeker_id") == user_id)
    hospital = None
    try:
        hospital = table("hospitals").select("id,name,manager_id").eq("id", hold["hospital_id"]).maybe_single().execute().data
    except Exception:
        hospital = None
    is_manager = bool(hospital and hospital.get("manager_id") == user_id)

    if not is_seeker_owner and not is_manager:
        if wants_json():
            return json_response(False, error="Unauthorized to cancel this bed hold", status=403, error_code="forbidden")
        return render_template("error.html", message="Unauthorized to cancel this bed hold."), 403

    # Try release RPC first
    released = None
    try:
        released = rpc("release_hospital_bed_hold", {
            "p_hold_id": hold_id,
            "p_actor_id": user_id,
            "p_new_status": "cancelled",
            "p_notes": "Cancelled by user",
        })
    except Exception:
        released = None

    if not released:
        # Fallback table update and capacity restore
        try:
            table("hospital_bed_holds").update({
                "status": "cancelled",
            }).eq("id", hold_id).execute()
        except Exception:
            pass

        bed_type = hold.get("bed_type", "general")
        col_name = HOSPITAL_BED_TYPES.get(bed_type, ("General", "general_available"))[1]
        try:
            avail = table("hospital_bed_availability").select(col_name).eq("hospital_id", hold["hospital_id"]).maybe_single().execute().data
            if avail:
                table("hospital_bed_availability").update({
                    col_name: (avail.get(col_name) or 0) + 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("hospital_id", hold["hospital_id"]).execute()
        except Exception:
            pass

    flash("Bed hold cancelled and capacity restored.", "success")
    if wants_json():
        return json_response(True, data={"message": "Bed hold cancelled and capacity restored."})
    if is_manager:
        return redirect(url_for("hospital_dashboard"))
    if request.referrer and "/seeker/dashboard" in request.referrer:
        return redirect(url_for("seeker_dashboard"))
    return redirect(url_for("emergency_bed_page"))


@app.post("/hospitals/holds/<hold_id>/admit")
@hospital_manager_required
def admit_hospital_bed_hold(hold_id):
    if not valid_uuid(hold_id):
        raise ValueError("Invalid hold id")

    hold = None
    try:
        hold = table("hospital_bed_holds").select("*").eq("id", hold_id).maybe_single().execute().data
    except Exception:
        hold = None

    if not hold or hold.get("status") != "held":
        raise ValueError("Active bed hold not found")

    user_id = session["user_id"]
    hospital = current_hospital()
    if not hospital or hold.get("hospital_id") != hospital.get("id"):
        if wants_json():
            return json_response(False, error="Unauthorized to admit this bed hold", status=403, error_code="forbidden")
        return render_template("error.html", message="Unauthorized to admit this bed hold."), 403

    released = None
    try:
        released = rpc("release_hospital_bed_hold", {
            "p_hold_id": hold_id,
            "p_actor_id": user_id,
            "p_new_status": "admitted",
            "p_notes": "Admitted by hospital manager",
        })
    except Exception as error:
        app.logger.info("release_hospital_bed_hold RPC not available (%s), using direct update fallback", error)
        released = None

    if not released:
        try:
            table("hospital_bed_holds").update({
                "status": "admitted",
            }).eq("id", hold_id).execute()
        except Exception:
            pass

    if hold.get("seeker_id"):
        try:
            create_notification(
                user_id=hold["seeker_id"],
                notification_type="bed_hold_admitted",
                title="Hospital Admission Confirmed",
                message=f"Your emergency admission at {hospital.get('name', 'the hospital')} has been processed and confirmed.",
            )
        except Exception:
            app.logger.warning("Could not record admission notification for seeker")

    flash("Patient admission confirmed. Bed hold finalized.", "success")
    if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
        return json_response(True, data={"message": "Patient admission confirmed."})
    return redirect(url_for("hospital_dashboard"))


@app.get("/seeker/emergency-bed")
@seeker_required
def emergency_bed_page():
    active_hold = None
    try:
        active_hold = (
            table("hospital_bed_holds")
            .select("*, hospitals!hospital_bed_holds_hospital_id_fkey(id,name,address,phone,lat,lng)")
            .eq("seeker_id", session["user_id"])
            .eq("status", "held")
            .gt("expires_at", datetime.now(timezone.utc).isoformat())
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        try:
            active_hold = (
                table("hospital_bed_holds")
                .select("*")
                .eq("seeker_id", session["user_id"])
                .eq("status", "held")
                .maybe_single()
                .execute()
                .data
            )
            if active_hold and active_hold.get("hospital_id"):
                hosp = (
                    table("hospitals")
                    .select("id,name,address,phone,lat,lng")
                    .eq("id", active_hold["hospital_id"])
                    .maybe_single()
                    .execute()
                    .data
                )
                active_hold["hospitals"] = hosp
        except Exception:
            active_hold = None

    if active_hold and active_hold.get("expires_at"):
        try:
            exp = datetime.fromisoformat(str(active_hold["expires_at"]).replace("Z", "+00:00"))
            if exp <= datetime.now(timezone.utc):
                active_hold = None
        except Exception:
            pass

    return render_template("seeker/emergency_bed.html", active_hold=active_hold)


@app.route("/seeker/driver-request", methods=["GET", "POST"])
@seeker_required
def seeker_driver_request():
    if request.method == "GET":
        driver_request = None
        matches = []
        booking = None
        request_id = request.args.get("request_id")
        if request_id:
            if not valid_uuid(request_id):
                raise ValueError("Invalid driver request id")
            driver_request = (
                table("driver_requests")
                .select("*")
                .eq("id", request_id)
                .eq("seeker_id", session["user_id"])
                .maybe_single()
                .execute()
                .data
            )
            if not driver_request:
                return render_request_error(404, "Driver request not found.")
            booking = (
                table("driver_bookings")
                .select("id,driver_id,status,start_at,end_at,accepted_at")
                .eq("request_id", request_id)
                .eq("seeker_id", session["user_id"])
                .maybe_single()
                .execute()
                .data
            )
            if booking:
                booking["driver"] = (
                    table("users")
                    .select("name,phone")
                    .eq("id", booking["driver_id"])
                    .maybe_single()
                    .execute()
                    .data
                )
                try:
                    booking["driver_profile"] = (
                        table("driver_profiles")
                        .select("experience_years,licence_category,languages,bio")
                        .eq("provider_id", booking["driver_id"])
                        .maybe_single()
                        .execute()
                        .data
                    )
                except Exception:
                    booking["driver_profile"] = None
                try:
                    booking["driver_location"] = (
                        table("driver_locations")
                        .select("lat,lng,sharing_enabled,updated_at")
                        .eq("driver_id", booking["driver_id"])
                        .maybe_single()
                        .execute()
                        .data
                    )
                except Exception:
                    booking["driver_location"] = None
            elif driver_request["status"] == "open":
                matches = rpc(
                    "find_drivers_for_request",
                    {"p_seeker_id": session["user_id"], "p_request_id": request_id},
                ) or []

        previous_requests = []
        try:
            req_query = table("driver_requests").select("id,pickup_address,destination_address,requested_start,status,created_at").eq("seeker_id", session["user_id"])
            if hasattr(req_query, "order"):
                req_query = req_query.order("created_at", desc=True)
            if hasattr(req_query, "limit"):
                req_query = req_query.limit(5)
            previous_requests = req_query.execute().data or []
        except Exception:
            previous_requests = []

        return render_template(
            "seeker/driver_request.html",
            driver_request=driver_request,
            booking=booking,
            driver_location=booking.get("driver_location") if booking else None,
            matches=matches,
            previous_requests=previous_requests,
        )

    data = request.form
    pickup = clean_text(data.get("pickup"), 250)
    destination = clean_text(data.get("destination"), 250)
    pickup_lat = coerce_float(data.get("pickup_lat"), "Pickup latitude", -90, 90)
    pickup_lng = coerce_float(data.get("pickup_lng"), "Pickup longitude", -180, 180)
    destination_lat = coerce_float(data.get("destination_lat"), "Destination latitude", -90, 90)
    destination_lng = coerce_float(data.get("destination_lng"), "Destination longitude", -180, 180)
    requested_date = clean_text(data.get("requested_date"), 10)
    requested_time = clean_text(data.get("requested_time"), 5)
    try:
        local_start = datetime.strptime(
            f"{requested_date}T{requested_time}", "%Y-%m-%dT%H:%M"
        )
        if local_start.strftime("%Y-%m-%d") != requested_date or local_start.strftime("%H:%M") != requested_time:
            raise ValueError
    except ValueError as error:
        raise ValueError("Requested date and time must use a valid YYYY-MM-DD and HH:MM value") from error
    try:
        offset_minutes = int(data.get("timezone_offset_minutes", ""))
    except (TypeError, ValueError) as error:
        raise ValueError("A valid local timezone offset is required") from error
    if offset_minutes < -840 or offset_minutes > 840:
        raise ValueError("Timezone offset is outside the valid range")
    requested_start = local_start.replace(
        tzinfo=timezone(timedelta(minutes=-offset_minutes))
    ).astimezone(timezone.utc)
    if requested_start <= datetime.now(timezone.utc):
        raise ValueError("Requested date and time must be in the future")
    duration_hours = coerce_float(data.get("duration_hours"), "Duration", 0.01, 168)
    duration_minutes = duration_hours * 60
    if not duration_minutes.is_integer():
        raise ValueError("Duration must be in whole minutes")
    amenities = set(data.getlist("amenities"))
    if not amenities.issubset({"food", "rest_breaks"}):
        raise ValueError("Invalid amenity selection")
    requested_radius = coerce_float(data.get("requested_radius_km") or 25, "Search radius", 1, 200)
    request_id = rpc(
        "create_driver_request",
        {
            "p_seeker_id": session["user_id"],
            "p_pickup_address": pickup,
            "p_pickup_lat": pickup_lat,
            "p_pickup_lng": pickup_lng,
            "p_destination_address": destination,
            "p_destination_lat": destination_lat,
            "p_destination_lng": destination_lng,
            "p_requested_start": requested_start.isoformat(),
            "p_duration_minutes": int(duration_minutes),
            "p_amenities": sorted(amenities),
            "p_requested_radius_km": requested_radius,
        },
    )
    if not request_id:
        raise RuntimeError("Driver request creation did not return a request identifier")
    flash("Your driver request was created.", "success")
    return redirect(url_for("seeker_driver_request", request_id=request_id))


@app.post("/seeker/driver-bookings/<booking_id>/cancel")
@seeker_required
def seeker_cancel_driver_booking(booking_id):
    if not valid_uuid(booking_id):
        raise ValueError("Invalid booking id")
    booking = None
    try:
        booking = (
            table("driver_bookings")
            .select("*")
            .eq("id", booking_id)
            .eq("seeker_id", session["user_id"])
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        booking = None
    if not booking:
        return render_request_error(404, "Booking not found.")

    if booking.get("status") in ("completed", "cancelled_by_driver", "cancelled_by_seeker"):
        raise ValueError(f"Booking is already in finalized status: {booking.get('status')}")

    data = parse_json_or_form()
    reason = clean_text(data.get("reason"), 500, required=False) or "Cancelled by passenger"
    now_iso = datetime.now(timezone.utc).isoformat()

    table("driver_bookings").update({
        "status": "cancelled_by_seeker",
        "cancelled_at": now_iso,
        "cancellation_reason": reason,
        "updated_at": now_iso,
    }).eq("id", booking_id).execute()

    if booking.get("request_id"):
        try:
            table("driver_requests").update({
                "status": "cancelled",
                "updated_at": now_iso,
            }).eq("id", booking["request_id"]).execute()
        except Exception:
            pass

    try:
        table("driver_booking_events").insert({
            "booking_id": booking_id,
            "event_type": "cancelled_by_seeker",
            "created_by": session["user_id"],
            "status_before": booking.get("status"),
            "status_after": "cancelled_by_seeker",
            "details": {"reason": reason},
            "created_at": now_iso,
        }).execute()
    except Exception:
        pass

    if booking.get("driver_id"):
        create_notification(
            user_id=booking["driver_id"],
            notification_type="driver_booking_cancelled",
            title="Trip Cancelled by Passenger",
            message=f"Passenger cancelled booking: {reason}",
        )

    flash("Your driver booking has been cancelled.", "success")
    if wants_json():
        return json_response(True, data={"message": "Your driver booking has been cancelled."})
    if request.referrer and "/seeker/dashboard" in request.referrer:
        return redirect(url_for("seeker_dashboard"))
    return redirect(url_for("seeker_driver_request", request_id=booking.get("request_id")))


@app.get("/seeker/driver-request/<request_id>/driver-location")
@seeker_required
def seeker_driver_location(request_id):
    if not valid_uuid(request_id):
        return json_response(False, error="Invalid driver request id", status=400)

    seeker_id = session.get("user_id")

    driver_req = None
    try:
        driver_req = (
            table("driver_requests")
            .select("id,seeker_id,pickup_address,pickup_lat,pickup_lng,destination_address,destination_lat,destination_lng,status")
            .eq("id", request_id)
            .maybe_single()
            .execute()
            .data
        )
    except Exception as err:
        app.logger.warning("Error fetching driver request: %s", err)
        driver_req = None

    if not driver_req:
        return json_response(False, error="Driver request not found", status=404)

    # Server-side authorization: only the seeker who created the request can access driver location
    if driver_req.get("seeker_id") != seeker_id:
        return json_response(False, error="Access denied: you are not the requester", status=403)

    req_status = (driver_req.get("status") or "").lower()
    if req_status in ("cancelled", "completed", "expired"):
        return json_response(False, error=f"Tracking is unavailable for {req_status} trips", status=400, data=None)

    booking = None
    try:
        booking = (
            table("driver_bookings")
            .select("id,driver_id,seeker_id,status,start_at,end_at")
            .eq("request_id", request_id)
            .eq("seeker_id", seeker_id)
            .maybe_single()
            .execute()
            .data
        )
    except Exception as err:
        app.logger.warning("Error fetching driver booking: %s", err)
        booking = None

    if not booking or not booking.get("driver_id"):
        return json_response(False, error="Tracking is unavailable for unassigned trips", status=400, data=None)

    booking_status = (booking.get("status") or "").lower()
    if booking_status in ("completed", "cancelled_by_driver", "cancelled_by_seeker", "expired", "rejected"):
        return json_response(False, error=f"Tracking is unavailable for {booking_status} trips", status=400, data=None)

    if booking_status not in ("accepted", "in_progress"):
        return json_response(False, error=f"Tracking is unavailable for trip with status '{booking_status}'", status=400, data=None)

    driver_id = booking["driver_id"]
    location = None
    try:
        location = (
            table("driver_locations")
            .select("lat,lng,accuracy_m,updated_at,sharing_enabled")
            .eq("driver_id", driver_id)
            .maybe_single()
            .execute()
            .data
        )
    except Exception as err:
        app.logger.warning("Error fetching driver location: %s", err)
        location = None

    if not location or not location.get("sharing_enabled") or location.get("lat") is None or location.get("lng") is None:
        return json_response(True, data=None, preserve_none=True)

    distance = None
    if driver_req.get("pickup_lat") is not None and driver_req.get("pickup_lng") is not None:
        try:
            distance = round(
                haversine(
                    float(driver_req["pickup_lat"]),
                    float(driver_req["pickup_lng"]),
                    float(location["lat"]),
                    float(location["lng"]),
                ),
                2,
            )
        except Exception:
            distance = None

    is_stale = False
    updated_at_str = location.get("updated_at")
    if updated_at_str:
        try:
            if isinstance(updated_at_str, datetime):
                up_dt = updated_at_str if updated_at_str.tzinfo else updated_at_str.replace(tzinfo=timezone.utc)
            else:
                up_dt = datetime.fromisoformat(str(updated_at_str).replace("Z", "+00:00"))
                if not up_dt.tzinfo:
                    up_dt = up_dt.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - up_dt).total_seconds()
            if age_seconds > 900:  # 15 minutes
                is_stale = True
        except Exception:
            is_stale = False

    driver_user = None
    try:
        driver_user = table("users").select("name").eq("id", driver_id).maybe_single().execute().data
    except Exception:
        driver_user = None

    data_payload = {
        "driver_id": driver_id,
        "driver_name": driver_user.get("name") if driver_user else "Driver",
        "lat": location["lat"],
        "lng": location["lng"],
        "accuracy_m": location.get("accuracy_m"),
        "updated_at": updated_at_str if isinstance(updated_at_str, str) else (updated_at_str.isoformat() if updated_at_str else None),
        "sharing_enabled": True,
        "trip_status": booking_status,
        "distance_km": distance,
        "is_stale": is_stale,
    }
    return json_response(True, data=data_payload)


@app.get("/hospitals/search")
@seeker_required
def search_hospitals():
    latitude = coerce_float(request.args.get("latitude"), "Latitude", -90, 90)
    longitude = coerce_float(request.args.get("longitude"), "Longitude", -180, 180)
    radius = coerce_float(request.args.get("radius") or 5, "Radius", 0.5, 50)
    if radius not in HOSPITAL_RADIUS_OPTIONS:
        raise ValueError("Radius must be 2, 5, 10, 20 or 50 km")
    bed_type = (request.args.get("bed_type") or "general").strip().lower()
    if bed_type not in HOSPITAL_BED_TYPES:
        raise ValueError("Invalid bed type")
    results = hospital_search_results(latitude, longitude, radius, bed_type)
    return json_response(
        data={
            "bed_type": bed_type,
            "radius_km": radius,
            "hospitals": results,
        }
    )


def get_seeker_bed_reservations(user_id):
    now_utc = datetime.now(timezone.utc)
    holds_data = []
    try:
        res = (
            table("hospital_bed_holds")
            .select("*, hospitals!hospital_bed_holds_hospital_id_fkey(id,name,address,phone,lat,lng)")
            .eq("seeker_id", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        holds_data = res.data or []
    except Exception:
        try:
            res = (
                table("hospital_bed_holds")
                .select("*")
                .eq("seeker_id", user_id)
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            holds_data = res.data or []
        except Exception as err:
            app.logger.warning("Error querying seeker bed holds: %s", err)
            holds_data = []

    if isinstance(holds_data, dict):
        holds_data = [holds_data]
    elif not isinstance(holds_data, list):
        holds_data = []

    # Ensure hospital details are attached (batch query where supported, fallback per item)
    missing_ids = list(
        {
            h["hospital_id"]
            for h in holds_data
            if isinstance(h, dict) and not h.get("hospitals") and h.get("hospital_id")
        }
    )
    if missing_ids:
        query = table("hospitals").select("id,name,address,phone,lat,lng")
        if hasattr(query, "in_"):
            try:
                hosp_rows = query.in_("id", missing_ids).execute().data or []
                hosp_map = {r["id"]: r for r in hosp_rows if isinstance(r, dict) and r.get("id")}
                for h in holds_data:
                    if isinstance(h, dict) and not h.get("hospitals") and h.get("hospital_id") in hosp_map:
                        h["hospitals"] = hosp_map[h["hospital_id"]]
            except Exception as batch_err:
                app.logger.debug("Batch hospital query failed: %s", batch_err)

    for h in holds_data:
        if isinstance(h, dict) and not h.get("hospitals") and h.get("hospital_id"):
            try:
                hosp = (
                    table("hospitals")
                    .select("id,name,address,phone,lat,lng")
                    .eq("id", h["hospital_id"])
                    .maybe_single()
                    .execute()
                    .data
                )
                if isinstance(hosp, list):
                    hosp = hosp[0] if hosp else {}
                h["hospitals"] = hosp or {}
            except Exception:
                pass

    active_hold = None
    processed_holds = []
    for h in holds_data:
        hosp = h.get("hospitals") or {}
        if isinstance(hosp, list):
            hosp = hosp[0] if hosp else {}
        bed_type = (h.get("bed_type") or "general").lower()
        bed_label = HOSPITAL_BED_TYPES.get(bed_type, (bed_type.title(), ""))[0]
        status = h.get("status") or "held"
        effective_status = status
        is_active = False

        expires_at = h.get("expires_at")
        if expires_at:
            try:
                if isinstance(expires_at, datetime):
                    exp_dt = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
                else:
                    exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                    if not exp_dt.tzinfo:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if status == "held":
                    if exp_dt <= now_utc:
                        effective_status = "expired"
                    else:
                        is_active = True
            except Exception:
                if status == "held":
                    is_active = True
        elif status == "held":
            is_active = True

        hold_item = {
            "id": h.get("id"),
            "hospital_id": h.get("hospital_id"),
            "hospital_name": hosp.get("name") or "Hospital Facility",
            "hospital_address": hosp.get("address") or "",
            "hospital_phone": hosp.get("phone") or "",
            "hospital_lat": hosp.get("lat"),
            "hospital_lng": hosp.get("lng"),
            "bed_type": bed_type,
            "bed_type_label": bed_label,
            "status": status,
            "effective_status": effective_status,
            "notes": h.get("notes"),
            "created_at": str(h.get("created_at")) if h.get("created_at") else None,
            "expires_at": str(expires_at) if expires_at else None,
            "is_active": is_active,
        }
        processed_holds.append(hold_item)
        if is_active and not active_hold:
            active_hold = hold_item

    return {
        "active_hold": active_hold,
        "recent_holds": processed_holds,
        "count": len(processed_holds),
    }


def get_seeker_driver_trips(user_id):
    bookings_data = []
    try:
        res = (
            table("driver_bookings")
            .select("id,driver_id,seeker_id,request_id,status,start_at,end_at,accepted_at,cancelled_at,completed_at,cancellation_reason,created_at,updated_at")
            .eq("seeker_id", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        bookings_data = res.data or []
    except Exception as err:
        app.logger.warning("Error querying seeker driver bookings: %s", err)
        bookings_data = []

    if isinstance(bookings_data, dict):
        bookings_data = [bookings_data]
    elif not isinstance(bookings_data, list):
        bookings_data = []

    requests_data = []
    try:
        res = (
            table("driver_requests")
            .select("id,seeker_id,pickup_address,destination_address,requested_start,duration_minutes,status,created_at,expires_at")
            .eq("seeker_id", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        requests_data = res.data or []
    except Exception as err:
        app.logger.warning("Error querying seeker driver requests: %s", err)
        requests_data = []

    if isinstance(requests_data, dict):
        requests_data = [requests_data]
    elif not isinstance(requests_data, list):
        requests_data = []

    requests_by_id = {r["id"]: r for r in requests_data if isinstance(r, dict) and r.get("id")}
    for b in bookings_data:
        if not isinstance(b, dict):
            continue
        req_id = b.get("request_id")
        if req_id and req_id not in requests_by_id:
            try:
                req = (
                    table("driver_requests")
                    .select("id,seeker_id,pickup_address,destination_address,requested_start,duration_minutes,status,created_at,expires_at")
                    .eq("id", req_id)
                    .maybe_single()
                    .execute()
                    .data
                )
                if req:
                    requests_by_id[req_id] = req
            except Exception:
                pass

    processed_trips = []
    booked_request_ids = set()

    for b in bookings_data:
        req_id = b.get("request_id")
        if req_id:
            booked_request_ids.add(req_id)
        req = requests_by_id.get(req_id) or {}
        if isinstance(req, list):
            req = req[0] if req else {}
        driver_id = b.get("driver_id")
        driver_user = {}
        driver_profile = {}
        driver_location = None

        if driver_id:
            try:
                driver_user = (
                    table("users")
                    .select("name,phone")
                    .eq("id", driver_id)
                    .maybe_single()
                    .execute()
                    .data
                    or {}
                )
                if isinstance(driver_user, list):
                    driver_user = driver_user[0] if driver_user else {}
            except Exception:
                pass
            try:
                driver_profile = (
                    table("driver_profiles")
                    .select("experience_years,licence_category")
                    .eq("provider_id", driver_id)
                    .maybe_single()
                    .execute()
                    .data
                    or {}
                )
                if isinstance(driver_profile, list):
                    driver_profile = driver_profile[0] if driver_profile else {}
            except Exception:
                pass
            try:
                loc = (
                    table("driver_locations")
                    .select("lat,lng,sharing_enabled,updated_at")
                    .eq("driver_id", driver_id)
                    .maybe_single()
                    .execute()
                    .data
                )
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                if loc and loc.get("sharing_enabled") and loc.get("lat") is not None:
                    driver_location = loc
            except Exception:
                pass

        booking_status = b.get("status") or "accepted"
        is_active = booking_status in ("accepted", "in_progress")
        can_cancel = (booking_status == "accepted")

        trip_item = {
            "type": "booking",
            "id": b.get("id"),
            "booking_id": b.get("id"),
            "request_id": req_id,
            "pickup_address": req.get("pickup_address") or "Pickup location",
            "destination_address": req.get("destination_address") or "Destination",
            "requested_start": str(req.get("requested_start") or b.get("start_at") or ""),
            "duration_minutes": req.get("duration_minutes") or 60,
            "status": booking_status,
            "status_label": booking_status.replace("_", " ").title(),
            "is_active": is_active,
            "can_cancel": can_cancel,
            "driver": {
                "name": driver_user.get("name") or "Assigned Driver",
                "phone": driver_user.get("phone") or "",
                "experience_years": driver_profile.get("experience_years"),
                "licence_category": driver_profile.get("licence_category") or "",
            } if driver_id else None,
            "driver_location": driver_location,
            "created_at": str(b.get("created_at") or ""),
        }
        processed_trips.append(trip_item)

    for r in requests_data:
        if r.get("id") in booked_request_ids:
            continue
        req_status = r.get("status") or "open"
        is_active = (req_status == "open")
        trip_item = {
            "type": "request",
            "id": r.get("id"),
            "booking_id": None,
            "request_id": r.get("id"),
            "pickup_address": r.get("pickup_address") or "Pickup location",
            "destination_address": r.get("destination_address") or "Destination",
            "requested_start": str(r.get("requested_start") or ""),
            "duration_minutes": r.get("duration_minutes") or 60,
            "status": req_status,
            "status_label": "Pending Driver" if req_status == "open" else req_status.replace("_", " ").title(),
            "is_active": is_active,
            "can_cancel": False,
            "driver": None,
            "driver_location": None,
            "created_at": str(r.get("created_at") or ""),
        }
        processed_trips.append(trip_item)

    processed_trips.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    active_trip = None
    for t in processed_trips:
        if t["is_active"]:
            active_trip = t
            break

    return {
        "active_trip": active_trip,
        "recent_trips": processed_trips[:5],
        "count": len(processed_trips),
    }


@app.get("/seeker/dashboard")
@seeker_required
def seeker_dashboard():
    expire_open_queries()
    user_id = session["user_id"]
    queries = (
        table("queries")
        .select("*")
        .eq("seeker_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    query_ids = [q["id"] for q in queries if q.get("id")]
    counts = {qid: 0 for qid in query_ids}
    if query_ids:
        try:
            resp_rows = (
                table("responses")
                .select("query_id")
                .in_("query_id", query_ids)
                .execute()
                .data
                or []
            )
            for r in resp_rows:
                qid = r.get("query_id")
                if qid in counts:
                    counts[qid] += 1
        except Exception as exc:
            app.logger.warning("Batch response count query failed, falling back: %s", exc)
            counts = {q["id"]: query_response_count(q["id"]) for q in queries}
    buckets = {status: [q for q in queries if q.get("status") == status] for status in ["open", "matched", "resolved", "expired"]}
    bed_reservations = get_seeker_bed_reservations(user_id)
    driver_trips = get_seeker_driver_trips(user_id)
    return render_template(
        "seeker/dashboard.html",
        buckets=buckets,
        counts=counts,
        bed_reservations=bed_reservations,
        driver_trips=driver_trips,
    )


@app.get("/seeker/active-reservations")
@seeker_required
def seeker_active_reservations():
    user_id = session["user_id"]
    bed_reservations = get_seeker_bed_reservations(user_id)
    driver_trips = get_seeker_driver_trips(user_id)
    return json_response(
        True,
        data={
            "bed_reservations": bed_reservations,
            "driver_trips": driver_trips,
        },
    )


@app.get("/seeker/post")
@seeker_required
def post_query_page():
    return render_template("seeker/post_query.html", query_id=str(uuid.uuid4()))


@app.post("/query/post")
@seeker_required
def post_query():
    data = request.form.to_dict()
    raw_lat = data.get("lat")
    raw_lng = data.get("lng")
    if raw_lat in {None, ""}:
        raw_lat = session.get("lat")
    if raw_lng in {None, ""}:
        raw_lng = session.get("lng")
    if raw_lat in {None, ""} or raw_lng in {None, ""}:
        return render_request_error(
            400,
            "Location coordinates are required to find nearby providers. Please use 'Use my current location' or enter your coordinates.",
            log_message="Missing coordinates in query post",
            error_code="missing_location",
            log_stack=False,
        )
    lat = coerce_float(raw_lat, "Latitude", -90, 90)
    lng = coerce_float(raw_lng, "Longitude", -180, 180)
    # Preserves seeker account profile location in session; request-specific coordinates are saved in record
    radius = coerce_float(data.get("radius_km") or 5, "Radius", 0.5, 50)
    urgency = clean_text(data.get("urgency"), 20)
    if urgency not in ALLOWED_URGENCY:
        raise ValueError("Invalid urgency")
    submitted_query_id = clean_text(data.get("query_id"), 36, required=False)
    query_id = submitted_query_id or str(uuid.uuid4())
    if not valid_uuid(query_id):
        raise ValueError("Invalid request identifier")
    record = {
        "id": query_id,
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
        "image_url": None,
        "expires_at": parse_expiration(data.get("expiration")),
    }
    if submitted_query_id:
        existing = (
            table("queries")
            .select("id")
            .eq("id", query_id)
            .eq("seeker_id", session["user_id"])
            .maybe_single()
            .execute()
            .data
        )
        if existing:
            return redirect(url_for("seeker_responses_page", query_id=query_id))

    image_file = request.files.get("image")
    if image_file and image_file.filename:
        try:
            record["image_url"] = upload_public_image(
                "query-images",
                image_file,
                session["user_id"],
            )["image_url"]
        except ValueError as error:
            return render_request_error(
                400,
                str(error),
                log_message="Query image validation failed",
                error_code="invalid_query_image",
                log_stack=False,
            )
        except Exception as error:
            status_code = error.status_code if isinstance(error, SupabaseRequestError) else None
            app.logger.error(
                "Query image upload failed exception_type=%s status=%s",
                type(error).__name__,
                status_code,
            )
            if status_code in {401, 403}:
                message = (
                    "Image storage rejected the upload. Please contact support or submit without an image; "
                    "your request was not saved."
                )
            elif status_code == 404:
                message = (
                    "Image storage is unavailable. Please contact support or submit without an image; "
                    "your request was not saved."
                )
            else:
                message = (
                    "We couldn't upload that image. Try again or submit without an image; "
                    "your request was not saved."
                )
            return render_request_error(
                502,
                message,
                log_message="Query image upload failed",
                error_code="query_image_upload_failed",
                log_stack=False,
            )
    try:
        table("queries").insert(record).execute()
    except Exception as error:
        if submitted_query_id:
            try:
                existing = (
                    table("queries")
                    .select("id")
                    .eq("id", query_id)
                    .eq("seeker_id", session["user_id"])
                    .maybe_single()
                    .execute()
                    .data
                )
            except Exception:
                existing = None
            if existing:
                return redirect(url_for("seeker_responses_page", query_id=query_id))
        status_code = error.status_code if isinstance(error, SupabaseRequestError) else None
        app.logger.error(
            "Query insert failed exception_type=%s status=%s",
            type(error).__name__,
            status_code,
        )
        return render_request_error(
            502,
            "Your request could not be saved. Please retry this form; an already-saved request will not be duplicated.",
            log_message="Query insert failed",
            error_code="query_save_failed",
            log_stack=False,
        )
    notify_nearby_providers(record)
    flash("Request posted successfully.", "success")
    return redirect(url_for("seeker_responses_page", query_id=record["id"]))


@app.post("/upload/query-image")
@seeker_required
def upload_query_image():
    file_storage = request.files.get("image")
    try:
        result = upload_public_image("query-images", file_storage, session["user_id"])
        return json_response(data={"image_url": result["image_url"]}, status=201)
    except ValueError as err:
        return json_response(False, error=str(err), status=400)
    except Exception as err:
        app.logger.error("Query image upload failed: %s", err)
        return json_response(False, error="Image upload failed. Please try again.", status=502)



@app.get("/query/list")
@provider_required
def list_queries():
    expire_open_queries()
    provider = current_profile()
    if not provider or provider.get("is_active") is not True:
        return json_response(False, error="Provider account is inactive", status=403)
    plat, plng, _ = get_provider_effective_location(provider)
    if plat is None or plng is None:
        return json_response(False, error="Update your location to discover nearby requests", status=400)
    radius = coerce_float(request.args.get("radius_km") or 5, "Radius", 0.5, 50)
    category = (request.args.get("category") or "").strip().lower()
    sort = request.args.get("sort") or "distance"
    rows = table("queries").select("*, users:seeker_id(name)").eq("status", "open").execute().data or []
    priority = {"very_urgent": 0, "urgent": 1, "normal": 2}
    items = []
    seen_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        if not row_id or row_id in seen_ids:
            continue
        if row.get("status") != "open":
            continue
        qlat, qlng, loc_src = get_query_effective_coordinates(row)
        if qlat is None or qlng is None:
            continue
        if query_is_expired(row):
            continue
        if category and category not in (row.get("category") or "").lower():
            continue
        try:
            distance = haversine(plat, plng, qlat, qlng)
        except (ValueError, TypeError):
            continue
        try:
            query_radius = float(row.get("radius_km")) if row.get("radius_km") is not None else float(radius)
            if query_radius <= 0:
                query_radius = float(radius)
        except (ValueError, TypeError):
            query_radius = float(radius)
        provider_radius = None
        if provider.get("service_radius_km"):
            try:
                provider_radius = float(provider["service_radius_km"])
            except (ValueError, TypeError):
                pass
        max_allowed = min(radius, query_radius, provider_radius) if provider_radius else min(radius, query_radius)
        if distance <= max_allowed:
            seen_ids.add(row_id)
            items.append(
                {
                    "id": row["id"],
                    "item_name": row.get("item_name", ""),
                    "category": row.get("category", ""),
                    "description": row.get("description", ""),
                    "seeker_name": (row.get("users") or {}).get("name", "Seeker"),
                    "location": row.get("location"),
                    "lat": qlat,
                    "lng": qlng,
                    "location_source": loc_src,
                    "distance": distance,
                    "distance_label": format_distance(distance),
                    "distance_type": "straight_line",
                    "distance_type_label": "straight-line distance",
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
    return json_response(data={"queries": items, "provider": {"lat": plat, "lng": plng}})


@app.get("/provider/dashboard")
@provider_required
def provider_dashboard():
    profile = current_profile()
    summary = provider_response_summary(session["user_id"])
    selected_responses = selected_provider_responses(session["user_id"])
    try:
        location = (
            table("provider_locations")
            .select("sharing_enabled")
            .eq("provider_id", session["user_id"])
            .maybe_single()
            .execute()
            .data
            or {}
        )
    except SupabaseRequestError as error:
        if error.status_code != 404:
            raise
        app.logger.warning("Live location table is unavailable; continuing without location sharing")
        location = {}
    driver_profile = None
    try:
        driver_profile = (
            table("driver_profiles")
            .select("verification_status,is_active")
            .eq("provider_id", session["user_id"])
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        driver_profile = None
    return render_template(
        "provider/dashboard.html",
        profile=profile,
        summary=summary,
        selected_responses=selected_responses,
        location_sharing=location.get("sharing_enabled") is True,
        driver_profile=driver_profile,
    )


def parse_driver_bio(bio_text):
    """
    Extracts structured vehicle details and profile photo URL from driver bio
    without requiring database schema changes.
    Format supported:
    [Vehicle: <details>]
    [Photo: <url>]
    <clean bio text>
    """
    if not bio_text:
        return {"vehicle_details": "", "photo_url": "", "bio": ""}

    vehicle_details = ""
    photo_url = ""
    clean_lines = []
    for line in bio_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[Vehicle:") and stripped.endswith("]"):
            vehicle_details = stripped[len("[Vehicle:"): -1].strip()
        elif stripped.startswith("[Photo:") and stripped.endswith("]"):
            photo_url = stripped[len("[Photo:"): -1].strip()
        elif stripped.startswith("Vehicle:") and not vehicle_details:
            vehicle_details = stripped[len("Vehicle:"):].strip()
        else:
            clean_lines.append(line)

    return {
        "vehicle_details": vehicle_details,
        "photo_url": photo_url,
        "bio": "\n".join(clean_lines).strip(),
    }


def serialize_driver_bio(bio_text, vehicle_details="", photo_url=""):
    """
    Combines bio text, vehicle details, and photo URL into driver_profiles.bio.
    Ensures total length stays within the 2000 character limit.
    """
    parts = []
    if vehicle_details and vehicle_details.strip():
        parts.append(f"[Vehicle: {vehicle_details.strip()}]")
    if photo_url and photo_url.strip():
        parts.append(f"[Photo: {photo_url.strip()}]")
    if bio_text and bio_text.strip():
        parts.append(bio_text.strip())
    combined = "\n".join(parts)
    return combined[:2000]


@app.get("/provider/driver-onboarding")
@provider_required
def provider_driver_onboarding_page():
    profile = current_profile() or {}
    driver_profile = (
        table("driver_profiles")
        .select("*")
        .eq("provider_id", session["user_id"])
        .maybe_single()
        .execute()
        .data
    )
    documents = []
    try:
        doc_query = (
            table("driver_verification_requests")
            .select("id,verification_type,status,document_storage_path,submitted_at,review_notes")
            .eq("driver_id", session["user_id"])
        )
        if hasattr(doc_query, "order"):
            doc_query = doc_query.order("submitted_at", desc=True)
        raw_docs = doc_query.execute().data or []
        for doc in raw_docs:
            signed_url = None
            if doc.get("document_storage_path"):
                try:
                    signed_url = create_signed_verification_url("driver-documents", doc["document_storage_path"], 300)
                except Exception:
                    signed_url = None
            documents.append({
                "id": doc.get("id"),
                "verification_type": doc.get("verification_type"),
                "status": doc.get("status"),
                "submitted_at": doc.get("submitted_at"),
                "review_notes": doc.get("review_notes"),
                "signed_url": signed_url,
            })
    except Exception as err:
        app.logger.warning("Could not fetch driver verification requests: %s", err)
        documents = []

    parsed_bio = parse_driver_bio((driver_profile or {}).get("bio") or "")

    location = {}
    try:
        location = (
            table("driver_locations")
            .select("lat,lng,sharing_enabled,updated_at")
            .eq("driver_id", session["user_id"])
            .maybe_single()
            .execute()
            .data
            or {}
        )
    except Exception:
        location = {}

    availability = {}
    try:
        availability = (
            table("driver_availability")
            .select("id,starts_at,ends_at,is_active")
            .eq("driver_id", session["user_id"])
            .eq("is_active", True)
            .maybe_single()
            .execute()
            .data
            or {}
        )
    except Exception:
        availability = {}

    doc_status_map = {
        "identity": None,
        "driving_license": None,
    }
    for doc in documents:
        vtype = doc.get("verification_type")
        if vtype in doc_status_map and doc_status_map[vtype] is None:
            doc_status_map[vtype] = doc

    return render_template(
        "driver/provider_onboarding.html",
        profile=profile,
        driver_profile=driver_profile,
        documents=documents,
        parsed_bio=parsed_bio,
        location=location,
        availability=availability,
        doc_status_map=doc_status_map,
    )


@app.post("/provider/driver-onboarding")
@provider_required
def provider_driver_onboarding_submit():
    data = parse_json_or_form()
    try:
        experience_years = int(str(data.get("experience_years", "")))
    except (TypeError, ValueError) as error:
        raise ValueError("Experience must be a whole number") from error
    if experience_years < 0 or experience_years > 60:
        raise ValueError("Experience must be between 0 and 60 years")
    licence_category = clean_text(data.get("licence_category"), 40, required=False)
    if not licence_category:
        raise ValueError("Licence category is required")
    languages_text = clean_text(data.get("languages"), 200, required=False)
    languages = [language.strip() for language in languages_text.split(",") if language.strip()]

    vehicle_details = clean_text(data.get("vehicle_details"), 200, required=False) or ""
    photo_url = clean_text(data.get("photo_url"), 500, required=False) or ""

    # Check if a profile photo was uploaded in this form
    photo_file = request.files.get("profile_photo") or request.files.get("photo")
    if photo_file and photo_file.filename:
        try:
            res_photo = upload_public_image(
                "response-images",
                photo_file,
                f"driver-photos/{session['user_id']}",
            )
            photo_url = res_photo["image_url"]
        except ValueError as err:
            flash(f"Profile photo error: {str(err)}", "error")
        except Exception as err:
            app.logger.error("Driver photo upload failed: %s", err)
            flash("Could not upload profile photo.", "error")

    # If photo_url or vehicle_details not in form, preserve from existing profile
    if not photo_url or not vehicle_details:
        try:
            existing_dp = (
                table("driver_profiles")
                .select("bio")
                .eq("provider_id", session["user_id"])
                .maybe_single()
                .execute()
                .data
            )
            if existing_dp:
                existing_parsed = parse_driver_bio(existing_dp.get("bio") or "")
                if not vehicle_details:
                    vehicle_details = existing_parsed.get("vehicle_details") or ""
                if not photo_url:
                    photo_url = existing_parsed.get("photo_url") or ""
        except Exception:
            pass

    raw_bio = clean_text(data.get("bio"), 1500, required=False) or ""
    combined_bio = serialize_driver_bio(raw_bio, vehicle_details, photo_url)

    service_radius = coerce_float(data.get("service_radius_km"), "Service radius", 0.01, 200)
    result = rpc(
        "save_driver_profile",
        {
            "p_provider_id": session["user_id"],
            "p_experience_years": experience_years,
            "p_licence_category": licence_category,
            "p_languages": languages,
            "p_bio": combined_bio or None,
            "p_service_radius_km": service_radius,
        },
    )
    if not result:
        raise RuntimeError("Driver profile save did not return the saved profile")

    # Process document uploads if present
    doc_fields = [
        ("identity_document", "identity"),
        ("driving_license_document", "driving_license"),
    ]
    docs_uploaded = 0
    for field_name, doc_type in doc_fields:
        file_obj = request.files.get(field_name)
        if file_obj and file_obj.filename:
            try:
                res = upload_private_verification_document(
                    bucket="driver-documents",
                    file_storage=file_obj,
                    user_id=session["user_id"],
                    doc_type=doc_type,
                )
                table("driver_verification_requests").insert({
                    "id": str(uuid.uuid4()),
                    "driver_id": session["user_id"],
                    "verification_type": doc_type,
                    "status": "submitted",
                    "document_storage_path": res["path"],
                }).execute()
                docs_uploaded += 1
            except ValueError as err:
                flash(f"Document error for {doc_type.replace('_', ' ')}: {str(err)}", "error")
            except Exception as err:
                app.logger.error("Driver document upload failed: %s", err)
                flash(f"Could not upload {doc_type.replace('_', ' ')} document.", "error")

    if wants_json():
        return json_response(True, data={
            "message": "Driver profile saved successfully. Verification remains pending until reviewer approval.",
            "profile": result,
            "photo_url": photo_url,
            "vehicle_details": vehicle_details,
            "docs_uploaded": docs_uploaded,
        })

    if docs_uploaded > 0:
        flash(f"Driver profile and {docs_uploaded} verification document(s) saved. Under review by authorized reviewer.", "success")
    else:
        flash("Driver profile saved. Verification remains pending until an authorized reviewer decides.", "success")
    return redirect(url_for("provider_driver_onboarding_page"))


@app.post("/provider/driver-photo/upload")
@provider_required
def provider_driver_photo_upload():
    file_obj = request.files.get("photo") or request.files.get("profile_photo")
    if not file_obj or not file_obj.filename:
        return json_response(False, error="Profile photo file is required.", status=400)
    try:
        validate_image(file_obj)
        res = upload_public_image("response-images", file_obj, f"driver-photos/{session['user_id']}")
        photo_url = res["image_url"]

        # If driver profile exists, update bio with the photo url so it persists
        try:
            driver_profile = (
                table("driver_profiles")
                .select("*")
                .eq("provider_id", session["user_id"])
                .maybe_single()
                .execute()
                .data
            )
            if driver_profile:
                parsed = parse_driver_bio(driver_profile.get("bio") or "")
                combined_bio = serialize_driver_bio(parsed["bio"], parsed["vehicle_details"], photo_url)
                rpc(
                    "save_driver_profile",
                    {
                        "p_provider_id": session["user_id"],
                        "p_experience_years": driver_profile["experience_years"],
                        "p_licence_category": driver_profile["licence_category"],
                        "p_languages": driver_profile.get("languages") or [],
                        "p_bio": combined_bio,
                        "p_service_radius_km": driver_profile["service_radius_km"],
                    },
                )
        except Exception as update_err:
            app.logger.warning("Could not auto-update driver profile with photo: %s", update_err)

        return json_response(True, data={
            "photo_url": photo_url,
            "filename": res["filename"],
            "message": "Driver profile photo uploaded successfully.",
        }, status=201)
    except ValueError as err:
        return json_response(False, error=str(err), status=400)
    except Exception as err:
        app.logger.error("Driver photo upload failed: %s", err)
        return json_response(False, error="Photo upload failed. Please try again.", status=502)



@app.post("/provider/driver-document/upload")
@provider_required
def provider_driver_document_upload():
    doc_type = request.form.get("verification_type")
    if doc_type not in {"identity", "driving_license"}:
        return json_response(False, error="Invalid verification document type. Must be 'identity' or 'driving_license'.", status=400)
    file_obj = request.files.get("document")
    if not file_obj or not file_obj.filename:
        return json_response(False, error="Document file is required.", status=400)
    try:
        res = upload_private_verification_document(
            bucket="driver-documents",
            file_storage=file_obj,
            user_id=session["user_id"],
            doc_type=doc_type,
        )
        doc_id = str(uuid.uuid4())
        table("driver_verification_requests").insert({
            "id": doc_id,
            "driver_id": session["user_id"],
            "verification_type": doc_type,
            "status": "submitted",
            "document_storage_path": res["path"],
        }).execute()
        signed_url = create_signed_verification_url("driver-documents", res["path"], 300)
        return json_response(True, data={
            "id": doc_id,
            "verification_type": doc_type,
            "filename": res["filename"],
            "signed_url": signed_url,
            "status": "submitted",
            "message": f"{doc_type.replace('_', ' ').title()} uploaded successfully.",
        }, status=201)
    except ValueError as err:
        return json_response(False, error=str(err), status=400)
    except Exception as err:
        app.logger.error("Async document upload failed: %s", err)
        return json_response(False, error="Document upload failed. Please try again.", status=502)


@app.route("/provider/driver-availability", methods=["GET", "POST"])
@provider_required
def provider_driver_availability():
    driver_profile = (
        table("driver_profiles")
        .select("provider_id,verification_status,is_active")
        .eq("provider_id", session["user_id"])
        .maybe_single()
        .execute()
        .data
    )
    if not driver_profile or driver_profile["verification_status"] != "approved" or not driver_profile["is_active"]:
        return render_request_error(
            403,
            "An active, approved driver profile is required to publish availability. Verification pending — availability publishing unlocks after approval.",
        )

    if request.method == "GET":
        location = (
            table("driver_locations")
            .select("lat,lng,sharing_enabled,updated_at")
            .eq("driver_id", session["user_id"])
            .maybe_single()
            .execute()
            .data
        )
        availability = (
            table("driver_availability")
            .select("id,starts_at,ends_at,is_active")
            .eq("driver_id", session["user_id"])
            .eq("is_active", True)
            .maybe_single()
            .execute()
            .data
        )
        return render_template(
            "driver/availability.html",
            location=location or {},
            availability=availability or {},
            driver_profile=driver_profile,
        )

    data = parse_json_or_form()
    start_value = clean_text(data.get("starts_at"), 16)
    end_value = clean_text(data.get("ends_at"), 16)
    try:
        start_at = datetime.strptime(start_value, "%Y-%m-%dT%H:%M")
        end_at = datetime.strptime(end_value, "%Y-%m-%dT%H:%M")
        offset_minutes = int(data.get("timezone_offset_minutes", ""))
    except (TypeError, ValueError) as error:
        raise ValueError("Availability start, end, and timezone must be valid") from error
    if offset_minutes < -840 or offset_minutes > 840:
        raise ValueError("Timezone offset is outside the valid range")
    local_timezone = timezone(timedelta(minutes=-offset_minutes))
    start_at = start_at.replace(tzinfo=local_timezone).astimezone(timezone.utc)
    end_at = end_at.replace(tzinfo=local_timezone).astimezone(timezone.utc)
    if start_at <= datetime.now(timezone.utc) or end_at <= start_at:
        raise ValueError("Availability must be a future time range")
    if end_at - start_at > timedelta(days=7):
        raise ValueError("Availability cannot exceed 7 days")
    sharing_enabled = data.get("sharing_enabled") in ("on", True, "true")
    latitude = coerce_float(data.get("lat"), "Latitude", -90, 90) if sharing_enabled else None
    longitude = coerce_float(data.get("lng"), "Longitude", -180, 180) if sharing_enabled else None
    result = rpc(
        "set_driver_presence",
        {
            "p_driver_id": session["user_id"],
            "p_starts_at": start_at.isoformat(),
            "p_ends_at": end_at.isoformat(),
            "p_sharing_enabled": sharing_enabled,
            "p_lat": latitude,
            "p_lng": longitude,
        },
    )
    if not result:
        raise RuntimeError("Driver availability update did not return an availability identifier")
    if wants_json():
        return json_response(True, data={
            "availability_id": result,
            "status": "Published",
            "message": "Driver availability and location-sharing preference saved.",
        })
    flash("Driver availability and location-sharing preference saved.", "success")
    next_url = data.get("next") or request.referrer
    if next_url and ("driver-onboarding" in next_url or "provider-onboarding" in next_url):
        return redirect(url_for("provider_driver_onboarding_page") + "#availabilitySection")
    return redirect(url_for("provider_driver_availability"))


@app.post("/provider/driver-availability/toggle")
@provider_required
def provider_driver_availability_toggle():
    driver_profile = (
        table("driver_profiles")
        .select("provider_id,verification_status,is_active")
        .eq("provider_id", session["user_id"])
        .maybe_single()
        .execute()
        .data
    )
    if not driver_profile or driver_profile.get("verification_status") != "approved" or not driver_profile.get("is_active"):
        return json_response(False, error="An active, approved driver profile is required to toggle availability.", status=403)

    data = parse_json_or_form()
    enable = data.get("active") in (True, "true", "on", "1")
    now_iso = datetime.now(timezone.utc).isoformat()

    if not enable:
        table("driver_availability").update({
            "is_active": False,
            "updated_at": now_iso,
        }).eq("driver_id", session["user_id"]).eq("is_active", True).execute()

        return json_response(True, data={
            "is_active": False,
            "status": "Not Published",
            "message": "Driver availability paused.",
        })
    else:
        return json_response(True, data={
            "is_active": True,
            "status": "Published",
            "message": "Driver availability active.",
        })


@app.post("/provider/driver-location")
@provider_required
def provider_refresh_driver_location():
    data = parse_json_or_form()
    sharing_enabled = data.get("sharing_enabled") in ("on", True, "true")
    if not sharing_enabled:
        raise ValueError("Location sharing must be enabled to refresh driver coordinates")
    latitude = coerce_float(data.get("lat"), "Latitude", -90, 90)
    longitude = coerce_float(data.get("lng"), "Longitude", -180, 180)

    driver_profile = (
        table("driver_profiles")
        .select("provider_id,verification_status,is_active")
        .eq("provider_id", session["user_id"])
        .maybe_single()
        .execute()
        .data
    )
    if not driver_profile or driver_profile.get("verification_status") != "approved" or not driver_profile.get("is_active"):
        return json_response(False, error="An active, approved driver profile is required to update driver location.", status=403)

    try:
        existing_loc = (
            table("driver_locations")
            .select("driver_id,sharing_enabled")
            .eq("driver_id", session["user_id"])
            .maybe_single()
            .execute()
            .data
        )
        if not existing_loc:
            table("driver_locations").insert({
                "driver_id": session["user_id"],
                "lat": latitude,
                "lng": longitude,
                "sharing_enabled": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        elif not existing_loc.get("sharing_enabled"):
            table("driver_locations").update({
                "sharing_enabled": True,
                "lat": latitude,
                "lng": longitude,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("driver_id", session["user_id"]).execute()
    except Exception as exc:
        app.logger.warning("Could not pre-ensure driver_locations record: %s", exc)

    result = rpc(
        "refresh_driver_location",
        {
            "p_driver_id": session["user_id"],
            "p_lat": latitude,
            "p_lng": longitude,
            "p_sharing_enabled": sharing_enabled,
        },
    )
    if not result:
        raise RuntimeError("Driver location refresh did not return its update time")
    return json_response(data={"updated_at": result})


@app.route("/provider/driver-requests", methods=["GET"])
@provider_required
def provider_driver_requests():
    driver_id = session["user_id"]
    try:
        matches = rpc(
            "list_driver_requests_for_driver",
            {"p_driver_id": driver_id},
        ) or []
    except Exception:
        matches = []

    active_bookings = []
    past_bookings = []
    try:
        bookings_query = table("driver_bookings").select("*").eq("driver_id", driver_id)
        if hasattr(bookings_query, "order"):
            bookings_query = bookings_query.order("created_at", desc=True)
        bookings_data = bookings_query.execute().data or []
        if isinstance(bookings_data, dict):
            bookings_data = [bookings_data]
        elif not isinstance(bookings_data, list):
            bookings_data = []

        seeker_ids = list({b["seeker_id"] for b in bookings_data if isinstance(b, dict) and b.get("seeker_id")})
        request_ids = list({b["request_id"] for b in bookings_data if isinstance(b, dict) and b.get("request_id")})

        seeker_map = {}
        if seeker_ids:
            try:
                s_query = table("users").select("id,name,phone")
                if hasattr(s_query, "in_"):
                    s_rows = s_query.in_("id", seeker_ids).execute().data or []
                    seeker_map = {r["id"]: r for r in s_rows if isinstance(r, dict) and r.get("id")}
            except Exception as s_err:
                app.logger.warning("Batch seeker lookup failed for driver bookings: %s", s_err)

        request_map = {}
        if request_ids:
            try:
                r_query = table("driver_requests").select("id,pickup_address,destination_address,duration_minutes,amenities")
                if hasattr(r_query, "in_"):
                    r_rows = r_query.in_("id", request_ids).execute().data or []
                    request_map = {r["id"]: r for r in r_rows if isinstance(r, dict) and r.get("id")}
            except Exception as r_err:
                app.logger.warning("Batch driver request lookup failed: %s", r_err)

        for b in bookings_data:
            if not isinstance(b, dict):
                continue
            seeker_id = b.get("seeker_id")
            if seeker_id and seeker_id in seeker_map:
                seeker_info = seeker_map[seeker_id]
            elif seeker_id:
                try:
                    seeker_info = table("users").select("name,phone").eq("id", seeker_id).maybe_single().execute().data
                except Exception:
                    seeker_info = None
            else:
                seeker_info = None
            b["seeker"] = seeker_info
            b["users"] = seeker_info

            req_id = b.get("request_id")
            if req_id and req_id in request_map:
                req_info = request_map[req_id]
            elif req_id:
                try:
                    req_info = table("driver_requests").select("pickup_address,destination_address,duration_minutes,amenities").eq("id", req_id).maybe_single().execute().data
                except Exception:
                    req_info = None
            else:
                req_info = None
            b["request"] = req_info
            b["driver_requests"] = req_info

            if b.get("status") in ("accepted", "in_progress"):
                active_bookings.append(b)
            else:
                past_bookings.append(b)
    except Exception as err:
        app.logger.warning("Could not query driver_bookings for provider: %s", err)

    return render_template(
        "driver/requests.html",
        requests=matches,
        active_bookings=active_bookings,
        past_bookings=past_bookings,
    )


@app.post("/provider/driver-bookings/<booking_id>/status")
@provider_required
def provider_update_driver_booking_status(booking_id):
    if not valid_uuid(booking_id):
        raise ValueError("Invalid booking id")
    driver_id = session["user_id"]
    booking = None
    try:
        booking = (
            table("driver_bookings")
            .select("*")
            .eq("id", booking_id)
            .eq("driver_id", driver_id)
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        booking = None
    if not booking:
        return render_request_error(404, "Driver booking not found.")

    data = parse_json_or_form()
    new_status = clean_text(data.get("status"), 30).lower()
    current_status = booking.get("status")

    # Validate state transitions
    valid_transitions = {
        "accepted": ["in_progress"],
        "in_progress": ["completed"],
    }
    allowed = valid_transitions.get(current_status, [])
    if new_status not in allowed:
        raise ValueError(f"Cannot transition booking from '{current_status}' to '{new_status}'.")

    now_iso = datetime.now(timezone.utc).isoformat()
    update_payload = {
        "status": new_status,
        "updated_at": now_iso,
    }
    if new_status == "in_progress":
        update_payload["started_at"] = now_iso
    elif new_status == "completed":
        update_payload["completed_at"] = now_iso

    table("driver_bookings").update(update_payload).eq("id", booking_id).execute()

    if new_status == "completed" and booking.get("request_id"):
        try:
            table("driver_requests").update({
                "status": "completed",
                "updated_at": now_iso,
            }).eq("id", booking["request_id"]).execute()
        except Exception:
            pass

    try:
        table("driver_booking_events").insert({
            "booking_id": booking_id,
            "event_type": new_status,
            "created_by": driver_id,
            "status_before": current_status,
            "status_after": new_status,
            "created_at": now_iso,
        }).execute()
    except Exception:
        pass

    status_label = "in transit / driver en route" if new_status == "in_progress" else "completed"
    if booking.get("seeker_id"):
        create_notification(
            user_id=booking["seeker_id"],
            notification_type="driver_status",
            title="Trip Status Updated",
            message=f"Your scheduled trip is now {status_label}.",
        )

    flash(f"Booking status updated to {new_status}.", "success")
    return redirect(url_for("provider_driver_requests"))


@app.post("/provider/driver-bookings/<booking_id>/cancel")
@provider_required
def provider_cancel_driver_booking(booking_id):
    if not valid_uuid(booking_id):
        raise ValueError("Invalid booking id")
    driver_id = session["user_id"]
    booking = None
    try:
        booking = (
            table("driver_bookings")
            .select("*")
            .eq("id", booking_id)
            .eq("driver_id", driver_id)
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        booking = None
    if not booking:
        return render_request_error(404, "Driver booking not found.")

    if booking.get("status") in ("completed", "cancelled_by_driver", "cancelled_by_seeker"):
        raise ValueError(f"Booking is already in finalized status: {booking.get('status')}")

    data = parse_json_or_form()
    reason = clean_text(data.get("reason"), 500, required=False) or "Cancelled by driver"
    now_iso = datetime.now(timezone.utc).isoformat()

    table("driver_bookings").update({
        "status": "cancelled_by_driver",
        "cancelled_at": now_iso,
        "cancellation_reason": reason,
        "updated_at": now_iso,
    }).eq("id", booking_id).execute()

    if booking.get("request_id"):
        try:
            table("driver_requests").update({
                "status": "cancelled",
                "updated_at": now_iso,
            }).eq("id", booking["request_id"]).execute()
        except Exception:
            pass

    try:
        table("driver_booking_events").insert({
            "booking_id": booking_id,
            "event_type": "cancelled_by_driver",
            "created_by": driver_id,
            "status_before": booking.get("status"),
            "status_after": "cancelled_by_driver",
            "details": {"reason": reason},
            "created_at": now_iso,
        }).execute()
    except Exception:
        pass

    if booking.get("seeker_id"):
        create_notification(
            user_id=booking["seeker_id"],
            notification_type="driver_cancelled",
            title="Trip Cancelled by Driver",
            message=f"Driver has cancelled this booking: {reason}",
        )

    flash("Booking has been cancelled.", "success")
    return redirect(url_for("provider_driver_requests"))


@app.post("/provider/driver-requests/<request_id>/accept")
@provider_required
def provider_accept_driver_request(request_id):
    if not valid_uuid(request_id):
        raise ValueError("Invalid driver request id")
    booking = rpc(
        "accept_driver_request",
        {"p_driver_id": session["user_id"], "p_request_id": request_id},
    )
    if not booking or not booking.get("id"):
        raise RuntimeError("Booking acceptance did not return the persisted booking")
    flash("Driver request accepted and booking created.", "success")
    return redirect(url_for("provider_driver_requests"))


@app.route("/reviewer/driver-profiles", methods=["GET", "POST"])
@login_required
def reviewer_driver_profiles():
    reviewer_id = session["user_id"]
    reviewer = (
        table("driver_reviewers")
        .select("user_id")
        .eq("user_id", reviewer_id)
        .maybe_single()
        .execute()
        .data
    )
    if not reviewer:
        return render_request_error(403, "Trusted driver reviewer authorization required.")
    session["is_reviewer"] = True

    if request.method == "POST":
        driver_id = clean_text(request.form.get("driver_id"), 36)
        if not valid_uuid(driver_id):
            raise ValueError("Invalid driver profile id")
        if driver_id == reviewer_id:
            return render_request_error(403, "Reviewers cannot approve their own driver profile.")
        decision = clean_text(request.form.get("decision"), 20)
        if decision not in {"under_review", "approved", "rejected", "suspended"}:
            raise ValueError("Invalid driver review decision")
        notes = clean_text(request.form.get("review_notes"), 2000, required=False)
        reviewed = rpc(
            "review_driver_profile",
            {
                "p_reviewer_id": reviewer_id,
                "p_driver_id": driver_id,
                "p_decision": decision,
                "p_review_notes": notes or None,
            },
        )
        if not reviewed:
            raise RuntimeError("Driver review did not return the persisted profile")
        flash(f"Driver profile review saved: {decision.replace('_', ' ')}.", "success")
        return redirect(url_for("reviewer_driver_profiles"))

    profiles = []
    for status in ("pending", "under_review"):
        status_profiles = (
            table("driver_profiles")
            .select("provider_id,experience_years,licence_category,languages,bio,verification_status,created_at,updated_at")
            .eq("verification_status", status)
            .order("updated_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
        for prof in status_profiles:
            docs = []
            try:
                doc_query = (
                    table("driver_verification_requests")
                    .select("id,verification_type,status,document_storage_path,submitted_at,review_notes")
                    .eq("driver_id", prof["provider_id"])
                )
                if hasattr(doc_query, "order"):
                    doc_query = doc_query.order("submitted_at", desc=True)
                raw_docs = doc_query.execute().data or []
                for doc in raw_docs:
                    signed_url = None
                    if doc.get("document_storage_path"):
                        try:
                            signed_url = create_signed_verification_url("driver-documents", doc["document_storage_path"], 300)
                        except Exception:
                            signed_url = None
                    docs.append({
                        "id": doc.get("id"),
                        "verification_type": doc.get("verification_type"),
                        "status": doc.get("status"),
                        "submitted_at": doc.get("submitted_at"),
                        "review_notes": doc.get("review_notes"),
                        "signed_url": signed_url,
                    })
            except Exception as doc_err:
                app.logger.warning("Failed fetching documents for driver %s: %s", prof.get("provider_id"), doc_err)
            prof["documents"] = docs
            profiles.append(prof)
    return render_template("driver/reviewer_profiles.html", profiles=profiles)


@app.get("/provider/request/<query_id>")
@provider_required
def provider_request_detail(query_id):
    if not valid_uuid(query_id):
        raise ValueError("Invalid request id")
    expire_open_queries()
    query = get_query(query_id)
    if not query:
        return render_request_error(404, "Request not found.")
    provider = current_profile()
    allowed, detail, status = provider_query_eligibility(query, provider)
    if not allowed:
        if wants_json():
            return json_response(False, error=detail, status=status)
        return render_template("error.html", message=detail), status
    distance = round(detail, 2)
    if wants_json():
        return json_response(True, data={"query": query, "distance": distance})
    return redirect(url_for("provider_respond_page", query_id=query_id))


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
        try:
            image_url = upload_public_image("response-images", request.files["image"], session["user_id"])["image_url"]
        except ValueError as error:
            if request.headers.get("Accept", "").startswith("application/json") or wants_json():
                return json_response(False, error=str(error), status=400)
            flash(f"Image error: {str(error)}", "error")
            return redirect(url_for("provider_respond_page", query_id=query_id))
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
    try:
        result = upload_public_image("response-images", request.files.get("image"), session["user_id"])
        return json_response(data={"image_url": result["image_url"]}, status=201)
    except ValueError as err:
        return json_response(False, error=str(err), status=400)
    except Exception as err:
        app.logger.error("Response image upload failed: %s", err)
        return json_response(False, error="Image upload failed. Please try again.", status=502)



@app.get("/seeker/responses/<query_id>")
@seeker_required
def seeker_responses_page(query_id):
    query = require_query_owner(query_id)
    if not query:
        return render_template("error.html", message="Request not found or access denied."), 404
    return render_template("seeker/responses.html", query=query)


class BoundedCache(OrderedDict):
    """Thread-safe LRU dictionary bounded by maxsize."""

    def __init__(self, maxsize=512, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.maxsize = maxsize
        self._lock = threading.RLock()

    def __getitem__(self, key):
        with self._lock:
            val = super().__getitem__(key)
            self.move_to_end(key)
            return val

    def __setitem__(self, key, value):
        with self._lock:
            if super().__contains__(key):
                self.move_to_end(key)
            super().__setitem__(key, value)
            if len(self) > self.maxsize:
                self.popitem(last=False)

    def get(self, key, default=None):
        with self._lock:
            try:
                val = super().__getitem__(key)
                self.move_to_end(key)
                return val
            except KeyError:
                return default

    def __contains__(self, key):
        with self._lock:
            return super().__contains__(key)

    def setdefault(self, key, default=None):
        with self._lock:
            if super().__contains__(key):
                self.move_to_end(key)
                return super().__getitem__(key)
            self[key] = default
            return default


_SIMILARITY_RESPONSE_CACHE = BoundedCache(maxsize=512)
_SIMILARITY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="nearfind_sim")
_SIMILARITY_IN_FLIGHT = set()
_SIMILARITY_IN_FLIGHT_LOCK = threading.Lock()


def _async_compute_similarity(ref_url, prod_url):
    cache_key = (ref_url, prod_url)
    try:
        sim_res = compare_images(
            ref_url,
            prod_url,
            ref_cache_key=ref_url,
            prod_cache_key=prod_url,
        )
        if sim_res and sim_res.get("available"):
            similarity = {
                "score": sim_res["score"],
                "level": sim_res["level"],
                "raw_similarity": sim_res["raw_similarity"],
                "disclaimer": sim_res.get("disclaimer", DISCLAIMER),
            }
        else:
            similarity = None
        _SIMILARITY_RESPONSE_CACHE[cache_key] = similarity
        return similarity
    except Exception as err:
        app.logger.warning("Background similarity calculation failed: %s", err)
        _SIMILARITY_RESPONSE_CACHE[cache_key] = None
        return None
    finally:
        with _SIMILARITY_IN_FLIGHT_LOCK:
            _SIMILARITY_IN_FLIGHT.discard(cache_key)


@app.get("/query/responses/<query_id>")
@seeker_required
def query_responses(query_id):
    query = require_query_owner(query_id)
    if not query:
        return json_response(False, error="Request not found or access denied", status=404)
    qlat, qlng, loc_source = get_query_effective_coordinates(query)
    rows = None
    try:
        res = (
            table("responses")
            .select("*, users!responses_provider_id_fkey(id,name,business_name,provider_type,phone,lat,lng)")
            .eq("query_id", query_id)
            .order("created_at", desc=False)
            .execute()
        )
        rows = res.data if res else []
    except Exception as err:
        app.logger.warning("responses select with join failed: %s; trying fallback", err)
        try:
            res = (
                table("responses")
                .select("*")
                .eq("query_id", query_id)
                .order("created_at", desc=False)
                .execute()
            )
            rows = res.data if res else []
            provider_ids = list({r["provider_id"] for r in rows if isinstance(r, dict) and r.get("provider_id")})
            if provider_ids:
                try:
                    u_res = table("users").select("id,name,business_name,provider_type,phone,lat,lng").in_("id", provider_ids).execute()
                    u_map = {u["id"]: u for u in (u_res.data or []) if isinstance(u, dict) and u.get("id")}
                    for r in rows:
                        if isinstance(r, dict) and not r.get("users") and r.get("provider_id") in u_map:
                            r["users"] = u_map[r["provider_id"]]
                except Exception as users_err:
                    app.logger.warning("Failed to lookup users for responses fallback: %s", users_err)
        except Exception as fallback_err:
            app.logger.error("Failed to query responses fallback for query %s: %s", query_id, fallback_err)
            return json_response(False, error="Responses could not be loaded at this time. Please try again.", status=502)

    if rows is None:
        app.logger.error("Response query produced no data object for query %s", query_id)
        return json_response(False, error="Responses could not be loaded at this time. Please try again.", status=502)

    if isinstance(rows, dict):
        rows = [rows]
    elif not isinstance(rows, list):
        rows = []

    responses = []
    seen_response_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        if not row_id or row_id in seen_response_ids:
            continue
        seen_response_ids.add(row_id)

        provider = row.get("users") or {}
        selected = row.get("status") == "selected"
        plat, plng, _ = get_provider_effective_location(provider)
        if not selected and provider.get("provider_type") == "individual" and plat is not None and plng is not None:
            plat, plng = round(plat, 3), round(plng, 3)
        if row.get("status") == "rejected":
            plat, plng = None, None
        distance = None
        if plat is not None and plng is not None and qlat is not None and qlng is not None:
            try:
                distance = round(haversine(float(qlat), float(qlng), float(plat), float(plng)), 2)
            except (ValueError, TypeError):
                distance = None
        similarity = None
        if query.get("image_url") and row.get("image_url"):
            cache_key = (query["image_url"], row["image_url"])
            if cache_key in _SIMILARITY_RESPONSE_CACHE:
                similarity = _SIMILARITY_RESPONSE_CACHE[cache_key]
            else:
                with _SIMILARITY_IN_FLIGHT_LOCK:
                    already_running = cache_key in _SIMILARITY_IN_FLIGHT
                    if not already_running:
                        _SIMILARITY_IN_FLIGHT.add(cache_key)
                        future = _SIMILARITY_EXECUTOR.submit(_async_compute_similarity, query["image_url"], row["image_url"])
                    else:
                        future = None

                # In test mode, allow fast mock futures to resolve immediately.
                # In production, do not block the HTTP request thread waiting for slow inference.
                if future is not None:
                    wait_time = 0.05 if app.testing else 0.0
                    try:
                        similarity = future.result(timeout=wait_time)
                    except concurrent.futures.TimeoutError:
                        similarity = None
                    except Exception as err:
                        app.logger.warning("Similarity calculation skipped: %s", err)
                        similarity = None

        responses.append(
            {
                "id": row.get("id"),
                "provider_id": row.get("provider_id"),
                "provider_name": provider.get("name"),
                "business_name": provider.get("business_name"),
                "provider_type": provider.get("provider_type"),
                "provider_lat": plat,
                "provider_lng": plng,
                "distance": distance,
                "distance_label": format_distance(distance) if distance is not None else "Distance unavailable",
                "distance_type": "straight_line",
                "message": row.get("message"),
                "price": row.get("price"),
                "image_url": row.get("image_url"),
                "status": row.get("status"),
                "phone": provider.get("phone") if selected or row.get("status") == "available" else None,
                "created_at": row.get("created_at"),
                "location_available": bool(plat is not None and plng is not None),
                "similarity": similarity,
            }
        )
    return json_response(data={"query": query, "responses": responses})


@app.post("/query/<query_id>/update-location")
@seeker_required
def update_query_location(query_id):
    query = require_query_owner(query_id)
    if not query:
        return json_response(False, error="Request not found or access denied", status=404)
    if query.get("status") != "open":
        return json_response(False, error="Only open requests can have location updated", status=409)
    data = parse_json_or_form()
    lat = coerce_float(data.get("lat"), "Latitude", -90, 90)
    lng = coerce_float(data.get("lng"), "Longitude", -180, 180)
    location_text = clean_text(data.get("location"), 250, required=False) or query.get("location")
    table("queries").update({"lat": lat, "lng": lng, "location": location_text}).eq("id", query_id).eq("seeker_id", session["user_id"]).execute()
    return json_response(True, data={"query_id": query_id, "lat": lat, "lng": lng, "location": location_text})


@app.get("/query/<query_id>/response/<response_id>/similarity")
@login_required
def response_image_similarity(query_id, response_id):
    if not valid_uuid(query_id) or not valid_uuid(response_id):
        return json_response(False, error="Invalid query or response id", status=400)

    user_id = session.get("user_id")

    query = table("queries").select("*").eq("id", query_id).maybe_single().execute().data
    if not query:
        return json_response(False, error="Query not found", status=404)

    resp_row = (
        table("responses")
        .select("*")
        .eq("id", response_id)
        .eq("query_id", query_id)
        .maybe_single()
        .execute()
        .data
    )
    if not resp_row:
        return json_response(False, error="Response not found", status=404)

    # Server-side authorization: only the seeker owner or the responding provider
    is_seeker = query.get("seeker_id") == user_id
    is_provider = resp_row.get("provider_id") == user_id
    if not (is_seeker or is_provider):
        return json_response(
            False,
            error="Access denied: you are not authorized to view this image comparison",
            status=403,
        )

    ref_image_url = query.get("image_url")
    prod_image_url = resp_row.get("image_url")

    cache_key = (ref_image_url, prod_image_url)
    if cache_key in _SIMILARITY_RESPONSE_CACHE and _SIMILARITY_RESPONSE_CACHE[cache_key]:
        cached = _SIMILARITY_RESPONSE_CACHE[cache_key]
        return json_response(True, data={
            "success": True,
            "available": True,
            "score": cached.get("score"),
            "level": cached.get("level"),
            "raw_similarity": cached.get("raw_similarity"),
            "status": "computed",
            "disclaimer": cached.get("disclaimer", DISCLAIMER),
        })

    result = compare_images(
        ref_image_url,
        prod_image_url,
        ref_cache_key=ref_image_url,
        prod_cache_key=prod_image_url,
    )
    if result.get("available"):
        _SIMILARITY_RESPONSE_CACHE[cache_key] = {
            "score": result.get("score"),
            "level": result.get("level"),
            "raw_similarity": result.get("raw_similarity"),
            "disclaimer": result.get("disclaimer", DISCLAIMER),
        }
    return json_response(True, data=result)


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
    qlat, qlng, _ = get_query_effective_coordinates(query)
    if qlat is not None and qlng is not None and valid_coordinates(location.get("lat"), location.get("lng")):
        distance = round(haversine(float(qlat), float(qlng), float(location["lat"]), float(location["lng"])), 2)
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

    # Server-side distance and radius verification before selection
    qlat, qlng, _ = get_query_effective_coordinates(query)
    provider_id = response.get("provider_id")
    if provider_id and qlat is not None and qlng is not None:
        try:
            prov_row = table("users").select("id,lat,lng,is_active,service_radius_km").eq("id", provider_id).maybe_single().execute().data
            if prov_row:
                plat, plng, _ = get_provider_effective_location(prov_row)
                if plat is not None and plng is not None:
                    dist = haversine(float(qlat), float(qlng), float(plat), float(plng))
                    query_radius = float(query.get("radius_km") or 5.0)
                    provider_radius = None
                    if prov_row.get("service_radius_km"):
                        try:
                            provider_radius = float(prov_row["service_radius_km"])
                        except (ValueError, TypeError):
                            pass
                    max_radius = min(query_radius, provider_radius) if provider_radius else query_radius
                    if dist > max_radius:
                        return json_response(
                            False,
                            error=f"Provider is outside the allowed search radius ({dist:.1f} km away, max {max_radius:.1f} km).",
                            status=403,
                        )
        except Exception as radius_err:
            app.logger.warning("Provider radius check failed: %s", radius_err)

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
    # Flask's debug reloader registers signal handlers that are not supported
    # when the app is launched from a non-main thread or some Windows runners.
    # Keep debug mode enabled for development while disabling the reloader to
    # avoid the "signal only works in main thread" crash.
    app.run(debug=Config.DEBUG, use_reloader=False)
