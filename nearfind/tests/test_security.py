import io
import math
import re
import socket
import ssl
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import app as nearfind
from services.location_service import format_distance, haversine, valid_coordinates
from services.storage_service import upload_public_image, validate_image
from services.supabase_service import (
    RestSupabaseClient,
    SupabaseConnectionError,
    _headers,
    _raise_for_supabase,
    _transport_diagnostic,
    supabase_auth_request,
)
from werkzeug.datastructures import FileStorage


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
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "matched"}

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        return self

    def execute(self):
        return SimpleNamespace(data=self.result)


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
        self.filters = []
        self.selected_columns = []

    def select(self, columns):
        self.selected_columns.append(columns)
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def lt(self, column, value):
        self.filters.append((column, value))
        return self

    def in_(self, column, values):
        self.filters.append((column, tuple(values)))
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

    def test_login_rotates_session_identity(self):
        self.csrf_from("/login")
        profile = {
            "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "email": "login@example.com",
            "role": "seeker",
            "name": "Login User",
            "is_active": True,
        }
        with self.client.session_transaction() as session:
            session["user_id"] = "old-user"
            session["role"] = "provider"
        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase()), patch.object(
            nearfind, "table", return_value=FakeProfileTable(profile)
        ):
            response = self.client.post(
                "/auth/login",
                data={"email": "login@example.com", "password": "NearFind123", "csrf_token": self.csrf_from("/login")},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], profile["id"])
            self.assertEqual(session["role"], "seeker")
            self.assertTrue(session.permanent)

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

    def test_rest_signup_posts_to_auth_endpoint_and_handles_success_response(self):
        self.csrf_from("/signup")
        supabase = RestSupabaseClient(
            "https://project-ref.supabase.co",
            "sb_publishable_test-key",
            "sb_publishable_test-key",
        )
        auth_response = httpx.Response(
            200,
            json={
                "user": {
                    "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "email": "signup@example.com",
                }
            },
        )
        try:
            with patch.object(supabase.http, "post", return_value=auth_response) as post, patch.object(
                nearfind, "get_supabase", return_value=supabase
            ), patch.object(nearfind, "table", return_value=FakeTable()):
                response = self.client.post(
                    "/auth/signup",
                    data={
                        "name": "Signup User",
                        "email": "signup@example.com",
                        "password": "NearFind123",
                        "phone": "9999999999",
                        "address": "Signup Address",
                        "role": "seeker",
                        "csrf_token": self.csrf_from("/signup"),
                    },
                    follow_redirects=False,
                )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/seeker/dashboard")
            self.assertEqual(post.call_args.args[0], "https://project-ref.supabase.co/auth/v1/signup")
            self.assertNotIn("/rest/v1", post.call_args.args[0])
            self.assertEqual(post.call_args.kwargs["json"]["email"], "signup@example.com")
            headers = post.call_args.kwargs["headers"]
            self.assertEqual(headers["apikey"], "sb_publishable_test-key")
            self.assertNotIn("Authorization", headers)
        finally:
            supabase.http.close()

    def test_rest_login_posts_to_auth_endpoint_with_publishable_key(self):
        self.csrf_from("/login")
        supabase = RestSupabaseClient(
            "https://project-ref.supabase.co",
            "sb_publishable_test-key",
            "sb_publishable_test-key",
        )
        profile = {
            "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "email": "login@example.com",
            "role": "seeker",
            "name": "Login User",
            "is_active": True,
        }
        auth_response = httpx.Response(
            200,
            json={
                "user": {
                    "id": profile["id"],
                    "email": profile["email"],
                }
            },
        )
        try:
            with patch.object(supabase.http, "post", return_value=auth_response) as post, patch.object(
                nearfind, "get_supabase", return_value=supabase
            ), patch.object(nearfind, "table", return_value=FakeProfileTable(profile)):
                response = self.client.post(
                    "/auth/login",
                    data={
                        "email": "login@example.com",
                        "password": "NearFind123",
                        "csrf_token": self.csrf_from("/login"),
                    },
                    follow_redirects=False,
                )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/seeker/dashboard")
            self.assertEqual(post.call_args.args[0], "https://project-ref.supabase.co/auth/v1/token")
            self.assertEqual(post.call_args.kwargs["params"], {"grant_type": "password"})
            headers = post.call_args.kwargs["headers"]
            self.assertEqual(headers["apikey"], "sb_publishable_test-key")
            self.assertNotIn("Authorization", headers)
        finally:
            supabase.http.close()

    def test_legacy_anon_key_retains_bearer_header(self):
        legacy_anon_key = "eyJ.test.legacy-anon"
        headers = _headers(legacy_anon_key)
        self.assertEqual(headers["apikey"], legacy_anon_key)
        self.assertEqual(headers["Authorization"], f"Bearer {legacy_anon_key}")

    def test_rest_http_client_honors_configured_environment(self):
        supabase = RestSupabaseClient(
            "https://project-ref.supabase.co",
            "sb_publishable_test-key",
            "sb_publishable_test-key",
        )
        try:
            self.assertTrue(supabase.http._trust_env)
            self.assertEqual(supabase.http.timeout.connect, 20)
        finally:
            supabase.http.close()

    def test_public_client_uses_publishable_key_not_service_role_key(self):
        with patch.object(nearfind.Config, "SUPABASE_URL", "https://project-ref.supabase.co"), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", "sb_publishable_test-key"
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_server-only-key"):
            supabase = nearfind.get_supabase(admin=False)
            try:
                self.assertEqual(supabase.key, "sb_publishable_test-key")
                self.assertNotEqual(supabase.key, "sb_secret_server-only-key")
            finally:
                supabase.http.close()

    def test_legacy_public_client_passes_anon_key_to_supabase_sdk(self):
        legacy_anon_key = "eyJ.test.legacy-anon"
        with patch.object(nearfind.Config, "SUPABASE_URL", "https://project-ref.supabase.co"), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", legacy_anon_key
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", "legacy-server-only-key"), patch(
            "services.supabase_service.create_client",
            return_value=object(),
        ) as create_client:
            nearfind.get_supabase(admin=False)
        create_client.assert_called_once_with(
            "https://project-ref.supabase.co",
            legacy_anon_key,
        )

    def test_transport_error_preserves_safe_category_without_cause_details(self):
        try:
            raise socket.gaierror(socket.EAI_NONAME, "sensitive resolver detail")
        except socket.gaierror as cause:
            error = httpx.ConnectError("sensitive URL or request detail")
            error.__cause__ = cause

        class FailedAuth:
            def __init__(self, error):
                self.error = error

            def sign_up(self, credentials):
                raise self.error

        with self.assertRaises(SupabaseConnectionError) as raised:
            supabase_auth_request(
                FailedAuth(error),
                "sign_up",
                {"email": "test@example.com", "password": "secret"},
            )
        self.assertEqual(raised.exception.diagnostic, "dns")
        self.assertEqual(raised.exception.exception_types, ("ConnectError", "gaierror"))
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_transport_error_categories_are_distinguished_from_http_errors(self):
        self.assertEqual(_transport_diagnostic(httpx.ConnectError("opaque")), "connect")
        self.assertEqual(_transport_diagnostic(httpx.ProxyError("opaque")), "proxy")
        self.assertEqual(_transport_diagnostic(httpx.ConnectTimeout("opaque")), "timeout")

        for cause, expected in (
            (ssl.SSLError("opaque TLS detail"), "tls"),
            (socket.gaierror(socket.EAI_NONAME, "opaque DNS detail"), "dns"),
            (ConnectionRefusedError("opaque connection detail"), "connection_refused"),
        ):
            with self.subTest(expected=expected):
                try:
                    raise cause
                except OSError as cause_error:
                    try:
                        raise httpx.ConnectError("opaque connect detail") from cause_error
                    except httpx.ConnectError as error:
                        self.assertEqual(_transport_diagnostic(error), expected)

        self.assertEqual(
            _transport_diagnostic(httpx.ReadError("opaque network detail")),
            "network",
        )

    def test_login_connection_failure_returns_sanitized_service_unavailable(self):
        self.csrf_from("/login")
        secret_details = "dns failure https://project-ref.supabase.co password=private-password"
        with patch.object(nearfind.Config, "SUPABASE_URL", "https://project-ref.supabase.co"), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", "private-anon-key"
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", "private-service-key"), patch.object(
            nearfind, "get_supabase", return_value=FakeSupabase()
        ), patch.object(FakeAuth, "sign_in_with_password", side_effect=httpx.ConnectError(secret_details)):
            with self.assertLogs(nearfind.app.logger, level="ERROR") as captured:
                response = self.client.post(
                    "/auth/login",
                    data={
                        "email": "login@example.com",
                        "password": "private-password",
                        "csrf_token": self.csrf_from("/login"),
                    },
                )
        body = response.get_data(as_text=True)
        logs = "\n".join(captured.output)
        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", body)
        self.assertNotIn(secret_details, body)
        self.assertNotIn("private-password", body + logs)
        self.assertNotIn("private-anon-key", body + logs)
        self.assertNotIn("private-service-key", body + logs)
        self.assertIn("diagnostic=connect", logs)
        self.assertIn("exception_types=ConnectError", logs)
        self.assertNotIn(secret_details, logs)

    def test_signup_connection_failure_returns_sanitized_service_unavailable(self):
        self.csrf_from("/signup")
        secret_details = "dns failure https://project-ref.supabase.co password=private-password"
        with patch.object(nearfind.Config, "SUPABASE_URL", "https://project-ref.supabase.co"), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", "private-anon-key"
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", "private-service-key"), patch.object(
            nearfind, "get_supabase", return_value=FakeSupabase()
        ), patch.object(FakeAuth, "sign_up", side_effect=httpx.ConnectError(secret_details)):
            with self.assertLogs(nearfind.app.logger, level="ERROR") as captured:
                response = self.client.post(
                    "/auth/signup",
                    data={
                        "name": "Signup User",
                        "email": "signup@example.com",
                        "password": "private-password",
                        "phone": "9999999999",
                        "address": "Signup Address",
                        "role": "seeker",
                        "csrf_token": self.csrf_from("/signup"),
                    },
                )
        body = response.get_data(as_text=True)
        logs = "\n".join(captured.output)
        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", body)
        self.assertNotIn(secret_details, body)
        self.assertNotIn("private-password", body + logs)
        self.assertNotIn("private-anon-key", body + logs)
        self.assertNotIn("private-service-key", body + logs)
        self.assertIn("diagnostic=connect", logs)
        self.assertIn("exception_types=ConnectError", logs)

    def test_signup_disabled_by_supabase_has_actionable_message(self):
        self.csrf_from("/signup")
        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase()), patch.object(
            FakeAuth,
            "sign_up",
            side_effect=nearfind.SupabaseRequestError(
                403,
                "Sign-ups are currently paused for this project",
                code="signup_disabled",
            ),
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
                    "csrf_token": self.csrf_from("/signup"),
                },
            )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Sign-ups are currently disabled", body)
        self.assertIn("project administrator", body)
        self.assertNotIn("paused for this project", body)

    def test_signup_invalid_public_key_suggests_configuration_check(self):
        self.csrf_from("/signup")
        with patch.object(nearfind, "get_supabase", return_value=FakeSupabase()), patch.object(
            FakeAuth,
            "sign_up",
            side_effect=nearfind.SupabaseRequestError(401, "Invalid API key", code="invalid_api_key"),
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
                    "csrf_token": self.csrf_from("/signup"),
                },
            )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 503)
        self.assertIn("SUPABASE_URL", body)
        self.assertIn("SUPABASE_ANON_KEY", body)
        self.assertNotIn("Invalid API key", body)

    def test_signup_missing_configuration_returns_safe_actionable_error(self):
        self.csrf_from("/signup")
        with patch.object(nearfind.Config, "SUPABASE_ANON_KEY", ""), patch.object(
            nearfind, "get_supabase"
        ) as get_supabase:
            response = self.client.post(
                "/auth/signup",
                data={
                    "name": "Signup User",
                    "email": "signup@example.com",
                    "password": "NearFind123",
                    "phone": "9999999999",
                    "address": "Signup Address",
                    "role": "seeker",
                    "csrf_token": self.csrf_from("/signup"),
                },
            )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 503)
        self.assertIn("SUPABASE_ANON_KEY", body)
        self.assertNotIn("private-anon-key", body)
        get_supabase.assert_not_called()

    def test_signup_invalid_supabase_url_returns_configuration_error_without_request(self):
        self.csrf_from("/signup")
        with patch.object(nearfind.Config, "SUPABASE_URL", "not-a-url"):
            response = self.client.post(
                "/auth/signup",
                data={
                    "name": "Signup User",
                    "email": "signup@example.com",
                    "password": "NearFind123",
                    "phone": "9999999999",
                    "address": "Signup Address",
                    "role": "seeker",
                    "csrf_token": self.csrf_from("/signup"),
                },
            )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 503)
        self.assertIn("SUPABASE_URL must be a valid HTTP(S) URL", body)
        self.assertNotIn("not-a-url", body)

    def test_supabase_signup_error_code_is_preserved(self):
        response = SimpleNamespace(
            status_code=403,
            json=lambda: {"code": "signup_disabled", "message": "Sign-ups are paused"},
            text="",
        )
        with self.assertRaises(nearfind.SupabaseRequestError) as raised:
            _raise_for_supabase(response)
        self.assertEqual(raised.exception.code, "signup_disabled")
        self.assertEqual(raised.exception.status_code, 403)

    def test_query_lifecycle_reuses_one_query_id_through_resolution(self):
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        seeker_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        provider_id = "22222222-2222-4222-8222-222222222222"
        response_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        store = {
            "queries": [],
            "responses": [],
            "users": [{"id": provider_id, "lat": 26.761, "lng": 83.374, "role": "provider", "is_active": True}],
            "notifications": [],
            "conversations": [],
        }

        class LifecycleResult:
            def __init__(self, data):
                self.data = data

        class LifecycleTable:
            def __init__(self, name):
                self.name = name
                self.filters = []
                self.payload = None
                self.single_row = False

            def select(self, columns):
                return self

            def insert(self, payload):
                self.payload = payload
                return self

            def update(self, payload):
                self.payload = payload
                return self

            def eq(self, column, value):
                self.filters.append((column, value))
                return self

            def order(self, column, desc=False):
                return self

            def maybe_single(self):
                self.single_row = True
                return self

            def single(self):
                self.single_row = True
                return self

            def execute(self):
                if self.payload is not None and self.name == "queries":
                    if self.filters:
                        rows = [row for row in store["queries"] if all(row.get(column) == value for column, value in self.filters)]
                        for row in rows:
                            row.update(self.payload)
                        return LifecycleResult(rows)
                    store["queries"].append(dict(self.payload))
                    return LifecycleResult([self.payload])
                if self.payload is not None and self.name == "responses":
                    rows = [row for row in store["responses"] if all(row.get(column) == value for column, value in self.filters)]
                    if not self.filters:
                        store["responses"].append(dict(self.payload))
                        return LifecycleResult([self.payload])
                    for row in rows:
                        row.update(self.payload)
                    return LifecycleResult(rows)
                if self.payload is not None and self.name == "notifications":
                    store["notifications"].append(dict(self.payload))
                    return LifecycleResult([self.payload])
                if self.payload is not None and self.name == "conversations":
                    store["conversations"].append(dict(self.payload))
                    return LifecycleResult([self.payload])
                rows = store[self.name]
                rows = [row for row in rows if all(row.get(column) == value for column, value in self.filters)]
                if self.single_row:
                    return LifecycleResult(rows[0] if rows else None)
                return LifecycleResult(rows)

        class LifecycleRpc:
            def rpc(self, name, payload):
                self.payload = payload
                return self

            def execute(self):
                query = next(row for row in store["queries"] if row["id"] == self.payload["p_query_id"])
                response = next(row for row in store["responses"] if row["id"] == self.payload["p_response_id"])
                query["status"] = "matched"
                response["status"] = "selected"
                return LifecycleResult({"status": "matched"})

        def lifecycle_table(name):
            return LifecycleTable(name)

        def lifecycle_owner(query_id):
            return next((row for row in store["queries"] if row["id"] == query_id and row["seeker_id"] == seeker_id), None)

        def lifecycle_query(query_id):
            return next((row for row in store["queries"] if row["id"] == query_id), None)

        with patch.object(nearfind, "table", side_effect=lifecycle_table), patch.object(
            nearfind, "expire_open_queries"
        ), patch.object(nearfind, "get_query", side_effect=lifecycle_query), patch.object(
            nearfind, "require_query_owner", side_effect=lifecycle_owner
        ), patch.object(
            nearfind.uuid, "uuid4", side_effect=[uuid.UUID(query_id), uuid.UUID(response_id)]
        ), patch.object(nearfind, "get_supabase", return_value=LifecycleRpc()):
            self.login_as(role="seeker", user_id=seeker_id)
            created = self.client.post(
                "/query/post",
                data={
                    "item_name": "Lifecycle Charger",
                    "category": "electronics",
                    "description": "Need a charger",
                    "location": "Golghar",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "radius_km": "5",
                    "urgency": "normal",
                    "expiration": "6",
                    "csrf_token": "known-csrf-token",
                },
                follow_redirects=False,
            )
            self.assertEqual(created.status_code, 302)
            self.assertEqual(len(store["queries"]), 1)
            self.assertEqual(store["queries"][0]["id"], query_id)
            self.assertEqual(store["queries"][0]["status"], "open")
            self.assertEqual(len(store["notifications"]), 1)
            self.assertEqual(store["notifications"][0]["type"], "new_request")
            self.assertEqual(store["notifications"][0]["query_id"], query_id)
            self.assertFalse(store["notifications"][0]["is_read"])

            self.login_as(role="provider", user_id=provider_id)
            with patch.object(nearfind, "current_profile", return_value={"is_active": True, "lat": 26.761, "lng": 83.374}):
                responded = self.client.post(
                    "/query/respond",
                    data={"query_id": query_id, "message": "Available", "price": "Rs 850", "csrf_token": "known-csrf-token"},
                    follow_redirects=False,
                )
            self.assertEqual(responded.status_code, 302)
            store["responses"][0]["id"] = response_id
            self.assertEqual(store["responses"][0]["query_id"], query_id)
            self.assertEqual(store["responses"][0]["status"], "available")
            self.assertEqual(store["queries"][0]["status"], "open")
            self.assertEqual(len(store["notifications"]), 2)
            self.assertEqual(store["notifications"][1]["type"], "provider_response")
            self.assertEqual(store["notifications"][1]["query_id"], query_id)
            self.assertEqual(store["notifications"][1]["response_id"], response_id)

            self.login_as(role="seeker", user_id=seeker_id)
            selected = self.client.post(
                "/query/select-provider",
                json={"query_id": query_id, "response_id": response_id},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(store["queries"][0]["status"], "matched")
            self.assertEqual(store["responses"][0]["query_id"], query_id)
            self.assertEqual(store["responses"][0]["status"], "selected")
            self.assertEqual(len(store["notifications"]), 3)
            self.assertEqual(store["notifications"][2]["type"], "provider_selected")
            self.assertEqual(store["notifications"][2]["query_id"], query_id)
            self.assertEqual(store["notifications"][2]["response_id"], response_id)

            resolved = self.client.post(
                "/query/resolve",
                json={"query_id": query_id},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
            self.assertEqual(resolved.status_code, 200)
            self.assertEqual(store["queries"][0]["id"], query_id)
            self.assertEqual(store["queries"][0]["status"], "resolved")
            self.assertEqual(len(store["queries"]), 1)
            self.assertEqual(len(store["notifications"]), 4)
            self.assertEqual(store["notifications"][3]["type"], "request_resolved")
            self.assertEqual(store["notifications"][3]["query_id"], query_id)
            self.assertEqual(store["notifications"][3]["response_id"], response_id)

            self.login_as(role="provider", user_id=provider_id)
            with patch.object(nearfind, "current_profile", return_value={"is_active": True, "lat": 26.761, "lng": 83.374}):
                provider_list = self.client.get("/query/list?radius_km=5")
            self.assertEqual(provider_list.status_code, 200)
            self.assertEqual(provider_list.json["data"]["queries"], [])

            self.login_as(role="seeker", user_id=seeker_id)
            dashboard = self.client.get("/seeker/dashboard")
            body = dashboard.get_data(as_text=True)
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn("Lifecycle Charger", body)
            self.assertIn("resolved", body)
            active_section = body.split("<h2>Active Requests</h2>", 1)[1].split("<h2>Matched</h2>", 1)[0]
            completed_section = body.split("<h2>Completed</h2>", 1)[1].split("<h2>Expired</h2>", 1)[0]
            self.assertNotIn("Lifecycle Charger", active_section)
            self.assertIn("Lifecycle Charger", completed_section)

    def test_signup_profile_failure_clears_session_and_returns_safe_error(self):
        self.csrf_from("/signup")
        with patch.object(nearfind.Config, "SUPABASE_URL", "https://example.supabase.co"), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", "anon-key"
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", "service-key"), patch.object(
            nearfind, "get_supabase", return_value=FakeSupabase()
        ), patch.object(nearfind, "table", side_effect=RuntimeError("database internals leaked")):
            response = self.client.post(
                "/auth/signup",
                data={
                    "name": "Signup User",
                    "email": "failure@example.com",
                    "password": "NearFind123",
                    "phone": "9999999999",
                    "address": "Signup Address",
                    "role": "seeker",
                    "csrf_token": self.csrf_from("/signup"),
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("database internals leaked", response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_post_query_page_renders_multipart_form_with_retry_identifier(self):
        self.login_as(role="seeker")
        response = self.client.get("/seeker/post")
        body = response.get_data(as_text=True)
        query_id_match = re.search(r'name="query_id" value="([0-9a-f-]{36})"', body)
        self.assertEqual(response.status_code, 200)
        self.assertIn('enctype="multipart/form-data"', body)
        self.assertIsNotNone(query_id_match)
        self.assertTrue(nearfind.valid_uuid(query_id_match.group(1)))

    def test_notifications_are_scoped_and_mark_read_only_for_owner(self):
        notification_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        rows = [
            {"id": notification_id, "user_id": "11111111-1111-4111-8111-111111111111", "type": "provider_response", "title": "Response", "message": "A response arrived", "query_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "response_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "is_read": False, "created_at": "2026-09-07T10:00:00Z"},
            {"id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "user_id": "99999999-9999-4999-8999-999999999999", "type": "new_request", "title": "Other", "message": "Private", "query_id": "ffffffff-ffff-4fff-8fff-ffffffffffff", "response_id": None, "is_read": False, "created_at": "2026-09-07T10:00:00Z"},
        ]

        class NotificationTable(FakeRowsTable):
            def limit(self, count):
                return self

            def execute(self):
                filtered = [row for row in self.rows if all(row.get(column) == value for column, value in self.filters)]
                if self.updated:
                    for row in filtered:
                        row.update(self.updated[-1])
                    return SimpleNamespace(data=filtered)
                return SimpleNamespace(data=filtered)

        self.login_as(user_id="11111111-1111-4111-8111-111111111111")
        with patch.object(nearfind, "table", return_value=NotificationTable(rows=rows)):
            response = self.client.get("/notifications")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["data"]["unread_count"], 1)
        self.assertEqual([item["id"] for item in response.json["data"]["notifications"]], [notification_id])

        with patch.object(nearfind, "table", return_value=NotificationTable(rows=rows)):
            marked = self.client.post(f"/notifications/{notification_id}/read", headers={"X-CSRFToken": "known-csrf-token"})
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(rows[0]["is_read"])

        with patch.object(nearfind, "table", return_value=NotificationTable(rows=rows)):
            marked_again = self.client.post(f"/notifications/{notification_id}/read", headers={"X-CSRFToken": "known-csrf-token"})
        self.assertEqual(marked_again.status_code, 200)
        self.assertTrue(rows[0]["is_read"])

        with patch.object(nearfind, "table", return_value=NotificationTable(rows=rows)):
            forbidden = self.client.post(
                "/notifications/eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee/read",
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(forbidden.status_code, 404)
        self.assertFalse(rows[1]["is_read"])

    def test_notifications_require_authentication_and_csrf_for_writes(self):
        self.assertEqual(self.client.get("/notifications").status_code, 401)
        self.login_as(user_id="11111111-1111-4111-8111-111111111111")
        with patch.object(nearfind, "table") as table_mock:
            response = self.client.post("/notifications/cccccccc-cccc-4ccc-8ccc-cccccccccccc/read")
        self.assertEqual(response.status_code, 403)
        table_mock.assert_not_called()

    def test_notification_identity_is_stable_for_duplicate_events(self):
        args = ("11111111-1111-4111-8111-111111111111", "provider_response", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
        self.assertEqual(nearfind.notification_id(*args), nearfind.notification_id(*args))
        self.assertNotEqual(nearfind.notification_id(*args), nearfind.notification_id(args[0], args[1], args[2], "dddddddd-dddd-4ddd-8ddd-dddddddddddd"))

    def test_chat_requires_membership_and_uses_session_sender(self):
        conversation = {
            "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "query_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "response_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "seeker_id": "11111111-1111-4111-8111-111111111111",
            "provider_id": "22222222-2222-4222-8222-222222222222",
        }
        messages = FakeRowsTable()
        self.login_as(role="seeker", user_id=conversation["seeker_id"])
        with patch.object(nearfind, "get_conversation_member", return_value=conversation), patch.object(
            nearfind, "table", return_value=messages
        ), patch.object(nearfind, "create_notification") as notify:
            response = self.client.post(
                f"/chat/{conversation['id']}/messages",
                json={"message": "Hello", "sender_id": conversation["provider_id"]},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(messages.inserted[0]["sender_id"], conversation["seeker_id"])
        self.assertEqual(messages.inserted[0]["conversation_id"], conversation["id"])
        self.assertEqual(notify.call_args.kwargs["identity_id"], messages.inserted[0]["id"])

        with patch.object(nearfind, "get_conversation_member", return_value=None):
            forbidden = self.client.get(f"/chat/{conversation['id']}/messages")
        self.assertEqual(forbidden.status_code, 404)

    def test_chat_rejects_empty_and_oversized_messages(self):
        conversation = {
            "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "query_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "response_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "seeker_id": "11111111-1111-4111-8111-111111111111",
            "provider_id": "22222222-2222-4222-8222-222222222222",
        }
        self.login_as(user_id=conversation["seeker_id"])
        with patch.object(nearfind, "get_conversation_member", return_value=conversation), patch.object(nearfind, "table") as table_mock:
            empty = self.client.post(
                f"/chat/{conversation['id']}/messages",
                json={"message": "   "},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
            oversized = self.client.post(
                f"/chat/{conversation['id']}/messages",
                json={"message": "x" * 2001},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(oversized.status_code, 400)
        table_mock.assert_not_called()

    def test_chat_message_succeeds_when_notification_write_fails(self):
        conversation = {
            "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "query_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "response_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "seeker_id": "11111111-1111-4111-8111-111111111111",
            "provider_id": "22222222-2222-4222-8222-222222222222",
        }
        messages = FakeRowsTable()
        self.login_as(role="seeker", user_id=conversation["seeker_id"])
        with patch.object(nearfind, "get_conversation_member", return_value=conversation), patch.object(
            nearfind, "table", return_value=messages
        ), patch.object(nearfind, "create_notification", side_effect=RuntimeError("notifications unavailable")):
            response = self.client.post(
                f"/chat/{conversation['id']}/messages",
                json={"message": "Still send this"},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["data"]["message"]["message"], "Still send this")

    def test_chat_frontend_owns_send_state_and_restores_it(self):
        script_path = nearfind.__file__.replace("app.py", "static/js/chat.js")
        with open(script_path, encoding="utf-8") as script_file:
            script = script_file.read()
        self.assertIn("let sending = false", script)
        self.assertIn("sending = true", script)
        self.assertIn("sending = false", script)
        self.assertIn('send.textContent = "Please wait..."', script)
        self.assertIn("form.dataset.submitting = \"false\"", script)
        self.assertIn('form.dataset.submitting = "false"', script)

    def test_response_comparison_frontend_handles_sorting_and_missing_values(self):
        script_path = nearfind.__file__.replace("app.py", "static/js/seeker-map.js")
        template_path = nearfind.__file__.replace("app.py", "templates/seeker/responses.html")
        with open(script_path, encoding="utf-8") as script_file, open(template_path, encoding="utf-8") as template_file:
            script = script_file.read()
            template = template_file.read()
        self.assertIn('option value="price"', template)
        self.assertIn('return "Price not provided"', script)
        self.assertIn('return "Distance unavailable"', script)
        self.assertIn("selectingResponses.has(responseId)", script)
        self.assertIn("No providers have responded yet", script)
        self.assertIn('img.addEventListener("error"', script)

    def test_duplicate_response_conflict_updates_existing_row_without_duplicate_notification(self):
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        provider_id = "22222222-2222-4222-8222-222222222222"
        existing_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        query = {"id": query_id, "status": "open", "seeker_id": "11111111-1111-4111-8111-111111111111", "lat": 26.7606, "lng": 83.3732, "radius_km": 5, "item_name": "Charger"}
        existing = {"id": existing_id, "image_url": None}
        class RaceResponseTable(FakeRowsTable):
            def __init__(self):
                super().__init__()
                self.lookup_count = 0

            def insert(self, payload):
                raise nearfind.SupabaseRequestError(
                    409,
                    "duplicate key value violates unique constraint responses_one_active_per_provider_query",
                )

            def execute(self):
                if self.updated:
                    return SimpleNamespace(data=self.updated[-1])
                if self.selected_columns:
                    self.lookup_count += 1
                    return SimpleNamespace(data=existing if self.lookup_count > 1 else None)
                return SimpleNamespace(data=None)

        responses = RaceResponseTable()
        self.login_as(role="provider", user_id=provider_id)
        with patch.object(nearfind, "expire_open_queries"), patch.object(nearfind, "get_query", return_value=query), patch.object(
            nearfind, "current_profile", return_value={"is_active": True, "lat": 26.761, "lng": 83.374}
        ), patch.object(nearfind, "table", side_effect=[responses, responses, responses, responses]), patch.object(
            nearfind, "notify_provider_for_response"
        ) as notify:
            result = self.client.post(
                "/query/respond",
                data={"query_id": query_id, "message": "Updated", "price": "850", "csrf_token": "known-csrf-token"},
                headers={"Accept": "application/json"},
            )
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json["data"]["response_id"], existing_id)
        self.assertEqual(responses.updated[-1]["message"], "Updated")
        notify.assert_not_called()

    def test_provider_can_update_only_own_latest_location(self):
        provider_id = "22222222-2222-4222-8222-222222222222"
        self.login_as(role="provider", user_id=provider_id)
        location = FakeRowsTable(single=None)
        with patch.object(nearfind, "current_profile", return_value={"role": "provider", "is_active": True}), patch.object(
            nearfind, "table", return_value=location
        ):
            response = self.client.post(
                "/location/update",
                json={"lat": 26.761, "lng": 83.374, "accuracy_m": 12, "sharing_enabled": True},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(location.inserted[0]["provider_id"], provider_id)
        self.assertTrue(location.inserted[0]["sharing_enabled"])

    def test_provider_location_update_reuses_existing_row(self):
        provider_id = "22222222-2222-4222-8222-222222222222"
        self.login_as(role="provider", user_id=provider_id)
        location = FakeRowsTable(single={"provider_id": provider_id})
        with patch.object(nearfind, "current_profile", return_value={"role": "provider", "is_active": True}), patch.object(
            nearfind, "table", return_value=location
        ):
            response = self.client.post(
                "/location/update",
                json={"lat": 26.762, "lng": 83.375, "accuracy_m": None, "sharing_enabled": True},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(location.inserted, [])
        self.assertEqual(location.updated[0]["provider_id"], provider_id)

    def test_location_update_requires_provider_authentication_and_role(self):
        self.assertEqual(self.client.post("/location/update", json={"lat": 26.7, "lng": 83.3}).status_code, 403)
        self.login_as(role="seeker")
        response = self.client.post(
            "/location/update",
            json={"lat": 26.7, "lng": 83.3},
            headers={"X-CSRFToken": "known-csrf-token"},
        )
        self.assertEqual(response.status_code, 403)

    def test_location_update_rejects_invalid_coordinates_and_accuracy(self):
        self.login_as(role="provider")
        with patch.object(nearfind, "current_profile", return_value={"role": "provider", "is_active": True}), patch.object(
            nearfind, "table"
        ) as table_mock:
            invalid_lat = self.client.post("/location/update", json={"lat": 91, "lng": 83.3}, headers={"X-CSRFToken": "known-csrf-token"})
            invalid_lng = self.client.post("/location/update", json={"lat": 26.7, "lng": 181}, headers={"X-CSRFToken": "known-csrf-token"})
            invalid_accuracy = self.client.post("/location/update", json={"lat": 26.7, "lng": 83.3, "accuracy_m": -1}, headers={"X-CSRFToken": "known-csrf-token"})
        self.assertEqual(invalid_lat.status_code, 400)
        self.assertEqual(invalid_lng.status_code, 400)
        self.assertEqual(invalid_accuracy.status_code, 400)
        table_mock.assert_not_called()

    def test_seeker_reads_only_selected_provider_location_for_owned_matched_query(self):
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.login_as(role="seeker", user_id="11111111-1111-4111-8111-111111111111")
        query = {"id": query_id, "seeker_id": "11111111-1111-4111-8111-111111111111", "status": "matched", "lat": 26.7606, "lng": 83.3732}
        selected = {"provider_id": "22222222-2222-4222-8222-222222222222"}
        location = {"lat": 26.761, "lng": 83.374, "accuracy_m": 10, "updated_at": "2026-09-07T10:00:00Z", "sharing_enabled": True}
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(
            nearfind, "table", side_effect=[FakeRowsTable(single=selected), FakeRowsTable(single=location)]
        ):
            response = self.client.get(f"/query/{query_id}/provider-location")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["data"]["lat"], location["lat"])
        self.assertIn("distance_km", response.json["data"])

    def test_resolved_query_does_not_expose_provider_location(self):
        self.login_as(role="seeker", user_id="11111111-1111-4111-8111-111111111111")
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "resolved"}
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(nearfind, "table") as table_mock:
            response = self.client.get("/query/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/provider-location")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json["data"])
        table_mock.assert_not_called()

    def test_disabled_provider_sharing_returns_no_location(self):
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.login_as(role="seeker", user_id="11111111-1111-4111-8111-111111111111")
        query = {"id": query_id, "seeker_id": "11111111-1111-4111-8111-111111111111", "status": "matched", "lat": 26.7606, "lng": 83.3732}
        selected = {"provider_id": "22222222-2222-4222-8222-222222222222"}
        location = {"lat": 26.761, "lng": 83.374, "accuracy_m": 10, "updated_at": "2026-09-07T10:00:00Z", "sharing_enabled": False}
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(
            nearfind, "table", side_effect=[FakeRowsTable(single=selected), FakeRowsTable(single=location)]
        ):
            response = self.client.get(f"/query/{query_id}/provider-location")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json["data"])

    def test_provider_location_stop_disables_sharing(self):
        self.login_as(role="provider", user_id="22222222-2222-4222-8222-222222222222")
        location = FakeRowsTable()
        with patch.object(nearfind, "current_profile", return_value={"role": "provider", "is_active": True}), patch.object(
            nearfind, "table", return_value=location
        ):
            response = self.client.post("/location/stop", headers={"X-CSRFToken": "known-csrf-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(location.updated[0]["sharing_enabled"], False)

    def test_conversation_identity_is_reused_for_selected_response(self):
        response_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        rows = []

        class ConversationTable(FakeRowsTable):
            def execute(self):
                if self.inserted:
                    rows.append(self.inserted[-1])
                    return SimpleNamespace(data=self.inserted[-1])
                return SimpleNamespace(data=next((row for row in rows if row["response_id"] == response_id), None))

        with patch.object(nearfind, "table", side_effect=[ConversationTable(), ConversationTable(), ConversationTable()]):
            first = nearfind.ensure_conversation(
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                response_id,
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            )
            second = nearfind.ensure_conversation(
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                response_id,
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(rows), 1)

    def test_new_request_notifications_require_nearby_active_providers(self):
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "item_name": "Nearby item", "lat": 26.7606, "lng": 83.3732, "radius_km": 5}
        users = [
            {"id": "11111111-1111-4111-8111-111111111111", "role": "provider", "is_active": True, "lat": 26.761, "lng": 83.374},
            {"id": "22222222-2222-4222-8222-222222222222", "role": "provider", "is_active": True, "lat": 27.4, "lng": 84.2},
            {"id": "33333333-3333-4333-8333-333333333333", "role": "provider", "is_active": False, "lat": 26.761, "lng": 83.374},
        ]
        with patch.object(nearfind, "table", return_value=FakeRowsTable(rows=users)), patch.object(
            nearfind, "create_notification"
        ) as notify:
            nearfind.notify_nearby_providers(query)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[0], users[0]["id"])
        self.assertEqual(notify.call_args.args[4], query["id"])

    def test_expiry_update_rechecks_open_status(self):
        expired = FakeRowsTable(rows=[{"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}])
        updates = FakeRowsTable()
        with patch.object(nearfind, "table", side_effect=[expired, updates]):
            nearfind.expire_open_queries()
        self.assertIn(("status", "open"), updates.filters)

    def test_resolve_updates_only_owned_matched_query(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "matched"}
        updates = FakeRowsTable()
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(
            nearfind, "table", return_value=updates
        ):
            response = self.client.post(
                "/query/resolve",
                json={"query_id": query["id"]},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(updates.updated, [{"status": "resolved"}])
        self.assertIn(("seeker_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), updates.filters)
        self.assertIn(("status", "matched"), updates.filters)

    def test_logout_is_post_only_and_requires_csrf(self):
        self.login_as()
        self.assertEqual(self.client.get("/auth/logout").status_code, 405)
        self.assertEqual(self.client.post("/auth/logout").status_code, 403)
        response = self.client.post("/auth/logout", data={"csrf_token": "known-csrf-token"})
        self.assertEqual(response.status_code, 302)

    def test_session_security_settings_are_enabled(self):
        self.assertTrue(nearfind.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(nearfind.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertEqual(nearfind.app.config["PERMANENT_SESSION_LIFETIME"], timedelta(hours=8))

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
        response_table = FakeRowsTable(rows)
        with patch.object(nearfind, "table", side_effect=[FakeRowsTable(single=query), response_table]):
            response = self.client.get(f"/query/responses/{query_id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json["data"]["responses"][0]
        self.assertIn("users!responses_provider_id_fkey(", response_table.selected_columns[0])
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

    def test_rejected_provider_does_not_receive_sensitive_contact_or_exact_location(self):
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": query_id, "seeker_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "status": "open", "lat": 26.7606, "lng": 83.3732}
        rows = [
            {
                "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "query_id": query_id,
                "provider_id": "22222222-2222-4222-8222-222222222222",
                "message": "No longer available",
                "price": "Rs 100",
                "status": "rejected",
                "users": {
                    "name": "Rejected Provider",
                    "business_name": "Private Shop",
                    "provider_type": "shop",
                    "phone": "9999999999",
                    "lat": 26.7610,
                    "lng": 83.3740,
                },
            }
        ]
        with patch.object(nearfind, "table", side_effect=[FakeRowsTable(single=query), FakeRowsTable(rows)]):
            response = self.client.get(f"/query/responses/{query_id}")
        payload = response.json["data"]["responses"][0]
        self.assertIsNone(payload["phone"])
        self.assertIsNone(payload["provider_lat"])
        self.assertIsNone(payload["provider_lng"])
        self.assertEqual(payload["provider_name"], "Rejected Provider")

    def test_seeker_cannot_select_provider_for_another_seekers_query(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        with patch.object(nearfind, "require_query_owner", return_value=None), patch.object(nearfind, "get_supabase") as supabase_mock:
            response = self.client.post(
                "/query/select-provider",
                json={"query_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "response_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 404)
        supabase_mock.assert_not_called()

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
                "status": "open",
                "image_url": None,
                "created_at": "2026-09-05T10:00:00Z",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
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
                "status": "open",
                "image_url": None,
                "created_at": "2026-09-05T10:00:00Z",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
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
        self.assertEqual(payload[0]["status"], "open")
        self.assertGreater(payload[0]["distance"], 0)
        self.assertTrue(payload[0]["distance_label"].endswith("away"))

    def test_provider_query_list_keeps_short_nonzero_distance_and_uses_meter_label(self):
        self.login_as(role="provider", user_id="22222222-2222-4222-8222-222222222222")
        provider_lat, provider_lng = 26.7606, 83.3732
        seeker_lat, seeker_lng = 26.76061, 83.3732
        provider = {
            "id": "22222222-2222-4222-8222-222222222222",
            "role": "provider",
            "is_active": True,
            "lat": provider_lat,
            "lng": provider_lng,
        }
        row = {
            "id": "11111111-1111-4111-8111-111111111111",
            "item_name": "USB cable",
            "category": "electronics",
            "description": "Need one cable",
            "users": {"name": "Seeker One"},
            "location": "Golghar",
            "lat": seeker_lat,
            "lng": seeker_lng,
            "radius_km": 5,
            "urgency": "urgent",
            "status": "open",
            "created_at": "2026-09-05T10:00:00Z",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        expected_distance = haversine(provider_lat, provider_lng, seeker_lat, seeker_lng)
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable([row])):
            response = self.client.get("/query/list?radius_km=5")
        result = response.json["data"]["queries"][0]
        self.assertAlmostEqual(result["distance"], expected_distance)
        self.assertGreater(result["distance"], 0)
        self.assertEqual(result["distance_label"], "1 m away")

    def test_provider_query_list_requires_saved_coordinates(self):
        self.login_as(role="provider", user_id="22222222-2222-4222-8222-222222222222")
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value={"is_active": True, "lat": None, "lng": 83.3732}
        ), patch.object(nearfind, "table") as table_mock:
            response = self.client.get("/query/list")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Update your location", response.json["error"]["message"])
        table_mock.assert_not_called()

    def test_provider_query_list_rejects_invalid_saved_coordinates_and_skips_invalid_request_rows(self):
        self.login_as(role="provider", user_id="22222222-2222-4222-8222-222222222222")
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value={"is_active": True, "lat": 91, "lng": 83.3732}
        ), patch.object(nearfind, "table") as table_mock:
            invalid_provider_response = self.client.get("/query/list")
        self.assertEqual(invalid_provider_response.status_code, 400)
        table_mock.assert_not_called()

        provider = {"is_active": True, "lat": 26.7606, "lng": 83.3732}
        invalid_row = {
            "id": "11111111-1111-4111-8111-111111111111",
            "lat": float("nan"),
            "lng": 83.3732,
            "status": "open",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable([invalid_row])):
            invalid_row_response = self.client.get("/query/list")
        self.assertEqual(invalid_row_response.status_code, 200)
        self.assertEqual(invalid_row_response.json["data"]["queries"], [])

    def test_distance_labels_use_meters_for_short_distances(self):
        self.assertEqual(haversine(26.7606, 83.3732, 26.7606, 83.3732), 0)
        self.assertEqual(format_distance(0), "0 m away")
        self.assertEqual(format_distance(0.0004), "<1 m away")
        self.assertEqual(format_distance(0.025), "25 m away")
        self.assertEqual(format_distance(1.234), "1.23 km away")
        self.assertGreater(haversine(26.7606, 83.3732, 27.0, 84.0), 0)

    def test_invalid_coordinates_cannot_become_a_zero_distance(self):
        for coordinates in (
            (None, 83.3732),
            ("not-a-coordinate", 83.3732),
            (float("nan"), 83.3732),
            (26.7606, float("inf")),
            (91, 83.3732),
            (26.7606, 181),
        ):
            with self.subTest(coordinates=coordinates):
                self.assertFalse(valid_coordinates(*coordinates))
                with self.assertRaises(ValueError):
                    haversine(*coordinates, 26.7606, 83.3732)
        with self.assertRaises(ValueError):
            format_distance(float("nan"))
        with self.assertRaises(ValueError):
            format_distance(-1)

    def test_provider_query_list_excludes_expired_rows_after_cleanup(self):
        self.login_as(role="provider", user_id="22222222-2222-4222-8222-222222222222")
        provider = {"is_active": True, "lat": 26.7606, "lng": 83.3732}
        rows = [{
            "id": "11111111-1111-4111-8111-111111111111",
            "item_name": "Expired cable",
            "category": "electronics",
            "description": "No longer active",
            "users": {"name": "Seeker One"},
            "location": "Golghar",
            "lat": 26.7610,
            "lng": 83.3740,
            "radius_km": 5,
            "urgency": "urgent",
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }]
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable(rows)):
            response = self.client.get("/query/list?radius_km=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["data"]["queries"], [])

    def test_provider_query_list_excludes_resolved_rows(self):
        self.login_as(role="provider", user_id="22222222-2222-4222-8222-222222222222")
        provider = {"is_active": True, "lat": 26.7606, "lng": 83.3732}
        rows = [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "item_name": "Open cable",
                "category": "electronics",
                "description": "Still needed",
                "users": {"name": "Seeker One"},
                "location": "Golghar",
                "lat": 26.7610,
                "lng": 83.3740,
                "radius_km": 5,
                "urgency": "urgent",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "status": "open",
            },
            {
                "id": "33333333-3333-4333-8333-333333333333",
                "item_name": "Resolved cable",
                "category": "electronics",
                "description": "Already completed",
                "users": {"name": "Seeker Two"},
                "location": "Golghar",
                "lat": 26.7610,
                "lng": 83.3740,
                "radius_km": 5,
                "urgency": "normal",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "status": "resolved",
            },
        ]
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable(rows)):
            response = self.client.get("/query/list?radius_km=5")
        self.assertEqual(response.status_code, 200)
        queries = response.json["data"]["queries"]
        self.assertEqual([query["id"] for query in queries], ["11111111-1111-4111-8111-111111111111"])
        self.assertNotIn("33333333-3333-4333-8333-333333333333", [query["id"] for query in queries])

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

    def test_provider_response_lookup_uses_session_provider_id(self):
        provider_id = "22222222-2222-4222-8222-222222222222"
        self.login_as(role="provider", user_id=provider_id)
        query = {"id": "11111111-1111-4111-8111-111111111111", "status": "open", "lat": 26.7606, "lng": 83.3732, "radius_km": 5}
        responses = FakeRowsTable(single={"id": "33333333-3333-4333-8333-333333333333", "image_url": "https://storage.example/safe.jpg"})
        with patch.object(nearfind, "expire_open_queries"), patch.object(nearfind, "get_query", return_value=query), patch.object(
            nearfind, "current_profile", return_value={"is_active": True, "lat": 26.761, "lng": 83.374}
        ), patch.object(nearfind, "table", return_value=responses):
            result = self.client.post(
                "/query/respond",
                data={"query_id": query["id"], "provider_id": "99999999-9999-4999-8999-999999999999", "price": "Rs 120", "message": "Available", "csrf_token": "known-csrf-token"},
            )
        self.assertEqual(result.status_code, 302)
        self.assertIn(("provider_id", provider_id), responses.filters)
        self.assertNotIn(("provider_id", "99999999-9999-4999-8999-999999999999"), responses.filters)

    def test_expired_open_query_rejects_provider_response(self):
        self.login_as(role="provider", user_id="22222222-2222-4222-8222-222222222222")
        query = {"id": "11111111-1111-4111-8111-111111111111", "status": "open", "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "lat": 26.7606, "lng": 83.3732, "radius_km": 5}
        with patch.object(nearfind, "expire_open_queries"), patch.object(nearfind, "get_query", return_value=query), patch.object(
            nearfind, "current_profile", return_value={"is_active": True, "lat": 26.761, "lng": 83.374}
        ), patch.object(nearfind, "table") as table_mock:
            result = self.client.post(
                "/query/respond",
                data={"query_id": query["id"], "price": "Rs 120", "message": "Available", "csrf_token": "known-csrf-token"},
            )
        self.assertEqual(result.status_code, 409)
        table_mock.assert_not_called()

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
        responses = FakeRowsTable(single={"id": "33333333-3333-4333-8333-333333333333", "image_url": "https://storage.example/safe.jpg"})
        with patch.object(nearfind, "expire_open_queries"), patch.object(nearfind, "get_query", return_value=query), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=responses):
            result = self.client.post(
                "/query/respond",
                data={
                    "query_id": query["id"],
                    "existing_image_url": "https://attacker.example/tracker.gif",
                    "price": "Rs 120",
                    "message": "Still available",
                    "csrf_token": "known-csrf-token",
                },
            )
        self.assertEqual(result.status_code, 302)
        self.assertEqual(responses.inserted, [])
        self.assertEqual(responses.updated[0]["status"], "available")
        self.assertEqual(responses.updated[0]["image_url"], "https://storage.example/safe.jpg")

    def test_expired_open_query_rejects_provider_selection(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "open", "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
        response_row = {"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "query_id": query["id"], "status": "available"}
        fake_rpc = FakeRpcClient({"status": "expired", "error": "Query has expired"})
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(nearfind, "table") as table_mock, patch.object(
            nearfind, "get_supabase", return_value=fake_rpc
        ):
            table_mock.return_value.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = response_row
            response = self.client.post(
                "/query/select-provider",
                json={"query_id": query["id"], "response_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(fake_rpc.calls[0][0], "select_provider_for_query")

    def test_selection_with_null_expiration_uses_existing_rpc_flow(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "open", "expires_at": None}
        response_row = {"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "query_id": query["id"], "status": "available"}
        fake_rpc = FakeRpcClient()
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(nearfind, "table") as table_mock, patch.object(
            nearfind, "get_supabase", return_value=fake_rpc
        ):
            table_mock.return_value.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = response_row
            response = self.client.post(
                "/query/select-provider",
                json={"query_id": query["id"], "response_id": response_row["id"]},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_rpc.calls[0][1]["p_seeker_id"], "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def test_already_matched_query_rejects_second_selection(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "matched"}
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(nearfind, "get_supabase") as supabase_mock:
            response = self.client.post(
                "/query/select-provider",
                json={"query_id": query["id"], "response_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 409)
        supabase_mock.assert_not_called()

    def test_selection_missing_csrf_is_rejected_before_rpc(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        with patch.object(nearfind, "get_supabase") as supabase_mock:
            response = self.client.post(
                "/query/select-provider",
                json={"query_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "response_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
            )
        self.assertEqual(response.status_code, 403)
        supabase_mock.assert_not_called()

    def test_selection_rpc_definition_locks_query_and_rechecks_expiration(self):
        schema = nearfind.__file__.replace("app.py", "schema.sql")
        with open(schema, encoding="utf-8") as schema_file:
            rpc = schema_file.read().split("create or replace function public.select_provider_for_query", 1)[1]
        self.assertIn("for update", rpc.lower())
        self.assertIn("selected_query.expires_at is not null", rpc)
        self.assertIn("set status = 'expired'", rpc)
        self.assertIn("and query_id = p_query_id", rpc)
        self.assertIn("and status = 'available'", rpc)

    def test_selection_rejects_response_from_another_query(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "open"}
        response_row = {"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "query_id": "different-query", "status": "available"}
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(nearfind, "table") as table_mock:
            table_mock.return_value.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = response_row
            response = self.client.post(
                "/query/select-provider",
                json={"query_id": query["id"], "response_id": response_row["id"]},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 409)

    def test_selection_rejects_unavailable_response(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "open"}
        response_row = {"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "query_id": query["id"], "status": "rejected"}
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(nearfind, "table") as table_mock:
            table_mock.return_value.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = response_row
            response = self.client.post(
                "/query/select-provider",
                json={"query_id": query["id"], "response_id": response_row["id"]},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 409)

    def test_upload_validation_rejects_empty_file(self):
        with self.assertRaises(ValueError):
            validate_image(FileStorage(stream=io.BytesIO(), filename="empty.png", content_type="image/png"))

    def test_upload_validation_rejects_oversized_file(self):
        with self.assertRaisesRegex(ValueError, "2 MB"):
            validate_image(FileStorage(stream=io.BytesIO(b"x" * (2 * 1024 * 1024 + 1)), filename="large.png", content_type="image/png"))

    def test_upload_validation_rejects_invalid_mime(self):
        with self.assertRaisesRegex(ValueError, "JPG, PNG and WEBP"):
            validate_image(FileStorage(stream=io.BytesIO(b"not-an-image"), filename="file.gif", content_type="image/gif"))

    def test_upload_validation_rejects_invalid_signature(self):
        with self.assertRaisesRegex(ValueError, "Invalid image file"):
            validate_image(FileStorage(stream=io.BytesIO(b"not-a-png"), filename="file.png", content_type="image/png"))

    def test_upload_validation_accepts_supported_image(self):
        valid_png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82'
        self.assertEqual(validate_image(FileStorage(stream=io.BytesIO(valid_png), filename="file.png", content_type="image/png")), ".png")

    def test_upload_validation_accepts_jpeg_and_webp(self):
        valid_jpeg = (
            b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06\x12AQ\x07aq\x13"2\x81\x08\x14B\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19\x1a&\'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xe2\xe8\xa2\x8a\xf9\x93\xf7\x13\xff\xd9'
        )
        valid_webp = b'RIFF<\x00\x00\x00WEBPVP8 0\x00\x00\x00\xd0\x01\x00\x9d\x01*\x01\x00\x01\x00\x01@&%\xa0\x02t\xba\x01\xf8\x00\x03\xb0\x00\xfe\xf2\xeb\x7f\xfc\xd8\x15\xcds\xef\xf7\xff\xd2\xe0\xfd.\x0f\xd2\xe0\xff\xd2\x90\x00\x00'
        self.assertEqual(validate_image(FileStorage(stream=io.BytesIO(valid_jpeg), filename="file.jpg", content_type="image/jpeg")), ".jpg")
        self.assertEqual(validate_image(FileStorage(stream=io.BytesIO(valid_jpeg), filename="file.jpeg", content_type="image/jpeg")), ".jpg")
        self.assertEqual(validate_image(FileStorage(stream=io.BytesIO(valid_webp), filename="file.webp", content_type="image/webp")), ".webp")

    def test_upload_validation_rejects_extension_mismatch(self):
        valid_png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82'
        with self.assertRaisesRegex(ValueError, "extension"):
            validate_image(FileStorage(stream=io.BytesIO(valid_png), filename="file.jpg", content_type="image/png"))

    def test_public_image_upload_accepts_jpeg_suffix_and_uses_bucket_path(self):
        class FakeBucket:
            def __init__(self):
                self.upload_args = None

            def upload(self, path, data, options):
                self.upload_args = (path, data, options)

            def get_public_url(self, path):
                return f"https://storage.example/query-images/{path}"

        class FakeStorage:
            def __init__(self):
                self.bucket_name = None
                self.bucket = FakeBucket()

            def from_(self, bucket_name):
                self.bucket_name = bucket_name
                return self.bucket

        valid_jpeg = (
            b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06\x12AQ\x07aq\x13"2\x81\x08\x14B\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19\x1a&\'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xe2\xe8\xa2\x8a\xf9\x93\xf7\x13\xff\xd9'
        )
        storage = FakeStorage()
        client = SimpleNamespace(storage=storage)
        image = FileStorage(
            stream=io.BytesIO(valid_jpeg),
            filename="reference.jpeg",
            content_type="image/jpeg",
        )
        with patch("services.storage_service.get_supabase", return_value=client):
            result = upload_public_image("query-images", image, "seeker-id")
        self.assertEqual(storage.bucket_name, "query-images")
        path, data, options = storage.bucket.upload_args
        self.assertRegex(path, r"^seeker-id/[0-9a-f]{32}\.jpg$")
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        self.assertEqual(options["content-type"], "image/jpeg")
        self.assertEqual(result["image_url"], f"https://storage.example/query-images/{path}")

    def test_storage_upload_uses_project_url_secret_key_and_unique_safe_paths(self):
        project_url = "https://project-ref.supabase.co"
        anon_key = "sb_publishable_test-key"
        server_key = "sb_secret_server-only-key"
        with patch.object(nearfind.Config, "SUPABASE_URL", project_url), patch.object(
            nearfind.Config, "SUPABASE_ANON_KEY", anon_key
        ), patch.object(nearfind.Config, "SUPABASE_SERVICE_ROLE_KEY", server_key):
            client = nearfind.get_supabase(admin=True)
        try:
            self.assertEqual(client.url, project_url)
            self.assertEqual(client.key, server_key)
            self.assertEqual(client.publishable_key, anon_key)
            requests = []

            def capture_upload(url, headers, content):
                requests.append((url, headers, content))
                return httpx.Response(200, json={"Key": "uploaded"})

            valid_jpeg = (
                b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06\x12AQ\x07aq\x13"2\x81\x08\x14B\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19\x1a&\'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xe2\xe8\xa2\x8a\xf9\x93\xf7\x13\xff\xd9'
            )
            with patch.object(client.http, "post", side_effect=capture_upload), patch(
                "services.storage_service.get_supabase", return_value=client
            ) as get_client:
                results = [
                    upload_public_image(
                        "query-images",
                        FileStorage(
                            stream=io.BytesIO(valid_jpeg),
                            filename="reference.jpg",
                            content_type="image/jpeg",
                        ),
                        "11111111-1111-4111-8111-111111111111",
                    )
                    for _ in range(2)
                ]


            self.assertEqual(get_client.call_count, 2)
            self.assertTrue(all(call.kwargs == {"admin": True} for call in get_client.call_args_list))
            self.assertEqual(len(requests), 2)
            paths = []
            for url, headers, content in requests:
                self.assertRegex(
                    url,
                    r"^https://project-ref\.supabase\.co/storage/v1/object/query-images/"
                    r"11111111-1111-4111-8111-111111111111/[0-9a-f]{32}\.jpg$",
                )
                paths.append(url.rsplit("/", 1)[-1])
                self.assertEqual(headers["apikey"], server_key)
                self.assertNotIn("Authorization", headers)
                self.assertEqual(headers["Content-Type"], "image/jpeg")
                self.assertEqual(headers["x-upsert"], "false")
                self.assertTrue(content.startswith(b"\xff\xd8\xff"))
            self.assertNotEqual(paths[0], paths[1])
            self.assertTrue(
                all(
                    result["image_url"].startswith(
                        f"{project_url}/storage/v1/object/public/query-images/"
                    )
                    for result in results
                )
            )
        finally:
            client.http.close()

    def test_post_query_uploads_image_and_persists_returned_url(self):
        self.login_as(role="seeker")
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        queries = FakeRowsTable()
        image_url = "https://storage.example/query-images/reference.jpg"
        with patch.object(nearfind, "table", return_value=queries), patch.object(
            nearfind, "upload_public_image", return_value={"image_url": image_url}
        ) as upload, patch.object(nearfind, "notify_nearby_providers"):
            response = self.client.post(
                "/query/post",
                data={
                    "query_id": query_id,
                    "item_name": "USB cable",
                    "category": "Electronics / IT",
                    "description": "USB-C cable",
                    "location": "Golghar",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "radius_km": "5",
                    "urgency": "urgent",
                    "expiration": "6",
                    "csrf_token": "known-csrf-token",
                    "image": (io.BytesIO(b"\xff\xd8\xffimage"), "reference.jpeg", "image/jpeg"),
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/seeker/responses/{query_id}")
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0], "query-images")
        self.assertEqual(queries.inserted[0]["image_url"], image_url)
        self.assertEqual(queries.inserted[0]["id"], query_id)

    def test_post_query_without_image_keeps_null_image_url(self):
        self.login_as(role="seeker")
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        queries = FakeRowsTable()
        with patch.object(nearfind, "table", return_value=queries), patch.object(
            nearfind, "upload_public_image"
        ) as upload, patch.object(nearfind, "notify_nearby_providers"):
            response = self.client.post(
                "/query/post",
                data={
                    "query_id": query_id,
                    "item_name": "USB cable",
                    "category": "Electronics / IT",
                    "description": "USB-C cable",
                    "location": "Golghar",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "urgency": "urgent",
                    "csrf_token": "known-csrf-token",
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(queries.inserted[0]["image_url"])
        upload.assert_not_called()

    def test_post_query_returns_specific_validation_error_for_invalid_image(self):
        self.login_as(role="seeker")
        queries = FakeRowsTable()
        with patch.object(nearfind, "table", return_value=queries), patch.object(
            nearfind, "upload_public_image", side_effect=ValueError("Invalid image file")
        ), patch.object(nearfind, "notify_nearby_providers"):
            response = self.client.post(
                "/query/post",
                data={
                    "query_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "item_name": "USB cable",
                    "category": "Electronics / IT",
                    "description": "USB-C cable",
                    "location": "Golghar",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "urgency": "urgent",
                    "csrf_token": "known-csrf-token",
                    "image": (io.BytesIO(b"not an image"), "reference.jpg", "image/jpeg"),
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid image file", response.get_data(as_text=True))
        self.assertEqual(queries.inserted, [])

    def test_post_query_reports_safe_image_upload_failure_without_insert(self):
        self.login_as(role="seeker")
        queries = FakeRowsTable()
        with patch.object(nearfind, "table", return_value=queries), patch.object(
            nearfind, "upload_public_image", side_effect=RuntimeError("secret file contents")
        ), patch.object(nearfind, "notify_nearby_providers"):
            with self.assertLogs(nearfind.app.logger, level="ERROR") as captured:
                response = self.client.post(
                    "/query/post",
                    data={
                        "query_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        "item_name": "USB cable",
                        "category": "Electronics / IT",
                        "description": "USB-C cable",
                        "location": "Golghar",
                        "lat": "26.7606",
                        "lng": "83.3732",
                        "urgency": "urgent",
                        "csrf_token": "known-csrf-token",
                        "image": (io.BytesIO(b"\xff\xd8\xffimage"), "reference.jpg", "image/jpeg"),
                    },
                    follow_redirects=False,
                )
        self.assertEqual(response.status_code, 502)
        self.assertIn("couldn&#39;t upload that image", response.get_data(as_text=True))
        self.assertNotIn("secret file contents", response.get_data(as_text=True))
        self.assertNotIn("secret file contents", "\n".join(captured.output))
        self.assertEqual(queries.inserted, [])

    def test_post_query_handles_missing_bucket_or_upload_permission_error(self):
        self.login_as(role="seeker")
        for status_code, detail, message in (
            (404, "Bucket not found", "Image storage is unavailable"),
            (403, "Not authorized to upload object", "Image storage rejected the upload"),
        ):
            with self.subTest(status_code=status_code):
                queries = FakeRowsTable()
                with patch.object(nearfind, "table", return_value=queries), patch.object(
                    nearfind,
                    "upload_public_image",
                    side_effect=nearfind.SupabaseRequestError(status_code, detail),
                ), self.assertLogs(nearfind.app.logger, level="ERROR") as captured:
                    response = self.client.post(
                        "/query/post",
                        data={
                            "query_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                            "item_name": "USB cable",
                            "category": "Electronics / IT",
                            "description": "USB-C cable",
                            "location": "Golghar",
                            "lat": "26.7606",
                            "lng": "83.3732",
                            "urgency": "urgent",
                            "csrf_token": "known-csrf-token",
                            "image": (io.BytesIO(b"\xff\xd8\xffimage"), "reference.jpg", "image/jpeg"),
                        },
                        follow_redirects=False,
                    )
                self.assertEqual(response.status_code, 502)
                body = response.get_data(as_text=True)
                logs = "\n".join(captured.output)
                self.assertIn(message, body)
                self.assertIn("submit without an image", body)
                self.assertIn(f"status={status_code}", logs)
                self.assertNotIn(detail, body + logs)
                self.assertEqual(queries.inserted, [])

    def test_post_query_database_failure_returns_safe_retry_message(self):
        self.login_as(role="seeker")
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

        class FailingInsertTable(FakeRowsTable):
            def insert(self, payload):
                raise nearfind.SupabaseRequestError(500, "private database details")

        first_lookup = FakeRowsTable()
        failing_insert = FailingInsertTable()
        retry_lookup = FakeRowsTable()
        with patch.object(
            nearfind,
            "table",
            side_effect=[first_lookup, failing_insert, retry_lookup],
        ), patch.object(nearfind, "notify_nearby_providers"):
            response = self.client.post(
                "/query/post",
                data={
                    "query_id": query_id,
                    "item_name": "USB cable",
                    "category": "Electronics / IT",
                    "description": "USB-C cable",
                    "location": "Golghar",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "urgency": "urgent",
                    "csrf_token": "known-csrf-token",
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 502)
        body = response.get_data(as_text=True)
        self.assertIn("already-saved request will not be duplicated", body)
        self.assertNotIn("private database details", body)

    def test_post_query_retry_does_not_upload_or_insert_existing_query_again(self):
        self.login_as(role="seeker")
        query_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        queries = FakeRowsTable(single={"id": query_id})
        with patch.object(nearfind, "table", return_value=queries), patch.object(
            nearfind, "upload_public_image"
        ) as upload, patch.object(nearfind, "notify_nearby_providers"):
            response = self.client.post(
                "/query/post",
                data={
                    "query_id": query_id,
                    "item_name": "USB cable",
                    "category": "Electronics / IT",
                    "description": "USB-C cable",
                    "location": "Golghar",
                    "lat": "26.7606",
                    "lng": "83.3732",
                    "urgency": "urgent",
                    "csrf_token": "known-csrf-token",
                    "image": (io.BytesIO(b"\xff\xd8\xffimage"), "reference.jpeg", "image/jpeg"),
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/seeker/responses/{query_id}")
        upload.assert_not_called()
        self.assertEqual(queries.inserted, [])

    def test_non_finite_coordinates_are_rejected(self):
        with self.assertRaises(ValueError):
            nearfind.coerce_float("nan", "Latitude", -90, 90)
        with self.assertRaises(ValueError):
            nearfind.coerce_float("inf", "Longitude", -180, 180)
        self.assertTrue(math.isfinite(nearfind.coerce_float("26.7", "Latitude", -90, 90)))

    def test_overlong_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "160 characters"):
            nearfind.clean_text("x" * 161, 160)

    def test_invalid_radius_and_expiration_are_rejected(self):
        with self.assertRaises(ValueError):
            nearfind.coerce_float("-1", "Radius", 0.5, 50)
        with self.assertRaises(ValueError):
            nearfind.coerce_float("51", "Radius", 0.5, 50)
        with self.assertRaises(ValueError):
            nearfind.parse_expiration("not-a-duration")

    def test_invalid_lifecycle_transition_is_rejected(self):
        self.login_as(user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        query = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "status": "expired"}
        with patch.object(nearfind, "require_query_owner", return_value=query), patch.object(nearfind, "table") as table_mock:
            response = self.client.post(
                "/query/resolve",
                json={"query_id": query["id"]},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 409)
        table_mock.assert_not_called()

    def test_logged_out_and_wrong_role_routes_are_rejected(self):
        self.assertEqual(self.client.get("/seeker/dashboard").status_code, 302)
        self.assertEqual(self.client.get("/provider/dashboard").status_code, 302)
        self.login_as(role="seeker")
        self.assertEqual(self.client.get("/provider/dashboard").status_code, 302)
        self.assertEqual(self.client.get("/provider/respond/11111111-1111-4111-8111-111111111111").status_code, 302)
        self.login_as(role="provider")
        self.assertEqual(self.client.get("/seeker/dashboard").status_code, 302)
        self.assertEqual(self.client.get("/seeker/post").status_code, 302)

    def test_provider_dashboard_still_renders_when_live_location_table_is_missing(self):
        self.login_as(role="provider")
        with patch.object(nearfind, "current_profile", return_value={"role": "provider", "is_active": True}), patch.object(
            nearfind, "provider_response_summary", return_value={"total": 0, "available": 0, "selected": 0, "rejected": 0, "sold": 0}
        ), patch.object(nearfind, "selected_provider_responses", return_value=[]), patch.object(
            nearfind, "table", side_effect=nearfind.SupabaseRequestError(404, "provider_locations is missing")
        ):
            response = self.client.get("/provider/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Nearby requests", response.data)

    def test_non_manager_cannot_update_hospital_availability(self):
        self.login_as(role="provider")
        with patch.object(nearfind, "current_profile", return_value={"role": "provider", "is_active": True}), patch.object(
            nearfind, "table"
        ) as table_mock:
            response = self.client.post(
                "/hospital/availability",
                data={"general_available": "4"},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 403)
        table_mock.assert_not_called()

    def test_hospital_manager_can_update_only_own_hospital_availability(self):
        hospital_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        self.login_as(role="provider")
        hospital = {"id": hospital_id, "name": "City Hospital", "address": "Main Road"}
        profile = {"role": "provider", "is_active": True, "is_hospital_manager": True}
        existing_hospital = FakeRowsTable(single=hospital)
        second_hospital = FakeRowsTable(single=hospital)
        current_availability = FakeRowsTable(single=None)
        availability_insert = FakeRowsTable()
        hospital_update = FakeRowsTable()
        with patch.object(nearfind, "current_profile", return_value=profile), patch.object(
            nearfind,
            "table",
            side_effect=[existing_hospital, second_hospital, current_availability, availability_insert, hospital_update],
        ):
            response = self.client.post(
                "/hospital/availability",
                data={
                    "hospital_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "general_available": "8",
                    "icu_available": "2",
                    "oxygen_available": "3",
                    "emergency_available": "1",
                    "isolation_available": "0",
                },
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(availability_insert.inserted[0]["hospital_id"], hospital_id)
        self.assertEqual(availability_insert.inserted[0]["icu_available"], 2)
        self.assertEqual(hospital_update.updated[0]["updated_at"], availability_insert.inserted[0]["updated_at"])

    def test_hospital_manager_rejects_negative_bed_counts(self):
        hospital = {"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "name": "City Hospital", "address": "Main Road"}
        self.login_as(role="provider")
        with patch.object(nearfind, "current_profile", return_value={"is_hospital_manager": True}), patch.object(
            nearfind, "table", return_value=FakeRowsTable(single=hospital)
        ):
            response = self.client.post(
                "/hospital/availability",
                data={"general_available": "-1"},
                headers={"X-CSRFToken": "known-csrf-token"},
            )
        self.assertEqual(response.status_code, 400)

    def test_supabase_technical_error_is_not_exposed(self):
        self.login_as()
        technical_message = "PostgreSQL relation secrets.internal_tokens does not exist"
        with patch.object(nearfind, "table", side_effect=nearfind.SupabaseRequestError(500, technical_message)):
            response = self.client.get("/seeker/dashboard")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(technical_message, response.get_data(as_text=True))
        self.assertIn("Unable to complete the request. Please try again.", response.get_data(as_text=True))

    def test_supabase_client_error_is_not_exposed(self):
        self.login_as()
        technical_message = "column internal_secret does not exist"
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "table", side_effect=nearfind.SupabaseRequestError(400, technical_message)
        ):
            response = self.client.get("/seeker/dashboard")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(technical_message, response.get_data(as_text=True))
        self.assertIn("Please check your input", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
