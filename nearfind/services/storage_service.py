import io
import uuid
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename
from config import ALLOWED_IMAGE_MIME, MAX_IMAGE_BYTES, MAX_IMAGE_DIMENSION, MAX_RAW_IMAGE_DIM
from services.supabase_service import get_supabase


def validate_image(file_storage, max_dimension=None):
    if not file_storage or not file_storage.filename:
        raise ValueError("Image file is required")
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size <= 0:
        raise ValueError("Image file is empty")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image must be {MAX_IMAGE_BYTES // (1024 * 1024)} MB or smaller")
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

    # Content validation and dimension checks via Pillow
    try:
        file_storage.stream.seek(0)
        with Image.open(file_storage.stream) as img:
            fmt = (img.format or "").upper()
            fmt_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
            if fmt not in fmt_map or fmt_map[fmt] != mimetype:
                raise ValueError("Only JPG, PNG and WEBP images are allowed")
            if img.width > MAX_RAW_IMAGE_DIM or img.height > MAX_RAW_IMAGE_DIM:
                raise ValueError(f"Image dimensions exceed maximum allowable limit ({MAX_RAW_IMAGE_DIM}×{MAX_RAW_IMAGE_DIM})")
            if max_dimension and (img.width > max_dimension or img.height > max_dimension):
                raise ValueError(f"Image dimensions must not exceed {max_dimension}×{max_dimension} pixels")
            img.verify()
    except ValueError:
        raise
    except Exception:
        raise ValueError("Invalid image file")
    finally:
        file_storage.stream.seek(0)

    return ALLOWED_IMAGE_MIME[mimetype]


def process_and_optimize_image(file_storage, max_dim=MAX_IMAGE_DIMENSION):
    """
    Validates, handles EXIF orientation, resizes oversized images to max_dim x max_dim
    preserving aspect ratio, and optimizes encoded image bytes.
    Ensures final payload does not exceed MAX_IMAGE_BYTES.
    """
    ext = validate_image(file_storage)
    file_storage.stream.seek(0)
    with Image.open(file_storage.stream) as raw_img:
        # Preserve orientation from camera/phone EXIF metadata
        try:
            img = ImageOps.exif_transpose(raw_img)
        except Exception:
            img = raw_img

        fmt = (raw_img.format or "").upper()
        if fmt == "JPEG":
            mimetype = "image/jpeg"
            save_format = "JPEG"
        elif fmt == "WEBP":
            mimetype = "image/webp"
            save_format = "WEBP"
        else:
            mimetype = "image/png"
            save_format = "PNG"

        # Limit dimensions to max_dim while preserving aspect ratio
        width, height = img.size
        if width > max_dim or height > max_dim:
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
            img.thumbnail((max_dim, max_dim), resample)

        out_buf = io.BytesIO()
        if save_format == "JPEG":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(out_buf, format="JPEG", quality=85, optimize=True)
        elif save_format == "WEBP":
            img.save(out_buf, format="WEBP", quality=85, method=4)
        else:  # PNG
            img.save(out_buf, format="PNG", optimize=True)

        optimized_bytes = out_buf.getvalue()

        # If payload exceeds MAX_IMAGE_BYTES after initial optimization, perform a reasonable additional compression attempt where supported
        if len(optimized_bytes) > MAX_IMAGE_BYTES:
            if save_format == "JPEG":
                retry_buf = io.BytesIO()
                img.save(retry_buf, format="JPEG", quality=70, optimize=True)
                retry_bytes = retry_buf.getvalue()
                if len(retry_bytes) <= MAX_IMAGE_BYTES or len(retry_bytes) < len(optimized_bytes):
                    optimized_bytes = retry_bytes
            elif save_format == "WEBP":
                retry_buf = io.BytesIO()
                img.save(retry_buf, format="WEBP", quality=70, method=6)
                retry_bytes = retry_buf.getvalue()
                if len(retry_bytes) <= MAX_IMAGE_BYTES or len(retry_bytes) < len(optimized_bytes):
                    optimized_bytes = retry_bytes
            elif save_format == "PNG":
                # For PNG, if image has an unused (fully opaque) alpha channel, convert to RGB to reduce byte size
                if img.mode == "RGBA":
                    extrema = img.getextrema()
                    if len(extrema) == 4 and extrema[3] == (255, 255):
                        retry_buf = io.BytesIO()
                        img.convert("RGB").save(retry_buf, format="PNG", optimize=True, compress_level=9)
                        retry_bytes = retry_buf.getvalue()
                        if len(retry_bytes) <= MAX_IMAGE_BYTES or len(retry_bytes) < len(optimized_bytes):
                            optimized_bytes = retry_bytes

        if len(optimized_bytes) > MAX_IMAGE_BYTES:
            raise ValueError(f"Image must be {MAX_IMAGE_BYTES // (1024 * 1024)} MB or smaller")

        return {
            "data": optimized_bytes,
            "ext": ext,
            "mimetype": mimetype,
            "width": img.width,
            "height": img.height,
        }


def upload_public_image(bucket, file_storage, prefix):
    processed = process_and_optimize_image(file_storage)
    ext = processed["ext"]
    original = secure_filename(file_storage.filename or "image")
    storage_path = f"{prefix}/{uuid.uuid4().hex}{ext}"
    data = processed["data"]
    client = get_supabase(admin=True)
    client.storage.from_(bucket).upload(
        storage_path,
        data,
        {"content-type": processed["mimetype"], "x-upsert": "false"},
    )
    public_url = client.storage.from_(bucket).get_public_url(storage_path)
    return {
        "path": storage_path,
        "image_url": public_url,
        "filename": original,
        "width": processed["width"],
        "height": processed["height"],
    }



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

