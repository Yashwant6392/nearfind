import os
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

_env = os.getenv("FLASK_ENV", "development")
_secret = os.getenv("SECRET_KEY")
_required_production_settings = {
    "SECRET_KEY": _secret,
    "SUPABASE_URL": os.getenv("SUPABASE_URL"),
    "SUPABASE_ANON_KEY": os.getenv("SUPABASE_ANON_KEY"),
    "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
}
if _env == "production":
    missing = [name for name, value in _required_production_settings.items() if not value]
    if missing:
        raise RuntimeError(f"Missing production configuration: {', '.join(missing)}")
    if len(_secret) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters in production")


class Config:
    SECRET_KEY = _secret or "dev-change-me"
    DEBUG = _env == "development"
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env == "production"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_REFRESH_EACH_REQUEST = True
    MAX_CONTENT_LENGTH = 6 * 1024 * 1024


ALLOWED_ROLES = {"seeker", "provider"}
ALLOWED_PROVIDER_TYPES = {"shop", "individual"}
ALLOWED_URGENCY = {"normal", "urgent", "very_urgent"}
QUERY_STATUSES = {"open", "matched", "resolved", "closed", "expired"}
RESPONSE_STATUSES = {"available", "selected", "rejected", "sold"}
ALLOWED_IMAGE_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MB (2 x 1024 x 1024 bytes)
MAX_IMAGE_DIMENSION = 1600  # 1600 x 1600 pixels max dimension
MAX_RAW_IMAGE_DIM = 4096  # Upper bound on raw dimension to protect memory

# Visual Similarity Configuration (MobileNetV3 feature embedding comparison)
# Note: Raw cosine similarities from generic un-fine-tuned CNN feature extractors
# are not calibrated probabilities. These thresholds define High and Medium match levels
# and are configurable via environment variables for calibration with empirical labeled data.
SIMILARITY_THRESHOLD_HIGH = float(os.getenv("SIMILARITY_THRESHOLD_HIGH", "0.80"))
SIMILARITY_THRESHOLD_MEDIUM = float(os.getenv("SIMILARITY_THRESHOLD_MEDIUM", "0.50"))

