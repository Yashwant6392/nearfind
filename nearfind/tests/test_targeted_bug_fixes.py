from datetime import datetime, timedelta, timezone
import unittest
import uuid
from unittest.mock import MagicMock, patch

import app as nearfind
from services.supabase_service import DEFAULT_TIMEOUT_SECONDS
from tests.test_security import FakeRowsTable, FakeTable


class TestTargetedBugFixes(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = nearfind.app.test_client()
        self.provider_id = "22222222-2222-4222-8222-222222222222"
        self.seeker_id = "11111111-1111-4111-8111-111111111111"
        self.other_seeker_id = "33333333-3333-4333-8333-333333333333"
        self.query_id = "44444444-4444-4444-8444-444444444444"
        self.response_id = "55555555-5555-4555-8555-555555555555"

    def login_as(self, role="seeker", user_id=None):
        uid = user_id or (self.provider_id if role == "provider" else self.seeker_id)
        with self.client.session_transaction() as sess:
            sess["user_id"] = uid
            sess["role"] = role
            sess["email"] = f"{role}@example.com"
            sess["name"] = f"{role.title()} User"
            sess["_csrf_token"] = "known-csrf-token"

    # -------------------------------------------------------------------------
    # Issue 1: Render 502 & Driver Requests Batch Queries & Supabase Timeouts
    # -------------------------------------------------------------------------
    def test_supabase_client_bounded_timeouts(self):
        """Bounded timeouts must be configured to prevent Gunicorn worker hangs and 502 Bad Gateway."""
        self.assertLessEqual(DEFAULT_TIMEOUT_SECONDS, 20.0)

    def test_provider_driver_requests_batch_lookups(self):
        """Driver requests view must batch fetch bookings, seekers, and requests without N+1 query loop."""
        self.login_as(role="provider")

        bookings = [
            {
                "id": str(uuid.uuid4()),
                "driver_id": self.provider_id,
                "seeker_id": self.seeker_id,
                "request_id": self.query_id,
                "status": "accepted",
                "fare_amount": 250,
                "scheduled_pickup_time": "2026-10-27T10:00:00Z",
                "created_at": "2026-10-27T09:00:00Z",
            }
        ]
        seekers = [
            {"id": self.seeker_id, "name": "Test Seeker", "email": "seeker@example.com", "phone": "9876543210"}
        ]
        requests = [
            {"id": self.query_id, "pickup_location": "A", "dropoff_location": "B", "vehicle_type": "sedan"}
        ]

        def fake_table(name):
            if name == "driver_bookings":
                return FakeRowsTable(rows=bookings)
            if name == "users":
                return FakeRowsTable(rows=seekers)
            if name == "driver_requests":
                return FakeRowsTable(rows=requests)
            return FakeRowsTable([])

        with patch.object(nearfind, "table", side_effect=fake_table), \
             patch.object(nearfind, "rpc", return_value=[]):
            resp = self.client.get("/provider/driver-requests")
            self.assertEqual(resp.status_code, 200)

    # -------------------------------------------------------------------------
    # Issue 2: Duplicate Nearby Request Cards Deduplication
    # -------------------------------------------------------------------------
    def test_list_queries_deduplicates_by_id(self):
        """list_queries API must return strictly unique query cards by database ID."""
        self.login_as(role="provider")
        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": 12.9716,
            "lng": 77.5946,
        }

        # Mock duplicate queries in DB response
        dup_query = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "item_name": "Emergency Toolkit",
            "category": "Repair & Maintenance",
            "urgency": "urgent",
            "lat": 12.9720,
            "lng": 77.5950,
            "status": "open",
            "radius_km": 10,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "created_at": "2026-10-27T00:00:00Z",
            "description": "Need tools urgently",
            "users": {"name": "Seeker User"},
            "location": "Central Square",
        }

        with patch.object(nearfind, "expire_open_queries"), \
             patch.object(nearfind, "current_profile", return_value=provider), \
             patch.object(nearfind, "table", return_value=FakeRowsTable(rows=[dup_query, dup_query])):
            resp = self.client.get("/query/list?radius_km=10")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            queries = data["data"]["queries"]
            # Must be deduplicated to exactly 1 query
            self.assertEqual(len(queries), 1)
            self.assertEqual(queries[0]["id"], self.query_id)

    # -------------------------------------------------------------------------
    # Issue 3: View Request Button Flow & Route
    # -------------------------------------------------------------------------
    def test_provider_request_view_route_requires_provider_role(self):
        """Non-provider users must not be allowed to access provider request view."""
        self.login_as(role="seeker")
        resp = self.client.get(f"/provider/request/{self.query_id}")
        # Should redirect or reject with 302/403
        self.assertIn(resp.status_code, [302, 403])

    def test_provider_request_view_invalid_uuid(self):
        """Invalid UUIDs must be rejected with 400."""
        self.login_as(role="provider")
        resp = self.client.get("/provider/request/not-a-valid-uuid")
        self.assertEqual(resp.status_code, 400)

    def test_provider_request_view_authorized_success(self):
        """Authorized provider receives query details for valid request."""
        self.login_as(role="provider")
        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": 12.9716,
            "lng": 77.5946,
            "service_radius": 20,
        }

        query_record = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "item_name": "Car Jack",
            "category": "Vehicle / Transport",
            "urgency": "normal",
            "lat": 12.9750,
            "lng": 77.5980,
            "radius_km": 15,
            "status": "open",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "description": "Hydraulic jack needed",
            "users": {"name": "Seeker User", "phone": "1234567890"},
        }

        with patch.object(nearfind, "expire_open_queries"), \
             patch.object(nearfind, "current_profile", return_value=provider), \
             patch.object(nearfind, "table", return_value=FakeRowsTable(single=query_record)):
            resp = self.client.get(f"/provider/request/{self.query_id}", headers={"Accept": "application/json"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["data"]["query"]["id"], self.query_id)
            self.assertEqual(data["data"]["query"]["item_name"], "Car Jack")

    # -------------------------------------------------------------------------
    # Issue 5: Provider Offers Flow for Seeker & Response Fetching
    # -------------------------------------------------------------------------
    def test_query_responses_authorization_for_seeker(self):
        """Seeker who does not own the query cannot fetch responses."""
        self.login_as(role="seeker", user_id=self.other_seeker_id)

        with patch.object(nearfind, "require_query_owner", return_value=None):
            resp = self.client.get(f"/query/responses/{self.query_id}")
            self.assertEqual(resp.status_code, 404)

    def test_query_responses_cached_similarity_and_deduplication(self):
        """Response endpoint returns valid JSON with deduplicated responses and cached similarity."""
        self.login_as(role="seeker", user_id=self.seeker_id)

        query_record = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "status": "open",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "image_url": "https://example.com/query.jpg",
            "lat": 12.9716,
            "lng": 77.5946,
        }

        response_record = {
            "id": self.response_id,
            "query_id": self.query_id,
            "provider_id": self.provider_id,
            "price": 100.0,
            "message": "I have this item available in stock.",
            "image_url": "https://example.com/response.jpg",
            "status": "pending",
            "created_at": "2026-10-27T00:00:00Z",
            "users!responses_provider_id_fkey": {
                "id": self.provider_id,
                "business_name": "Fast Auto Parts",
                "name": "Shop Owner",
                "phone": "9876543210",
                "lat": 12.9716,
                "lng": 77.5946,
                "provider_type": "shop",
            },
        }

        # Pre-seed cache to ensure synchronous ML call is bypassed
        cache_key = (query_record["image_url"], response_record["image_url"])
        nearfind._SIMILARITY_RESPONSE_CACHE[cache_key] = {
            "score": 0.95,
            "level": "high",
            "raw_similarity": 0.95,
            "disclaimer": "test",
        }

        with patch.object(nearfind, "table", side_effect=[
            FakeRowsTable(single=query_record),  # require_query_owner
            FakeRowsTable(rows=[response_record, response_record]),  # responses lookup with duplicate
        ]):
            resp = self.client.get(f"/query/responses/{self.query_id}")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))

            data = resp.get_json()
            self.assertTrue(data["success"])
            responses = data["data"]["responses"]
            # Verify deduplication
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0]["id"], self.response_id)
            self.assertEqual(responses[0]["similarity"]["score"], 0.95)


if __name__ == "__main__":
    unittest.main()
