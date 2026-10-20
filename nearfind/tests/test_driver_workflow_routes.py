import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import app as nearfind


class WorkflowTable:
    def __init__(self, data=None):
        self.data = data
        self.selected = None
        self.filters = []
        self.limit_value = None

    def select(self, columns):
        self.selected = columns
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def maybe_single(self):
        return self

    def order(self, column, desc=False):
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def execute(self):
        return SimpleNamespace(data=self.data)


class DriverWorkflowRouteTests(unittest.TestCase):
    USER_ID = "11111111-1111-4111-8111-111111111111"
    REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def login_as(self, role="provider", user_id=USER_ID):
        with self.client.session_transaction() as session:
            session.update(user_id=user_id, role=role, _csrf_token="known-csrf-token")

    def approved_profile(self):
        return WorkflowTable({"provider_id": self.USER_ID, "verification_status": "approved", "is_active": True})

    def test_availability_requires_approved_profile_and_saves_coordinates_with_rpc(self):
        self.login_as()
        with patch.object(nearfind, "table", return_value=WorkflowTable(None)):
            denied = self.client.get("/provider/driver-availability")
        self.assertEqual(denied.status_code, 403)
        self.assertIn(b"active, approved driver profile is required", denied.data)

        start = datetime.now() + timedelta(days=2)
        end = start + timedelta(hours=8)
        form = {
            "csrf_token": "known-csrf-token",
            "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": end.strftime("%Y-%m-%dT%H:%M"),
            "timezone_offset_minutes": "0",
            "sharing_enabled": "on",
            "lat": "26.7606",
            "lng": "83.3732",
        }
        with patch.object(nearfind, "table", return_value=self.approved_profile()), patch.object(
            nearfind, "rpc", return_value="availability-id"
        ) as rpc_mock:
            response = self.client.post("/provider/driver-availability", data=form)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(rpc_mock.call_args.args[0], "set_driver_presence")
        self.assertEqual(rpc_mock.call_args.args[1]["p_driver_id"], self.USER_ID)
        self.assertEqual(rpc_mock.call_args.args[1]["p_lat"], 26.7606)
        self.assertTrue(rpc_mock.call_args.args[1]["p_sharing_enabled"])

    def test_driver_requests_are_database_discovery_and_acceptance_is_csrf_protected(self):
        self.login_as()
        requests = [
            {
                "request_id": self.REQUEST_ID,
                "seeker_name": "Seeker",
                "pickup_address": "Pickup",
                "destination_address": "Destination",
                "requested_start": "2026-10-20T09:00:00+05:30",
                "requested_end": "2026-10-20T10:00:00+05:30",
                "duration_minutes": 60,
                "amenities": ["food"],
                "distance_km": 4.2,
            }
        ]
        with patch.object(nearfind, "rpc", return_value=requests) as rpc_mock:
            page = self.client.get("/provider/driver-requests")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Pickup to Destination", page.data)
        self.assertIn(b"4.2 km away", page.data)
        self.assertEqual(rpc_mock.call_args.args[0], "list_driver_requests_for_driver")

        missing_csrf = self.client.post(
            f"/provider/driver-requests/{self.REQUEST_ID}/accept",
            data={},
        )
        self.assertEqual(missing_csrf.status_code, 403)

        with patch.object(
            nearfind,
            "rpc",
            return_value={"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "accepted"},
        ) as accept_rpc:
            accepted = self.client.post(
                f"/provider/driver-requests/{self.REQUEST_ID}/accept",
                data={"csrf_token": "known-csrf-token"},
            )
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(accept_rpc.call_args.args[0], "accept_driver_request")
        self.assertEqual(accept_rpc.call_args.args[1]["p_driver_id"], self.USER_ID)
        self.assertEqual(accept_rpc.call_args.args[1]["p_request_id"], self.REQUEST_ID)

    def test_reviewer_authorization_is_grant_based_and_self_review_is_denied(self):
        self.login_as(role="seeker")
        with patch.object(nearfind, "table", return_value=WorkflowTable(None)):
            denied = self.client.get("/reviewer/driver-profiles")
        self.assertEqual(denied.status_code, 403)

        with patch.object(nearfind, "table", return_value=WorkflowTable({"user_id": self.USER_ID})), patch.object(
            nearfind, "rpc", side_effect=AssertionError("self-review must be denied")
        ):
            denied_self_review = self.client.post(
                "/reviewer/driver-profiles",
                data={
                    "csrf_token": "known-csrf-token",
                    "driver_id": self.USER_ID,
                    "decision": "approved",
                },
            )
        self.assertEqual(denied_self_review.status_code, 403)

    def test_trusted_reviewer_decision_is_persisted_before_success_flash(self):
        self.login_as(role="seeker")
        profile = {
            "provider_id": self.REQUEST_ID,
            "experience_years": 4,
            "licence_category": "LMV",
            "languages": ["Hindi"],
            "bio": "Local driver",
            "verification_status": "pending",
        }
        with patch.object(nearfind, "table", return_value=WorkflowTable({"user_id": self.USER_ID})), patch.object(
            nearfind, "rpc", return_value=profile
        ) as review_rpc:
            response = self.client.post(
                "/reviewer/driver-profiles",
                data={
                    "csrf_token": "known-csrf-token",
                    "driver_id": self.REQUEST_ID,
                    "decision": "approved",
                    "review_notes": "Identity and licence checked offline.",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(review_rpc.call_args.args[0], "review_driver_profile")
        self.assertEqual(review_rpc.call_args.args[1]["p_reviewer_id"], self.USER_ID)
        self.assertEqual(review_rpc.call_args.args[1]["p_decision"], "approved")
        self.assertEqual(review_rpc.call_args.args[1]["p_review_notes"], "Identity and licence checked offline.")

    def test_routes_reject_other_roles(self):
        self.login_as(role="seeker")
        response = self.client.get("/provider/driver-requests")
        self.assertEqual(response.headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
