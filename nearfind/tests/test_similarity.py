"""
tests/test_similarity.py

Comprehensive automated tests for NearFind AI Product Image Similarity Matching.
These tests verify:
- Cosine similarity computation and normalization
- Lazy model loading, test model injection, and singleton state
- In-memory LRU embedding cache behavior
- Safe image loading and SSRF protection
- Error handling for missing, corrupt, oversized, and unsupported formats
- Safe degradation on model unavailability
- Server-side authorization on similarity endpoints
- No GPU, no live model weight downloads, and no external paid API calls.
"""

import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

import app as nearfind
from services.similarity_service import (
    DISCLAIMER,
    compare_images,
    compute_cosine_similarity,
    extract_embedding,
    is_safe_image_url,
    load_image,
    normalize_similarity,
    reset_model,
    set_model_for_testing,
)


def _make_test_image(color=(255, 0, 0), size=(32, 32)):
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class FakeTable:
    def __init__(self, data=None):
        self._data = data if data is not None else []
        self._is_single = False

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def maybe_single(self):
        self._is_single = True
        return self

    def single(self):
        self._is_single = True
        return self

    def execute(self):
        if self._is_single:
            val = self._data[0] if self._data else None
            return SimpleNamespace(data=val)
        return SimpleNamespace(data=self._data)


class TestSimilarityMathAndLogic(unittest.TestCase):
    def setUp(self):
        reset_model()

    def tearDown(self):
        reset_model()

    def test_compute_cosine_similarity_identical_vectors(self):
        v1 = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        sim = compute_cosine_similarity(v1, v1)
        self.assertAlmostEqual(sim, 1.0, places=5)

    def test_compute_cosine_similarity_orthogonal_vectors(self):
        v1 = np.array([1.0, 0.0], dtype=np.float32)
        v2 = np.array([0.0, 1.0], dtype=np.float32)
        sim = compute_cosine_similarity(v1, v2)
        self.assertAlmostEqual(sim, 0.0, places=5)

    def test_compute_cosine_similarity_opposite_vectors(self):
        v1 = np.array([1.0, 0.0], dtype=np.float32)
        v2 = np.array([-1.0, 0.0], dtype=np.float32)
        sim = compute_cosine_similarity(v1, v2)
        self.assertAlmostEqual(sim, -1.0, places=5)

    def test_compute_cosine_similarity_zero_vector(self):
        v1 = np.array([0.0, 0.0], dtype=np.float32)
        v2 = np.array([1.0, 1.0], dtype=np.float32)
        sim = compute_cosine_similarity(v1, v2)
        self.assertEqual(sim, 0.0)

    def test_normalize_similarity_scores_and_levels(self):
        high = normalize_similarity(0.92)
        self.assertEqual(high["score"], 92.0)
        self.assertEqual(high["level"], "High Match")
        self.assertEqual(high["raw_similarity"], 0.92)

        border_high = normalize_similarity(0.80)
        self.assertEqual(border_high["score"], 80.0)
        self.assertEqual(border_high["level"], "High Match")

        moderate = normalize_similarity(0.65)
        self.assertEqual(moderate["score"], 65.0)
        self.assertEqual(moderate["level"], "Moderate Match")

        border_mod = normalize_similarity(0.50)
        self.assertEqual(border_mod["score"], 50.0)
        self.assertEqual(border_mod["level"], "Moderate Match")

        low = normalize_similarity(0.35)
        self.assertEqual(low["score"], 35.0)
        self.assertEqual(low["level"], "Low Match")

        negative = normalize_similarity(-0.4)
        self.assertEqual(negative["score"], 0.0)
        self.assertEqual(negative["level"], "Low Match")

        overflow = normalize_similarity(1.5)
        self.assertEqual(overflow["score"], 100.0)
        self.assertEqual(overflow["level"], "High Match")


class TestSafeImageLoading(unittest.TestCase):
    def test_is_safe_image_url(self):
        self.assertTrue(is_safe_image_url("https://supabase.co/storage/v1/product.jpg"))
        self.assertTrue(is_safe_image_url("http://cdn.example.com/item.png"))

        self.assertFalse(is_safe_image_url("http://localhost/secret.jpg"))
        self.assertFalse(is_safe_image_url("http://127.0.0.1:8000/api"))
        self.assertFalse(is_safe_image_url("http://10.0.0.5/item.jpg"))
        self.assertFalse(is_safe_image_url("http://172.16.1.1/item.jpg"))
        self.assertFalse(is_safe_image_url("http://192.168.1.100/test.jpg"))
        self.assertFalse(is_safe_image_url("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(is_safe_image_url("ftp://example.com/test.jpg"))
        self.assertFalse(is_safe_image_url(""))

    def test_load_image_from_pil(self):
        raw = Image.new("RGBA", (10, 10), color="blue")
        img = load_image(raw)
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(img.size, (10, 10))

    def test_load_image_from_bytes(self):
        data = _make_test_image()
        img = load_image(data)
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(img.size, (32, 32))

    def test_load_image_from_bytes_io(self):
        data = _make_test_image()
        bio = io.BytesIO(data)
        img = load_image(bio)
        self.assertEqual(img.size, (32, 32))

    def test_load_image_empty_bytes_raises(self):
        with self.assertRaises(ValueError):
            load_image(b"")

    def test_load_image_oversized_bytes_raises(self):
        oversized = b"x" * (6 * 1024 * 1024)
        with self.assertRaises(ValueError):
            load_image(oversized)

    def test_load_image_unsafe_url_raises(self):
        with self.assertRaises(ValueError) as ctx:
            load_image("http://127.0.0.1:5000/internal.jpg")
        self.assertIn("unsafe or restricted host", str(ctx.exception))

    def test_load_image_corrupt_data_raises(self):
        with self.assertRaises(Exception):
            load_image(b"not an image at all")


class TestEmbeddingExtractionAndCaching(unittest.TestCase):
    def setUp(self):
        reset_model()

    def tearDown(self):
        reset_model()

    def test_extract_embedding_with_mock_model(self):
        call_count = 0

        def mock_model(image):
            nonlocal call_count
            call_count += 1
            return np.array([3.0, 4.0], dtype=np.float32)

        set_model_for_testing(mock_model)
        img = Image.new("RGB", (20, 20), color="white")

        emb = extract_embedding(img, cache_key="key1")
        # Embedding should be normalized (norm([3, 4]) = 5 -> [0.6, 0.8])
        self.assertEqual(call_count, 1)
        self.assertAlmostEqual(emb[0], 0.6, places=5)
        self.assertAlmostEqual(emb[1], 0.8, places=5)

        # Second call with same cache_key must hit cache without invoking model
        emb2 = extract_embedding(img, cache_key="key1")
        self.assertEqual(call_count, 1)
        np.testing.assert_array_equal(emb, emb2)


class TestCompareImages(unittest.TestCase):
    def setUp(self):
        reset_model()

    def tearDown(self):
        reset_model()

    def test_compare_images_missing_inputs(self):
        res1 = compare_images(None, None)
        self.assertFalse(res1["success"])
        self.assertFalse(res1["available"])
        self.assertEqual(res1["status"], "missing_image")
        self.assertEqual(res1["disclaimer"], DISCLAIMER)

        res2 = compare_images(_make_test_image(), None)
        self.assertFalse(res2["success"])
        self.assertEqual(res2["status"], "missing_image")

    def test_compare_images_corrupt_reference(self):
        res = compare_images(b"corrupt reference bytes", _make_test_image())
        self.assertFalse(res["success"])
        self.assertFalse(res["available"])
        self.assertEqual(res["status"], "invalid_reference_image")
        self.assertEqual(res["disclaimer"], DISCLAIMER)

    def test_compare_images_corrupt_product(self):
        res = compare_images(_make_test_image(), b"corrupt product bytes")
        self.assertFalse(res["success"])
        self.assertFalse(res["available"])
        self.assertEqual(res["status"], "invalid_product_image")
        self.assertEqual(res["disclaimer"], DISCLAIMER)

    def test_compare_images_model_unavailable(self):
        def failing_model(img):
            raise RuntimeError("Torch model download failed")

        set_model_for_testing(failing_model)
        res = compare_images(_make_test_image(), _make_test_image())
        self.assertFalse(res["success"])
        self.assertFalse(res["available"])
        self.assertEqual(res["status"], "model_unavailable")
        self.assertIn("currently unavailable", res["error"])
        self.assertEqual(res["disclaimer"], DISCLAIMER)

    def test_compare_images_successful_comparison(self):
        def mock_model(img):
            # Deterministic embedding based on image color
            r, g, b = img.getpixel((0, 0))
            return np.array([float(r), float(g), float(b)], dtype=np.float32)

        set_model_for_testing(mock_model)
        img1 = _make_test_image(color=(255, 0, 0))
        img2 = _make_test_image(color=(255, 0, 0))

        res = compare_images(img1, img2)
        self.assertTrue(res["success"])
        self.assertTrue(res["available"])
        self.assertEqual(res["status"], "computed")
        self.assertEqual(res["score"], 100.0)
        self.assertEqual(res["level"], "High Match")
        self.assertEqual(res["disclaimer"], DISCLAIMER)


class TestSimilarityEndpointsAndAuth(unittest.TestCase):
    def setUp(self):
        nearfind.app.config["TESTING"] = True
        self.client = nearfind.app.test_client()
        reset_model()

    def tearDown(self):
        reset_model()

    def test_response_image_similarity_invalid_uuids(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "11111111-1111-4111-8111-111111111111"

        res = self.client.get("/query/invalid-uuid/response/also-invalid/similarity")
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])

    def test_response_image_similarity_unauthorized_user(self):
        query_id = "11111111-1111-4111-8111-111111111111"
        response_id = "22222222-2222-4222-8222-222222222222"
        seeker_id = "33333333-3333-4333-8333-333333333333"
        provider_id = "44444444-4444-4444-8444-444444444444"
        stranger_id = "55555555-5555-4555-8555-555555555555"

        with self.client.session_transaction() as sess:
            sess["user_id"] = stranger_id

        fake_query = {"id": query_id, "seeker_id": seeker_id, "image_url": "https://cdn.example.com/ref.jpg"}
        fake_response = {"id": response_id, "query_id": query_id, "provider_id": provider_id, "image_url": "https://cdn.example.com/prod.jpg"}

        def fake_table(name):
            if name == "queries":
                return FakeTable([fake_query])
            if name == "responses":
                return FakeTable([fake_response])
            return FakeTable([])

        with patch.object(nearfind, "table", side_effect=fake_table):
            res = self.client.get(f"/query/{query_id}/response/{response_id}/similarity")
            self.assertEqual(res.status_code, 403)
            data = res.get_json()
            self.assertFalse(data["success"])
            err_msg = data["error"].get("message") if isinstance(data["error"], dict) else str(data["error"])
            self.assertIn("Access denied", err_msg)

    def test_response_image_similarity_authorized_seeker(self):
        query_id = "11111111-1111-4111-8111-111111111111"
        response_id = "22222222-2222-4222-8222-222222222222"
        seeker_id = "33333333-3333-4333-8333-333333333333"
        provider_id = "44444444-4444-4444-8444-444444444444"

        with self.client.session_transaction() as sess:
            sess["user_id"] = seeker_id

        fake_query = {"id": query_id, "seeker_id": seeker_id, "image_url": "https://cdn.example.com/ref.jpg"}
        fake_response = {"id": response_id, "query_id": query_id, "provider_id": provider_id, "image_url": "https://cdn.example.com/prod.jpg"}

        def fake_table(name):
            if name == "queries":
                return FakeTable([fake_query])
            if name == "responses":
                return FakeTable([fake_response])
            return FakeTable([])

        mock_sim_result = {
            "success": True,
            "available": True,
            "score": 88.5,
            "level": "High Match",
            "raw_similarity": 0.885,
            "status": "computed",
            "disclaimer": DISCLAIMER,
        }

        with patch.object(nearfind, "table", side_effect=fake_table), patch.object(
            nearfind, "compare_images", return_value=mock_sim_result
        ):
            res = self.client.get(f"/query/{query_id}/response/{response_id}/similarity")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["data"]["score"], 88.5)
            self.assertEqual(data["data"]["level"], "High Match")
            self.assertEqual(data["data"]["disclaimer"], DISCLAIMER)

    def test_query_responses_includes_similarity_payload(self):
        query_id = "11111111-1111-4111-8111-111111111111"
        seeker_id = "33333333-3333-4333-8333-333333333333"
        provider_id = "44444444-4444-4444-8444-444444444444"
        response_id = "22222222-2222-4222-8222-222222222222"

        with self.client.session_transaction() as sess:
            sess["user_id"] = seeker_id
            sess["role"] = "seeker"

        fake_query = {
            "id": query_id,
            "seeker_id": seeker_id,
            "item_name": "Drill",
            "image_url": "https://cdn.example.com/drill.jpg",
            "lat": 12.9,
            "lng": 77.6,
        }
        fake_response_row = {
            "id": response_id,
            "query_id": query_id,
            "provider_id": provider_id,
            "status": "available",
            "message": "Have exact drill",
            "price": "500",
            "image_url": "https://cdn.example.com/drill_offer.jpg",
            "created_at": "2026-10-16T10:00:00Z",
            "users": {
                "id": provider_id,
                "name": "Bob",
                "business_name": "Bob Hardware",
                "provider_type": "Hardware",
                "lat": 12.91,
                "lng": 77.61,
            },
        }

        mock_sim_result = {
            "success": True,
            "available": True,
            "score": 94.2,
            "level": "High Match",
            "raw_similarity": 0.942,
            "status": "computed",
            "disclaimer": DISCLAIMER,
        }

        with patch.object(
            nearfind, "require_query_owner", return_value=fake_query
        ), patch.object(
            nearfind, "table", return_value=FakeTable([fake_response_row])
        ), patch.object(
            nearfind, "compare_images", return_value=mock_sim_result
        ):
            res = self.client.get(f"/query/responses/{query_id}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            responses = data["data"]["responses"]
            self.assertEqual(len(responses), 1)
            sim = responses[0]["similarity"]
            self.assertIsNotNone(sim)
            self.assertEqual(sim["score"], 94.2)
            self.assertEqual(sim["level"], "High Match")
            self.assertEqual(sim["disclaimer"], DISCLAIMER)


if __name__ == "__main__":
    unittest.main()
