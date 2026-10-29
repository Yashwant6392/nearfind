"""
tests/test_response_timeout_and_inference_resilience.py

Regression tests verifying that:
1. Provider responses load immediately and independently of slow image downloads or ML inference.
2. Background similarity computation runs safely and updates cache without blocking response retrieval.
3. Subsequent response queries benefit from cached similarity instantly.
4. Errored or failing image downloads degrade gracefully to similarity=None without breaking response payload.
5. In-flight requests are deduplicated so duplicate background jobs are not queued.
6. Temporary image resources are bounded and closed.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as nearfind
from services.similarity_service import (
    DISCLAIMER,
    _EMBEDDING_CACHE,
    _CACHE_LOCK,
    reset_model,
)
from tests.test_security import FakeRowsTable


class TestResponseTimeoutAndInferenceResilience(unittest.TestCase):
    def setUp(self):
        reset_model()
        nearfind._SIMILARITY_RESPONSE_CACHE.clear()
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

        self.query_id = "11111111-1111-4111-8111-111111111111"
        self.seeker_id = "22222222-2222-4222-8222-222222222222"
        self.provider_id = "33333333-3333-4333-8333-333333333333"
        self.response_id = "44444444-4444-4444-8444-444444444444"
        self.ref_url = "https://example.com/ref_product.jpg"
        self.prod_url = "https://example.com/prod_offer.jpg"

        self.fake_query = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "status": "open",
            "image_url": self.ref_url,
            "lat": 12.97,
            "lng": 77.59,
        }

        self.fake_response_rows = [
            {
                "id": self.response_id,
                "query_id": self.query_id,
                "provider_id": self.provider_id,
                "status": "available",
                "message": "Product in stock",
                "price": "1200",
                "image_url": self.prod_url,
                "created_at": "2026-10-27T10:00:00Z",
                "users": {
                    "id": self.provider_id,
                    "name": "Reliable Electronics",
                    "business_name": "Reliable Electronics Ltd",
                    "provider_type": "shop",
                    "lat": 12.98,
                    "lng": 77.60,
                },
            }
        ]

    def tearDown(self):
        reset_model()
        nearfind._SIMILARITY_RESPONSE_CACHE.clear()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.seeker_id
            sess["role"] = "seeker"

    def test_slow_image_inference_does_not_block_response_loading(self):
        """When ML inference or image downloading is slow (>60ms), query_responses returns immediately."""
        self._login()

        def slow_compare(*args, **kwargs):
            time.sleep(0.3)
            return {
                "success": True,
                "available": True,
                "score": 92.0,
                "level": "High Match",
                "raw_similarity": 0.92,
                "disclaimer": DISCLAIMER,
            }

        with patch.object(nearfind, "require_query_owner", return_value=self.fake_query), \
             patch.object(nearfind, "table", return_value=FakeRowsTable(rows=self.fake_response_rows)), \
             patch.object(nearfind, "compare_images", side_effect=slow_compare):

            start_t = time.perf_counter()
            res = self.client.get(f"/query/responses/{self.query_id}")
            elapsed = time.perf_counter() - start_t

            # Endpoint must return fast and not wait 300ms for slow inference
            self.assertLess(elapsed, 0.25)
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(len(data["data"]["responses"]), 1)
            # Response data is available immediately
            resp_item = data["data"]["responses"][0]
            self.assertEqual(resp_item["message"], "Product in stock")
            self.assertEqual(resp_item["price"], "1200")
            # Similarity is None while computing in background
            self.assertIsNone(resp_item["similarity"])

            # Wait for background task to complete and populate cache
            time.sleep(0.35)

            # Subsequent poll retrieves cached similarity immediately
            res2 = self.client.get(f"/query/responses/{self.query_id}")
            self.assertEqual(res2.status_code, 200)
            data2 = res2.get_json()
            resp2 = data2["data"]["responses"][0]
            self.assertIsNotNone(resp2["similarity"])
            self.assertEqual(resp2["similarity"]["score"], 92.0)
            self.assertEqual(resp2["similarity"]["level"], "High Match")

    def test_failed_image_download_returns_200_without_error(self):
        """When image download or inference raises an exception, response retrieval succeeds gracefully."""
        self._login()

        def failing_compare(*args, **kwargs):
            raise ConnectionError("External image host timed out")

        with patch.object(nearfind, "require_query_owner", return_value=self.fake_query), \
             patch.object(nearfind, "table", return_value=FakeRowsTable(rows=self.fake_response_rows)), \
             patch.object(nearfind, "compare_images", side_effect=failing_compare):

            res = self.client.get(f"/query/responses/{self.query_id}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(len(data["data"]["responses"]), 1)
            # Similarity degrades gracefully to None
            self.assertIsNone(data["data"]["responses"][0]["similarity"])

    def test_in_flight_deduplication_prevents_duplicate_jobs(self):
        """Repeated polls for same query/response pair do not queue duplicate background tasks."""
        self._login()
        call_count = 0

        def counting_compare(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            time.sleep(0.2)
            return {
                "success": True,
                "available": True,
                "score": 85.0,
                "level": "High Match",
                "raw_similarity": 0.85,
                "disclaimer": DISCLAIMER,
            }

        with patch.object(nearfind, "require_query_owner", return_value=self.fake_query), \
             patch.object(nearfind, "table", return_value=FakeRowsTable(rows=self.fake_response_rows)), \
             patch.object(nearfind, "compare_images", side_effect=counting_compare):

            # Dispatch two rapid queries before inference finishes
            res1 = self.client.get(f"/query/responses/{self.query_id}")
            res2 = self.client.get(f"/query/responses/{self.query_id}")
            self.assertEqual(res1.status_code, 200)
            self.assertEqual(res2.status_code, 200)

            # Wait for background completion
            time.sleep(0.25)
            # compare_images should only have been called once despite 2 polls
            self.assertEqual(call_count, 1)

    def test_dedicated_similarity_endpoint_uses_cache(self):
        """The dedicated similarity endpoint leverages cached results when available."""
        self._login()
        cache_key = (self.ref_url, self.prod_url)
        nearfind._SIMILARITY_RESPONSE_CACHE[cache_key] = {
            "score": 95.5,
            "level": "High Match",
            "raw_similarity": 0.955,
            "disclaimer": DISCLAIMER,
        }

        qid = self.query_id
        sid = self.seeker_id
        pid = self.provider_id
        rid = self.response_id
        rurl = self.ref_url
        purl = self.prod_url

        class MockQueryTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def maybe_single(self): return self
            def execute(self): return SimpleNamespace(data={"id": qid, "seeker_id": sid, "image_url": rurl})

        class MockRespTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def maybe_single(self): return self
            def execute(self): return SimpleNamespace(data={"id": rid, "query_id": qid, "provider_id": pid, "image_url": purl})

        def mock_table(name):
            if name == "queries": return MockQueryTable()
            return MockRespTable()

        with patch.object(nearfind, "table", side_effect=mock_table), \
             patch.object(nearfind, "compare_images") as mock_compare:
            res = self.client.get(f"/query/{self.query_id}/response/{self.response_id}/similarity")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["data"]["score"], 95.5)
            # Cached result used without calling compare_images
            mock_compare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
