import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import app as nearfind


class DriverRequestTable:
    def __init__(self, data):
        self.data = data

    def select(self, columns):
        return self

    def eq(self, column, value):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        return SimpleNamespace(data=self.data)


class SeekerDriverRequestTests(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()
        self.login_as()

    def login_as(self, role="seeker"):
        with self.client.session_transaction() as session:
            session.update(
                user_id="11111111-1111-4111-8111-111111111111",
                role=role,
                _csrf_token="known-csrf-token",
            )

    def valid_form(self, **overrides):
        future_dt = datetime.now(timezone.utc) + timedelta(days=2)
        return {
            "csrf_token": "known-csrf-token",
            "pickup": "Central Station",
            "pickup_lat": "26.7606",
            "pickup_lng": "83.3732",
            "destination": "City Hospital",
            "destination_lat": "26.77",
            "destination_lng": "83.38",
            "requested_date": future_dt.strftime("%Y-%m-%d"),
            "requested_time": "10:00",
            "timezone_offset_minutes": "0",
            "duration_hours": "2.5",
            "requested_radius_km": "25",
            **overrides,
        }

    def test_seeker_page_renders_driver_trip_form_and_map_selection(self):
        response = self.client.get("/seeker/driver-request")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('name="csrf_token" value="known-csrf-token"', body)
        for name in (
            "pickup",
            "pickup_lat",
            "pickup_lng",
            "destination",
            "destination_lat",
            "destination_lng",
            "requested_date",
            "requested_time",
            "duration_hours",
            "amenities",
            "requested_radius_km",
        ):
            self.assertIn(f'name="{name}"', body)
        self.assertIn("driverRequestMap", body)
        self.assertIn("matching will show only approved, available drivers", body)

    def test_page_requires_seeker_role(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/seeker/driver-request").headers["Location"], "/login")

        self.login_as("provider")
        self.assertEqual(self.client.get("/seeker/driver-request").headers["Location"], "/")

    def test_post_requires_csrf_token(self):
        response = self.client.post(
            "/seeker/driver-request",
            data={key: value for key, value in self.valid_form().items() if key != "csrf_token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Invalid or missing CSRF token.", response.data)

    def test_post_validates_required_fields_and_formats_without_database_access(self):
        invalid_forms = [
            {"pickup": ""},
            {"destination": ""},
            {"pickup_lat": ""},
            {"pickup_lat": "91"},
            {"destination_lng": "181"},
            {"requested_date": ""},
            {"requested_date": "20-10-2026"},
            {"requested_date": "2026-02-30"},
            {"requested_time": ""},
            {"requested_time": "9:30 AM"},
            {"timezone_offset_minutes": "900"},
            {"duration_hours": ""},
            {"duration_hours": "two"},
            {"duration_hours": "0"},
            {"duration_hours": "169"},
            {"duration_hours": "0.001"},
            {"amenities": "unapproved"},
        ]
        with patch.object(nearfind, "rpc", side_effect=AssertionError("invalid request must not access database")):
            for override in invalid_forms:
                with self.subTest(override=override):
                    response = self.client.post(
                        "/seeker/driver-request",
                        data=self.valid_form(**override),
                    )
                    self.assertEqual(response.status_code, 400)

    def test_valid_request_is_persisted_and_redirected_to_real_matching_results(self):
        request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        call_results = [
            request_id,
            [
                {
                    "driver_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "display_name": "Verified Driver",
                    "experience_years": 5,
                    "licence_category": "LMV",
                    "languages": ["Hindi"],
                    "bio": "Local driving",
                    "distance_km": 2.0,
                }
            ],
        ]
        future_dt = datetime.now(timezone.utc) + timedelta(days=2)
        request_row = {
            "id": request_id,
            "seeker_id": "11111111-1111-4111-8111-111111111111",
            "pickup_address": "Central Station",
            "destination_address": "City Hospital",
            "requested_start": f"{future_dt.strftime('%Y-%m-%d')}T10:00:00+00:00",
            "duration_minutes": 150,
            "status": "open",
        }
        with patch.object(nearfind, "rpc", side_effect=call_results) as rpc_mock, patch.object(
            nearfind,
            "table",
            side_effect=[DriverRequestTable(request_row), DriverRequestTable(None)],
        ):
            response = self.client.post(
                "/seeker/driver-request",
                data=self.valid_form(amenities=["food", "rest_breaks"]),
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn(f"request_id={request_id}", response.headers["Location"])
            page = self.client.get(response.headers["Location"])

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Your request has been saved.", page.data)
        self.assertIn(b"Verified Driver", page.data)
        self.assertIn(b"2.0 km from pickup", page.data)
        self.assertEqual(rpc_mock.call_args_list[0].args[0], "create_driver_request")
        args = rpc_mock.call_args_list[0].args[1]
        self.assertEqual(args["p_seeker_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(args["p_duration_minutes"], 150)
        self.assertEqual(args["p_amenities"], ["food", "rest_breaks"])
        self.assertEqual(rpc_mock.call_args_list[1].args[0], "find_drivers_for_request")


if __name__ == "__main__":
    unittest.main()
