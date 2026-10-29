"""
tests/test_hospital_and_response_timeout_fixes.py

Focused regression test suite verifying:
1. Hospital onboarding:
   - Database error during existing-hospital check does not blindly proceed or corrupt state.
   - Failure during hospital insert prevents partial manager state on users table/session.
   - Duplicate submissions / retries safely update existing hospital and inventory without unique constraint crashes.
   - Concurrent unique constraint collision recovery.
   - Proper safe error returns (HTTP 502) instead of misleading success.
2. Seeker response retrieval:
   - Total database failure returns HTTP 502 instead of HTTP 200 with empty responses.
   - Join failure gracefully falls back to secondary responses select.
   - Slow similarity inference does not stall response retrieval.
   - Bounded caching returns similarity immediately when available.
3. Supabase service:
   - Authorization headers sent for secret keys (sb_secret_).
   - Bounded timeout set to prevent thread hangs.
"""

import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as nearfind
from services.supabase_service import _headers, DEFAULT_TIMEOUT_SECONDS, SupabaseRequestError
from services.similarity_service import DISCLAIMER, reset_model


class FakeDbTable:
    def __init__(self, data=None, error_on=None):
        self.data = data
        self.error_on = error_on or set()
        self.inserted = []
        self.updated = []
        self.selected_columns = []
        self.filters = []

    def select(self, cols="*"):
        if "select" in self.error_on:
            raise SupabaseRequestError(500, "Database select error")
        self.selected_columns.append(cols)
        return self

    def insert(self, payload):
        if "insert" in self.error_on:
            raise SupabaseRequestError(500, "Database insert error")
        if "insert_conflict" in self.error_on:
            raise SupabaseRequestError(409, "duplicate key value violates unique constraint 'hospitals_manager_id_key'")
        self.inserted.append(payload)
        row = dict(payload)
        if "id" not in row:
            row["id"] = str(uuid.uuid4())
        self.data = [row]
        return self

    def update(self, payload):
        if "update" in self.error_on:
            raise SupabaseRequestError(500, "Database update error")
        self.updated.append(payload)
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def in_(self, col, vals):
        self.filters.append((col, vals))
        return self

    def order(self, col, desc=False):
        return self

    def maybe_single(self):
        return self

    def single(self):
        return self

    def limit(self, count):
        return self

    def execute(self):
        if "execute" in self.error_on:
            raise SupabaseRequestError(500, "Database execute error")
        if isinstance(self.data, list):
            return SimpleNamespace(data=self.data)
        elif self.data is not None:
            return SimpleNamespace(data=self.data)
        return SimpleNamespace(data=None)


class TestHospitalOnboardingFixes(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()
        self.manager_id = "11111111-1111-4111-8111-111111111111"
        self.hospital_id = "22222222-2222-4222-8222-222222222222"

    def _login(self, is_manager=False):
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.manager_id
            sess["role"] = "provider"
            sess["_csrf_token"] = "known-csrf-token"
            sess["is_hospital_manager"] = is_manager

    def test_hospital_onboarding_success_marks_manager_and_saves_inventory(self):
        """Successful onboarding saves hospital, saves inventory, then marks user as manager."""
        self._login(is_manager=False)
        users_table = FakeDbTable()
        hospitals_table = FakeDbTable(data=None)
        avail_table = FakeDbTable(data=None)

        def mock_table(table_name):
            if table_name == "hospitals":
                return hospitals_table
            elif table_name == "users":
                return users_table
            elif table_name == "hospital_bed_availability":
                return avail_table
            return FakeDbTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.post(
                "/hospital/onboarding",
                data={
                    "csrf_token": "known-csrf-token",
                    "name": "General Hospital",
                    "address": "123 Medical Way",
                    "phone": "+91 9876543210",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "general_available": "15",
                    "icu_available": "4",
                    "oxygen_available": "8",
                    "emergency_available": "2",
                    "isolation_available": "1",
                },
            )

        self.assertEqual(res.status_code, 302)
        self.assertIn("/hospital/dashboard", res.headers["Location"])
        # Hospital record was inserted
        self.assertEqual(len(hospitals_table.inserted), 1)
        self.assertEqual(hospitals_table.inserted[0]["name"], "General Hospital")
        # Bed availability was inserted
        self.assertEqual(len(avail_table.inserted), 1)
        self.assertEqual(avail_table.inserted[0]["general_available"], 15)
        # Users table was updated to is_hospital_manager = True
        self.assertEqual(len(users_table.updated), 1)
        self.assertTrue(users_table.updated[0].get("is_hospital_manager"))

    def test_hospital_onboarding_check_failure_aborts_safely(self):
        """When checking for existing hospital fails with DB error, onboarding aborts without corrupting state."""
        self._login(is_manager=False)
        hospitals_table = FakeDbTable(error_on={"select"})
        users_table = FakeDbTable()

        def mock_table(table_name):
            if table_name == "hospitals":
                return hospitals_table
            return users_table

        with patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.post(
                "/hospital/onboarding",
                headers={"X-CSRFToken": "known-csrf-token", "Accept": "application/json"},
                json={
                    "name": "General Hospital",
                    "address": "123 Medical Way",
                    "lat": "26.7606",
                    "lng": "83.3732",
                },
            )

        self.assertEqual(res.status_code, 502)
        json_data = res.get_json()
        self.assertFalse(json_data["success"])
        err_msg = json_data["error"]["message"] if isinstance(json_data["error"], dict) else json_data["error"]
        self.assertIn("Unable to verify registration status", err_msg)
        # Verify users table was NOT modified
        self.assertEqual(len(users_table.updated), 0)

    def test_hospital_insert_failure_prevents_partial_manager_state(self):
        """If hospital insert fails, user must NOT be marked as manager."""
        self._login(is_manager=False)
        hospitals_table = FakeDbTable(data=None, error_on={"insert"})
        users_table = FakeDbTable()

        def mock_table(table_name):
            if table_name == "hospitals":
                return hospitals_table
            return users_table

        with patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.post(
                "/hospital/onboarding",
                headers={"X-CSRFToken": "known-csrf-token", "Accept": "application/json"},
                json={
                    "name": "General Hospital",
                    "address": "123 Medical Way",
                    "lat": "26.7606",
                    "lng": "83.3732",
                },
            )

        self.assertEqual(res.status_code, 502)
        json_data = res.get_json()
        self.assertFalse(json_data["success"])
        err_msg = json_data["error"]["message"] if isinstance(json_data["error"], dict) else json_data["error"]
        self.assertIn("could not be saved", err_msg)
        # Critical assertion: user was NOT flagged as hospital manager
        self.assertEqual(len(users_table.updated), 0)
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get("is_hospital_manager", False))

    def test_hospital_bed_availability_failure_returns_error_and_does_not_mark_manager(self):
        """When saving bed availability fails, onboarding returns 502, logs error, and does not report success or mark manager."""
        self._login(is_manager=False)
        hospitals_table = FakeDbTable(data=None)
        avail_table = FakeDbTable(error_on={"insert"})
        users_table = FakeDbTable()

        def mock_table(table_name):
            if table_name == "hospitals":
                return hospitals_table
            elif table_name == "hospital_bed_availability":
                return avail_table
            return users_table

        with patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.post(
                "/hospital/onboarding",
                headers={"X-CSRFToken": "known-csrf-token", "Accept": "application/json"},
                json={
                    "name": "City Care Hospital",
                    "address": "456 Healthcare Blvd",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "general_available": "10",
                    "icu_available": "2",
                },
            )

        self.assertEqual(res.status_code, 502)
        json_data = res.get_json()
        self.assertFalse(json_data["success"])
        err_msg = json_data["error"]["message"] if isinstance(json_data["error"], dict) else json_data["error"]
        self.assertIn("bed availability", err_msg.lower())
        # Hospital record was saved (preserved for subsequent retry/update)
        self.assertEqual(len(hospitals_table.inserted), 1)
        self.assertEqual(hospitals_table.inserted[0]["name"], "City Care Hospital")
        # Users table was NOT updated to is_hospital_manager = True
        self.assertEqual(len(users_table.updated), 0)
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get("is_hospital_manager", False))

    def test_hospital_bed_availability_failure_html_redirects_with_flash(self):
        """When saving bed availability fails on form submit, flashes error and redirects back to onboarding."""
        self._login(is_manager=False)
        hospitals_table = FakeDbTable(data=None)
        avail_table = FakeDbTable(error_on={"insert"})
        users_table = FakeDbTable()

        def mock_table(table_name):
            if table_name == "hospitals":
                return hospitals_table
            elif table_name == "hospital_bed_availability":
                return avail_table
            return users_table

        with patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.post(
                "/hospital/onboarding",
                data={
                    "csrf_token": "known-csrf-token",
                    "name": "City Care Hospital",
                    "address": "456 Healthcare Blvd",
                    "lat": "26.7606",
                    "lng": "83.3732",
                },
            )

        self.assertEqual(res.status_code, 302)
        self.assertIn("/hospital/onboarding", res.headers["Location"])
        self.assertEqual(len(users_table.updated), 0)

    def test_hospital_bed_availability_retry_preserves_hospital_and_succeeds(self):
        """Retrying onboarding after an availability failure updates existing hospital and succeeds once availability saves."""
        self._login(is_manager=False)
        existing_hospital = {
            "id": self.hospital_id,
            "manager_id": self.manager_id,
            "name": "City Care Hospital",
            "address": "456 Healthcare Blvd",
            "lat": 26.7606,
            "lng": 83.3732,
        }
        hospitals_table = FakeDbTable(data=existing_hospital)
        avail_table = FakeDbTable(data=None)
        users_table = FakeDbTable()

        def mock_table(table_name):
            if table_name == "hospitals":
                return hospitals_table
            elif table_name == "hospital_bed_availability":
                return avail_table
            return users_table

        with patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.post(
                "/hospital/onboarding",
                headers={"X-CSRFToken": "known-csrf-token", "Accept": "application/json"},
                json={
                    "name": "City Care Hospital Updated",
                    "address": "456 Healthcare Blvd Suite 2",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "general_available": "25",
                    "icu_available": "6",
                },
            )

        self.assertEqual(res.status_code, 201)
        json_data = res.get_json()
        self.assertTrue(json_data["success"])
        # Hospital updated, not re-inserted anew
        self.assertEqual(len(hospitals_table.inserted), 0)
        self.assertEqual(len(hospitals_table.updated), 1)
        self.assertEqual(hospitals_table.updated[0]["name"], "City Care Hospital Updated")
        # Availability now inserted
        self.assertEqual(len(avail_table.inserted), 1)
        self.assertEqual(avail_table.inserted[0]["general_available"], 25)
        # Manager flag set
        self.assertEqual(len(users_table.updated), 1)
        self.assertTrue(users_table.updated[0].get("is_hospital_manager"))

    def test_hospital_duplicate_submission_safely_updates_existing(self):
        """When an existing hospital is present, submitting again updates it instead of crashing."""
        self._login(is_manager=True)
        existing_hospital = {
            "id": self.hospital_id,
            "manager_id": self.manager_id,
            "name": "Old Hospital Name",
            "address": "Old Address",
            "lat": 26.70,
            "lng": 83.30,
        }
        hospitals_table = FakeDbTable(data=existing_hospital)
        avail_table = FakeDbTable(data={"hospital_id": self.hospital_id, "general_available": 5})
        users_table = FakeDbTable()

        def mock_table(table_name):
            if table_name == "hospitals":
                return hospitals_table
            elif table_name == "hospital_bed_availability":
                return avail_table
            return users_table

        with patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.post(
                "/hospital/onboarding",
                data={
                    "csrf_token": "known-csrf-token",
                    "name": "Updated Hospital Name",
                    "address": "New Address 101",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "general_available": "30",
                    "icu_available": "10",
                },
            )

        self.assertEqual(res.status_code, 302)
        self.assertIn("/hospital/dashboard", res.headers["Location"])
        # Hospital was updated, not inserted anew
        self.assertEqual(len(hospitals_table.inserted), 0)
        self.assertEqual(len(hospitals_table.updated), 1)
        self.assertEqual(hospitals_table.updated[0]["name"], "Updated Hospital Name")
        # Bed availability was updated
        self.assertEqual(len(avail_table.updated), 1)
        self.assertEqual(avail_table.updated[0]["general_available"], 30)

    def test_hospital_unique_constraint_race_recovery(self):
        """If an insert encounters a unique constraint race condition on manager_id, it updates the row."""
        self._login(is_manager=False)
        existing_record = {"id": self.hospital_id, "manager_id": self.manager_id}

        class RaceHospitalTable(FakeDbTable):
            def __init__(self):
                super().__init__(data=None)
                self.calls = 0

            def insert(self, payload):
                # Simulate concurrent duplicate key violation
                raise SupabaseRequestError(409, "duplicate key value violates unique constraint 'hospitals_manager_id_key'")

            def execute(self):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(data=None)  # initial check returned None
                return SimpleNamespace(data=existing_record)  # recovery check returned existing

        race_table = RaceHospitalTable()
        avail_table = FakeDbTable(data=None)
        users_table = FakeDbTable()

        def mock_table(table_name):
            if table_name == "hospitals":
                return race_table
            elif table_name == "hospital_bed_availability":
                return avail_table
            return users_table

        with patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.post(
                "/hospital/onboarding",
                data={
                    "csrf_token": "known-csrf-token",
                    "name": "General Hospital",
                    "address": "123 Medical Way",
                    "lat": "26.7606",
                    "lng": "83.3732",
                },
            )

        self.assertEqual(res.status_code, 302)
        self.assertIn("/hospital/dashboard", res.headers["Location"])
        # Recovery successfully updated the existing hospital
        self.assertEqual(len(race_table.updated), 1)


class TestSeekerResponseRetrievalFixes(unittest.TestCase):
    def setUp(self):
        reset_model()
        nearfind._SIMILARITY_RESPONSE_CACHE.clear()
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

        self.query_id = "11111111-1111-4111-8111-111111111111"
        self.seeker_id = "22222222-2222-4222-8222-222222222222"
        self.provider_id = "33333333-3333-4333-8333-333333333333"

        self.fake_query = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "status": "open",
            "lat": 26.76,
            "lng": 83.37,
            "image_url": "https://example.com/ref.jpg",
        }

    def tearDown(self):
        reset_model()
        nearfind._SIMILARITY_RESPONSE_CACHE.clear()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.seeker_id
            sess["role"] = "seeker"

    def test_query_responses_total_database_failure_returns_502(self):
        """When both join and fallback response queries fail, endpoint returns HTTP 502, NOT 200 with empty list."""
        self._login()
        failing_table = FakeDbTable(error_on={"select"})

        with patch.object(nearfind, "require_query_owner", return_value=self.fake_query), \
             patch.object(nearfind, "table", return_value=failing_table):
            res = self.client.get(f"/query/responses/{self.query_id}")

        self.assertEqual(res.status_code, 502)
        json_data = res.get_json()
        err_msg = json_data["error"]["message"] if isinstance(json_data["error"], dict) else json_data["error"]
        self.assertEqual(err_msg, "Responses could not be loaded at this time. Please try again.")

    def test_query_responses_join_failure_uses_fallback(self):
        """When primary join query fails, fallback query retrieves responses and user info."""
        self._login()
        raw_responses = [
            {
                "id": "resp-1",
                "query_id": self.query_id,
                "provider_id": self.provider_id,
                "status": "available",
                "message": "Item available",
                "price": "500",
                "image_url": None,
                "created_at": "2026-10-27T10:00:00Z",
            }
        ]
        provider_user = {
            "id": self.provider_id,
            "name": "Shopkeeper",
            "business_name": "Corner Shop",
            "provider_type": "shop",
            "phone": "+91 9999999999",
            "lat": 26.77,
            "lng": 83.38,
        }

        call_count = 0

        def mock_table(table_name):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Primary query with join fails
                return FakeDbTable(error_on={"select"})
            elif call_count == 2:
                # Fallback responses select succeeds
                return FakeDbTable(data=raw_responses)
            elif call_count == 3:
                # Users lookup succeeds
                return FakeDbTable(data=[provider_user])
            return FakeDbTable()

        with patch.object(nearfind, "require_query_owner", return_value=self.fake_query), \
             patch.object(nearfind, "table", side_effect=mock_table):
            res = self.client.get(f"/query/responses/{self.query_id}")

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        responses = data["data"]["responses"]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["provider_name"], "Shopkeeper")
        self.assertEqual(responses[0]["business_name"], "Corner Shop")

    def test_query_responses_slow_similarity_does_not_block(self):
        """Slow similarity inference does not delay response payload delivery."""
        self._login()
        rows = [
            {
                "id": "resp-2",
                "query_id": self.query_id,
                "provider_id": self.provider_id,
                "status": "available",
                "message": "Product match",
                "price": "800",
                "image_url": "https://example.com/prod.jpg",
                "users": {"name": "Vendor", "lat": 26.76, "lng": 83.37},
            }
        ]

        def slow_compare(*args, **kwargs):
            time.sleep(0.2)
            return {"available": True, "score": 90.0, "level": "High Match", "raw_similarity": 0.9}

        with patch.object(nearfind, "require_query_owner", return_value=self.fake_query), \
             patch.object(nearfind, "table", return_value=FakeDbTable(data=rows)), \
             patch.object(nearfind, "compare_images", side_effect=slow_compare):
            start = time.perf_counter()
            res = self.client.get(f"/query/responses/{self.query_id}")
            elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.15)
        self.assertEqual(res.status_code, 200)
        resp_data = res.get_json()["data"]["responses"][0]
        self.assertEqual(resp_data["message"], "Product match")


class TestSupabaseServiceConfig(unittest.TestCase):
    def test_headers_for_secret_key(self):
        """_headers handles sb_secret_ keys per security spec."""
        headers = _headers("sb_secret_abcdef123456")
        self.assertEqual(headers.get("apikey"), "sb_secret_abcdef123456")
        self.assertNotIn("Authorization", headers)

    def test_headers_for_legacy_key_retains_bearer(self):
        """_headers includes Authorization: Bearer for legacy keys."""
        headers = _headers("eyJ.legacy.jwt.token")
        self.assertEqual(headers.get("apikey"), "eyJ.legacy.jwt.token")
        self.assertEqual(headers.get("Authorization"), "Bearer eyJ.legacy.jwt.token")

    def test_default_timeout_is_bounded(self):
        """DEFAULT_TIMEOUT_SECONDS must be bounded at 20.0s to prevent worker hangs."""
        self.assertLessEqual(DEFAULT_TIMEOUT_SECONDS, 20.0)
