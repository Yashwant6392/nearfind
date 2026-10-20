import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as nearfind


class SearchTable:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.filters = []
        self.updated = []
        self.inserted = []

    def select(self, columns):
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def gte(self, column, value):
        self.filters.append((column, value))
        return self

    def lte(self, column, value):
        self.filters.append((column, value))
        return self

    def in_(self, column, values):
        self.filters.append((column, tuple(values)))
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)

    def update(self, payload):
        self.updated.append(payload)
        return self

    def insert(self, payload):
        self.inserted.append(payload)
        return self


class HospitalSearchTests(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def login_as_seeker(self):
        with self.client.session_transaction() as session:
            session.update(user_id="11111111-1111-4111-8111-111111111111", role="seeker", _csrf_token="known-csrf-token")

    def test_authenticated_seeker_searches_verified_hospitals(self):
        self.login_as_seeker()
        updated = (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()
        hospitals = SearchTable(
            rows=[
                {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "name": "City Hospital", "address": "Main Road", "phone": "123", "lat": 26.761, "lng": 83.374, "is_verified": True},
            ]
        )
        availability = SearchTable(
            rows=[
                {"hospital_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "general_available": 8, "icu_available": 2, "oxygen_available": 3, "emergency_available": 1, "isolation_available": 0, "updated_at": updated},
            ]
        )
        with patch.object(nearfind, "table", side_effect=[hospitals, availability]):
            response = self.client.get("/hospitals/search?latitude=26.7606&longitude=83.3732&radius=5&bed_type=icu")
        self.assertEqual(response.status_code, 200)
        result = response.json["data"]["hospitals"][0]
        self.assertEqual(result["bed_type"], "icu")
        self.assertEqual(result["requested_count"], 2)
        self.assertEqual(result["availability_status"], "available")
        self.assertEqual(result["freshness_state"], "fresh")
        self.assertNotIn("manager_id", result)
        self.assertEqual(hospitals.updated, [])
        self.assertEqual(availability.updated, [])
        self.assertEqual(hospitals.inserted, [])
        self.assertEqual(availability.inserted, [])

    def test_unauthenticated_search_returns_structured_auth_error(self):
        response = self.client.get("/hospitals/search?latitude=26.7&longitude=83.3&radius=5&bed_type=general")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json["success"])

    def test_search_rejects_invalid_location_radius_and_bed_type(self):
        self.login_as_seeker()
        cases = [
            "latitude=91&longitude=83.3&radius=5&bed_type=general",
            "latitude=26.7&longitude=181&radius=5&bed_type=general",
            "latitude=26.7&longitude=83.3&radius=1&bed_type=general",
            "latitude=26.7&longitude=83.3&radius=5&bed_type=vip",
        ]
        for query in cases:
            with self.subTest(query=query):
                response = self.client.get(f"/hospitals/search?{query}")
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["success"])

    def test_search_filters_by_exact_distance_and_sorts_available_fresh_first(self):
        self.login_as_seeker()
        hospitals = SearchTable(
            rows=[
                {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "name": "Full Nearby", "address": "A", "lat": 26.7606, "lng": 83.3732, "is_verified": True},
                {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "name": "Available Farther", "address": "B", "lat": 26.77, "lng": 83.38, "is_verified": True},
                {"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "name": "Outside Radius", "address": "C", "lat": 27.4, "lng": 84.2, "is_verified": True},
            ]
        )
        availability = SearchTable(
            rows=[
                {"hospital_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "general_available": 0, "icu_available": 0, "oxygen_available": 0, "emergency_available": 0, "isolation_available": 0, "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()},
                {"hospital_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "general_available": 4, "icu_available": 0, "oxygen_available": 0, "emergency_available": 0, "isolation_available": 0, "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()},
                {"hospital_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "general_available": 9, "icu_available": 0, "oxygen_available": 0, "emergency_available": 0, "isolation_available": 0, "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()},
            ]
        )
        with patch.object(nearfind, "table", side_effect=[hospitals, availability]):
            response = self.client.get("/hospitals/search?latitude=26.7606&longitude=83.3732&radius=5&bed_type=general")
        results = response.json["data"]["hospitals"]
        self.assertEqual([result["name"] for result in results], ["Available Farther", "Full Nearby"])
        self.assertLessEqual(results[0]["distance_km"], 5)

    def test_full_and_stale_states_are_explicit(self):
        self.login_as_seeker()
        hospitals = SearchTable(rows=[{"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "name": "Old Hospital", "address": "A", "lat": 26.7606, "lng": 83.3732, "is_verified": True}])
        availability = SearchTable(rows=[{"hospital_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "general_available": 0, "icu_available": 0, "oxygen_available": 0, "emergency_available": 0, "isolation_available": 0, "updated_at": (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()}])
        with patch.object(nearfind, "table", side_effect=[hospitals, availability]):
            response = self.client.get("/hospitals/search?latitude=26.7606&longitude=83.3732&radius=2&bed_type=general")
        result = response.json["data"]["hospitals"][0]
        self.assertEqual(result["availability_status"], "full")
        self.assertEqual(result["freshness_state"], "stale")
        self.assertEqual(result["freshness_label"], "Availability may be outdated")

    def test_freshness_rules_are_deterministic(self):
        now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        self.assertEqual(nearfind.hospital_freshness("2026-09-13T11:45:00+00:00", now)["state"], "fresh")
        self.assertEqual(nearfind.hospital_freshness("2026-09-13T10:00:00+00:00", now)["state"], "aging")
        self.assertEqual(nearfind.hospital_freshness("2026-09-13T07:00:00+00:00", now)["state"], "stale")


class HospitalSearchStaticTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_emergency_page_has_accessible_controls_and_states(self):
        template = (self.root / "templates" / "seeker" / "emergency_bed.html").read_text(encoding="utf-8")
        script = (self.root / "static" / "js" / "hospital-search.js").read_text(encoding="utf-8")
        self.assertIn('id="useEmergencyLocation"', template)
        self.assertIn('aria-live="polite"', template)
        self.assertIn('id="hospitalDetails"', template)
        self.assertIn("No hospitals with recently reported availability were found nearby.", script)
        self.assertIn("Availability may be outdated", script)
        self.assertIn("refreshHospitals", template)
        self.assertIn("maps/dir/?api=1", script)
        self.assertIn('target = "_blank"', script)
        self.assertIn("result.hospital_id", script)
        self.assertIn("markers = new Map", script)
        self.assertIn("setSelected", script)
        self.assertIn("searching", script)
        self.assertIn("createElement", script)
        self.assertNotRegex(script, r"\binnerHTML\b|\beval\s*\(|new Function")


if __name__ == "__main__":
    unittest.main()
