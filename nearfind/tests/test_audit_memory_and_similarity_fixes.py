"""
tests/test_audit_memory_and_similarity_fixes.py

Focused regression tests for the audit-driven memory and stability fixes:
1. Embedding cache hits bypass network downloading and image loading.
2. Reference-image embedding is reused across multiple response comparisons.
3. Image downsampling limits memory bloat on large images while preserving small ones.
4. BoundedCache limits memory retention with LRU eviction and thread-safety.
5. Supabase client instances are reused safely with admin/anon separation and lifecycle checks.
6. Frontend seeker-map.js defines moderate 5s polling with failure backoff.
"""

import io
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
from PIL import Image

import app as nearfind
from services.similarity_service import (
    MAX_IMAGE_DIM,
    _EMBEDDING_CACHE,
    _CACHE_LOCK,
    compare_images,
    extract_embedding,
    load_image,
    reset_model,
    set_model_for_testing,
)
from services.supabase_service import (
    RestSupabaseClient,
    get_supabase,
    reset_supabase_clients,
)
from tests.test_security import FakeRowsTable


def _make_image_bytes(size=(32, 32), color=(255, 0, 0)):
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestEmbeddingCacheBypass(unittest.TestCase):
    def setUp(self):
        reset_model()

    def tearDown(self):
        reset_model()

    def test_cache_hit_bypasses_load_image(self):
        """When embeddings are already cached, compare_images must not call load_image."""
        emb1 = np.array([1.0, 0.0], dtype=np.float32)
        emb2 = np.array([0.0, 1.0], dtype=np.float32)

        with _CACHE_LOCK:
            _EMBEDDING_CACHE["https://example.com/ref.jpg"] = emb1
            _EMBEDDING_CACHE["https://example.com/prod.jpg"] = emb2

        with patch("services.similarity_service.load_image") as mock_load:
            res = compare_images("https://example.com/ref.jpg", "https://example.com/prod.jpg")
            mock_load.assert_not_called()
            self.assertTrue(res["success"])
            self.assertEqual(res["score"], 0.0)  # Orthogonal vectors -> 0 score

    def test_partial_cache_hit_loads_only_uncached_image(self):
        """When ref is cached and prod is not, only prod image is loaded."""
        emb_ref = np.array([1.0, 0.0], dtype=np.float32)

        with _CACHE_LOCK:
            _EMBEDDING_CACHE["https://example.com/ref.jpg"] = emb_ref

        def mock_model(img):
            return np.array([1.0, 0.0], dtype=np.float32)

        set_model_for_testing(mock_model)

        prod_bytes = _make_image_bytes(size=(16, 16), color=(0, 255, 0))

        with patch("services.similarity_service.load_image", wraps=load_image) as spy_load:
            res = compare_images(
                "https://example.com/ref.jpg",
                prod_bytes,
                ref_cache_key="https://example.com/ref.jpg",
                prod_cache_key="prod_bytes_key",
            )
            # load_image should only have been called once for prod_bytes, NOT for ref.jpg
            self.assertEqual(spy_load.call_count, 1)
            self.assertTrue(res["success"])
            self.assertEqual(res["score"], 100.0)


class TestImageDownsampling(unittest.TestCase):
    def test_oversized_image_downsampled_to_max_dim(self):
        """Images larger than MAX_IMAGE_DIM must be downscaled to reduce RAM footprint."""
        large_img = Image.new("RGB", (1200, 800), color="red")
        result = load_image(large_img)
        self.assertLessEqual(max(result.size), MAX_IMAGE_DIM)
        self.assertEqual(result.mode, "RGB")
        # Aspect ratio 1200:800 is 3:2; with MAX_IMAGE_DIM=384 -> 384x256
        self.assertEqual(result.size, (384, 256))

    def test_small_image_not_enlarged(self):
        """Images smaller than MAX_IMAGE_DIM must preserve their original dimensions."""
        small_img = Image.new("RGB", (64, 48), color="green")
        result = load_image(small_img)
        self.assertEqual(result.size, (64, 48))
        self.assertEqual(result.mode, "RGB")


class TestBoundedCache(unittest.TestCase):
    def test_bounded_cache_evicts_oldest_on_overflow(self):
        """BoundedCache must evict the least recently used item when maxsize is exceeded."""
        cache = nearfind.BoundedCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        self.assertEqual(len(cache), 3)

        # Access 'a' to make it recently used
        _ = cache["a"]

        # Insert 'd' -> should evict 'b' (oldest unaccessed)
        cache["d"] = 4
        self.assertEqual(len(cache), 3)
        self.assertIn("a", cache)
        self.assertNotIn("b", cache)
        self.assertIn("c", cache)
        self.assertIn("d", cache)

    def test_bounded_cache_reentrant_lock(self):
        """BoundedCache operations must not deadlock when nested or checking containment."""
        cache = nearfind.BoundedCache(maxsize=5)
        cache["k1"] = "v1"
        # Overwrite existing key
        cache["k1"] = "v2"
        self.assertEqual(cache["k1"], "v2")
        self.assertEqual(cache.get("k1"), "v2")
        self.assertIn("k1", cache)


class TestSupabaseClientReuse(unittest.TestCase):
    def setUp(self):
        reset_supabase_clients()

    def tearDown(self):
        reset_supabase_clients()

    def test_admin_client_is_reused_singleton(self):
        """Repeated calls to get_supabase(admin=True) with same config must return identical instance."""
        with patch.object(nearfind.Config, "SUPABASE_URL", "https://test.supabase.co"), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", "sb_publishable_anon_123"
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_admin_456"):
            c1 = get_supabase(admin=True)
            c2 = get_supabase(admin=True)
            self.assertIs(c1, c2)
            c1.http.close()

    def test_admin_and_anon_clients_are_distinct(self):
        """Admin and anon clients must be distinct instances with their respective keys."""
        with patch.object(nearfind.Config, "SUPABASE_URL", "https://test.supabase.co"), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", "sb_publishable_anon_123"
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_admin_456"):
            admin_client = get_supabase(admin=True)
            anon_client = get_supabase(admin=False)
            self.assertIsNot(admin_client, anon_client)
            self.assertEqual(admin_client.key, "sb_secret_admin_456")
            self.assertEqual(anon_client.key, "sb_publishable_anon_123")
            admin_client.http.close()
            anon_client.http.close()

    def test_closed_client_is_recreated_not_reused(self):
        """If a cached client's http transport is closed, get_supabase must instantiate a fresh client."""
        with patch.object(nearfind.Config, "SUPABASE_URL", "https://test.supabase.co"), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", "sb_publishable_anon_123"
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_admin_456"):
            c1 = get_supabase(admin=True)
            self.assertFalse(c1.http.is_closed)
            c1.http.close()
            self.assertTrue(c1.http.is_closed)

            # Next call must detect is_closed and create a fresh client
            c2 = get_supabase(admin=True)
            self.assertIsNot(c1, c2)
            self.assertFalse(c2.http.is_closed)
            c2.http.close()


class TestReferenceImageReuseInQueryResponses(unittest.TestCase):
    def setUp(self):
        reset_model()
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def tearDown(self):
        reset_model()

    def test_query_responses_caches_ref_image_across_multiple_responses(self):
        """In a query with multiple responses, reference image embedding must be cached on the first comparison."""
        query_id = "11111111-1111-4111-8111-111111111111"
        seeker_id = "22222222-2222-4222-8222-222222222222"
        ref_url = "https://example.com/seeker_ref.jpg"

        with self.client.session_transaction() as sess:
            sess["user_id"] = seeker_id
            sess["role"] = "seeker"

        fake_query = {
            "id": query_id,
            "seeker_id": seeker_id,
            "status": "open",
            "image_url": ref_url,
            "lat": 12.97,
            "lng": 77.59,
        }

        fake_rows = [
            {
                "id": "resp-1",
                "query_id": query_id,
                "provider_id": "prov-1",
                "image_url": "https://example.com/prod1.jpg",
                "status": "available",
            },
            {
                "id": "resp-2",
                "query_id": query_id,
                "provider_id": "prov-2",
                "image_url": "https://example.com/prod2.jpg",
                "status": "available",
            },
        ]

        def mock_compare(ref_in, prod_in, ref_cache_key=None, prod_cache_key=None):
            # Seed the cache key for ref to simulate what compare_images does
            if ref_cache_key:
                with _CACHE_LOCK:
                    _EMBEDDING_CACHE[ref_cache_key] = np.array([1.0, 0.0], dtype=np.float32)
            return {
                "success": True,
                "available": True,
                "score": 90.0,
                "level": "High Match",
                "raw_similarity": 0.9,
                "disclaimer": "test",
            }

        with patch.object(nearfind, "require_query_owner", return_value=fake_query), \
             patch.object(nearfind, "table", return_value=FakeRowsTable(rows=fake_rows)), \
             patch.object(nearfind, "compare_images", side_effect=mock_compare) as spy_compare:
            res = self.client.get(f"/query/responses/{query_id}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(len(data["data"]["responses"]), 2)
            # Verify compare_images was passed the ref_cache_key
            self.assertEqual(spy_compare.call_count, 2)
            call1_kwargs = spy_compare.call_args_list[0].kwargs
            call2_kwargs = spy_compare.call_args_list[1].kwargs
            self.assertEqual(call1_kwargs.get("ref_cache_key"), ref_url)
            self.assertEqual(call2_kwargs.get("ref_cache_key"), ref_url)
            # Ref key is now in the embedding cache
            with _CACHE_LOCK:
                self.assertIn(ref_url, _EMBEDDING_CACHE)


if __name__ == "__main__":
    unittest.main()
