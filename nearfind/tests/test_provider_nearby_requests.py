import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app import app, haversine
import app as nearfind


class FakeRowsTable:
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


class ProviderNearbyRequestsTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        self.provider_id = "bfa01dd4-079b-4c08-8f1a-6a43cdd0568a"
        self.seeker_id = "11111111-2222-3333-4444-555555555555"

    def login_as_provider(self, lat=None, lng=None, is_active=True, service_radius_km=None):
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.provider_id
            sess["role"] = "provider"
            sess["email"] = "provider@example.com"
            sess["name"] = "Test Provider"
            sess["_csrf_token"] = "test-csrf-token"
            if lat is not None:
                sess["lat"] = lat
            if lng is not None:
                sess["lng"] = lng

    def test_missing_provider_coordinates_returns_400(self):
        self.login_as_provider()
        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": None,
            "lng": None,
        }
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table") as table_mock:
            resp = self.client.get("/query/list")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data.get("success"))
        self.assertIn("Update your location to discover nearby requests", data["error"]["message"])
        # Verify queries table was not queried when coordinates are missing
        called_tables = [c[0][0] for c in table_mock.call_args_list if c[0]]
        self.assertNotIn("queries", called_tables)

    def test_invalid_provider_coordinates_returns_400(self):
        self.login_as_provider()
        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": 95.0,  # Invalid latitude (> 90)
            "lng": 80.2450,
        }
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table") as table_mock:
            resp = self.client.get("/query/list")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("Update your location", data["error"]["message"])
        called_tables = [c[0][0] for c in table_mock.call_args_list if c[0]]
        self.assertNotIn("queries", called_tables)

    def test_valid_nearby_request_returned_within_discovery_radius(self):
        # Provider at Panki Kanpur: 26.4799, 80.2450
        # Seeker at PSIT Kanpur: 26.450219, 80.190871
        # Straight-line distance: ~6.23 km
        plat, plng = 26.4799, 80.2450
        qlat, qlng = 26.450219, 80.190871
        self.login_as_provider(lat=plat, lng=plng)

        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": plat,
            "lng": plng,
        }
        row = {
            "id": "351bb050-f9e5-4b5f-bc15-ba30862e5a89",
            "item_name": "Dell 65W USB-C Charger",
            "category": "Electronics",
            "description": "black color 65w",
            "users": {"name": "Test Seeker"},
            "location": "psit kanpur",
            "lat": qlat,
            "lng": qlng,
            "radius_km": 25,
            "urgency": "urgent",
            "status": "open",
            "created_at": "2026-10-28T16:45:08.425541+00:00",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        }

        # With discovery radius = 10 km, the ~6.23 km request is within radius
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable([row])):
            resp = self.client.get("/query/list?radius_km=10")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        queries = data["data"]["queries"]
        self.assertEqual(len(queries), 1)
        q = queries[0]
        self.assertEqual(q["id"], "351bb050-f9e5-4b5f-bc15-ba30862e5a89")
        self.assertEqual(q["item_name"], "Dell 65W USB-C Charger")
        self.assertEqual(q["status"], "open")
        self.assertEqual(q["seeker_name"], "Test Seeker")
        self.assertAlmostEqual(q["distance"], haversine(plat, plng, qlat, qlng), places=2)
        self.assertTrue(q["distance_label"].endswith("away"))

    def test_request_outside_provider_discovery_radius_is_excluded(self):
        # Distance is ~6.23 km. When provider sets radius = 5 km, it must be excluded.
        plat, plng = 26.4799, 80.2450
        qlat, qlng = 26.450219, 80.190871
        self.login_as_provider(lat=plat, lng=plng)

        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": plat,
            "lng": plng,
        }
        row = {
            "id": "351bb050-f9e5-4b5f-bc15-ba30862e5a89",
            "item_name": "Dell 65W USB-C Charger",
            "category": "Electronics",
            "location": "psit kanpur",
            "lat": qlat,
            "lng": qlng,
            "radius_km": 25,
            "status": "open",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        }

        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable([row])):
            resp = self.client.get("/query/list?radius_km=5")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["data"]["queries"]), 0)

        # But when radius is expanded to 10 km or 25 km, it is included
        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable([row])):
            resp_expanded = self.client.get("/query/list?radius_km=25")

        self.assertEqual(resp_expanded.status_code, 200)
        self.assertEqual(len(resp_expanded.get_json()["data"]["queries"]), 1)

    def test_request_outside_seeker_radius_is_excluded_even_if_provider_radius_large(self):
        # Distance ~6.23 km. Provider searches with 50 km, but seeker requested max radius 3 km.
        plat, plng = 26.4799, 80.2450
        qlat, qlng = 26.450219, 80.190871
        self.login_as_provider(lat=plat, lng=plng)

        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": plat,
            "lng": plng,
        }
        row = {
            "id": "351bb050-f9e5-4b5f-bc15-ba30862e5a89",
            "item_name": "Dell 65W USB-C Charger",
            "lat": qlat,
            "lng": qlng,
            "radius_km": 3,  # Seeker wants within 3 km only!
            "status": "open",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        }

        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable([row])):
            resp = self.client.get("/query/list?radius_km=50")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["data"]["queries"]), 0)

    def test_expired_and_non_open_requests_are_excluded(self):
        plat, plng = 26.4799, 80.2450
        qlat, qlng = 26.4790, 80.2440  # Very close (~100m)
        self.login_as_provider(lat=plat, lng=plng)

        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": plat,
            "lng": plng,
        }
        expired_row = {
            "id": "11111111-0000-0000-0000-000000000001",
            "item_name": "Expired Request",
            "lat": qlat,
            "lng": qlng,
            "radius_km": 10,
            "status": "open",
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        }
        closed_row = {
            "id": "11111111-0000-0000-0000-000000000002",
            "item_name": "Matched Request",
            "lat": qlat,
            "lng": qlng,
            "radius_km": 10,
            "status": "matched",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }

        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable([expired_row, closed_row])):
            resp = self.client.get("/query/list?radius_km=10")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["data"]["queries"]), 0)

    def test_provider_service_radius_cap_is_respected(self):
        # Distance ~6.23 km. Provider has profile service_radius_km = 4 km.
        plat, plng = 26.4799, 80.2450
        qlat, qlng = 26.450219, 80.190871
        self.login_as_provider(lat=plat, lng=plng)

        provider = {
            "id": self.provider_id,
            "role": "provider",
            "is_active": True,
            "lat": plat,
            "lng": plng,
            "service_radius_km": 4.0,  # Provider capped at 4 km
        }
        row = {
            "id": "351bb050-f9e5-4b5f-bc15-ba30862e5a89",
            "item_name": "Dell 65W USB-C Charger",
            "lat": qlat,
            "lng": qlng,
            "radius_km": 25,
            "status": "open",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        }

        with patch.object(nearfind, "expire_open_queries"), patch.object(
            nearfind, "current_profile", return_value=provider
        ), patch.object(nearfind, "table", return_value=FakeRowsTable([row])):
            resp = self.client.get("/query/list?radius_km=25")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["data"]["queries"]), 0)

    def test_update_location_saves_valid_coords_and_updates_session(self):
        self.login_as_provider()
        fake_table = FakeRowsTable()
        with patch.object(nearfind, "table", return_value=fake_table):
            resp = self.client.post(
                "/user/update-location",
                json={"lat": 26.4799, "lng": 80.2450},
                headers={"X-CSRFToken": "test-csrf-token"},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertAlmostEqual(data["data"]["lat"], 26.4799)
        self.assertAlmostEqual(data["data"]["lng"], 80.2450)
        self.assertEqual(len(fake_table.updated), 1)
        self.assertAlmostEqual(fake_table.updated[0]["lat"], 26.4799)
        self.assertAlmostEqual(fake_table.updated[0]["lng"], 80.2450)

        # Check session was updated
        with self.client.session_transaction() as sess:
            self.assertAlmostEqual(sess["lat"], 26.4799)
            self.assertAlmostEqual(sess["lng"], 80.2450)


if __name__ == "__main__":
    unittest.main()
