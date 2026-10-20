import unittest
from datetime import datetime, timedelta, timezone
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
        self._maybe_single = False
        self._single = False

    def select(self, columns):
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

    def insert(self, record):
        return self

    def update(self, record):
        return self


class DriverAvailabilityAndAmenitiesTests(unittest.TestCase):
    PROVIDER_ID = "22222222-2222-4222-8222-222222222222"
    SEEKER_ID = "11111111-1111-4111-8111-111111111111"

    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def login_as(self, role="provider", user_id=None):
        uid = user_id or (self.PROVIDER_ID if role == "provider" else self.SEEKER_ID)
        with self.client.session_transaction() as session:
            session.update(user_id=uid, role=role, _csrf_token="known-csrf-token")

    def test_unapproved_driver_sees_clear_lock_reason_and_cannot_publish(self):
        self.login_as(role="provider")
        # Profile is pending verification
        pending_profile = {
            "provider_id": self.PROVIDER_ID,
            "verification_status": "pending",
            "is_active": True,
        }
        with patch.object(nearfind, "table", return_value=MockTable(pending_profile)):
            denied = self.client.get("/provider/driver-availability")
            self.assertEqual(denied.status_code, 403)
            self.assertIn(b"active, approved driver profile is required", denied.data)
            self.assertIn(b"Verification pending", denied.data)

        # POST attempt is also blocked
        start = datetime.now(timezone.utc) + timedelta(days=2)
        end = start + timedelta(hours=4)
        form = {
            "csrf_token": "known-csrf-token",
            "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": end.strftime("%Y-%m-%dT%H:%M"),
            "timezone_offset_minutes": "0",
            "sharing_enabled": "on",
            "lat": "26.7606",
            "lng": "83.3732",
        }
        with patch.object(nearfind, "table", return_value=MockTable(pending_profile)):
            post_denied = self.client.post("/provider/driver-availability", data=form)
            self.assertEqual(post_denied.status_code, 403)

    def test_unapproved_driver_cannot_toggle_availability_or_refresh_location(self):
        self.login_as(role="provider")
        pending_profile = {
            "provider_id": self.PROVIDER_ID,
            "verification_status": "pending",
            "is_active": True,
        }
        with patch.object(nearfind, "table", return_value=MockTable(pending_profile)):
            toggle_resp = self.client.post(
                "/provider/driver-availability/toggle",
                json={"active": False},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
            self.assertEqual(toggle_resp.status_code, 403)

            refresh_resp = self.client.post(
                "/provider/driver-location",
                data={"csrf_token": "known-csrf-token", "sharing_enabled": "on", "lat": "26.76", "lng": "83.37"},
            )
            self.assertEqual(refresh_resp.status_code, 403)

    def test_approved_driver_can_publish_availability_and_toggle(self):
        self.login_as(role="provider")
        approved_profile = {
            "provider_id": self.PROVIDER_ID,
            "verification_status": "approved",
            "is_active": True,
        }
        start = datetime.now(timezone.utc) + timedelta(days=1)
        end = start + timedelta(hours=6)
        form = {
            "csrf_token": "known-csrf-token",
            "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": end.strftime("%Y-%m-%dT%H:%M"),
            "timezone_offset_minutes": "0",
            "sharing_enabled": "on",
            "lat": "26.7606",
            "lng": "83.3732",
        }
        with patch.object(nearfind, "table", return_value=MockTable(approved_profile)), patch.object(
            nearfind, "rpc", return_value="test-avail-uuid"
        ) as rpc_mock:
            # HTML submit redirects back to availability
            resp = self.client.post("/provider/driver-availability", data=form)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(rpc_mock.call_args.args[0], "set_driver_presence")

            # JSON submit returns confirmed Published status
            json_resp = self.client.post(
                "/provider/driver-availability",
                json=form,
                headers={"Accept": "application/json", "X-CSRFToken": "known-csrf-token"},
            )
            self.assertEqual(json_resp.status_code, 200)
            self.assertTrue(json_resp.json["success"])
            self.assertEqual(json_resp.json["data"]["status"], "Published")

    def test_approved_driver_can_toggle_availability_pause(self):
        self.login_as(role="provider")
        approved_profile = {
            "provider_id": self.PROVIDER_ID,
            "verification_status": "approved",
            "is_active": True,
        }
        with patch.object(nearfind, "table", return_value=MockTable(approved_profile)):
            resp = self.client.post(
                "/provider/driver-availability/toggle",
                json={"active": False},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json["success"])
            self.assertEqual(resp.json["data"]["status"], "Not Published")

    def test_approved_driver_refresh_location_requires_sharing_enabled(self):
        self.login_as(role="provider")
        approved_profile = {
            "provider_id": self.PROVIDER_ID,
            "verification_status": "approved",
            "is_active": True,
        }
        # Attempt without sharing_enabled fails
        with patch.object(nearfind, "table", return_value=MockTable(approved_profile)):
            resp = self.client.post(
                "/provider/driver-location",
                data={"csrf_token": "known-csrf-token", "lat": "26.76", "lng": "83.37"},
            )
            self.assertEqual(resp.status_code, 400)

        # With sharing_enabled succeeds
        with patch.object(nearfind, "table", return_value=MockTable(approved_profile)), patch.object(
            nearfind, "rpc", return_value="2026-10-20T10:00:00Z"
        ):
            resp = self.client.post(
                "/provider/driver-location",
                data={"csrf_token": "known-csrf-token", "sharing_enabled": "on", "lat": "26.7606", "lng": "83.3732"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json["success"])
            self.assertEqual(resp.json["data"]["updated_at"], "2026-10-20T10:00:00Z")

    def test_provider_onboarding_ui_shows_distinct_lock_states(self):
        self.login_as(role="provider")
        # Incomplete profile state
        with patch.object(nearfind, "current_profile", return_value={"name": "Driver User"}), patch.object(
            nearfind, "table", return_value=MockTable([])
        ):
            resp = self.client.get("/provider/driver-onboarding")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_data(as_text=True)
            self.assertIn("Profile incomplete", body)
            self.assertIn("Verification Pending to Publish", body)

        # Pending verification state
        pending_profile = {
            "provider_id": self.PROVIDER_ID,
            "experience_years": 4,
            "licence_category": "LMV",
            "languages": ["Hindi"],
            "bio": "Local driver",
            "service_radius_km": 25,
            "verification_status": "pending",
            "is_active": True,
        }
        documents = [
            {"id": "doc-1", "driver_id": self.PROVIDER_ID, "verification_type": "identity", "status": "submitted"},
            {"id": "doc-2", "driver_id": self.PROVIDER_ID, "verification_type": "driving_license", "status": "submitted"},
        ]
        with patch.object(nearfind, "current_profile", return_value={"name": "Driver User"}), patch.object(
            nearfind, "table", side_effect=[MockTable(pending_profile), MockTable(documents), MockTable(None), MockTable(None)]
        ):
            resp = self.client.get("/provider/driver-onboarding")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_data(as_text=True)
            self.assertIn("Verification pending — availability publishing unlocks after approval", body)

    def test_seeker_driver_request_page_renders_amenities_and_current_location_button(self):
        self.login_as(role="seeker")
        with patch.object(nearfind, "table", return_value=MockTable([])):
            resp = self.client.get("/seeker/driver-request")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_data(as_text=True)
            # Use My Current Location button
            self.assertIn("useMyCurrentLocationBtn", body)
            self.assertIn("Use My Current Location", body)
            # Distinct point selectors
            self.assertIn("selectPickupPoint", body)
            self.assertIn("selectDestinationPoint", body)
            self.assertIn("Set Pickup Point", body)
            self.assertIn("Set Destination Point", body)
            # Optional Amenities section with cards and checkboxes
            self.assertIn("Optional Amenities", body)
            self.assertIn('name="amenities" type="checkbox" value="food"', body)
            self.assertIn('name="amenities" type="checkbox" value="rest_breaks"', body)
            self.assertIn("Food &amp; Meals", body)
            self.assertIn("Rest Breaks", body)


if __name__ == "__main__":
    unittest.main()
