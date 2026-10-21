import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import app as nearfind
from services.supabase_service import SupabaseRequestError


class FakeAuthResult:
    def __init__(self, user=None, session=None):
        self.user = user
        self.session = session


class FakeQueryChain:
    def __init__(self, data=None):
        self._data = data

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def maybe_single(self):
        return self

    def single(self):
        return self

    def insert(self, *args, **kwargs):
        return self

    def update(self, *args, **kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class FakeTableClient:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def __call__(self, table_name):
        data = self.tables.get(table_name, [])
        return FakeQueryChain(data)


class AuthRoutingAndLocationAuditTests(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def csrf_from(self, path="/login"):
        response = self.client.get(path)
        body = response.data.decode("utf-8")
        match = re.search(r'name="csrf-token" content="([^"]+)"', body)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', body)
        self.assertIsNotNone(match, f"Could not find CSRF token on {path}")
        return match.group(1)

    # -------------------------------------------------------------
    # Issue 1: Authentication Error Mapping & Flow Isolation Tests
    # -------------------------------------------------------------

    def test_login_rate_limit_returns_specific_message(self):
        """HTTP 429 during sign_in_with_password must return a login-specific rate-limit message, not generic invalid credentials."""
        csrf = self.csrf_from("/login")
        rate_limit_err = SupabaseRequestError(429, "Too many requests", code="over_request_rate_limit")
        with patch.object(nearfind, "supabase_auth_request", side_effect=rate_limit_err):
            resp = self.client.post(
                "/auth/login",
                data={"email": "rate_limited_user@example.com", "password": "Password123", "csrf_token": csrf},
            )
            self.assertEqual(resp.status_code, 429)
            body = resp.get_data(as_text=True)
            self.assertIn("Too many login attempts. Please wait and try again later.", body)
            self.assertNotIn("Invalid email or password", body)
            self.assertNotIn("Sign-up rate limit reached", body)

    def test_login_unconfirmed_email_returns_verification_message(self):
        """Supabase email_not_confirmed must return a clear prompt instructing the user to verify their email."""
        csrf = self.csrf_from("/login")
        unconfirmed_err = SupabaseRequestError(400, "Email not confirmed", code="email_not_confirmed")
        with patch.object(nearfind, "supabase_auth_request", side_effect=unconfirmed_err):
            resp = self.client.post(
                "/auth/login",
                data={"email": "unverified@example.com", "password": "Password123", "csrf_token": csrf},
            )
            self.assertEqual(resp.status_code, 403)
            body = resp.get_data(as_text=True)
            self.assertIn("Please verify your email address before signing in", body)
            self.assertNotIn("Invalid email or password", body)
            self.assertNotIn("Sign-up rate limit reached", body)

    def test_login_invalid_credentials_returns_generic_message(self):
        """Wrong password or unknown email returns generic 400 Invalid email or password."""
        csrf = self.csrf_from("/login")
        invalid_err = SupabaseRequestError(400, "Invalid login credentials", code="invalid_credentials")
        with patch.object(nearfind, "supabase_auth_request", side_effect=invalid_err):
            resp = self.client.post(
                "/auth/login",
                data={"email": "wrongpass@example.com", "password": "BadPassword", "csrf_token": csrf},
            )
            self.assertEqual(resp.status_code, 400)
            body = resp.get_data(as_text=True)
            self.assertIn("Invalid email or password.", body)
            self.assertNotIn("Sign-up rate limit reached", body)

    def test_signup_rate_limit_returns_signup_specific_message(self):
        """HTTP 429 during sign_up must display sign-up specific rate limit message."""
        csrf = self.csrf_from("/signup")
        rate_limit_err = SupabaseRequestError(429, "Rate limit exceeded", code="over_email_send_rate_limit")
        with patch.object(nearfind, "supabase_auth_request", side_effect=rate_limit_err):
            resp = self.client.post(
                "/auth/signup",
                data={
                    "name": "New Seeker",
                    "email": "newseeker@example.com",
                    "password": "Password123",
                    "phone": "+91 98765 43210",
                    "address": "123 Park Street",
                    "role": "seeker",
                    "csrf_token": csrf,
                },
            )
            self.assertEqual(resp.status_code, 429)
            body = resp.get_data(as_text=True)
            self.assertIn("Sign-up rate limit reached. Please wait and try again later.", body)

    def test_logout_clears_session_without_deleting_account(self):
        """Logging out must clear session cookies and redirect to /login."""
        csrf = self.csrf_from("/login")
        with self.client.session_transaction() as session:
            session["user_id"] = "user-12345"
            session["role"] = "seeker"
            session["email"] = "user@example.com"
            session["_csrf_token"] = csrf

        resp = self.client.post("/auth/logout", data={"csrf_token": csrf}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

        # Verify session is empty
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)
            self.assertNotIn("role", session)
            self.assertNotIn("email", session)

    # -------------------------------------------------------------
    # Issue 2: Role Routing, Dashboards & Account Switching Tests
    # -------------------------------------------------------------

    def test_seeker_login_redirects_to_seeker_dashboard(self):
        """Seeker login credentials redirect directly to /seeker/dashboard."""
        csrf = self.csrf_from("/login")
        user = SimpleNamespace(id="seeker-uuid-1111", email="seeker@example.com")
        profile = {
            "id": "seeker-uuid-1111",
            "email": "seeker@example.com",
            "role": "seeker",
            "name": "Alice Seeker",
            "is_active": True,
        }
        tables = {"users": profile, "driver_reviewers": None}
        with patch.object(nearfind, "supabase_auth_request", return_value=FakeAuthResult(user=user)), \
             patch.object(nearfind, "table", side_effect=FakeTableClient(tables)):
            resp = self.client.post(
                "/auth/login",
                data={"email": "seeker@example.com", "password": "Password123", "csrf_token": csrf},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/seeker/dashboard", resp.headers["Location"])

    def test_provider_login_redirects_to_provider_dashboard(self):
        """Provider login credentials redirect directly to /provider/dashboard."""
        csrf = self.csrf_from("/login")
        user = SimpleNamespace(id="provider-uuid-2222", email="provider@example.com")
        profile = {
            "id": "provider-uuid-2222",
            "email": "provider@example.com",
            "role": "provider",
            "name": "Bob Store",
            "is_active": True,
        }
        tables = {"users": profile, "driver_reviewers": None}
        with patch.object(nearfind, "supabase_auth_request", return_value=FakeAuthResult(user=user)), \
             patch.object(nearfind, "table", side_effect=FakeTableClient(tables)):
            resp = self.client.post(
                "/auth/login",
                data={"email": "provider@example.com", "password": "Password123", "csrf_token": csrf},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/provider/dashboard", resp.headers["Location"])

    def test_seeker_cannot_access_provider_dashboard(self):
        """A logged-in seeker accessing /provider/dashboard is rejected and safely redirected."""
        with self.client.session_transaction() as session:
            session["user_id"] = "seeker-uuid-1111"
            session["role"] = "seeker"

        # Direct access to provider dashboard as seeker
        resp = self.client.get("/provider/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

        # Following redirect to root routes seeker to seeker dashboard
        follow_resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(follow_resp.status_code, 302)
        self.assertIn("/seeker/dashboard", follow_resp.headers["Location"])

    def test_provider_cannot_access_seeker_dashboard(self):
        """A logged-in provider accessing /seeker/dashboard is rejected and safely redirected."""
        with self.client.session_transaction() as session:
            session["user_id"] = "provider-uuid-2222"
            session["role"] = "provider"

        resp = self.client.get("/seeker/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

        follow_resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(follow_resp.status_code, 302)
        self.assertIn("/provider/dashboard", follow_resp.headers["Location"])

    def test_account_switching_resets_role_completely(self):
        """Logging out as Provider and logging in as Seeker does not retain the Provider role."""
        csrf = self.csrf_from("/login")

        # 1. Start as provider
        with self.client.session_transaction() as session:
            session["user_id"] = "provider-uuid-2222"
            session["role"] = "provider"
            session["_csrf_token"] = csrf

        # 2. Logout
        logout_resp = self.client.post("/auth/logout", data={"csrf_token": csrf})
        self.assertEqual(logout_resp.status_code, 302)

        # 3. Login as seeker
        csrf2 = self.csrf_from("/login")
        user = SimpleNamespace(id="seeker-uuid-3333", email="newseeker@example.com")
        profile = {
            "id": "seeker-uuid-3333",
            "email": "newseeker@example.com",
            "role": "seeker",
            "name": "New Seeker",
            "is_active": True,
        }
        tables = {"users": profile, "driver_reviewers": None}
        with patch.object(nearfind, "supabase_auth_request", return_value=FakeAuthResult(user=user)), \
             patch.object(nearfind, "table", side_effect=FakeTableClient(tables)):
            login_resp = self.client.post(
                "/auth/login",
                data={"email": "newseeker@example.com", "password": "Password123", "csrf_token": csrf2},
                follow_redirects=False,
            )
            self.assertEqual(login_resp.status_code, 302)
            self.assertIn("/seeker/dashboard", login_resp.headers["Location"])

        # Verify active session has ONLY seeker role
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("role"), "seeker")
            self.assertEqual(session.get("user_id"), "seeker-uuid-3333")

    def test_signup_server_side_role_preselection(self):
        """GET /signup?role=provider pre-selects provider in HTML and displays provider inputs."""
        resp = self.client.get("/signup?role=provider")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('<option value="provider" selected>', html)
        self.assertNotIn('<option value="seeker" selected>', html)

        # GET /signup?role=seeker pre-selects seeker
        resp2 = self.client.get("/signup?role=seeker")
        self.assertEqual(resp2.status_code, 200)
        html2 = resp2.get_data(as_text=True)
        self.assertIn('<option value="seeker" selected>', html2)
        self.assertNotIn('<option value="provider" selected>', html2)

    def test_navbar_renders_strict_role_links(self):
        """Navbar strictly separates seeker links and provider links without fallback leakage."""
        # Seeker navbar has Home and Find Help CTA
        with self.client.session_transaction() as session:
            session["user_id"] = "seeker-1111"
            session["role"] = "seeker"
        resp = self.client.get("/signup")
        html = resp.get_data(as_text=True)
        self.assertIn('data-role="seeker"', html)
        self.assertIn("/seeker/dashboard", html)
        self.assertNotIn("/provider/dashboard", html)

        # Provider navbar has Nearby requests and no seeker dashboard
        with self.client.session_transaction() as session:
            session["user_id"] = "provider-2222"
            session["role"] = "provider"
        resp2 = self.client.get("/signup")
        html2 = resp2.get_data(as_text=True)
        self.assertIn('data-role="provider"', html2)
        self.assertIn("/provider/dashboard", html2)
        self.assertNotIn("/seeker/dashboard", html2)

    # -------------------------------------------------------------
    # Issue 3: Saved Coordinates UI Component Tests
    # -------------------------------------------------------------

    def test_saved_coordinates_compact_header_ui(self):
        """Seeker dashboard renders compact location indicator in header and preserves all controls."""
        with self.client.session_transaction() as session:
            session["user_id"] = "seeker-1111"
            session["role"] = "seeker"
            session["lat"] = 26.4504
            session["lng"] = 80.1904

        tables = {
            "queries": [],
            "hospital_reservations": [],
            "driver_bookings": [],
        }
        with patch.object(nearfind, "table", side_effect=FakeTableClient(tables)):
            resp = self.client.get("/seeker/dashboard")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)

            # 1. Location indicator is inside dashboard head
            self.assertIn('id="seekerLocationBanner"', html)
            self.assertIn("Location saved", html)
            self.assertIn("26.4504, 80.1904", html)

            # 2. Controls preserved
            self.assertIn('id="seekerUpdateLocationBtn"', html)
            self.assertIn('id="seekerManualToggleBtn"', html)
            self.assertIn('id="seekerManualLocationCard"', html)
            self.assertIn('id="manualSeekerLat"', html)
            self.assertIn('id="manualSeekerLng"', html)
            self.assertIn('id="saveSeekerManualLocationBtn"', html)
            self.assertIn('id="cancelSeekerManualLocationBtn"', html)

            # 3. Main header and CTA actions remain dominant
            self.assertIn("What do you need help with?", html)
            self.assertIn("+ Start a request", html)

    def test_saved_coordinates_unset_state_renders_cleanly(self):
        """When seeker has no saved coordinates, compact indicator displays unset state."""
        with self.client.session_transaction() as session:
            session["user_id"] = "seeker-1111"
            session["role"] = "seeker"
            session["lat"] = None
            session["lng"] = None

        tables = {
            "queries": [],
            "hospital_reservations": [],
            "driver_bookings": [],
        }
        with patch.object(nearfind, "table", side_effect=FakeTableClient(tables)):
            resp = self.client.get("/seeker/dashboard")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)

            self.assertIn('id="seekerLocationBanner"', html)
            self.assertIn("Location unset", html)
            self.assertIn('id="seekerUpdateLocationBtn"', html)
            self.assertIn('id="seekerManualToggleBtn"', html)


if __name__ == "__main__":
    unittest.main()
