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
        self._maybe_single = False
        self._single = False

    def select(self, columns):
        self.selected_columns = columns
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def maybe_single(self):
        self._maybe_single = True
        return self

    def single(self):
        self._single = True
        return self

    def order(self, column, desc=False):
        return self

    def limit(self, value):
        return self

    def execute(self):
        filtered = list(self.data)
        for col, val in self.filters:
            filtered = [r for r in filtered if r.get(col) == val]
        if self._maybe_single or self._single:
            result_data = filtered[0] if filtered else None
        else:
            result_data = filtered
        return SimpleNamespace(data=result_data)


class DriverLiveLocationTrackingTests(unittest.TestCase):
    SEEKER_ID = "11111111-1111-4111-8111-111111111111"
    OTHER_SEEKER_ID = "99999999-9999-4999-8999-999999999999"
    DRIVER_ID = "22222222-2222-4222-8222-222222222222"
    REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    BOOKING_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def login_as(self, role="seeker", user_id=None):
        uid = user_id or self.SEEKER_ID
        with self.client.session_transaction() as session:
            session.update(
                user_id=uid,
                role=role,
                _csrf_token="known-csrf-token",
            )

    def mock_tables(self, request_row=None, booking_row=None, location_row=None, user_row=None):
        def table_resolver(name):
            if name == "driver_requests":
                return MockTable(request_row)
            if name == "driver_bookings":
                return MockTable(booking_row)
            if name == "driver_locations":
                return MockTable(location_row)
            if name == "users":
                return MockTable(user_row)
            return MockTable([])
        return table_resolver

    def error_message(self, response):
        err = response.json.get("error", "")
        if isinstance(err, dict):
            return err.get("message", "")
        return str(err)

    # 1. Assigned seeker can retrieve authorized driver location
    def test_assigned_seeker_can_retrieve_authorized_driver_location(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        now_utc = datetime.now(timezone.utc)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "pickup_address": "Station Road",
            "pickup_lat": 26.7606,
            "pickup_lng": 83.3732,
            "destination_address": "City Hospital",
            "destination_lat": 26.7800,
            "destination_lng": 83.3900,
            "status": "matched",
        }
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.DRIVER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "in_progress",
            "start_at": (now_utc - timedelta(minutes=10)).isoformat(),
            "end_at": (now_utc + timedelta(hours=2)).isoformat(),
        }
        location_row = {
            "driver_id": self.DRIVER_ID,
            "lat": 26.7650,
            "lng": 83.3750,
            "accuracy_m": 8.5,
            "updated_at": (now_utc - timedelta(minutes=1)).isoformat(),
            "sharing_enabled": True,
        }
        user_row = {"id": self.DRIVER_ID, "name": "Rajesh Kumar"}

        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row, booking_row, location_row, user_row)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        data = response.json["data"]
        self.assertIsNotNone(data)
        self.assertEqual(data["driver_id"], self.DRIVER_ID)
        self.assertEqual(data["driver_name"], "Rajesh Kumar")
        self.assertEqual(data["lat"], 26.7650)
        self.assertEqual(data["lng"], 83.3750)
        self.assertEqual(data["trip_status"], "in_progress")
        self.assertTrue(data["sharing_enabled"])
        self.assertFalse(data["is_stale"])
        self.assertIsNotNone(data["distance_km"])
        self.assertGreater(data["distance_km"], 0)

    # 2. Other seekers cannot retrieve that location
    def test_other_seekers_cannot_retrieve_driver_location(self):
        self.login_as(role="seeker", user_id=self.OTHER_SEEKER_ID)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,  # owned by different seeker
            "status": "matched",
            "pickup_lat": 26.7606,
            "pickup_lng": 83.3732,
        }
        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row=request_row)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json["success"])
        self.assertIn("Access denied", self.error_message(response))
        self.assertNotIn("data", response.json)

    def test_unauthenticated_user_cannot_access_endpoint(self):
        # Without logging in, API returns 401 JSON authentication_required
        response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json["success"])

    def test_provider_role_cannot_access_seeker_endpoint(self):
        self.login_as(role="provider", user_id=self.DRIVER_ID)
        response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json["success"])

    def test_invalid_uuid_returns_400(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        response = self.client.get("/seeker/driver-request/not-a-uuid/driver-location")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["success"])
        self.assertIn("Invalid driver request id", self.error_message(response))

    def test_nonexistent_request_returns_404(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row=None)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json["success"])
        self.assertIn("Driver request not found", self.error_message(response))

    # 3. Tracking is unavailable for unassigned, cancelled, or completed trips
    def test_tracking_unavailable_for_unassigned_trip(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "status": "open",
        }
        # No booking created yet
        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row=request_row, booking_row=None)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["success"])
        self.assertIn("unassigned", self.error_message(response).lower())

    def test_tracking_unavailable_for_cancelled_request(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "status": "cancelled",
        }
        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row=request_row)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["success"])
        self.assertIn("cancelled", self.error_message(response).lower())

    def test_tracking_unavailable_for_completed_booking(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "status": "completed",
        }
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.DRIVER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "completed",
        }
        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row=request_row, booking_row=booking_row)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["success"])
        self.assertIn("completed", self.error_message(response).lower())

    def test_tracking_unavailable_for_cancelled_by_driver(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "status": "matched",
        }
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.DRIVER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "cancelled_by_driver",
        }
        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row=request_row, booking_row=booking_row)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["success"])
        self.assertIn("cancelled", self.error_message(response).lower())

    # 4. Missing/stale coordinates and API errors
    def test_disabled_location_sharing_returns_none_data(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "status": "matched",
            "pickup_lat": 26.7606,
            "pickup_lng": 83.3732,
        }
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.DRIVER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "in_progress",
        }
        # sharing_enabled is False
        location_row = {
            "driver_id": self.DRIVER_ID,
            "lat": 26.7650,
            "lng": 83.3750,
            "sharing_enabled": False,
        }
        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row, booking_row, location_row)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        self.assertIsNone(response.json["data"])

    def test_missing_coordinates_returns_none_data(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "status": "matched",
            "pickup_lat": 26.7606,
            "pickup_lng": 83.3732,
        }
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.DRIVER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "in_progress",
        }
        location_row = {
            "driver_id": self.DRIVER_ID,
            "lat": None,
            "lng": None,
            "sharing_enabled": True,
        }
        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row, booking_row, location_row)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        self.assertIsNone(response.json["data"])

    def test_stale_coordinates_flagged_with_is_stale(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        now_utc = datetime.now(timezone.utc)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "status": "matched",
            "pickup_lat": 26.7606,
            "pickup_lng": 83.3732,
        }
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.DRIVER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "in_progress",
        }
        # updated_at 25 minutes ago (> 15 min threshold)
        location_row = {
            "driver_id": self.DRIVER_ID,
            "lat": 26.7650,
            "lng": 83.3750,
            "sharing_enabled": True,
            "updated_at": (now_utc - timedelta(minutes=25)).isoformat(),
        }
        user_row = {"id": self.DRIVER_ID, "name": "Driver"}

        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row, booking_row, location_row, user_row)):
            response = self.client.get(f"/seeker/driver-request/{self.REQUEST_ID}/driver-location")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        self.assertTrue(response.json["data"]["is_stale"])

    # 5. Polling lifecycle and Leaflet marker updates
    def test_seeker_driver_request_ui_includes_tracking_map_and_script(self):
        self.login_as(role="seeker", user_id=self.SEEKER_ID)
        request_row = {
            "id": self.REQUEST_ID,
            "seeker_id": self.SEEKER_ID,
            "pickup_address": "Station",
            "pickup_lat": 26.7606,
            "pickup_lng": 83.3732,
            "destination_address": "Hospital",
            "destination_lat": 26.7800,
            "destination_lng": 83.3900,
            "status": "matched",
            "requested_start": "2026-10-20T10:00:00Z",
            "duration_minutes": 60,
        }
        booking_row = {
            "id": self.BOOKING_ID,
            "driver_id": self.DRIVER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "in_progress",
            "start_at": "2026-10-20T10:00:00Z",
            "end_at": "2026-10-20T11:00:00Z",
        }

        with patch.object(nearfind, "table", side_effect=self.mock_tables(request_row, booking_row)):
            response = self.client.get(f"/seeker/driver-request?request_id={self.REQUEST_ID}")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("driverRequestMap", body)
        self.assertIn("driverTrackingStatus", body)
        self.assertIn("driverDistanceInfo", body)
        self.assertIn("driverLastUpdated", body)
        self.assertIn("NEARFIND_DRIVER_REQUEST", body)
        self.assertIn("NEARFIND_DRIVER_BOOKING", body)
        self.assertIn("driver-request-map.js", body)

    def test_driver_request_map_js_contains_lifecycle_and_stale_handlers(self):
        js_path = Path(nearfind.__file__).resolve().parent / "static" / "js" / "driver-request-map.js"
        js_content = js_path.read_text(encoding="utf-8")

        # Polling and overlapping prevention
        self.assertIn("isFetching", js_content)
        self.assertIn("POLL_INTERVAL", js_content)
        self.assertIn("visibilitychange", js_content)
        self.assertIn("beforeunload", js_content)
        self.assertIn("pagehide", js_content)

        # Leaflet marker update without recreating map
        self.assertIn("driverMarker.setLatLng", js_content)
        self.assertIn("is_stale", js_content)
        self.assertIn("in_progress", js_content)
        self.assertIn("driver-location", js_content)


if __name__ == "__main__":
    unittest.main()
