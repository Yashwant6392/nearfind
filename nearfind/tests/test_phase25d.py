import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as nearfind


class SelectedResponseTable:
    def __init__(self, rows):
        self.rows = rows
        self.selected = None
        self.filters = []

    def select(self, columns):
        self.selected = columns
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def order(self, column, desc=False):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class MatchedJourneyTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_provider_selected_summary_requests_only_authorized_destination_fields(self):
        table = SelectedResponseTable(
            [{"id": "response-id", "query_id": "query-id", "status": "selected", "queries": {"lat": 26.7, "lng": 83.3}}]
        )
        with patch.object(nearfind, "table", return_value=table):
            rows = nearfind.selected_provider_responses("provider-id")
        self.assertEqual(rows[0]["queries"]["lat"], 26.7)
        self.assertIn("queries(id,item_name,description,location,lat,lng,status,users:seeker_id(phone))", table.selected)
        self.assertIn(("provider_id", "provider-id"), table.filters)
        self.assertIn(("status", "selected"), table.filters)

    def test_matched_templates_expose_navigation_and_existing_chat_hooks(self):
        seeker_template = (self.root / "templates" / "seeker" / "responses.html").read_text(encoding="utf-8")
        provider_template = (self.root / "templates" / "provider" / "dashboard.html").read_text(encoding="utf-8")
        navigation = (self.root / "static" / "js" / "navigation.js").read_text(encoding="utf-8")
        seeker_script = (self.root / "static" / "js" / "seeker-map.js").read_text(encoding="utf-8")
        provider_script = (self.root / "static" / "js" / "provider-map.js").read_text(encoding="utf-8")
        self.assertIn("matched-next-action", seeker_template)
        self.assertIn("data-detail-directions", seeker_template)
        self.assertIn("data-customer-navigation", provider_template)
        self.assertIn("chat_for_response", provider_template)
        self.assertIn("https://www.google.com/maps/dir/", navigation)
        self.assertIn("NearFindNavigationUrl", seeker_script)
        self.assertIn("NearFindNavigationUrl", provider_script)
        self.assertIn("Location may be outdated", seeker_script)
        self.assertIn("Sharing may be off", seeker_script)
        self.assertIn("Customer destination available for navigation", provider_template)
        self.assertIn("data-customer-call", provider_template)
        self.assertNotIn("en_route", seeker_template + provider_template + seeker_script + provider_script)
        self.assertNotIn("innerHTML", navigation + seeker_script + provider_script)

    def test_navigation_helper_rejects_invalid_coordinates(self):
        navigation = (self.root / "static" / "js" / "navigation.js").read_text(encoding="utf-8")
        self.assertIn("lat < -90", navigation)
        self.assertIn("lng > 180", navigation)
        self.assertIn("searchParams.set", navigation)


if __name__ == "__main__":
    unittest.main()
