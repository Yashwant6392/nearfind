"""
tests/test_distance_and_driver_profile_audit.py

Comprehensive regression tests covering:
1. Profile/login location differs from request location: distance uses request location.
2. Updating the request location changes the calculated distance.
3. Provider outside requested radius is excluded and cannot be selected.
4. Missing or invalid coordinates fail safely.
5. Existing requests without request-specific coordinates follow documented fallback policy.
6. Driver profile and identity-document uploads enforce authorization and private access.
"""

import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from werkzeug.datastructures import FileStorage

import app as nearfind
from services.location_service import haversine


class MockQuery:
    def __init__(self, data=None):
        self._data = data
        self._single = False

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

    def lt(self, *args, **kwargs):
        return self

    def lte(self, *args, **kwargs):
        return self

    def gt(self, *args, **kwargs):
        return self

    def gte(self, *args, **kwargs):
        return self

    def neq(self, *args, **kwargs):
        return self

    def single(self):
        self._single = True
        return self

    def maybe_single(self):
        self._single = True
        return self

    def insert(self, record):
        return self

    def update(self, record):
        return self

    def execute(self):
        if self._single:
            if isinstance(self._data, list):
                res = self._data[0] if self._data else None
            else:
                res = self._data
            return SimpleNamespace(data=res)
        if isinstance(self._data, list):
            return SimpleNamespace(data=self._data)
        if self._data is None:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[self._data])


class MockTable:
    def __init__(self, data=None):
        self._data = data

    def select(self, *args, **kwargs):
        return MockQuery(self._data).select(*args, **kwargs)

    def update(self, *args, **kwargs):
        return MockQuery(self._data).update(*args, **kwargs)

    def insert(self, *args, **kwargs):
        return MockQuery(self._data).insert(*args, **kwargs)

    def eq(self, *args, **kwargs):
        return MockQuery(self._data).eq(*args, **kwargs)

    def in_(self, *args, **kwargs):
        return MockQuery(self._data).in_(*args, **kwargs)

    def lt(self, *args, **kwargs):
        return MockQuery(self._data).lt(*args, **kwargs)

    def execute(self):
        return MockQuery(self._data).execute()


def get_error_message(response):
    payload = response.get_json() or {}
    err = payload.get("error")
    if isinstance(err, dict):
        return err.get("message", "")
    return str(err or "")


class DistanceAndDriverAuditTests(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()
        self.seeker_id = "11111111-1111-4111-8111-111111111111"
        self.provider_id = "22222222-2222-4222-8222-222222222222"
        self.query_id = "33333333-3333-4333-8333-333333333333"
        self.response_id = "44444444-4444-4444-8444-444444444444"

    def login_as(self, role="seeker", user_id=None, lat=None, lng=None):
        with self.client.session_transaction() as session:
            session.update(
                user_id=user_id or (self.seeker_id if role == "seeker" else self.provider_id),
                role=role,
                _csrf_token="audit-test-csrf-token",
                lat=lat,
                lng=lng,
            )

    # -------------------------------------------------------------------------
    # 1. Profile/login location differs from request location:
    #    Distance must use request location, NOT profile location.
    # -------------------------------------------------------------------------
    def test_distance_uses_request_location_not_profile_location(self):
        # Seeker profile is in Gorakhpur (26.7606, 83.3732)
        # But this specific request delivery is in New Delhi (28.6139, 77.2090)
        # Provider is in Noida (28.5355, 77.3910)
        # Straight-line distance from New Delhi to Noida is ~19.6 km.
        # Distance from Gorakhpur to Noida is > 650 km.
        self.login_as(role="seeker", user_id=self.seeker_id, lat=26.7606, lng=83.3732)

        query_record = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "item_name": "Test Item",
            "lat": 28.6139,
            "lng": 77.2090,
            "radius_km": 25.0,
            "status": "open",
            "location": "Connaught Place, New Delhi",
        }
        provider_user = {
            "id": self.provider_id,
            "name": "Noida Supplier",
            "business_name": "Noida Electronics",
            "provider_type": "shop",
            "phone": "9876543210",
            "lat": 28.5355,
            "lng": 77.3910,
            "is_active": True,
        }
        resp_record = {
            "id": self.response_id,
            "query_id": self.query_id,
            "provider_id": self.provider_id,
            "message": "In stock in Noida",
            "price": "500",
            "status": "available",
            "created_at": "2026-10-20T00:00:00Z",
            "users": provider_user,
        }

        def mock_table(name):
            if name == "queries":
                return MockTable(query_record)
            elif name == "responses":
                return MockTable([resp_record])
            elif name == "users":
                return MockTable(provider_user)
            elif name == "provider_locations":
                return MockTable(None)
            return MockTable()

        with patch("app.table", side_effect=mock_table):
            res = self.client.get(f"/query/responses/{self.query_id}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()["data"]
            self.assertEqual(len(data["responses"]), 1)
            calculated_distance = data["responses"][0]["distance"]

            # Distance must be ~19.6 km (New Delhi -> Noida), NOT 650+ km (Gorakhpur -> Noida)
            expected_distance = round(haversine(28.6139, 77.2090, 28.5355, 77.3910), 2)
            self.assertAlmostEqual(calculated_distance, expected_distance, places=1)
            self.assertLess(calculated_distance, 30.0)
            self.assertEqual(data["responses"][0]["distance_type"], "straight_line")

    # -------------------------------------------------------------------------
    # 2. Updating request location changes calculated distance
    # -------------------------------------------------------------------------
    def test_updating_request_location_changes_calculated_distance(self):
        self.login_as(role="seeker", user_id=self.seeker_id)

        # Initial request location: Delhi (28.6139, 77.2090)
        query_record = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "lat": 28.6139,
            "lng": 77.2090,
            "location": "Delhi Initial",
            "status": "open",
        }
        provider_user = {
            "id": self.provider_id,
            "lat": 28.5355,
            "lng": 77.3910,
            "is_active": True,
        }
        resp_record = {
            "id": self.response_id,
            "query_id": self.query_id,
            "provider_id": self.provider_id,
            "status": "available",
            "users": provider_user,
        }

        updated_lat = 28.4595
        updated_lng = 77.0266  # Gurugram

        def mock_table(name):
            if name == "queries":
                return MockTable(query_record)
            elif name == "responses":
                return MockTable([resp_record])
            elif name == "users":
                return MockTable(provider_user)
            return MockTable()

        # Update request location
        with patch("app.table", side_effect=mock_table):
            update_res = self.client.post(
                f"/query/{self.query_id}/update-location",
                data={
                    "csrf_token": "audit-test-csrf-token",
                    "lat": updated_lat,
                    "lng": updated_lng,
                    "location": "Gurugram Delivery Point",
                },
            )
            self.assertEqual(update_res.status_code, 200)

            # Change in query record
            query_record["lat"] = updated_lat
            query_record["lng"] = updated_lng

            # Query responses again to verify distance recalculation
            res = self.client.get(f"/query/responses/{self.query_id}")
            data = res.get_json()["data"]
            new_distance = data["responses"][0]["distance"]

            # Expected distance from Gurugram (28.4595, 77.0266) to Noida (28.5355, 77.3910) is ~36.6 km
            expected_new = round(haversine(updated_lat, updated_lng, 28.5355, 77.3910), 2)
            self.assertAlmostEqual(new_distance, expected_new, places=1)
            self.assertNotEqual(new_distance, round(haversine(28.6139, 77.2090, 28.5355, 77.3910), 2))

    # -------------------------------------------------------------------------
    # 3. Provider outside requested radius is excluded and cannot be selected
    # -------------------------------------------------------------------------
    def test_provider_outside_radius_excluded_and_rejected_on_selection(self):
        # Request in Delhi with radius = 10 km
        query_record = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "item_name": "Emergency Tool",
            "lat": 28.6139,
            "lng": 77.2090,
            "radius_km": 10.0,
            "status": "open",
        }
        # Provider is 25 km away in Greater Noida (28.4744, 77.5040)
        distant_provider = {
            "id": self.provider_id,
            "lat": 28.4744,
            "lng": 77.5040,
            "is_active": True,
            "service_radius_km": 50.0,
        }
        distant_response = {
            "id": self.response_id,
            "query_id": self.query_id,
            "provider_id": self.provider_id,
            "status": "available",
        }

        # 3a: In provider discovery (/query/list), distant provider must NOT see the 10km request
        self.login_as(role="provider", user_id=self.provider_id)

        def mock_table_list(name):
            if name == "queries":
                return MockTable([query_record])
            elif name == "users":
                return MockTable(distant_provider)
            elif name == "provider_locations":
                return MockTable(None)
            return MockTable()

        with patch("app.current_profile", return_value=distant_provider), \
             patch("app.table", side_effect=mock_table_list):
            list_res = self.client.get("/query/list?radius_km=50")
            self.assertEqual(list_res.status_code, 200)
            items = list_res.get_json()["data"]["queries"]
            self.assertEqual(len(items), 0, "Provider 25km away must not match a 10km request")

        # 3b: In seeker select-provider, selecting the 25km provider must be rejected with 403
        self.login_as(role="seeker", user_id=self.seeker_id)

        def mock_table_select(name):
            if name == "queries":
                return MockTable(query_record)
            elif name == "responses":
                return MockTable(distant_response)
            elif name == "users":
                return MockTable(distant_provider)
            elif name == "provider_locations":
                return MockTable(None)
            return MockTable()

        with patch("app.table", side_effect=mock_table_select):
            select_res = self.client.post(
                "/query/select-provider",
                data={
                    "csrf_token": "audit-test-csrf-token",
                    "query_id": self.query_id,
                    "response_id": self.response_id,
                },
            )
            self.assertEqual(select_res.status_code, 403)
            self.assertIn("outside the allowed search radius", get_error_message(select_res))

    # -------------------------------------------------------------------------
    # 4. Missing or invalid coordinates fail safely
    # -------------------------------------------------------------------------
    def test_missing_or_invalid_coordinates_fail_safely(self):
        self.login_as(role="seeker", user_id=self.seeker_id)

        query_invalid = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "lat": None,
            "lng": None,
            "status": "open",
        }
        prov_record = {
            "id": self.provider_id,
            "lat": 28.5,
            "lng": 77.5,
            "is_active": True,
        }
        resp_record = {
            "id": self.response_id,
            "query_id": self.query_id,
            "provider_id": self.provider_id,
            "status": "available",
            "users": prov_record,
        }

        # With no coordinates on query and no coordinates on seeker profile
        def mock_table(name):
            if name == "queries":
                return MockTable(query_invalid)
            elif name == "responses":
                return MockTable([resp_record])
            elif name == "users":
                return MockTable({"id": self.seeker_id, "lat": None, "lng": None})
            return MockTable()

        with patch("app.table", side_effect=mock_table):
            # query_responses returns distance=None without crash or fabricated 0
            res = self.client.get(f"/query/responses/{self.query_id}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()["data"]
            self.assertIsNone(data["responses"][0]["distance"])
            self.assertEqual(data["responses"][0]["distance_label"], "Distance unavailable")

            # provider_query_eligibility fails safely when query coordinates are missing
            allowed, msg, status = nearfind.provider_query_eligibility(query_invalid, prov_record)
            self.assertFalse(allowed)
            self.assertEqual(status, 409)
            self.assertIn("valid location", msg)

            # get_query_effective_coordinates safely handles missing coordinates
            qlat, qlng, qsrc = nearfind.get_query_effective_coordinates(query_invalid)
            self.assertIsNone(qlat)
            self.assertIsNone(qlng)
            self.assertEqual(qsrc, "missing")

    # -------------------------------------------------------------------------
    # 5. Existing requests without request-specific coordinates follow documented fallback policy
    # -------------------------------------------------------------------------
    def test_legacy_requests_follow_documented_profile_fallback_policy(self):
        # Legacy query record has NULL coordinates
        legacy_query = {
            "id": self.query_id,
            "seeker_id": self.seeker_id,
            "lat": None,
            "lng": None,
            "radius_km": 10.0,
            "status": "open",
        }
        # Seeker user profile has valid coordinates in Gorakhpur
        seeker_profile = {
            "id": self.seeker_id,
            "lat": 26.7606,
            "lng": 83.3732,
        }
        # Provider is 3 km away
        nearby_provider = {
            "id": self.provider_id,
            "lat": 26.7800,
            "lng": 83.3900,
            "is_active": True,
        }
        resp_record = {
            "id": self.response_id,
            "query_id": self.query_id,
            "provider_id": self.provider_id,
            "status": "available",
            "users": nearby_provider,
        }

        # Test helper directly
        def mock_table(name):
            if name == "users":
                return MockTable(seeker_profile)
            return MockTable()

        with patch("app.table", side_effect=mock_table):
            lat, lng, source = nearfind.get_query_effective_coordinates(legacy_query)
            self.assertEqual(source, "profile_fallback")
            self.assertAlmostEqual(lat, 26.7606)
            self.assertAlmostEqual(lng, 83.3732)

        # In query responses: distance calculated correctly from fallback profile coordinates
        self.login_as(role="seeker", user_id=self.seeker_id)

        def mock_table_responses(name):
            if name == "queries":
                return MockTable(legacy_query)
            elif name == "responses":
                return MockTable([resp_record])
            elif name == "users":
                return MockTable(seeker_profile)
            return MockTable()

        with patch("app.table", side_effect=mock_table_responses):
            res = self.client.get(f"/query/responses/{self.query_id}")
            self.assertEqual(res.status_code, 200)
            resp_data = res.get_json()["data"]["responses"][0]
            expected_dist = round(haversine(26.7606, 83.3732, 26.7800, 83.3900), 2)
            self.assertAlmostEqual(resp_data["distance"], expected_dist, places=1)
            self.assertTrue(resp_data["location_available"])

    # -------------------------------------------------------------------------
    # 6. Driver profile and identity-document uploads enforce authorization and private access
    # -------------------------------------------------------------------------
    def test_driver_profile_and_documents_authorization_and_validation(self):
        # 6a: Unauthenticated user is redirected to /login
        res_unauth = self.client.get("/provider/driver-onboarding")
        self.assertEqual(res_unauth.status_code, 302)
        self.assertIn("/login", res_unauth.headers["Location"])

        # 6b: Seeker cannot access driver onboarding (redirects to /)
        self.login_as(role="seeker")
        res_seeker = self.client.get("/provider/driver-onboarding")
        self.assertEqual(res_seeker.status_code, 302)
        self.assertEqual(res_seeker.headers["Location"], "/")

        # 6c: Disallowed file extension / executable rejected on document upload
        self.login_as(role="provider")
        fake_exe = (io.BytesIO(b"MZ\x90\x00\x03\x00\x00\x00"), "malicious.exe")
        res_bad_file = self.client.post(
            "/provider/driver-document/upload",
            data={
                "csrf_token": "audit-test-csrf-token",
                "verification_type": "identity",
                "document": fake_exe,
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res_bad_file.status_code, 400)
        self.assertIn("Only JPG, PNG and PDF", get_error_message(res_bad_file))

        # 6d: Profile photo upload rejects non-image formats (e.g. PDF)
        fake_pdf = (io.BytesIO(b"%PDF-1.4 test"), "not_an_image.pdf")
        res_bad_photo = self.client.post(
            "/provider/driver-photo/upload",
            data={
                "csrf_token": "audit-test-csrf-token",
                "photo": fake_pdf,
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res_bad_photo.status_code, 400)

        # 6e: Valid image uploads successfully to public photo storage with 201 status
        valid_jpeg = (
            io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb" + b"\x00" * 100),
            "driver_avatar.jpg",
        )
        with patch("app.upload_public_image", return_value={"image_url": "https://storage.supabase.co/photo.jpg", "filename": "driver_avatar.jpg"}), \
             patch("app.table", return_value=MockTable()):
            res_photo = self.client.post(
                "/provider/driver-photo/upload",
                data={
                    "csrf_token": "audit-test-csrf-token",
                    "photo": valid_jpeg,
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(res_photo.status_code, 201)
            self.assertEqual(res_photo.get_json()["data"]["photo_url"], "https://storage.supabase.co/photo.jpg")


if __name__ == "__main__":
    unittest.main()
