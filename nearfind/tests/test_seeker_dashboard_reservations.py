import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as nearfind


class MockTable:
    def __init__(self, data=None):
        if data is None:
            self.data = []
        elif isinstance(data, list):
            self.data = data
        else:
            self.data = [data]
        self.filters = []
        self.selected_columns = None
        self.order_by = None
        self.limit_val = None
        self._maybe_single = False
        self._single = False
        self.updated = []
        self.inserted = []

    def select(self, columns):
        self.selected_columns = columns
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def lt(self, column, value):
        return self

    def lte(self, column, value):
        return self

    def gt(self, column, value):
        return self

    def gte(self, column, value):
        return self

    def maybe_single(self):
        self._maybe_single = True
        return self

    def single(self):
        self._single = True
        return self

    def order(self, column, desc=False):
        self.order_by = (column, desc)
        return self

    def limit(self, value):
        self.limit_val = value
        return self

    def update(self, payload):
        self.updated.append(payload)
        return self

    def insert(self, payload):
        self.inserted.append(payload)
        return self

    def execute(self):
        filtered = list(self.data)
        for col, val in self.filters:
            filtered = [r for r in filtered if r.get(col) == val]
        if self.limit_val is not None:
            filtered = filtered[:self.limit_val]
        if self._maybe_single or self._single:
            result_data = filtered[0] if filtered else None
        else:
            result_data = filtered
        return SimpleNamespace(data=result_data)


class SeekerDashboardReservationsTests(unittest.TestCase):
    SEEKER_ID = "11111111-1111-4111-8111-111111111111"
    OTHER_SEEKER_ID = "99999999-9999-4999-8999-999999999999"
    PROVIDER_ID = "22222222-2222-4222-8222-222222222222"
    HOSPITAL_ID = "33333333-3333-4333-8333-333333333333"
    HOLD_ID = "44444444-4444-4444-8444-444444444444"
    BOOKING_ID = "55555555-5555-4555-8555-555555555555"
    REQUEST_ID = "66666666-6666-4666-8666-666666666666"

    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def login_as(self, role="seeker", user_id=None):
        uid = user_id or (self.SEEKER_ID if role == "seeker" else self.PROVIDER_ID)
        with self.client.session_transaction() as session:
            session.update(
                user_id=uid,
                role=role,
                _csrf_token="known-csrf-token",
            )

    # -----------------------------------------------------------------
    # Authorization & Guard Tests
    # -----------------------------------------------------------------

    def test_active_reservations_endpoint_requires_authentication(self):
        response = self.client.get("/seeker/active-reservations")
        self.assertEqual(response.status_code, 401)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "authentication_required")

    def test_active_reservations_endpoint_requires_seeker_role(self):
        self.login_as(role="provider")
        response = self.client.get("/seeker/active-reservations")
        self.assertEqual(response.status_code, 403)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "forbidden")

    def test_seeker_dashboard_page_requires_seeker_role(self):
        # Anonymous redirects to login
        res_anon = self.client.get("/seeker/dashboard")
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn("/login", res_anon.headers["Location"])

        # Provider redirects away
        self.login_as(role="provider")
        res_prov = self.client.get("/seeker/dashboard")
        self.assertEqual(res_prov.status_code, 302)
        self.assertIn("/", res_prov.headers["Location"])

    # -----------------------------------------------------------------
    # Seeker Dashboard HTML Rendering Tests
    # -----------------------------------------------------------------

    def test_seeker_dashboard_renders_active_bed_hold(self):
        self.login_as(role="seeker")
        future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        hold_row = {
            "id": self.HOLD_ID,
            "hospital_id": self.HOSPITAL_ID,
            "seeker_id": self.SEEKER_ID,
            "bed_type": "icu",
            "status": "held",
            "expires_at": future_time,
            "hospitals": {
                "id": self.HOSPITAL_ID,
                "name": "Apex Heart & Trauma Center",
                "address": "Civil Lines, Gorakhpur",
                "phone": "+91 9988776655",
                "lat": 26.76,
                "lng": 83.37,
            },
        }

        def mock_table(name):
            if name == "hospital_bed_holds":
                return MockTable([hold_row])
            elif name == "queries":
                return MockTable([])
            elif name == "driver_bookings":
                return MockTable([])
            elif name == "driver_requests":
                return MockTable([])
            return MockTable([])

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/seeker/dashboard")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Apex Heart", body)
        self.assertIn("ICU Bed", body)
        self.assertIn("Active Hold", body)
        self.assertIn("+91 9988776655", body)
        self.assertIn(f"/hospitals/holds/{self.HOLD_ID}/cancel", body)
        self.assertIn("Cancel Hold", body)

    def test_seeker_dashboard_renders_active_driver_booking(self):
        self.login_as(role="seeker")
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "accepted",
            "start_at": "2026-10-14T09:00:00Z",
            "end_at": "2026-10-14T11:00:00Z",
            "created_at": "2026-10-13T20:00:00Z",
        }
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "pickup_address": "Railway Station, Gate 1",
            "destination_address": "Medical College Road",
            "requested_start": "2026-10-14T09:00:00Z",
            "duration_minutes": 120,
            "status": "matched",
            "created_at": "2026-10-13T20:00:00Z",
        }
        driver_user = {"id": self.PROVIDER_ID, "name": "Rajesh Kumar", "phone": "+91 9876543210"}
        driver_profile = {"provider_id": self.PROVIDER_ID, "experience_years": 8, "licence_category": "commercial"}

        def mock_table(name):
            if name == "driver_bookings":
                return MockTable([booking_row])
            elif name == "driver_requests":
                return MockTable([request_row])
            elif name == "users":
                return MockTable([driver_user])
            elif name == "driver_profiles":
                return MockTable([driver_profile])
            return MockTable([])

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/seeker/dashboard")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Railway Station, Gate 1 &rarr; Medical College Road", body)
        self.assertIn("Rajesh Kumar", body)
        self.assertIn("8 yrs exp", body)
        self.assertIn("+91 9876543210", body)
        self.assertIn("Accepted", body)
        self.assertIn(f"/seeker/driver-bookings/{self.BOOKING_ID}/cancel", body)
        self.assertIn("Cancel Booking", body)

    def test_seeker_dashboard_renders_clean_empty_states(self):
        self.login_as(role="seeker")

        def mock_table(name):
            return MockTable([])

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/seeker/dashboard")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("No active emergency bed reservations.", body)
        self.assertIn("Find emergency bed", body)
        self.assertIn("No driver trips or requests yet.", body)
        self.assertIn("Request a driver for my car", body)

    # -----------------------------------------------------------------
    # JSON Synchronization API & Per-User Isolation Tests
    # -----------------------------------------------------------------

    def test_active_reservations_json_returns_structured_payload(self):
        self.login_as(role="seeker")
        future_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        hold_row = {
            "id": self.HOLD_ID,
            "hospital_id": self.HOSPITAL_ID,
            "seeker_id": self.SEEKER_ID,
            "bed_type": "oxygen",
            "status": "held",
            "expires_at": future_time,
            "hospitals": {
                "id": self.HOSPITAL_ID,
                "name": "St. Mary Emergency Clinic",
                "address": "Park Avenue",
                "phone": "+91 9119119110",
                "lat": 26.75,
                "lng": 83.35,
            },
        }
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "in_progress",
            "start_at": "2026-10-13T21:00:00Z",
            "end_at": "2026-10-13T22:30:00Z",
            "created_at": "2026-10-13T20:50:00Z",
        }
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "pickup_address": "Home",
            "destination_address": "Airport Terminal",
            "requested_start": "2026-10-13T21:00:00Z",
            "duration_minutes": 90,
            "status": "matched",
            "created_at": "2026-10-13T20:50:00Z",
        }
        driver_user = {"id": self.PROVIDER_ID, "name": "Deepak Driver", "phone": "+91 9000000001"}

        def mock_table(name):
            if name == "hospital_bed_holds":
                return MockTable([hold_row])
            elif name == "driver_bookings":
                return MockTable([booking_row])
            elif name == "driver_requests":
                return MockTable([request_row])
            elif name == "users":
                return MockTable([driver_user])
            return MockTable([])

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/seeker/active-reservations")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        res_data = data["data"]

        # Validate Bed Hold Data
        bed_data = res_data["bed_reservations"]
        self.assertIsNotNone(bed_data["active_hold"])
        self.assertEqual(bed_data["active_hold"]["id"], self.HOLD_ID)
        self.assertEqual(bed_data["active_hold"]["hospital_name"], "St. Mary Emergency Clinic")
        self.assertEqual(bed_data["active_hold"]["bed_type"], "oxygen")
        self.assertTrue(bed_data["active_hold"]["is_active"])

        # Validate Driver Trip Data
        trip_data = res_data["driver_trips"]
        self.assertIsNotNone(trip_data["active_trip"])
        self.assertEqual(trip_data["active_trip"]["id"], self.BOOKING_ID)
        self.assertEqual(trip_data["active_trip"]["status"], "in_progress")
        self.assertTrue(trip_data["active_trip"]["is_active"])
        self.assertFalse(trip_data["active_trip"]["can_cancel"])  # in_progress cannot be cancelled
        self.assertEqual(trip_data["active_trip"]["driver"]["name"], "Deepak Driver")

    def test_active_reservations_enforces_user_data_isolation(self):
        # Current seeker is SEEKER_ID
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        other_hold = {
            "id": "other-hold-uuid",
            "hospital_id": self.HOSPITAL_ID,
            "seeker_id": self.OTHER_SEEKER_ID,  # Different user
            "bed_type": "icu",
            "status": "held",
        }
        other_booking = {
            "id": "other-booking-uuid",
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.OTHER_SEEKER_ID,  # Different user
            "request_id": "other-req-uuid",
            "status": "accepted",
        }

        def mock_table(name):
            if name == "hospital_bed_holds":
                return MockTable([other_hold])
            elif name == "driver_bookings":
                return MockTable([other_booking])
            return MockTable([])

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/seeker/active-reservations")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        # Other user's records must NOT be visible
        self.assertIsNone(data["bed_reservations"]["active_hold"])
        self.assertEqual(len(data["bed_reservations"]["recent_holds"]), 0)
        self.assertIsNone(data["driver_trips"]["active_trip"])
        self.assertEqual(len(data["driver_trips"]["recent_trips"]), 0)

    def test_expired_bed_hold_is_treated_as_inactive_expired(self):
        self.login_as(role="seeker")
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        hold_row = {
            "id": self.HOLD_ID,
            "hospital_id": self.HOSPITAL_ID,
            "seeker_id": self.SEEKER_ID,
            "bed_type": "general",
            "status": "held",  # still marked held in DB, but expires_at is past
            "expires_at": past_time,
            "hospitals": {"name": "City General Hospital"},
        }

        def mock_table(name):
            if name == "hospital_bed_holds":
                return MockTable([hold_row])
            return MockTable([])

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/seeker/active-reservations")

        self.assertEqual(response.status_code, 200)
        bed_data = response.get_json()["data"]["bed_reservations"]
        self.assertIsNone(bed_data["active_hold"])
        self.assertEqual(len(bed_data["recent_holds"]), 1)
        self.assertEqual(bed_data["recent_holds"][0]["effective_status"], "expired")
        self.assertFalse(bed_data["recent_holds"][0]["is_active"])

    # -----------------------------------------------------------------
    # Static Assets & Client Safety Checks
    # -----------------------------------------------------------------

    def test_seeker_reservations_js_is_safe_and_has_visibility_handling(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "seeker-reservations.js"
        self.assertTrue(script_path.exists())
        code = script_path.read_text(encoding="utf-8")

        # Zero innerHTML guarantee
        self.assertNotIn("innerHTML", code)
        self.assertNotRegex(code, r"\beval\s*\(|new Function")

        # Polling & Visibility lifecycle guarantees
        self.assertIn("visibilitychange", code)
        self.assertIn("document.hidden", code)
        self.assertIn("pagehide", code)
        self.assertIn("replaceChildren", code)
        self.assertIn("/seeker/active-reservations", code)
        self.assertIn("csrf_token", code)


if __name__ == "__main__":
    unittest.main()
