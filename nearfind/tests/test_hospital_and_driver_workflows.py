import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import app as nearfind


class MockTable:
    def __init__(self, data=None):
        self.data = data
        self.filters = []
        self.inserted = []
        self.updated = []

    def select(self, columns):
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def gt(self, column, value):
        self.filters.append((column, value))
        return self

    def maybe_single(self):
        return self

    def order(self, column, desc=False):
        return self

    def limit(self, value):
        return self

    def insert(self, payload):
        self.inserted.append(payload)
        row = dict(payload)
        if "id" not in row:
            row["id"] = "test-inserted-uuid"
        self.data = [row]
        return self

    def update(self, payload):
        self.updated.append(payload)
        return self

    def execute(self):
        if isinstance(self.data, list):
            return SimpleNamespace(data=self.data)
        elif self.data is not None:
            return SimpleNamespace(data=self.data)
        return SimpleNamespace(data=None)


class HospitalAndDriverWorkflowTests(unittest.TestCase):
    SEEKER_ID = "11111111-1111-4111-8111-111111111111"
    PROVIDER_ID = "22222222-2222-4222-8222-222222222222"
    HOSPITAL_ID = "33333333-3333-4333-8333-333333333333"
    BOOKING_ID = "44444444-4444-4444-8444-444444444444"
    REQUEST_ID = "55555555-5555-4555-8555-555555555555"
    HOLD_ID = "66666666-6666-4666-8666-666666666666"

    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def login_as(self, role="seeker", user_id=None, is_hospital_manager=False):
        user_id = user_id or (self.SEEKER_ID if role == "seeker" else self.PROVIDER_ID)
        with self.client.session_transaction() as session:
            session.update(
                user_id=user_id,
                role=role,
                _csrf_token="known-csrf-token",
                is_hospital_manager=is_hospital_manager,
            )

    # ---------------------------------------------------------
    # Hospital Onboarding & Management Tests
    # ---------------------------------------------------------

    def test_hospital_onboarding_requires_login(self):
        response = self.client.get("/hospital/onboarding")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_hospital_onboarding_renders_form(self):
        self.login_as(role="provider")
        with patch.object(nearfind, "table", return_value=MockTable(None)):
            response = self.client.get("/hospital/onboarding")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Register Hospital Profile", body)
        self.assertIn('name="name"', body)
        self.assertIn('name="general_available"', body)
        self.assertIn('name="icu_available"', body)

    def test_hospital_onboarding_saves_facility_and_initial_inventory(self):
        self.login_as(role="provider")
        captured_inserts = {}

        def mock_table(table_name):
            t = MockTable(None)
            captured_inserts[table_name] = t
            return t

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.post(
                "/hospital/onboarding",
                data={
                    "csrf_token": "known-csrf-token",
                    "name": "Metro General Hospital",
                    "address": "45 Park Street, Gorakhpur",
                    "phone": "+91 9876543210",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "general_available": "20",
                    "icu_available": "5",
                    "oxygen_available": "10",
                    "emergency_available": "4",
                    "isolation_available": "2",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/hospital/dashboard", response.headers["Location"])
        # Verify hospital insert
        self.assertTrue(len(captured_inserts["hospitals"].inserted) > 0)
        hosp_rec = captured_inserts["hospitals"].inserted[0]
        self.assertEqual(hosp_rec["name"], "Metro General Hospital")
        self.assertEqual(hosp_rec["lat"], 26.7606)

    def test_hospital_dashboard_redirects_if_no_facility_registered(self):
        self.login_as(role="provider", is_hospital_manager=True)
        # Mock current_profile returning manager, but current_hospital returning None
        profile_table = MockTable({"id": self.PROVIDER_ID, "is_hospital_manager": True})
        hosp_table = MockTable(None)

        def mock_table(table_name):
            if table_name == "users":
                return profile_table
            return hosp_table

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/hospital/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/hospital/onboarding", response.headers["Location"])

    # ---------------------------------------------------------
    # Hospital Emergency Bed Holds Tests
    # ---------------------------------------------------------

    def test_hold_hospital_bed_decrements_availability(self):
        self.login_as(role="seeker")
        hosp_mock = MockTable({"id": self.HOSPITAL_ID, "name": "City Hospital", "manager_id": self.PROVIDER_ID})
        holds_check = MockTable(None)  # no active hold
        avail_mock = MockTable({"hospital_id": self.HOSPITAL_ID, "icu_available": 3})
        notif_mock = MockTable()

        def mock_table(table_name):
            if table_name == "hospitals":
                return hosp_mock
            elif table_name == "hospital_bed_holds":
                return holds_check
            elif table_name == "hospital_bed_availability":
                return avail_mock
            elif table_name == "notifications":
                return notif_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table), patch.object(
            nearfind, "rpc", side_effect=Exception("RPC not loaded")
        ):
            response = self.client.post(
                f"/hospitals/{self.HOSPITAL_ID}/hold-bed",
                headers={"X-CSRFToken": "known-csrf-token"},
                json={"bed_type": "icu"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["bed_type"], "icu")
        self.assertEqual(data["remaining_available"], 2)
        self.assertEqual(avail_mock.updated[0]["icu_available"], 2)

    def test_hold_hospital_bed_rejects_when_capacity_exhausted(self):
        self.login_as(role="seeker")
        hosp_mock = MockTable({"id": self.HOSPITAL_ID, "name": "City Hospital", "manager_id": self.PROVIDER_ID})
        holds_check = MockTable(None)
        avail_mock = MockTable({"hospital_id": self.HOSPITAL_ID, "icu_available": 0})

        def mock_table(table_name):
            if table_name == "hospitals":
                return hosp_mock
            elif table_name == "hospital_bed_holds":
                return holds_check
            elif table_name == "hospital_bed_availability":
                return avail_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table), patch.object(
            nearfind, "rpc", side_effect=Exception("RPC not loaded")
        ):
            response = self.client.post(
                f"/hospitals/{self.HOSPITAL_ID}/hold-bed",
                headers={"X-CSRFToken": "known-csrf-token"},
                json={"bed_type": "icu"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("No ICU beds currently available", response.get_json()["error"]["message"])

    def test_hold_hospital_bed_prevents_duplicate_active_holds(self):
        self.login_as(role="seeker")
        hosp_mock = MockTable({"id": self.HOSPITAL_ID, "name": "City Hospital", "manager_id": self.PROVIDER_ID})
        active_hold = MockTable({"id": "existing-hold-id", "hospital_id": self.HOSPITAL_ID, "status": "held"})

        def mock_table(table_name):
            if table_name == "hospitals":
                return hosp_mock
            elif table_name == "hospital_bed_holds":
                return active_hold
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.post(
                f"/hospitals/{self.HOSPITAL_ID}/hold-bed",
                headers={"X-CSRFToken": "known-csrf-token"},
                json={"bed_type": "general"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("already have an active emergency bed hold", response.get_json()["error"]["message"])

    def test_cancel_hospital_bed_hold_restores_capacity(self):
        self.login_as(role="seeker")
        hold_mock = MockTable({
            "id": self.HOLD_ID,
            "hospital_id": self.HOSPITAL_ID,
            "seeker_id": self.SEEKER_ID,
            "bed_type": "oxygen",
            "status": "held",
        })
        hosp_mock = MockTable({"id": self.HOSPITAL_ID, "manager_id": self.PROVIDER_ID})
        avail_mock = MockTable({"hospital_id": self.HOSPITAL_ID, "oxygen_available": 2})

        def mock_table(table_name):
            if table_name == "hospital_bed_holds":
                return hold_mock
            elif table_name == "hospitals":
                return hosp_mock
            elif table_name == "hospital_bed_availability":
                return avail_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table), patch.object(
            nearfind, "rpc", side_effect=Exception("RPC not loaded")
        ):
            response = self.client.post(
                f"/hospitals/holds/{self.HOLD_ID}/cancel",
                data={"csrf_token": "known-csrf-token"},
            )

        self.assertEqual(response.status_code, 302)
        # Capacity restored by +1
        self.assertEqual(avail_mock.updated[0]["oxygen_available"], 3)
        self.assertEqual(hold_mock.updated[0]["status"], "cancelled")

    def test_cancel_hospital_bed_hold_by_manager_redirects_to_hospital_dashboard(self):
        self.login_as(role="provider", is_hospital_manager=True)
        profile_table = MockTable({"id": self.PROVIDER_ID, "is_hospital_manager": True})
        hold_mock = MockTable({
            "id": self.HOLD_ID,
            "hospital_id": self.HOSPITAL_ID,
            "seeker_id": self.SEEKER_ID,
            "bed_type": "icu",
            "status": "held",
        })
        hosp_mock = MockTable({"id": self.HOSPITAL_ID, "manager_id": self.PROVIDER_ID})
        avail_mock = MockTable({"hospital_id": self.HOSPITAL_ID, "icu_available": 1})

        def mock_table(table_name):
            if table_name == "users":
                return profile_table
            elif table_name == "hospital_bed_holds":
                return hold_mock
            elif table_name == "hospitals":
                return hosp_mock
            elif table_name == "hospital_bed_availability":
                return avail_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table), patch.object(
            nearfind, "rpc", side_effect=Exception("RPC not loaded")
        ):
            response = self.client.post(
                f"/hospitals/holds/{self.HOLD_ID}/cancel",
                data={"csrf_token": "known-csrf-token"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/hospital/dashboard", response.headers["Location"])
        self.assertEqual(avail_mock.updated[0]["icu_available"], 2)
        self.assertEqual(hold_mock.updated[0]["status"], "cancelled")

    def test_admit_hospital_bed_hold_finalizes_hold_without_restoring_capacity(self):
        self.login_as(role="provider", is_hospital_manager=True)
        profile_table = MockTable({"id": self.PROVIDER_ID, "is_hospital_manager": True})
        hosp_table = MockTable({"id": self.HOSPITAL_ID, "name": "City Hospital", "manager_id": self.PROVIDER_ID})
        hold_mock = MockTable({
            "id": self.HOLD_ID,
            "hospital_id": self.HOSPITAL_ID,
            "seeker_id": self.SEEKER_ID,
            "bed_type": "emergency",
            "status": "held",
        })
        avail_mock = MockTable({"hospital_id": self.HOSPITAL_ID, "emergency_available": 4})
        notif_mock = MockTable()

        def mock_table(table_name):
            if table_name == "users":
                return profile_table
            elif table_name == "hospitals":
                return hosp_table
            elif table_name == "hospital_bed_holds":
                return hold_mock
            elif table_name == "hospital_bed_availability":
                return avail_mock
            elif table_name == "notifications":
                return notif_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table), patch.object(
            nearfind, "rpc", return_value={"id": self.HOLD_ID, "status": "admitted"}
        ):
            response = self.client.post(
                f"/hospitals/holds/{self.HOLD_ID}/admit",
                data={"csrf_token": "known-csrf-token"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/hospital/dashboard", response.headers["Location"])
        # Availability should NOT be restored (remains 4, not updated)
        self.assertEqual(len(avail_mock.updated), 0)
        # Notification created for seeker
        self.assertTrue(len(notif_mock.inserted) > 0)
        self.assertEqual(notif_mock.inserted[0]["type"], "bed_hold_admitted")

    def test_admit_hospital_bed_hold_requires_hospital_manager(self):
        self.login_as(role="seeker", is_hospital_manager=False)
        response = self.client.post(
            f"/hospitals/holds/{self.HOLD_ID}/admit",
            data={"csrf_token": "known-csrf-token"},
        )
        self.assertEqual(response.status_code, 403)

    def test_admit_hospital_bed_hold_rejects_unauthorized_manager(self):
        self.login_as(role="provider", is_hospital_manager=True)
        profile_table = MockTable({"id": self.PROVIDER_ID, "is_hospital_manager": True})
        # Manager owns DIFFERENT hospital
        hosp_table = MockTable({"id": "other-hospital-uuid", "name": "Other Hospital", "manager_id": self.PROVIDER_ID})
        hold_mock = MockTable({
            "id": self.HOLD_ID,
            "hospital_id": self.HOSPITAL_ID,  # belongs to HOSPITAL_ID, not other-hospital-uuid
            "seeker_id": self.SEEKER_ID,
            "bed_type": "icu",
            "status": "held",
        })

        def mock_table(table_name):
            if table_name == "users":
                return profile_table
            elif table_name == "hospitals":
                return hosp_table
            elif table_name == "hospital_bed_holds":
                return hold_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.post(
                f"/hospitals/holds/{self.HOLD_ID}/admit",
                data={"csrf_token": "known-csrf-token"},
            )

        self.assertEqual(response.status_code, 403)

    def test_hospital_dashboard_renders_incoming_holds_and_actions(self):
        self.login_as(role="provider", is_hospital_manager=True)
        profile_table = MockTable({"id": self.PROVIDER_ID, "is_hospital_manager": True})
        hosp_table = MockTable({"id": self.HOSPITAL_ID, "name": "City General", "manager_id": self.PROVIDER_ID})
        avail_table = MockTable({"hospital_id": self.HOSPITAL_ID, "general_available": 10})
        holds_table = MockTable([
            {
                "id": self.HOLD_ID,
                "hospital_id": self.HOSPITAL_ID,
                "seeker_id": self.SEEKER_ID,
                "bed_type": "icu",
                "status": "held",
                "created_at": "2026-10-13T01:00:00Z",
                "expires_at": "2026-10-13T03:00:00Z",
                "notes": "Patient in ambulance",
                "users": {"name": "Alice Seeker", "phone": "+91 9999988888"},
            }
        ])

        def mock_table(table_name):
            if table_name == "users":
                return profile_table
            elif table_name == "hospitals":
                return hosp_table
            elif table_name == "hospital_bed_availability":
                return avail_table
            elif table_name == "hospital_bed_holds":
                return holds_table
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/hospital/dashboard")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Incoming Emergency Bed Holds", body)
        self.assertIn("Alice Seeker", body)
        self.assertIn("+91 9999988888", body)
        self.assertIn("Admit Patient", body)
        self.assertIn("Cancel Hold", body)

    def test_seeker_emergency_bed_page_displays_active_hold_details(self):
        self.login_as(role="seeker")
        future_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        hold_mock = MockTable({
            "id": self.HOLD_ID,
            "hospital_id": self.HOSPITAL_ID,
            "seeker_id": self.SEEKER_ID,
            "bed_type": "icu",
            "status": "held",
            "expires_at": future_time,
            "hospitals": {
                "id": self.HOSPITAL_ID,
                "name": "Apollo Emergency Care",
                "address": "77 Ring Road",
                "phone": "+91 8888877777",
                "lat": 26.761,
                "lng": 83.374,
            },
        })

        def mock_table(table_name):
            if table_name == "hospital_bed_holds":
                return hold_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.get("/seeker/emergency-bed")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Active Emergency Hold", body)
        self.assertIn("Apollo Emergency Care", body)
        self.assertIn("Icu Bed", body)
        self.assertIn("Call Hospital (+91 8888877777)", body)
        self.assertIn("Cancel Bed Hold", body)

    # ---------------------------------------------------------
    # Driver Booking Status Transitions & Cancellation Tests
    # ---------------------------------------------------------

    def test_provider_transitions_booking_accepted_to_in_progress(self):
        self.login_as(role="provider")
        booking_mock = MockTable({
            "id": self.BOOKING_ID,
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "accepted",
        })
        events_mock = MockTable()
        notif_mock = MockTable()

        def mock_table(table_name):
            if table_name == "driver_bookings":
                return booking_mock
            elif table_name == "driver_booking_events":
                return events_mock
            elif table_name == "notifications":
                return notif_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.post(
                f"/provider/driver-bookings/{self.BOOKING_ID}/status",
                data={"csrf_token": "known-csrf-token", "status": "in_progress"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(booking_mock.updated[0]["status"], "in_progress")
        self.assertIn("started_at", booking_mock.updated[0])
        # Event logged
        self.assertEqual(events_mock.inserted[0]["event_type"], "in_progress")
        self.assertEqual(events_mock.inserted[0]["status_before"], "accepted")

    def test_provider_transitions_booking_in_progress_to_completed(self):
        self.login_as(role="provider")
        booking_mock = MockTable({
            "id": self.BOOKING_ID,
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "in_progress",
        })
        requests_mock = MockTable()
        events_mock = MockTable()
        notif_mock = MockTable()

        def mock_table(table_name):
            if table_name == "driver_bookings":
                return booking_mock
            elif table_name == "driver_requests":
                return requests_mock
            elif table_name == "driver_booking_events":
                return events_mock
            elif table_name == "notifications":
                return notif_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.post(
                f"/provider/driver-bookings/{self.BOOKING_ID}/status",
                data={"csrf_token": "known-csrf-token", "status": "completed"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(booking_mock.updated[0]["status"], "completed")
        self.assertIn("completed_at", booking_mock.updated[0])
        self.assertEqual(requests_mock.updated[0]["status"], "completed")

    def test_provider_rejects_invalid_status_transition(self):
        self.login_as(role="provider")
        booking_mock = MockTable({
            "id": self.BOOKING_ID,
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "accepted",
        })

        with patch.object(nearfind, "table", return_value=booking_mock):
            response = self.client.post(
                f"/provider/driver-bookings/{self.BOOKING_ID}/status",
                data={"csrf_token": "known-csrf-token", "status": "completed"},  # cannot jump accepted -> completed
            )

        self.assertEqual(response.status_code, 400)

    def test_provider_cannot_update_another_drivers_booking(self):
        self.login_as(role="provider", user_id="another-driver-id")
        booking_mock = MockTable(None)  # not found for this driver

        with patch.object(nearfind, "table", return_value=booking_mock):
            response = self.client.post(
                f"/provider/driver-bookings/{self.BOOKING_ID}/status",
                data={"csrf_token": "known-csrf-token", "status": "in_progress"},
            )

        self.assertEqual(response.status_code, 404)

    def test_provider_cancels_driver_booking(self):
        self.login_as(role="provider")
        booking_mock = MockTable({
            "id": self.BOOKING_ID,
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "accepted",
        })
        requests_mock = MockTable()
        events_mock = MockTable()
        notif_mock = MockTable()

        def mock_table(table_name):
            if table_name == "driver_bookings":
                return booking_mock
            elif table_name == "driver_requests":
                return requests_mock
            elif table_name == "driver_booking_events":
                return events_mock
            elif table_name == "notifications":
                return notif_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.post(
                f"/provider/driver-bookings/{self.BOOKING_ID}/cancel",
                data={"csrf_token": "known-csrf-token", "reason": "Mechanical vehicle issue"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(booking_mock.updated[0]["status"], "cancelled_by_driver")
        self.assertEqual(booking_mock.updated[0]["cancellation_reason"], "Mechanical vehicle issue")
        self.assertEqual(requests_mock.updated[0]["status"], "cancelled")

    def test_seeker_cancels_driver_booking(self):
        self.login_as(role="seeker")
        booking_mock = MockTable({
            "id": self.BOOKING_ID,
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "accepted",
        })
        requests_mock = MockTable()
        events_mock = MockTable()
        notif_mock = MockTable()

        def mock_table(table_name):
            if table_name == "driver_bookings":
                return booking_mock
            elif table_name == "driver_requests":
                return requests_mock
            elif table_name == "driver_booking_events":
                return events_mock
            elif table_name == "notifications":
                return notif_mock
            return MockTable()

        with patch.object(nearfind, "table", side_effect=mock_table):
            response = self.client.post(
                f"/seeker/driver-bookings/{self.BOOKING_ID}/cancel",
                data={"csrf_token": "known-csrf-token", "reason": "Change of plans"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(booking_mock.updated[0]["status"], "cancelled_by_seeker")
        self.assertEqual(booking_mock.updated[0]["cancellation_reason"], "Change of plans")
        self.assertEqual(requests_mock.updated[0]["status"], "cancelled")

    def test_cannot_cancel_already_finalized_booking(self):
        self.login_as(role="seeker")
        booking_mock = MockTable({
            "id": self.BOOKING_ID,
            "driver_id": self.PROVIDER_ID,
            "seeker_id": self.SEEKER_ID,
            "request_id": self.REQUEST_ID,
            "status": "completed",
        })

        with patch.object(nearfind, "table", return_value=booking_mock):
            response = self.client.post(
                f"/seeker/driver-bookings/{self.BOOKING_ID}/cancel",
                data={"csrf_token": "known-csrf-token", "reason": "Late cancel"},
            )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
