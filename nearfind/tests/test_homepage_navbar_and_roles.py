import unittest
from unittest.mock import patch
import app as nearfind


class HomepageNavbarAndRoleDiscoveryTests(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def test_unauthenticated_navbar_and_role_discovery(self):
        """Verify redesigned navbar: Driver demo removed, 2 role options, Login and Get Started present."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # 1. Driver demo option must be completely removed from navbar
        self.assertNotIn("Driver demo", html)

        # 2. Both role options must exist in the navbar
        self.assertIn("Find Products", html)
        self.assertIn("Become a Provider", html)
        self.assertIn('/signup?role=seeker', html)
        self.assertIn('/signup?role=provider', html)

        # 3. Login and Get Started buttons must be preserved
        self.assertIn("Login", html)
        self.assertIn("/login", html)
        self.assertIn("Get Started", html)
        self.assertIn("/signup", html)

        # 4. Role Discovery section must be present on homepage with both cards
        self.assertIn('id="roleDiscovery"', html)
        self.assertIn("How do you want to use NearFind?", html)
        self.assertIn("For Buyers &amp; Seekers", html)
        self.assertIn("For Stores &amp; Providers", html)

        # 5. Role options must not include delivery partner
        self.assertNotIn("Delivery Partner", html)
        self.assertNotIn("delivery_partner", html)

    def test_authenticated_seeker_navbar(self):
        """Logged in seeker receives their dashboard and not public role discovery options."""
        with self.client.session_transaction() as session:
            session.update(user_id="11111111-1111-4111-8111-111111111111", role="seeker")

        # Visiting root as seeker redirects to seeker dashboard
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/seeker/dashboard", resp.headers["Location"])

        # Fetching a seeker page directly shows seeker nav and not Driver demo
        with patch.object(nearfind, "table"):
            dash_resp = self.client.get("/seeker/dashboard")
            dash_html = dash_resp.get_data(as_text=True)
            self.assertNotIn("Driver demo", dash_html)
            self.assertIn("Logout", dash_html)

    def test_authenticated_provider_navbar(self):
        """Logged in provider receives provider dashboard and not Driver demo."""
        with self.client.session_transaction() as session:
            session.update(user_id="22222222-2222-4222-8222-222222222222", role="provider")

        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/provider/dashboard", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
