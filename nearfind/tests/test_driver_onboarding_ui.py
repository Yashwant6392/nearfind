import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as nearfind


class DriverProfileTable:
    def __init__(self, data=None):
        self.data = data

    def select(self, columns):
        return self

    def eq(self, column, value):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        return SimpleNamespace(data=self.data)


class DriverOnboardingUiTests(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def login_as(self, role):
        with self.client.session_transaction() as session:
            session.update(
                user_id="11111111-1111-4111-8111-111111111111",
                role=role,
                _csrf_token="known-csrf-token",
            )

    def test_provider_get_renders_profile_form_and_csrf_token(self):
        self.login_as("provider")
        with patch.object(nearfind, "current_profile", return_value={"name": "Driver User"}), patch.object(
            nearfind, "table", return_value=DriverProfileTable()
        ):
            response = self.client.get("/provider/driver-onboarding")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('name="csrf_token" value="known-csrf-token"', body)
        self.assertIn('name="experience_years"', body)
        self.assertIn('name="licence_category"', body)
        self.assertIn('name="languages"', body)
        self.assertIn('name="bio"', body)
        self.assertIn('name="service_radius_km"', body)
        self.assertIn("No driver profile has been saved yet.", body)
        self.assertIn("does not upload documents", body)
        self.assertIn("trusted reviewer must independently verify", body)
        self.assertIn("Do not enter a licence number.", body)

    def test_onboarding_page_keeps_existing_login_and_role_restrictions(self):
        self.assertEqual(self.client.get("/provider/driver-onboarding").headers["Location"], "/login")
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "known-csrf-token"
        self.assertEqual(
            self.client.post(
                "/provider/driver-onboarding",
                data={"csrf_token": "known-csrf-token"},
            ).headers["Location"],
            "/login",
        )

        self.login_as("seeker")
        self.assertEqual(self.client.get("/provider/driver-onboarding").headers["Location"], "/")
        self.assertEqual(
            self.client.post(
                "/provider/driver-onboarding",
                data={"csrf_token": "known-csrf-token"},
            ).headers["Location"],
            "/",
        )

    def test_profile_post_validates_and_saves_using_database_rpc(self):
        self.login_as("provider")
        invalid_values = [
            {"experience_years": "two", "licence_category": "LMV", "service_radius_km": "25"},
            {"experience_years": "-1"},
            {"experience_years": "61"},
            {"licence_category": "L" * 41},
            {"languages": "L" * 201},
            {"bio": "B" * 2001},
            {"service_radius_km": "201"},
        ]
        for form in invalid_values:
            with self.subTest(form=form):
                response = self.client.post(
                    "/provider/driver-onboarding",
                    data={
                        "csrf_token": "known-csrf-token",
                        "experience_years": "4",
                        "licence_category": "LMV",
                        "languages": "Hindi, English",
                        "bio": "Local transport services",
                        "service_radius_km": "25",
                        **form,
                    },
                )
                self.assertEqual(response.status_code, 400)

        with patch.object(nearfind, "rpc", return_value={"provider_id": "11111111-1111-4111-8111-111111111111"}) as rpc_mock:
            response = self.client.post(
                "/provider/driver-onboarding",
                data={
                    "csrf_token": "known-csrf-token",
                    "experience_years": "4",
                    "licence_category": "LMV",
                    "languages": "Hindi, English",
                    "bio": "Local transport services",
                    "service_radius_km": "25",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/provider/driver-onboarding")
        rpc_args = rpc_mock.call_args.args
        self.assertEqual(rpc_args[0], "save_driver_profile")
        self.assertEqual(rpc_args[1]["p_experience_years"], 4)
        self.assertEqual(rpc_args[1]["p_languages"], ["Hindi", "English"])
        self.assertNotIn("licence_number", rpc_args[1])

        saved_profile = {
            "experience_years": 4,
            "licence_category": "LMV",
            "languages": ["Hindi", "English"],
            "bio": "Local transport services",
            "service_radius_km": 25,
            "verification_status": "pending",
            "review_notes": None,
        }
        with patch.object(nearfind, "current_profile", return_value={"name": "Driver User"}), patch.object(
            nearfind, "table", return_value=DriverProfileTable(saved_profile)
        ):
            response = self.client.get(response.headers["Location"], follow_redirects=True)
        self.assertIn(b"Driver profile saved.", response.data)
        self.assertIn(b"Pending", response.data)


if __name__ == "__main__":
    unittest.main()
