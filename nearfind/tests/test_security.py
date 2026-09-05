import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as nearfind


class FakeTable:
    def insert(self, payload):
        return self

    def update(self, payload):
        return self

    def eq(self, column, value):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class FakeRpcClient:
    def __init__(self):
        self.calls = []

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        return self

    def execute(self):
        return SimpleNamespace(data={"status": "matched"})


class FakeAuth:
    def sign_up(self, credentials):
        return SimpleNamespace(user=SimpleNamespace(id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", email=credentials["email"]))

    def sign_in_with_password(self, credentials):
        return SimpleNamespace(user=SimpleNamespace(id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", email=credentials["email"]))


class FakeSupabase:
    auth = FakeAuth()


class FakeProfileTable(FakeTable):
    def __init__(self, profile=None):
        self.profile = profile

    def select(self, columns):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        return SimpleNamespace(data=self.profile if self.profile is not None else [])


class FakeRowsTable(FakeTable):
    def __init__(self, rows=None, single=None):
        self.rows = rows or []
        self.single = single
        self.inserted = []
        self.updated = []

    def select(self, columns):
        return self

    def eq(self, column, value):
        return self

    def order(self, column, desc=False):
        return self

    def maybe_single(self):
        return self

    def single(self):
        return self

    def insert(self, payload):
        self.inserted.append(payload)
        return self

    def update(self, payload):
        self.updated.append(payload)
        return self

    def execute(self):
        if self.inserted:
            return SimpleNamespace(data=self.inserted[-1])
        if self.updated:
            return SimpleNamespace(data=self.updated[-1])
        if self.single is not None:
            return SimpleNamespace(data=self.single)
        return SimpleNamespace(data=self.rows)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = nearfind.app.test_client()

    def csrf_from(self, path):
        response = self.client.get(path)
        body = response.data.decode("utf-8")
        match = re.search(r'name="csrf-token" content="([^"]+)"', body)
        self.assertIsNotNone(match)
        return match.group(1)

    def login_as(self, role="seeker", user_id="11111111-1111-4111-8111-111111111111"):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["role"] = role
            session["email"] = f"{role}@example.com"
            session["name"] = f"{role.title()} User"
            session["_csrf_token"] = "known-csrf-token"

    def test_missing_csrf_rejected_for_location_update(self):
        self.login_as()
        response = self.client.post("/user/update-location", json={"lat": 26.7, "lng": 83.3})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["error"], "Invalid or missing CSRF token.")

    def test_invalid_csrf_rejected_for_location_update(self):
        self.login_as()
        response = self.client.post(
            "/user/update-location",
            json={"lat": 26.7, "lng": 83.3},
            headers={"X-CSRFToken": "wrong-token"},
        )
        self.assertEqual(response.status_code, 403)

    def test_valid_csrf_allows_location_update(self):
        self.login_as()
        with patch.object(nearfind, "table", return_value=FakeTable()):
            response = self.client.post(
                "/user/update-location",
                json={"lat": 26.7, "lng": 83.3},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])

    def test_all_state_changing_routes_reject_missing_csrf(self):
        self.login_as(role="seeker")
        routes = [
            ("post", "/auth/signup", {}),
            ("post", "/auth/login", {}),
            ("post", "/auth/logout", {}),
            ("post", "/user/update-location", {"json": {"lat": 26.7, "lng": 83.3}}),
            ("post", "/query/post", {}),
            ("post", "/upload/query-image", {}),
            ("post", "/query/respond", {}),
            ("post", "/upload/response-image", {}),
            ("post", "/query/select-provider", {"json": {}}),
            ("post", "/query/resolve", {"json": {}}),
        ]
        for method, path, kwargs in routes:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 403)

    def test_login_with_valid_csrf_uses_supabase_auth(self):
        self.csrf_from("/login")
        profile = {
            "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "email": "login@example.com",
            "role": "seeker",
            "name": "Login User",
            "lat": 26.7,
            "lng": 83.3,
            "is_active": True,
        }
        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase()), patch.object(
            nearfind, "table", return_value=FakeProfileTable(profile)
        ):
            response = self.client.post(
                "/auth/login",
                data={"email": "login@example.com", "password": "NearFind123", "csrf_token": self.csrf_from("/login")},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/seeker/dashboard")

    def test_signup_with_valid_csrf_uses_public_auth_not_admin_api(self):
        self.csrf_from("/signup")
        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase()), patch.object(
            nearfind, "table", return_value=FakeTable()
        ):
            response = self.client.post(
                "/auth/signup",
                data={
                    "name": "Signup User",
                    "email": "signup@example.com",
                    "password": "NearFind123",
                    "phone": "9999999999",
                    "address": "Signup Address",
                    "role": "seeker",
                    "lat": "26.7",
                    "lng": "83.3",
                    "csrf_token": self.csrf_from("/signup"),
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/seeker/dashboard")

    def test_logout_is_post_only_and_requires_csrf(self):
        self.login_as()
        self.assertEqual(self.client.get("/auth/logout").status_code, 405)
        self.assertEqual(self.client.post("/auth/logout").status_code, 403)
        response = self.client.post("/auth/logout", data={"csrf_token": "known-csrf-token"})
        self.assertEqual(response.status_code, 302)

    def test_provider_inside_radius_allowed(self):
        query = {"status": "open", "lat": 26.7606, "lng": 83.3732, "radius_km": 5}
        provider = {"is_active": True, "lat": 26.7610, "lng": 83.3740}
        allowed, detail, status = nearfind.provider_query_eligibility(query, provider)
        self.assertTrue(allowed)
        self.assertEqual(status, 200)

    def test_provider_outside_radius_rejected(self):
        query = {"status": "open", "lat": 26.7606, "lng": 83.3732, "radius_km": 1}
        provider = {"is_active": True, "lat": 27.0, "lng": 84.0}
        allowed, detail, status = nearfind.provider_query_eligibility(query, provider)
        self.assertFalse(allowed)
        self.assertEqual(status, 403)
        self.assertEqual(detail, "This request is outside your service radius.")

    def test_provider_without_location_rejected(self):
        query = {"status": "open", "lat": 26.7606, "lng": 83.3732, "radius_km": 5}
        allowed, detail, status = nearfind.provider_query_eligibility(query, {"is_active": True, "lat": None, "lng": None})
        self.assertFalse(allowed)
        self.assertEqual(status, 403)

    def test_inactive_provider_rejected_for_response(self):
        query = {"status": "open", "lat": 26.7606, "lng": 83.3732, "radius_km": 5}
        allowed, detail, status = nearfind.provider_query_eligibility(query, {"is_active": False, "lat": 26.7610, "lng": 83.3740})
        self.assertFalse(allowed)
        self.assertEqual(status, 403)
        self.assertEqual(detail, "Provider account is inactive.")

    def test_closed_query_rejected_for_provider_response(self):
        query = {"status": "resolved", "lat": 26.7606, "lng": 83.3732, "radius_km": 5}
        provider = {"is_active": True, "lat": 26.7610, "lng": 83.3740}
        allowed, detail, status = nearfind.provider_query_eligibility(query, provider)
        self.assertFalse(allowed)
        self.assertEqual(status, 409)

    def test_select_provider_uses_session_seeker_and_rpc(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        fake_rpc = FakeRpcClient()
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "open"}
        response = {"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "query_id": query["id"], "status": "available"}
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(
            nearfind, "table"
        ) as table_mock, patch.object(nearfind, "get_supabase", return_value=fake_rpc):
            table_mock.return_value.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = response
            result = self.client.post(
                "/query/select-provider",
                json={"query_id": query["id"], "response_id": response["id"], "seeker_id": "malicious"},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(fake_rpc.calls[0][0], "select_provider_for_query")
        self.assertEqual(fake_rpc.calls[0][1]["p_seeker_id"], "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def test_query_responses_requires_owner_and_returns_provider_identity(self):
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": query_id, "seeker_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "status": "open", "lat": 26.7606, "lng": 83.3732}
        rows = [
            {
                "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "query_id": query_id,
                "provider_id": "22222222-2222-4222-8222-222222222222",
                "message": "Available now",
                "price": "Rs 100",
                "image_url": "https://example.test/product.jpg",
                "status": "available",
                "created_at": "2026-09-05T10:00:00Z",
                "users": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "name": "Provider User",
                    "business_name": "Provider Shop",
                    "provider_type": "shop",
                    "phone": "9999999999",
                    "lat": 26.7610,
                    "lng": 83.3740,
                },
            }
        ]
        with patch.object(nearfind, "table", side_effect=[FakeRowsTable(single=query), FakeRowsTable(rows)]):
            response = self.client.get(f"/query/responses/{query_id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json["data"]["responses"][0]
        self.assertEqual(payload["provider_name"], "Provider User")
        self.assertEqual(payload["business_name"], "Provider Shop")
        self.assertEqual(payload["provider_type"], "shop")
        self.assertEqual(payload["message"], "Available now")
        self.assertEqual(payload["price"], "Rs 100")
        self.assertEqual(payload["status"], "available")

    def test_query_responses_rejects_non_owner(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        with patch.object(nearfind, "require_query_owner", return_value=None):
            response = self.client.get("/query/responses/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        self.assertEqual(response.status_code, 404)

    def test_provider_query_list_uses_real_distance_and_location_payload(self):
        self.login_as(role="provider", user_id="22222222-2222-4222-8222-222222222222")
        provider = {"id": "22222222-2222-4222-8222-222222222222", "role": "provider", "is_active": True, "lat": 26.7606, "lng": 83.3732}
        rows = [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "item_name": "USB cable",
                "category": "electronics",
                "description": "Need one cable",
                "users": {"name": "Seeker One"},
                "location": "Golghar",
                "lat": 26.7610,
                "lng": 83.3740,
                "radius_km": 5,
                "urgency": "urgent",
                "image_url": None,
                "created_at": "2026-09-05T10:00:00Z",
                "expires_at": "2026-09-05T12:00:00Z",
            },
            {
                "id": "33333333-3333-4333-8333-333333333333",
                "item_name": "Far item",
                "category": "electronics",
                "description": "Too far",
                "users": {"name": "Seeker Two"},
                "location": "Far market",
                "lat": 27.4,
                "lng": 84.2,
                "radius_km": 1,
                "urgency": "normal",
                "image_url": None,
                "created_at": "2026-09-05T10:00:00Z",
                "expires_at": "2026-09-05T12:00:00Z",
            },
        ]
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable(rows)):
            response = self.client.get("/query/list?radius_km=5&category=electronics&sort=distance")
        self.assertEqual(response.status_code, 200)
        payload = response.json["data"]["queries"]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["location"], "Golghar")
        self.assertEqual(payload[0]["seeker_name"], "Seeker One")

    def test_provider_response_uses_session_provider_id_not_form_provider_id(self):
        provider_id = "22222222-2222-4222-8222-222222222222"
        self.login_as(role="provider", user_id=provider_id)
        query = {
            "id": "11111111-1111-4111-8111-111111111111",
            "status": "open",
            "lat": 26.7606,
            "lng": 83.3732,
            "radius_km": 5,
        }
        provider = {"id": provider_id, "is_active": True, "lat": 26.7610, "lng": 83.3740}
        responses = FakeRowsTable(single=None)
        with patch.object(nearfind, "expire_open_queries") as expire_mock, patch.object(
            nearfind, "get_query", return_value=query
        ), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=responses):
            result = self.client.post(
                "/query/respond",
                data={
                    "query_id": query["id"],
                    "provider_id": "99999999-9999-4999-8999-999999999999",
                    "price": "Rs 100",
                    "message": "Available now",
                    "csrf_token": "known-csrf-token",
                },
            )
        self.assertEqual(result.status_code, 302)
        expire_mock.assert_called_once()
        self.assertEqual(responses.inserted[0]["provider_id"], provider_id)

    def test_duplicate_provider_response_updates_existing_response(self):
        provider_id = "22222222-2222-4222-8222-222222222222"
        self.login_as(role="provider", user_id=provider_id)
        query = {
            "id": "11111111-1111-4111-8111-111111111111",
            "status": "open",
            "lat": 26.7606,
            "lng": 83.3732,
            "radius_km": 5,
        }
        provider = {"id": provider_id, "is_active": True, "lat": 26.7610, "lng": 83.3740}
        responses = FakeRowsTable(single={"id": "33333333-3333-4333-8333-333333333333"})
        with patch.object(nearfind, "expire_open_queries"), patch.object(nearfind, "get_query", return_value=query), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=responses):
            result = self.client.post(
                "/query/respond",
                data={
                    "query_id": query["id"],
                    "price": "Rs 120",
                    "message": "Still available",
                    "csrf_token": "known-csrf-token",
                },
            )
        self.assertEqual(result.status_code, 302)
        self.assertEqual(responses.inserted, [])
        self.assertEqual(responses.updated[0]["status"], "available")


if __name__ == "__main__":
    unittest.main()
