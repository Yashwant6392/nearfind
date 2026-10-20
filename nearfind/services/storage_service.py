import uuid
from werkzeug.utils import secure_filename
from config import ALLOWED_IMAGE_MIME, MAX_IMAGE_BYTES
from services.supabase_service import get_supabase


def validate_image(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("Image file is required")
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size <= 0:
        raise ValueError("Image file is empty")
    if size > MAX_IMAGE_BYTES:
        raise ValueError("Image must be 5 MB or smaller")
    mimetype = file_storage.mimetype
    if mimetype not in ALLOWED_IMAGE_MIME:
        raise ValueError("Only JPG, PNG and WEBP images are allowed")
    filename = secure_filename(file_storage.filename)
    expected_extension = ALLOWED_IMAGE_MIME[mimetype]
    allowed_extensions = {expected_extension}
    if mimetype == "image/jpeg":
        allowed_extensions.add(".jpeg")
    if not filename.lower().endswith(tuple(allowed_extensions)):
        raise ValueError("Image extension does not match its MIME type")
    header = file_storage.stream.read(512)
    file_storage.stream.seek(0)
    signatures = {
        "image/jpeg": header.startswith(b"\xff\xd8\xff"),
        "image/png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
    }
    if not signatures.get(mimetype):
        raise ValueError("Invalid image file")
    return ALLOWED_IMAGE_MIME[mimetype]


def upload_public_image(bucket, file_storage, prefix):
    ext = validate_image(file_storage)
    original = secure_filename(file_storage.filename or "image")
    storage_path = f"{prefix}/{uuid.uuid4().hex}{ext}"
    data = file_storage.read()
    client = get_supabase(admin=True)
    client.storage.from_(bucket).upload(
        storage_path,
        data,
        {"content-type": file_storage.mimetype, "x-upsert": "false"},
    )
    public_url = client.storage.from_(bucket).get_public_url(storage_path)
    return {"path": storage_path, "image_url": public_url, "filename": original}


ALLOWED_VERIFICATION_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/pdf": ".pdf",
}
MAX_DOC_BYTES = 5 * 1024 * 1024  # 5 MB


def validate_verification_document(file_storage):
    """
    Validates identity and licence verification documents.
    Strictly checks size, MIME type, extension, and file signatures.
    Only JPG, PNG, and PDF formats under 5 MB are permitted.
    """
    if not file_storage or not file_storage.filename:
        raise ValueError("Verification document file is required")
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size <= 0:
        raise ValueError("Verification document is empty")
    if size > MAX_DOC_BYTES:
        raise ValueError("Verification document must be 5 MB or smaller")
    mimetype = file_storage.mimetype
    if mimetype not in ALLOWED_VERIFICATION_MIME:
        raise ValueError("Only JPG, PNG and PDF documents are allowed for verification")
    filename = secure_filename(file_storage.filename)
    expected_extension = ALLOWED_VERIFICATION_MIME[mimetype]
    allowed_extensions = {expected_extension}
    if mimetype == "image/jpeg":
        allowed_extensions.add(".jpeg")
    if not filename.lower().endswith(tuple(allowed_extensions)):
        raise ValueError("Document extension does not match its MIME type")
    header = file_storage.stream.read(512)
    file_storage.stream.seek(0)
    signatures = {
        "image/jpeg": header.startswith(b"\xff\xd8\xff"),
        "image/png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        "application/pdf": header.startswith(b"%PDF-"),
    }
    if not signatures.get(mimetype):
        raise ValueError("Invalid verification document format or corrupted header")
    return ALLOWED_VERIFICATION_MIME[mimetype]


def upload_private_verification_document(bucket, file_storage, user_id, doc_type):
    """
    Uploads verification documents exclusively to private storage.
    Never exposes public URLs. Access requires reviewer authorization.
    """
    ext = validate_verification_document(file_storage)
    safe_doc_type = secure_filename(doc_type or "doc")
    storage_path = f"driver-verification/{user_id}/{safe_doc_type}_{uuid.uuid4().hex}{ext}"
    data = file_storage.read()
    client = get_supabase(admin=True)
    client.storage.from_(bucket).upload(
        storage_path,
        data,
        {"content-type": file_storage.mimetype, "x-upsert": "false"},
    )
    return {"path": storage_path, "filename": secure_filename(file_storage.filename or "doc")}


def create_signed_verification_url(bucket, storage_path, expires_in=300):
    """
    Generates a time-bounded signed URL for authorized reviewers only.
    Default expiry is 300 seconds (5 minutes).
    """
    client = get_supabase(admin=True)
    res = client.storage.from_(bucket).create_signed_url(storage_path, expires_in)
    if isinstance(res, dict):
        return res.get("signedURL") or res.get("signedUrl")
    return getattr(res, "signed_url", None) or getattr(res, "signedURL", None)

