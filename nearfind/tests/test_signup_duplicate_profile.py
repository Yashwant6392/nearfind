"""
tests/test_signup_duplicate_profile.py

Targeted tests for signup profile creation, duplicate-key handling,
trigger-created stub profiles, and retry safety.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as nearfind
from services.supabase_service import SupabaseConnectionError, SupabaseRequestError


class FakeAuth:
    def __init__(self, user_id="11111111-1111-4111-8111-111111111111", raise_on_signup=None, raise_on_login=None):
        self.user_id = user_id
        self.raise_on_signup = raise_on_signup
        self.raise_on_login = raise_on_login

    def sign_up(self, credentials):
        if self.raise_on_signup:
            raise self.raise_on_signup
        return SimpleNamespace(
            user=SimpleNamespace(id=self.user_id, email=credentials["email"])
        )

    def sign_in_with_password(self, credentials):
        if self.raise_on_login:
            raise self.raise_on_login
        return SimpleNamespace(
            user=SimpleNamespace(id=self.user_id, email=credentials["email"])
        )


class FakeSupabase:
    def __init__(self, user_id="11111111-1111-4111-8111-111111111111", raise_on_signup=None, raise_on_login=None):
        self.auth = FakeAuth(user_id=user_id, raise_on_signup=raise_on_signup, raise_on_login=raise_on_login)


class FakeUsersTable:
    def __init__(self, existing_profile=None, raise_on_insert=None, raise_on_update=None, raise_on_select=None):
        self.existing_profile = dict(existing_profile) if existing_profile else None
        self.raise_on_insert = raise_on_insert
        self.raise_on_update = raise_on_update
        self.raise_on_select = raise_on_select
        self.inserted = []
        self.updated = []
        self._target_id = None
        self._is_single = False
        self._pending_update = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, column, value):
        if column == "id":
            self._target_id = value
        return self

    def maybe_single(self):
        self._is_single = True
        return self

    def single(self):
        self._is_single = True
        return self

    def insert(self, record):
        if self.raise_on_insert:
            raise self.raise_on_insert
        self.inserted.append(record)
        self.existing_profile = dict(record)
        return self

    def update(self, data):
        if self.raise_on_update:
            raise self.raise_on_update
        self._pending_update = data
        self.updated.append(data)
        if self.existing_profile:
            self.existing_profile.update(data)
        return self

    def execute(self):
        if self.raise_on_select:
            raise self.raise_on_select
        if self._is_single:
            return SimpleNamespace(data=self.existing_profile)
        return SimpleNamespace(data=[self.existing_profile] if self.existing_profile else [])


class TestSignupDuplicateProfile(unittest.TestCase):
    def setUp(self):
        nearfind.app.config["TESTING"] = True
        nearfind.app.config["WTF_CSRF_ENABLED"] = False
        self.client = nearfind.app.test_client()
        self.user_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def csrf_token(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "valid-test-token"
        return "valid-test-token"

    def test_signup_successful_new_profile(self):
        csrf = self.csrf_token()
        users_table = FakeUsersTable(existing_profile=None)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Alice Seeker",
                    "email": "alice@example.com",
                    "password": "Password123!",
                    "phone": "9876543210",
                    "address": "123 Main St",
                    "role": "seeker",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 302)
            self.assertIn("/seeker/dashboard", res.headers.get("Location", ""))
            self.assertEqual(len(users_table.inserted), 1)
            self.assertEqual(users_table.inserted[0]["id"], self.user_id)
            self.assertEqual(users_table.inserted[0]["email"], "alice@example.com")
            self.assertEqual(users_table.inserted[0]["role"], "seeker")

            with self.client.session_transaction() as sess:
                self.assertEqual(sess["user_id"], self.user_id)
                self.assertEqual(sess["role"], "seeker")
                self.assertEqual(sess["name"], "Alice Seeker")

    def test_signup_trigger_stub_profile_already_exists(self):
        """Simulates Supabase trigger creating a stub record upon auth.sign_up."""
        csrf = self.csrf_token()
        stub_profile = {
            "id": self.user_id,
            "email": "bob@example.com",
            "name": "",
            "phone": "",
            "address": "",
            "role": None,
            "provider_type": None,
            "is_hospital_manager": False,
        }
        users_table = FakeUsersTable(existing_profile=stub_profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Bob Provider",
                    "email": "bob@example.com",
                    "password": "Password123!",
                    "phone": "9123456780",
                    "address": "456 Market St",
                    "role": "provider",
                    "provider_type": "shop",
                    "business_name": "Bob Store",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 302)
            self.assertIn("/provider/dashboard", res.headers.get("Location", ""))
            # Must NOT attempt duplicate insert
            self.assertEqual(len(users_table.inserted), 0)
            # Must safely update missing stub fields
            self.assertEqual(len(users_table.updated), 1)
            updated = users_table.updated[0]
            self.assertEqual(updated["name"], "Bob Provider")
            self.assertEqual(updated["role"], "provider")
            self.assertEqual(updated["provider_type"], "shop")
            self.assertEqual(updated["business_name"], "Bob Store")

            with self.client.session_transaction() as sess:
                self.assertEqual(sess["user_id"], self.user_id)
                self.assertEqual(sess["role"], "provider")
                self.assertEqual(sess["name"], "Bob Provider")

    def test_signup_existing_profile_preserves_role_and_authorization(self):
        """An existing user's role and authorization must not be overwritten by signup form."""
        csrf = self.csrf_token()
        complete_profile = {
            "id": self.user_id,
            "email": "existing@example.com",
            "name": "Existing Provider",
            "phone": "9999999999",
            "address": "Hospital Road",
            "role": "provider",
            "provider_type": "individual",
            "is_hospital_manager": True,
        }
        users_table = FakeUsersTable(existing_profile=complete_profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            # Attempt to sign up as seeker with same email
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Malicious Overwrite",
                    "email": "existing@example.com",
                    "password": "Password123!",
                    "phone": "8888888888",
                    "address": "Nowhere",
                    "role": "seeker",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 302)
            # Must redirect to existing provider dashboard because role was preserved
            self.assertIn("/provider/dashboard", res.headers.get("Location", ""))
            # No duplicate insert
            self.assertEqual(len(users_table.inserted), 0)

            with self.client.session_transaction() as sess:
                self.assertEqual(sess["user_id"], self.user_id)
                self.assertEqual(sess["role"], "provider")
                self.assertEqual(sess["name"], "Existing Provider")
                self.assertTrue(sess["is_hospital_manager"])

    def test_signup_mismatched_email_rejected_with_409(self):
        csrf = self.csrf_token()
        mismatched_profile = {
            "id": self.user_id,
            "email": "different@example.com",
            "name": "Wrong User",
            "role": "seeker",
        }
        users_table = FakeUsersTable(existing_profile=mismatched_profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "New Name",
                    "email": "attacker@example.com",
                    "password": "Password123!",
                    "phone": "9999999999",
                    "address": "123 St",
                    "role": "seeker",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 409)
            self.assertIn("could not be completed", res.get_data(as_text=True))
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)

    def test_signup_409_race_condition_reconciled_safely(self):
        """Simulates race condition: select initially sees no row, but insert hits 409 users_pkey."""
        csrf = self.csrf_token()
        users_table = FakeUsersTable(
            existing_profile=None,
            raise_on_insert=SupabaseRequestError(
                409, 'duplicate key value violates unique constraint "users_pkey"', code="23505"
            ),
        )
        # When recheck query runs, existing stub is found
        stub_profile = {
            "id": self.user_id,
            "email": "race@example.com",
            "name": "",
            "phone": "",
            "address": "",
            "role": None,
        }

        call_count = 0
        def fake_table(name):
            nonlocal call_count
            if name == "users":
                call_count += 1
                if call_count >= 2:
                    users_table.existing_profile = stub_profile
                return users_table
            return users_table

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", side_effect=fake_table
        ):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Race User",
                    "email": "race@example.com",
                    "password": "Password123!",
                    "phone": "9999999999",
                    "address": "Race St",
                    "role": "seeker",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 302)
            self.assertIn("/seeker/dashboard", res.headers.get("Location", ""))
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["user_id"], self.user_id)
                self.assertEqual(sess["name"], "Race User")
                self.assertEqual(sess["role"], "seeker")

    def test_signup_database_failure_returns_502_and_clears_session(self):
        csrf = self.csrf_token()
        users_table = FakeUsersTable(
            existing_profile=None,
            raise_on_insert=RuntimeError("Database connection lost"),
        )

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Crash User",
                    "email": "crash@example.com",
                    "password": "Password123!",
                    "phone": "9999999999",
                    "address": "Crash St",
                    "role": "seeker",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 502)
            self.assertIn("could not be completed", res.get_data(as_text=True))
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)

    def test_signup_existing_email_rejected_by_supabase_auth_returns_409(self):
        """When Supabase Auth rejects signup because user already registered, return clear 409."""
        csrf = self.csrf_token()
        supabase_fake = FakeSupabase(
            raise_on_signup=SupabaseRequestError(400, "A user with this email address has already been registered", code="user_already_exists")
        )

        with patch.object(nearfind, "get_supabase", return_value=supabase_fake):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Existing User",
                    "email": "existing@example.com",
                    "password": "Password123!",
                    "phone": "9999999999",
                    "address": "123 Main St",
                    "role": "seeker",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 409)
            self.assertIn("already exists", res.get_data(as_text=True))


class TestLoginFlow(unittest.TestCase):
    def setUp(self):
        nearfind.app.config["TESTING"] = True
        nearfind.app.config["WTF_CSRF_ENABLED"] = False
        self.client = nearfind.app.test_client()
        self.user_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def csrf_token(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "valid-test-token"
        return "valid-test-token"

    def test_login_successful_seeker(self):
        csrf = self.csrf_token()
        profile = {
            "id": self.user_id,
            "email": "seeker@example.com",
            "name": "Sam Seeker",
            "role": "seeker",
            "is_active": True,
            "is_hospital_manager": False,
        }
        users_table = FakeUsersTable(existing_profile=profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "seeker@example.com",
                    "password": "CorrectPassword123!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 302)
            self.assertIn("/seeker/dashboard", res.headers.get("Location", ""))
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["user_id"], self.user_id)
                self.assertEqual(sess["role"], "seeker")
                self.assertEqual(sess["name"], "Sam Seeker")

    def test_login_successful_provider(self):
        csrf = self.csrf_token()
        profile = {
            "id": self.user_id,
            "email": "provider@example.com",
            "name": "Pam Provider",
            "role": "provider",
            "provider_type": "shop",
            "is_active": True,
            "is_hospital_manager": True,
        }
        users_table = FakeUsersTable(existing_profile=profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "provider@example.com",
                    "password": "CorrectPassword123!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 302)
            self.assertIn("/provider/dashboard", res.headers.get("Location", ""))
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["user_id"], self.user_id)
                self.assertEqual(sess["role"], "provider")
                self.assertTrue(sess["is_hospital_manager"])

    def test_login_invalid_credentials_returns_400_with_clear_message(self):
        csrf = self.csrf_token()
        supabase_fake = FakeSupabase(
            raise_on_login=SupabaseRequestError(400, "Invalid login credentials", code="invalid_credentials")
        )

        with patch.object(nearfind, "get_supabase", return_value=supabase_fake):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "user@example.com",
                    "password": "WrongPassword!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 400)
            self.assertIn("Invalid email or password", res.get_data(as_text=True))
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)

    def test_login_profile_loading_failure_returns_502(self):
        csrf = self.csrf_token()
        users_table = FakeUsersTable(
            existing_profile=None,
            raise_on_select=RuntimeError("Database query timed out"),
        )

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "user@example.com",
                    "password": "Password123!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 502)
            self.assertIn("Unable to load your profile", res.get_data(as_text=True))
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)

    def test_login_inactive_account_returns_403(self):
        csrf = self.csrf_token()
        profile = {
            "id": self.user_id,
            "email": "banned@example.com",
            "name": "Banned User",
            "role": "seeker",
            "is_active": False,
        }
        users_table = FakeUsersTable(existing_profile=profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "banned@example.com",
                    "password": "Password123!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 403)
            self.assertIn("inactive", res.get_data(as_text=True))
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)

    def test_login_connection_error_returns_503(self):
        csrf = self.csrf_token()
        supabase_fake = FakeSupabase(
            raise_on_login=SupabaseConnectionError("connect", ("ConnectError",))
        )

        with patch.object(nearfind, "get_supabase", return_value=supabase_fake):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "user@example.com",
                    "password": "Password123!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )

            self.assertEqual(res.status_code, 503)
            self.assertIn("temporarily unavailable", res.get_data(as_text=True))
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)


class TestLocationValidationAndSessionFlow(unittest.TestCase):
    def setUp(self):
        nearfind.app.config["TESTING"] = True
        nearfind.app.config["WTF_CSRF_ENABLED"] = False
        self.client = nearfind.app.test_client()
        self.user_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def csrf_token(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "valid-test-token"
        return "valid-test-token"

    def test_signup_valid_coordinates_persisted(self):
        csrf = self.csrf_token()
        users_table = FakeUsersTable(existing_profile=None)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Location Seeker",
                    "email": "loc@example.com",
                    "password": "Password123!",
                    "phone": "9876543210",
                    "address": "456 Park Ave",
                    "role": "seeker",
                    "lat": "12.9716",
                    "lng": "77.5946",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 302)
            self.assertEqual(len(users_table.inserted), 1)
            inserted = users_table.inserted[0]
            self.assertEqual(inserted["lat"], 12.9716)
            self.assertEqual(inserted["lng"], 77.5946)
            self.assertIsNotNone(inserted["last_location_update"])

    def test_signup_missing_coordinates_allowed_as_none(self):
        csrf = self.csrf_token()
        users_table = FakeUsersTable(existing_profile=None)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "No Loc Seeker",
                    "email": "noloc@example.com",
                    "password": "Password123!",
                    "phone": "9876543210",
                    "address": "789 Lane",
                    "role": "seeker",
                    "lat": "",
                    "lng": "",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 302)
            self.assertEqual(len(users_table.inserted), 1)
            inserted = users_table.inserted[0]
            self.assertIsNone(inserted["lat"])
            self.assertIsNone(inserted["lng"])

    def test_signup_latitude_out_of_range(self):
        csrf = self.csrf_token()
        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Bad Lat Seeker",
                    "email": "badlat@example.com",
                    "password": "Password123!",
                    "phone": "9876543210",
                    "address": "789 Lane",
                    "role": "seeker",
                    "lat": "95.5",
                    "lng": "77.5946",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 400)
            self.assertIn("Latitude must be at most 90", res.get_data(as_text=True))

    def test_signup_longitude_out_of_range(self):
        csrf = self.csrf_token()
        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Bad Lng Seeker",
                    "email": "badlng@example.com",
                    "password": "Password123!",
                    "phone": "9876543210",
                    "address": "789 Lane",
                    "role": "seeker",
                    "lat": "12.9716",
                    "lng": "-185.0",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 400)
            self.assertIn("Longitude must be at least -180", res.get_data(as_text=True))

    def test_signup_mismatched_coordinates_rejected(self):
        csrf = self.csrf_token()
        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)):
            res = self.client.post(
                "/auth/signup",
                data={
                    "name": "Mismatched Seeker",
                    "email": "mismatched@example.com",
                    "password": "Password123!",
                    "phone": "9876543210",
                    "address": "789 Lane",
                    "role": "seeker",
                    "lat": "12.9716",
                    "lng": "",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 400)
            self.assertIn("Both latitude and longitude are required", res.get_data(as_text=True))

    def test_login_loads_saved_coordinates_into_session(self):
        csrf = self.csrf_token()
        profile = {
            "id": self.user_id,
            "email": "savedloc@example.com",
            "name": "Saved Loc User",
            "role": "seeker",
            "lat": 12.9716,
            "lng": 77.5946,
            "is_active": True,
        }
        users_table = FakeUsersTable(existing_profile=profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "savedloc@example.com",
                    "password": "Password123!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 302)
            self.assertIn("/seeker/dashboard", res.headers.get("Location", ""))
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["lat"], 12.9716)
                self.assertEqual(sess["lng"], 77.5946)

    def test_login_missing_coordinates_leaves_session_none(self):
        csrf = self.csrf_token()
        profile = {
            "id": self.user_id,
            "email": "nolocuser@example.com",
            "name": "No Loc User",
            "role": "seeker",
            "lat": None,
            "lng": None,
            "is_active": True,
        }
        users_table = FakeUsersTable(existing_profile=profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "nolocuser@example.com",
                    "password": "Password123!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 302)
            with self.client.session_transaction() as sess:
                self.assertIsNone(sess.get("lat"))
                self.assertIsNone(sess.get("lng"))

    def test_role_preservation_for_provider_and_hospital_manager(self):
        csrf = self.csrf_token()
        profile = {
            "id": self.user_id,
            "email": "hospital@example.com",
            "name": "Dr. Smith",
            "role": "provider",
            "is_hospital_manager": True,
            "is_active": True,
        }
        users_table = FakeUsersTable(existing_profile=profile)

        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase(user_id=self.user_id)), patch.object(
            nearfind, "table", return_value=users_table
        ):
            res = self.client.post(
                "/auth/login",
                data={
                    "email": "hospital@example.com",
                    "password": "Password123!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 302)
            self.assertIn("/provider/dashboard", res.headers.get("Location", ""))
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["role"], "provider")
                self.assertTrue(sess["is_hospital_manager"])


if __name__ == "__main__":
    unittest.main()
