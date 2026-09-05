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
