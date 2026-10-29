"""
tests/test_image_upload_limits.py

Comprehensive regression test suite for NearFind image upload limits:
- Maximum upload size of 2 MB (2 * 1024 * 1024 bytes)
- Maximum dimensions of 1600 x 1600 pixels (aspect ratio preserved upon resizing)
- Strict content validation via Pillow (rejects corrupt content, fake headers, unsupported formats)
- Allowed formats: JPEG, PNG, and WebP only
- Excessive dimension bounds (> 4096 x 4096 rejected to protect memory)
- End-to-end route upload validation for seeker query, provider response, and driver profile photos
- Standalone TechExpo demo upload limit (preserved at 5 MB) and format verification
"""

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageOps
from werkzeug.datastructures import FileStorage

from app import app
from config import MAX_IMAGE_BYTES, MAX_IMAGE_DIMENSION, MAX_RAW_IMAGE_DIM
from demo_similarity import DEMO_MAX_IMAGE_BYTES, demo_app
from services.storage_service import (
    process_and_optimize_image,
    upload_public_image,
    validate_image,
)


def _make_image_stream(size=(100, 100), fmt="JPEG", color=(255, 0, 0)):
    buf = io.BytesIO()
    img = Image.new("RGB", size, color=color)
    img.save(buf, format=fmt)
    buf.seek(0)
    return buf


class TestImageUploadLimitsAndValidation(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    # 1. Size limit tests (2 MB)
    def test_validate_image_rejects_empty_file(self):
        empty_storage = FileStorage(stream=io.BytesIO(), filename="test.jpg", content_type="image/jpeg")
        with self.assertRaisesRegex(ValueError, "empty"):
            validate_image(empty_storage)

    def test_validate_image_rejects_oversized_file_over_2mb(self):
        oversized_data = b"x" * (MAX_IMAGE_BYTES + 1)
        oversized_storage = FileStorage(
            stream=io.BytesIO(oversized_data),
            filename="large.jpg",
            content_type="image/jpeg",
        )
        with self.assertRaisesRegex(ValueError, "2 MB or smaller"):
            validate_image(oversized_storage)

    def test_validate_image_accepts_file_under_2mb(self):
        buf = _make_image_stream(size=(400, 300), fmt="JPEG")
        storage = FileStorage(stream=buf, filename="good.jpg", content_type="image/jpeg")
        ext = validate_image(storage)
        self.assertEqual(ext, ".jpg")

    # 2. Format validation tests
    def test_validate_image_rejects_unsupported_format_gif(self):
        buf = io.BytesIO()
        img = Image.new("RGB", (50, 50), color=(0, 255, 0))
        img.save(buf, format="GIF")
        buf.seek(0)
        storage = FileStorage(stream=buf, filename="anim.gif", content_type="image/gif")
        with self.assertRaisesRegex(ValueError, "Only JPG, PNG and WEBP images are allowed"):
            validate_image(storage)

    def test_validate_image_rejects_fake_header_with_invalid_content(self):
        # Starts with valid PNG signature but contains corrupted non-image body
        corrupt_png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x00CorruptedDataNotAValidPngChunk"
        storage = FileStorage(stream=io.BytesIO(corrupt_png), filename="bad.png", content_type="image/png")
        with self.assertRaisesRegex(ValueError, "Invalid image file"):
            validate_image(storage)

    def test_validate_image_rejects_mismatched_extension(self):
        buf = _make_image_stream(size=(100, 100), fmt="JPEG")
        storage = FileStorage(stream=buf, filename="fake_png.png", content_type="image/jpeg")
        with self.assertRaisesRegex(ValueError, "Image extension does not match its MIME type"):
            validate_image(storage)

    # 3. Dimension limit tests
    def test_validate_image_rejects_excessive_dimensions_over_4096(self):
        buf = _make_image_stream(size=(4200, 2000), fmt="JPEG")
        storage = FileStorage(stream=buf, filename="huge_dim.jpg", content_type="image/jpeg")
        with self.assertRaisesRegex(ValueError, "exceed maximum allowable limit"):
            validate_image(storage)

    def test_validate_image_strict_dimension_check(self):
        buf = _make_image_stream(size=(1800, 900), fmt="JPEG")
        storage = FileStorage(stream=buf, filename="over_1600.jpg", content_type="image/jpeg")
        with self.assertRaisesRegex(ValueError, "must not exceed 1600×1600 pixels"):
            validate_image(storage, max_dimension=1600)

    # 4. Dimension resizing and optimization preserving aspect ratio
    def test_process_and_optimize_resizes_oversized_preserving_aspect_ratio(self):
        # 2400 x 1200 has 2:1 aspect ratio. When scaled to fit 1600x1600, it should be 1600 x 800.
        buf = _make_image_stream(size=(2400, 1200), fmt="JPEG")
        storage = FileStorage(stream=buf, filename="wide.jpg", content_type="image/jpeg")
        processed = process_and_optimize_image(storage, max_dim=MAX_IMAGE_DIMENSION)

        self.assertEqual(processed["width"], 1600)
        self.assertEqual(processed["height"], 800)
        self.assertEqual(processed["ext"], ".jpg")
        self.assertEqual(processed["mimetype"], "image/jpeg")
        self.assertLessEqual(len(processed["data"]), MAX_IMAGE_BYTES)

    def test_process_and_optimize_preserves_smaller_dimensions(self):
        # 800 x 600 is under 1600x1600, should keep exact 800 x 600 dimensions
        buf = _make_image_stream(size=(800, 600), fmt="PNG")
        storage = FileStorage(stream=buf, filename="photo.png", content_type="image/png")
        processed = process_and_optimize_image(storage, max_dim=MAX_IMAGE_DIMENSION)

        self.assertEqual(processed["width"], 800)
        self.assertEqual(processed["height"], 600)
        self.assertEqual(processed["ext"], ".png")
        self.assertLessEqual(len(processed["data"]), MAX_IMAGE_BYTES)

    def test_process_and_optimize_jpeg_retry_compression_succeeds(self):
        """When initial JPEG quality=85 exceeds MAX_IMAGE_BYTES, retry at quality=70 brings it under limit."""
        buf = _make_image_stream(size=(400, 400), fmt="JPEG")
        storage = FileStorage(stream=buf, filename="test.jpg", content_type="image/jpeg")

        call_count = 0
        save_qualities = []

        def mock_save(self, fp, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            save_qualities.append(kwargs.get("quality"))
            if call_count == 1:
                fp.write(b"\xff\xd8\xff" + b"x" * (MAX_IMAGE_BYTES + 1024))
            else:
                fp.write(b"\xff\xd8\xff" + b"x" * 50000)

        with patch.object(Image.Image, "save", side_effect=mock_save, autospec=True):
            processed = process_and_optimize_image(storage, max_dim=MAX_IMAGE_DIMENSION)

        self.assertEqual(call_count, 2)
        self.assertEqual(save_qualities, [85, 70])
        self.assertLessEqual(len(processed["data"]), MAX_IMAGE_BYTES)
        self.assertEqual(processed["ext"], ".jpg")
        self.assertEqual(processed["mimetype"], "image/jpeg")

    def test_process_and_optimize_webp_retry_compression_succeeds(self):
        """When initial WebP quality=85 exceeds MAX_IMAGE_BYTES, retry at quality=70 method=6 brings it under limit."""
        buf = _make_image_stream(size=(400, 400), fmt="WEBP")
        storage = FileStorage(stream=buf, filename="test.webp", content_type="image/webp")

        call_count = 0
        save_params = []

        def mock_save(self, fp, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            save_params.append((kwargs.get("quality"), kwargs.get("method")))
            if call_count == 1:
                fp.write(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x" * (MAX_IMAGE_BYTES + 2048))
            else:
                fp.write(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x" * 30000)

        with patch.object(Image.Image, "save", side_effect=mock_save, autospec=True):
            processed = process_and_optimize_image(storage, max_dim=MAX_IMAGE_DIMENSION)

        self.assertEqual(call_count, 2)
        self.assertEqual(save_params, [(85, 4), (70, 6)])
        self.assertLessEqual(len(processed["data"]), MAX_IMAGE_BYTES)
        self.assertEqual(processed["ext"], ".webp")
        self.assertEqual(processed["mimetype"], "image/webp")

    def test_process_and_optimize_png_opaque_alpha_retry(self):
        """When opaque RGBA PNG exceeds MAX_IMAGE_BYTES, retry converts to RGB to reduce byte size."""
        img = Image.new("RGBA", (200, 200), color=(200, 100, 50, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        storage = FileStorage(stream=buf, filename="opaque.png", content_type="image/png")

        call_count = 0

        def mock_save(self, fp, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                fp.write(b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_BYTES + 1024))
            else:
                fp.write(b"\x89PNG\r\n\x1a\n" + b"x" * 40000)

        with patch.object(Image.Image, "save", side_effect=mock_save, autospec=True):
            processed = process_and_optimize_image(storage, max_dim=MAX_IMAGE_DIMENSION)

        self.assertEqual(call_count, 2)
        self.assertLessEqual(len(processed["data"]), MAX_IMAGE_BYTES)
        self.assertEqual(processed["ext"], ".png")
        self.assertEqual(processed["mimetype"], "image/png")

    def test_process_and_optimize_rejects_image_if_still_oversized_after_retry(self):
        """If image remains oversized even after retry compression, raises ValueError without accepting it."""
        buf = _make_image_stream(size=(400, 400), fmt="JPEG")
        storage = FileStorage(stream=buf, filename="oversized.jpg", content_type="image/jpeg")

        call_count = 0

        def mock_save(self, fp, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Both attempts exceed MAX_IMAGE_BYTES
            fp.write(b"\xff\xd8\xff" + b"x" * (MAX_IMAGE_BYTES + 5000))

        with patch.object(Image.Image, "save", side_effect=mock_save, autospec=True):
            with self.assertRaises(ValueError) as ctx:
                process_and_optimize_image(storage, max_dim=MAX_IMAGE_DIMENSION)

        self.assertEqual(call_count, 2)
        self.assertIn("2 MB or smaller", str(ctx.exception))

    def test_driver_document_limit_unaffected(self):
        """Driver document upload limits remain 5 MB and are not modified by image limits."""
        from services.storage_service import MAX_DOC_BYTES, ALLOWED_VERIFICATION_MIME
        self.assertEqual(MAX_DOC_BYTES, 5 * 1024 * 1024)
        self.assertIn("application/pdf", ALLOWED_VERIFICATION_MIME)

    # 5. Full upload helper integration test
    def test_upload_public_image_stores_optimized_bytes(self):
        class MockBucket:
            def __init__(self):
                self.uploaded_path = None
                self.uploaded_data = None
                self.options = None

            def upload(self, path, data, options):
                self.uploaded_path = path
                self.uploaded_data = data
                self.options = options

            def get_public_url(self, path):
                return f"https://mockstorage.supabase.co/test/{path}"

        class MockStorage:
            def __init__(self):
                self.bucket = MockBucket()

            def from_(self, bucket_name):
                return self.bucket

        mock_client = SimpleNamespace(storage=MockStorage())
        buf = _make_image_stream(size=(2000, 1500), fmt="JPEG")
        storage = FileStorage(stream=buf, filename="sample.jpeg", content_type="image/jpeg")

        with patch("services.storage_service.get_supabase", return_value=mock_client):
            res = upload_public_image("query-images", storage, "user-123")

        self.assertIn("image_url", res)
        self.assertEqual(res["width"], 1600)
        self.assertEqual(res["height"], 1200)
        # Verify uploaded data is valid JPEG
        with Image.open(io.BytesIO(mock_client.storage.bucket.uploaded_data)) as saved_img:
            self.assertEqual(saved_img.size, (1600, 1200))


class TestImageUploadRoutes(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def login_as(self, role="seeker", user_id="00000000-0000-4000-8000-000000000001"):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["role"] = role
            session["email"] = f"{role}@example.com"
            session["name"] = f"{role.title()} User"
            session["_csrf_token"] = "known-csrf-token"

    def test_seeker_query_image_upload_under_2mb_succeeds(self):
        self.login_as(role="seeker")
        buf = _make_image_stream(size=(500, 500), fmt="JPEG")

        with patch("app.upload_public_image", return_value={"image_url": "https://storage/img.jpg"}):
            res = self.client.post(
                "/upload/query-image",
                data={"image": (buf, "item.jpg", "image/jpeg")},
                content_type="multipart/form-data",
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertEqual(data["data"]["image_url"], "https://storage/img.jpg")

    def test_seeker_query_image_upload_oversized_returns_400(self):
        self.login_as(role="seeker")
        oversized = io.BytesIO(b"x" * (MAX_IMAGE_BYTES + 100))

        res = self.client.post(
            "/upload/query-image",
            data={"image": (oversized, "big.jpg", "image/jpeg")},
            content_type="multipart/form-data",
            headers={"X-CSRFToken": "known-csrf-token"},
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertIn("2 MB or smaller", str(data["error"]))

    def test_provider_response_image_upload_under_2mb_succeeds(self):
        self.login_as(role="provider", user_id="00000000-0000-4000-8000-000000000002")
        buf = _make_image_stream(size=(600, 400), fmt="JPEG")

        with patch("app.upload_public_image", return_value={"image_url": "https://storage/prod.jpg"}):
            res = self.client.post(
                "/upload/response-image",
                data={"image": (buf, "prod.jpg", "image/jpeg")},
                content_type="multipart/form-data",
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertEqual(data["data"]["image_url"], "https://storage/prod.jpg")

    def test_provider_driver_photo_upload_rejects_file_over_2mb(self):
        self.login_as(role="provider", user_id="00000000-0000-4000-8000-000000000002")
        oversized = io.BytesIO(b"x" * (MAX_IMAGE_BYTES + 100))

        res = self.client.post(
            "/provider/driver-photo/upload",
            data={"photo": (oversized, "huge.jpg", "image/jpeg")},
            content_type="multipart/form-data",
            headers={"X-CSRFToken": "known-csrf-token"},
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertIn("2 MB or smaller", str(data["error"]))




class TestDemoImageUploadLimits(unittest.TestCase):
    def setUp(self):
        demo_app.config.update(TESTING=True)
        self.client = demo_app.test_client()

    def test_demo_accepts_valid_image_under_5mb(self):
        buf1 = _make_image_stream(size=(120, 120), fmt="JPEG", color=(50, 100, 150))
        buf2 = _make_image_stream(size=(120, 120), fmt="JPEG", color=(50, 100, 150))

        res = self.client.post(
            "/demo/compare",
            data={
                "ref_image": (buf1, "ref.jpg"),
                "prod_image": (buf2, "prod.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertGreater(data["score"], 90.0)

    def test_demo_rejects_file_over_5mb(self):
        oversized = io.BytesIO(b"x" * (DEMO_MAX_IMAGE_BYTES + 100))
        normal = _make_image_stream(size=(64, 64), fmt="JPEG")

        res = self.client.post(
            "/demo/compare",
            data={
                "ref_image": (oversized, "large.jpg"),
                "prod_image": (normal, "normal.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertIn("5 MB", data["error"])

    def test_demo_rejects_unsupported_format(self):
        gif_buf = io.BytesIO()
        img = Image.new("RGB", (64, 64), color=(20, 40, 60))
        img.save(gif_buf, format="GIF")
        gif_buf.seek(0)
        normal = _make_image_stream(size=(64, 64), fmt="JPEG")

        res = self.client.post(
            "/demo/compare",
            data={
                "ref_image": (gif_buf, "ref.gif"),
                "prod_image": (normal, "prod.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertIn("Only JPG, PNG and WEBP", data["error"])


if __name__ == "__main__":
    unittest.main()
