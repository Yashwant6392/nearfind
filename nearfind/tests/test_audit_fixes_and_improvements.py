import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as nearfind
from services.location_service import (
    coerce_float,
    format_distance,
    haversine,
    public_provider_location,
    valid_coordinates,
)
from services.storage_service import (
    ALLOWED_VERIFICATION_MIME,
    MAX_DOC_BYTES,
    create_signed_verification_url,
    upload_private_verification_document,
    validate_verification_document,
)
from werkzeug.datastructures import FileStorage


class TestLocationServiceAndCoordinates(unittest.TestCase):
    def test_valid_coordinates(self):
        self.assertTrue(valid_coordinates(12.9716, 77.5946))
        self.assertTrue(valid_coordinates("12.9716", "77.5946"))
        self.assertTrue(valid_coordinates(-90, -180))
        self.assertTrue(valid_coordinates(90, 180))
        self.assertTrue(valid_coordinates(0, 0))

    def test_invalid_coordinates(self):
        self.assertFalse(valid_coordinates(95.0, 77.0))
        self.assertFalse(valid_coordinates(-91.0, 77.0))
        self.assertFalse(valid_coordinates(12.0, 185.0))
        self.assertFalse(valid_coordinates(12.0, -185.0))
        self.assertFalse(valid_coordinates("invalid", 77.0))
        self.assertFalse(valid_coordinates(None, None))
        self.assertFalse(valid_coordinates(float("nan"), 77.0))

    def test_haversine_distance(self):
        dist = haversine(12.9716, 77.5946, 13.0827, 80.2707)
        self.assertGreater(dist, 280)
        self.assertLess(dist, 310)

        zero_dist = haversine(12.9716, 77.5946, 12.9716, 77.5946)
        self.assertEqual(zero_dist, 0.0)

    def test_haversine_invalid_raises_error(self):
        with self.assertRaises(ValueError):
            haversine(95.0, 77.0, 12.0, 77.0)

    def test_format_distance(self):
        self.assertEqual(format_distance(0.5), "500 m away")
        self.assertEqual(format_distance(2.456), "2.46 km away")
        self.assertEqual(format_distance(0.0005), "<1 m away")

    def test_public_provider_location_privacy(self):
        individual = {"lat": 12.345678, "lng": 77.876543, "provider_type": "individual"}
        lat, lng = public_provider_location(individual, selected=False)
        self.assertEqual(lat, 12.346)
        self.assertEqual(lng, 77.877)

        exact_lat, exact_lng = public_provider_location(individual, selected=True)
        self.assertEqual(exact_lat, 12.345678)
        self.assertEqual(exact_lng, 77.876543)

        shop = {"lat": 12.345678, "lng": 77.876543, "provider_type": "shop"}
        shop_lat, shop_lng = public_provider_location(shop, selected=False)
        self.assertEqual(shop_lat, 12.345678)
        self.assertEqual(shop_lng, 77.876543)


class TestPostQueryCoordinateValidation(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def login_as_seeker(self, lat=None, lng=None):
        with self.client.session_transaction() as session:
            session.update(
                user_id="22222222-2222-4222-8222-222222222222",
                role="seeker",
                _csrf_token="test-csrf-token",
            )
            if lat is not None:
                session["lat"] = lat
            if lng is not None:
                session["lng"] = lng

    def test_post_query_rejects_missing_coordinates_with_400(self):
        self.login_as_seeker(lat=None, lng=None)
        with patch.object(nearfind, "current_profile", return_value={"lat": None, "lng": None}):
            response = self.client.post(
                "/query/post",
                data={
                    "csrf_token": "test-csrf-token",
                    "item_name": "Emergency Medicine",
                    "category": "Pharmacy",
                    "urgency": "medium",
                    "radius_km": "10",
                    "lat": "",
                    "lng": "",
                },
            )
        self.assertEqual(response.status_code, 400)
        body = response.get_data(as_text=True)
        self.assertIn("Location coordinates are required", body)

    def test_post_query_rejects_out_of_range_coordinates_with_400(self):
        self.login_as_seeker(lat=None, lng=None)
        with patch.object(nearfind, "current_profile", return_value={"lat": None, "lng": None}):
            response = self.client.post(
                "/query/post",
                data={
                    "csrf_token": "test-csrf-token",
                    "item_name": "Emergency Medicine",
                    "category": "Pharmacy",
                    "urgency": "medium",
                    "radius_km": "10",
                    "lat": "120.0",  # Invalid latitude (> 90)
                    "lng": "77.5946",
                },
            )
        self.assertEqual(response.status_code, 400)
        body = response.get_data(as_text=True)
        self.assertIn("Latitude must be at most 90", body)

    def test_post_query_accepts_valid_coordinates_and_syncs_session(self):
        self.login_as_seeker(lat=None, lng=None)

        class MockQueryTable:
            def __init__(self):
                self.inserted_data = None

            def insert(self, record):
                self.inserted_data = record
                return self

            def select(self, cols):
                return self

            def eq(self, col, val):
                return self

            def maybe_single(self):
                return self

            def execute(self):
                return SimpleNamespace(data=None)

        mock_query_table = MockQueryTable()

        with patch.object(nearfind, "current_profile", return_value={"lat": None, "lng": None}), \
             patch.object(nearfind, "table", return_value=mock_query_table), \
             patch.object(nearfind, "notify_nearby_providers", return_value=3):
            response = self.client.post(
                "/query/post",
                data={
                    "csrf_token": "test-csrf-token",
                    "item_name": "Oxygen Cylinder",
                    "category": "Medical Equipment",
                    "description": "5L portable cylinder",
                    "location": "Indiranagar",
                    "urgency": "urgent",
                    "radius_km": "15",
                    "lat": "12.9716",
                    "lng": "77.5946",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/seeker/responses/", response.headers["Location"])
        self.assertIsNotNone(mock_query_table.inserted_data)
        self.assertEqual(mock_query_table.inserted_data["lat"], 12.9716)
        self.assertEqual(mock_query_table.inserted_data["lng"], 77.5946)


class TestDriverDocumentSecurity(unittest.TestCase):
    def test_validate_valid_pdf_document(self):
        pdf_content = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
        file_obj = FileStorage(
            stream=io.BytesIO(pdf_content),
            filename="licence_scan.pdf",
            content_type="application/pdf",
        )
        ext = validate_verification_document(file_obj)
        self.assertEqual(ext, ".pdf")

    def test_validate_valid_jpeg_document(self):
        jpeg_content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb"
        file_obj = FileStorage(
            stream=io.BytesIO(jpeg_content),
            filename="profile_photo.jpg",
            content_type="image/jpeg",
        )
        ext = validate_verification_document(file_obj)
        self.assertEqual(ext, ".jpg")

    def test_validate_valid_png_document(self):
        png_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        file_obj = FileStorage(
            stream=io.BytesIO(png_content),
            filename="proof.png",
            content_type="image/png",
        )
        ext = validate_verification_document(file_obj)
        self.assertEqual(ext, ".png")

    def test_reject_dangerous_or_disallowed_extension(self):
        dangerous_extensions = ["payload.exe", "script.sh", "page.html", "exploit.php", "badge.svg"]
        for fname in dangerous_extensions:
            file_obj = FileStorage(
                stream=io.BytesIO(b"malicious content here"),
                filename=fname,
                content_type="application/octet-stream",
            )
            with self.assertRaises(ValueError) as cm:
                validate_verification_document(file_obj)
            self.assertIn("Only JPG, PNG and PDF", str(cm.exception))

    def test_reject_fake_extension_mime_mismatch(self):
        file_obj = FileStorage(
            stream=io.BytesIO(b"This is not a real PDF file!"),
            filename="fake.pdf",
            content_type="application/pdf",
        )
        with self.assertRaises(ValueError) as cm:
            validate_verification_document(file_obj)
        self.assertIn("Invalid verification document format", str(cm.exception))

    def test_reject_oversized_document(self):
        file_obj = FileStorage(
            stream=io.BytesIO(b"%PDF-1.4" + b"0" * (MAX_DOC_BYTES + 20)),
            filename="large_scan.pdf",
            content_type="application/pdf",
        )
        with self.assertRaises(ValueError) as cm:
            validate_verification_document(file_obj)
        self.assertIn("Verification document must be 5 MB or smaller", str(cm.exception))

    def test_upload_private_verification_document(self):
        pdf_content = b"%PDF-1.4\nvalid document content"
        file_obj = FileStorage(
            stream=io.BytesIO(pdf_content),
            filename="licence.pdf",
            content_type="application/pdf",
        )
        mock_client = MagicMock()
        mock_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_bucket

        with patch("services.storage_service.get_supabase", return_value=mock_client):
            res = upload_private_verification_document(
                bucket="driver-documents",
                file_storage=file_obj,
                user_id="user-123",
                doc_type="driving_licence",
            )

        self.assertIn("driver-verification/user-123/driving_licence_", res["path"])
        self.assertTrue(res["path"].endswith(".pdf"))
        self.assertEqual(res["filename"], "licence.pdf")
        self.assertNotIn("image_url", res)
        mock_bucket.upload.assert_called_once()

    def test_signed_verification_url_expiration(self):
        mock_client = MagicMock()
        mock_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_bucket
        mock_bucket.create_signed_url.return_value = {
            "signedURL": "https://supabase.co/storage/v1/object/sign/driver-documents/path?token=xyz"
        }

        with patch("services.storage_service.get_supabase", return_value=mock_client):
            url = create_signed_verification_url(
                bucket="driver-documents",
                storage_path="driver-verification/user-123/doc.pdf",
                expires_in=300,
            )

        self.assertIn("https://supabase.co", url)
        mock_bucket.create_signed_url.assert_called_once_with(
            "driver-verification/user-123/doc.pdf", 300
        )


class TestSeekerDashboardReservationsBatch(unittest.TestCase):
    def test_batch_lookup_uses_in_query_when_available(self):
        from app import get_seeker_bed_reservations

        holds_rows = [
            {"id": "r1", "hospital_id": "hosp-1", "bed_type": "icu", "status": "held", "created_at": "2026-10-19T20:00:00Z", "expires_at": "2099-01-01T00:00:00Z"},
            {"id": "r2", "hospital_id": "hosp-2", "bed_type": "oxygen", "status": "held", "created_at": "2026-10-19T20:00:00Z", "expires_at": "2099-01-01T00:00:00Z"},
        ]

        class MockQuery:
            def __init__(self, table_name):
                self.table_name = table_name
                self.in_called = False

            def select(self, cols):
                return self

            def eq(self, col, val):
                return self

            def order(self, col, desc=True):
                return self

            def limit(self, count):
                return self

            def in_(self, col, vals):
                self.in_called = True
                return self

            def execute(self):
                if self.table_name == "hospital_bed_holds":
                    return SimpleNamespace(data=holds_rows)
                elif self.table_name == "hospitals":
                    return SimpleNamespace(
                        data=[
                            {"id": "hosp-1", "name": "City Care Hospital", "address": "123 Main St", "phone": "12345"},
                            {"id": "hosp-2", "name": "Metro General Hospital", "address": "456 Side St", "phone": "67890"},
                        ]
                    )
                return SimpleNamespace(data=[])

        def fake_table(name):
            return MockQuery(name)

        with patch("app.table", side_effect=fake_table):
            result = get_seeker_bed_reservations("user-seeker-id")

        self.assertIsInstance(result, dict)
        self.assertIsNotNone(result.get("active_hold"))
        self.assertEqual(result["active_hold"]["hospital_name"], "City Care Hospital")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["recent_holds"][1]["hospital_name"], "Metro General Hospital")


class TestRoleAuthorizationAndLocationEndpoints(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def test_unauthenticated_provider_dashboard_redirects_to_login(self):
        response = self.client.get("/provider/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_seeker_cannot_access_provider_dashboard(self):
        with self.client.session_transaction() as session:
            session.update(
                user_id="seeker-id-1",
                role="seeker",
                _csrf_token="tok",
            )
        response = self.client.get("/provider/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_unauthorized_user_cannot_access_reviewer_console(self):
        with self.client.session_transaction() as session:
            session.update(
                user_id="random-user-id",
                role="seeker",
                _csrf_token="tok",
            )
        class MockEmptyTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def maybe_single(self): return self
            def execute(self): return SimpleNamespace(data=None)

        with patch("app.table", return_value=MockEmptyTable()):
            response = self.client.get("/reviewer/driver-profiles")
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Trusted driver reviewer authorization required", response.data)

    def test_reviewer_cannot_approve_own_driver_profile(self):
        reviewer_id = "33333333-3333-4333-8333-333333333333"
        with self.client.session_transaction() as session:
            session.update(
                user_id=reviewer_id,
                role="provider",
                _csrf_token="tok",
            )

        class MockReviewerTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def maybe_single(self): return self
            def execute(self): return SimpleNamespace(data={"user_id": reviewer_id})

        with patch("app.table", return_value=MockReviewerTable()):
            response = self.client.post(
                "/reviewer/driver-profiles",
                data={
                    "csrf_token": "tok",
                    "driver_id": reviewer_id,  # Attempting to approve own profile
                    "decision": "approved",
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Reviewers cannot approve their own driver profile", response.data)

    def test_update_location_endpoint_validation(self):
        with self.client.session_transaction() as session:
            session.update(
                user_id="test-user-id",
                role="seeker",
                _csrf_token="tok",
            )

        # Invalid lat/lng
        response = self.client.post(
            "/user/update-location",
            headers={"X-CSRFToken": "tok"},
            json={"lat": 95.0, "lng": 77.0},
        )
        self.assertEqual(response.status_code, 400)

        # Valid lat/lng
        class MockUserTable:
            def update(self, *a): return self
            def eq(self, *a): return self
            def execute(self): return SimpleNamespace(data=[{"id": "test-user-id"}])

        with patch("app.table", return_value=MockUserTable()):
            response = self.client.post(
                "/user/update-location",
                headers={"X-CSRFToken": "tok"},
                json={"lat": 12.9716, "lng": 77.5946},
            )
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertTrue(json_data.get("success"))
        self.assertAlmostEqual(json_data["data"]["lat"], 12.9716)
        self.assertAlmostEqual(json_data["data"]["lng"], 77.5946)


class TestDistanceAndNavigationAccuracy(unittest.TestCase):
    def test_coordinate_order_and_symmetry(self):
        # Latitudes must come first, longitudes second
        panki_lat, panki_lng = 26.4716, 80.2443
        kanpur_central_lat, kanpur_central_lng = 26.4539, 80.3513

        dist_ab = haversine(panki_lat, panki_lng, kanpur_central_lat, kanpur_central_lng)
        dist_ba = haversine(kanpur_central_lat, kanpur_central_lng, panki_lat, panki_lng)
        self.assertAlmostEqual(dist_ab, dist_ba, places=5)

        # Swapping lat and lng for coordinates where lng > 90 must raise ValueError
        with self.assertRaises(ValueError):
            haversine(80.2443, 26.4716, 120.0, 15.0)  # 120 is invalid latitude

    def test_panki_to_kanpur_central_sanity_check(self):
        # Panki to Kanpur Central is ~10.86 km straight-line (corresponding to ~11-13 km road distance)
        panki_lat, panki_lng = 26.4716, 80.2443
        kanpur_central_lat, kanpur_central_lng = 26.4539, 80.3513

        straight_line_km = haversine(panki_lat, panki_lng, kanpur_central_lat, kanpur_central_lng)
        self.assertGreater(straight_line_km, 10.0, "Straight-line distance should be at least 10 km")
        self.assertLess(straight_line_km, 11.5, "Straight-line distance should be less than 11.5 km")
        self.assertAlmostEqual(straight_line_km, 10.86, delta=0.2)

    def test_metres_and_kilometres_formatting(self):
        self.assertEqual(format_distance(0.0004), "<1 m away")
        self.assertEqual(format_distance(0.05), "50 m away")
        self.assertEqual(format_distance(0.5), "500 m away")
        self.assertEqual(format_distance(0.999), "999 m away")
        self.assertEqual(format_distance(1.0), "1.00 km away")
        self.assertEqual(format_distance(12.345), "12.35 km away")

    def test_missing_and_invalid_coordinates(self):
        self.assertFalse(valid_coordinates(None, None))
        self.assertFalse(valid_coordinates(26.4716, None))
        self.assertFalse(valid_coordinates("invalid", 80.2443))
        self.assertFalse(valid_coordinates(float("nan"), 80.2443))
        self.assertFalse(valid_coordinates(91.0, 80.0))
        self.assertFalse(valid_coordinates(26.0, 181.0))

        with self.assertRaises(ValueError):
            haversine(None, 80.0, 26.0, 80.0)

    def test_distant_global_locations(self):
        # London to Tokyo: ~9560 km
        london_lat, london_lng = 51.5074, -0.1278
        tokyo_lat, tokyo_lng = 35.6762, 139.6503
        dist_london_tokyo = haversine(london_lat, london_lng, tokyo_lat, tokyo_lng)
        self.assertGreater(dist_london_tokyo, 9500)
        self.assertLess(dist_london_tokyo, 9650)

        # New York to Sydney: ~16000 km
        ny_lat, ny_lng = 40.7128, -74.0060
        sydney_lat, sydney_lng = -33.8688, 151.2093
        dist_ny_sydney = haversine(ny_lat, ny_lng, sydney_lat, sydney_lng)
        self.assertGreater(dist_ny_sydney, 15900)
        self.assertLess(dist_ny_sydney, 16150)


class TestQueryResponsesLocationSafety(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def test_responses_endpoint_missing_coordinates_safe(self):
        with self.client.session_transaction() as session:
            session.update(
                user_id="seeker-123",
                role="seeker",
                _csrf_token="tok",
            )

        fake_query = {
            "id": "query-abc",
            "seeker_id": "seeker-123",
            "item_name": "Test Item",
            "lat": None,  # Missing query lat/lng
            "lng": None,
        }

        fake_responses = [
            {
                "id": "resp-1",
                "query_id": "query-abc",
                "provider_id": "prov-1",
                "status": "available",
                "price": 100,
                "message": "I have it",
                "created_at": "2026-10-20T00:00:00Z",
                "image_url": None,
                "users": {
                    "id": "prov-1",
                    "name": "Shop A",
                    "business_name": "Shop A Store",
                    "provider_type": "shop",
                    "phone": "+919876543210",
                    "lat": 26.47,
                    "lng": 80.24,
                },
            }
        ]

        class MockQueryTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def maybe_single(self): return self
            def order(self, *a, **k): return self
            def execute(self): return SimpleNamespace(data=[fake_query])

        class MockResponseTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def order(self, *a, **k): return self
            def execute(self): return SimpleNamespace(data=fake_responses)

        def mock_table(table_name):
            if table_name == "queries":
                return MockQueryTable()
            return MockResponseTable()

        with patch("app.table", side_effect=mock_table), \
             patch("app.require_query_owner", return_value=fake_query):
            response = self.client.get("/query/responses/query-abc")
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data.get("success"))
            resp_item = data["data"]["responses"][0]
            # Since query coordinates were None, distance should safely be None without error
            self.assertIsNone(resp_item["distance"])
            self.assertTrue(resp_item["location_available"])


class TestServerSideRadiusAndDistanceEnforcement(unittest.TestCase):
    def test_provider_query_eligibility_10km_vs_20km(self):
        from app import provider_query_eligibility

        # Seeker request at Kanpur Central with 10 km radius
        query = {
            "id": "q-1",
            "lat": 26.4539,
            "lng": 80.3513,
            "radius_km": 10.0,
            "seeker_id": "seeker-1",
            "status": "open",
            "expires_at": "2099-01-01T00:00:00Z",
        }

        # Provider 1: ~10.86 km away (outside 10 km request radius)
        provider_outside = {
            "id": "prov-out",
            "is_active": True,
            "lat": 26.4716,
            "lng": 80.2443,
            "service_radius_km": 25.0,
        }
        allowed, detail, status = provider_query_eligibility(query, provider_outside)
        self.assertFalse(allowed)
        self.assertEqual(status, 403)
        self.assertIn("service radius", detail.lower())

        # Provider 2: ~20 km away (far outside 10 km request radius)
        provider_20km = {
            "id": "prov-20km",
            "is_active": True,
            "lat": 26.6339,
            "lng": 80.3513,
            "service_radius_km": 50.0,
        }
        allowed_20, detail_20, status_20 = provider_query_eligibility(query, provider_20km)
        self.assertFalse(allowed_20)
        self.assertEqual(status_20, 403)
        self.assertIn("service radius", detail_20.lower())

        # Provider 3: ~3 km away (inside 10 km request radius)
        provider_inside = {
            "id": "prov-in",
            "is_active": True,
            "lat": 26.4650,
            "lng": 80.3400,
            "service_radius_km": 15.0,
        }
        allowed_in, detail_in, status_in = provider_query_eligibility(query, provider_inside)
        self.assertTrue(allowed_in)
        self.assertEqual(status_in, 200)
        self.assertIsInstance(detail_in, float)
        self.assertLess(detail_in, 10.0)

    def test_provider_restricted_by_own_smaller_service_radius(self):
        from app import provider_query_eligibility

        # Seeker query has 20 km radius
        query = {
            "id": "q-wide",
            "lat": 26.4539,
            "lng": 80.3513,
            "radius_km": 20.0,
            "seeker_id": "seeker-1",
            "status": "open",
            "expires_at": "2099-01-01T00:00:00Z",
        }

        # Provider is 10.86 km away, but provider's own service radius is only 5 km
        provider_small_radius = {
            "id": "prov-small",
            "is_active": True,
            "lat": 26.4716,
            "lng": 80.2443,
            "service_radius_km": 5.0,
        }
        allowed, detail, status = provider_query_eligibility(query, provider_small_radius)
        self.assertFalse(allowed)
        self.assertEqual(status, 403)
        self.assertIn("service radius", detail.lower())

    def test_list_queries_distance_label_and_server_radius_filtering(self):
        nearfind.app.config.update(TESTING=True)
        client = nearfind.app.test_client()
        with client.session_transaction() as session:
            session.update(
                user_id="prov-1",
                role="provider",
                _csrf_token="tok",
            )

        fake_provider = {
            "id": "prov-1",
            "role": "provider",
            "is_active": True,
            "lat": 26.4539,
            "lng": 80.3513,
        }

        mock_rows = [
            # Query 1: ~3 km away, seeker radius 10 km -> Should be included
            {
                "id": "q-near",
                "seeker_id": "s-1",
                "lat": 26.4650,
                "lng": 80.3400,
                "radius_km": 10.0,
                "status": "open",
                "item_name": "Near Medicine",
                "created_at": "2026-10-20T00:00:00Z",
            },
            # Query 2: ~20 km away, seeker radius 10 km -> Must be excluded by server radius filter
            {
                "id": "q-far",
                "seeker_id": "s-2",
                "lat": 26.6339,
                "lng": 80.3513,
                "radius_km": 10.0,
                "status": "open",
                "item_name": "Far Medicine",
                "category": "Pharmacy",
                "created_at": "2026-10-20T00:00:00Z",
            },
        ]

        class MockQueryBuilder:
            def select(self, *a): return self
            def eq(self, *a): return self
            def lt(self, *a): return self
            def update(self, *a): return self
            def order(self, *a, **k): return self
            def limit(self, *a): return self
            def execute(self): return SimpleNamespace(data=mock_rows)

        with patch("app.table", return_value=MockQueryBuilder()), \
             patch("app.current_profile", return_value=fake_provider):
            response = client.get("/query/list?radius_km=15")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get("success"))
        results = data["data"]["queries"]
        result_ids = [r["id"] for r in results]
        self.assertIn("q-near", result_ids)
        self.assertNotIn("q-far", result_ids, "20 km query must NOT be returned when seeker radius is 10 km")

        near_item = next(r for r in results if r["id"] == "q-near")
        self.assertEqual(near_item.get("distance_type"), "straight_line")
        self.assertEqual(near_item.get("distance_type_label"), "straight-line distance")
        self.assertTrue(near_item.get("distance_label", "").endswith("away"))


class TestDriverDocumentUploadAndReviewerWorkflow(unittest.TestCase):
    def setUp(self):
        nearfind.app.config.update(TESTING=True)
        self.client = nearfind.app.test_client()

    def test_async_driver_document_upload_success(self):
        with self.client.session_transaction() as session:
            session.update(
                user_id="prov-driver-1",
                role="provider",
                _csrf_token="tok",
            )

        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
        data = {
            "verification_type": "identity",
            "document": (io.BytesIO(pdf_bytes), "aadhaar_scan.pdf", "application/pdf"),
        }

        mock_storage_res = {
            "bucket": "driver-documents",
            "path": "driver-verification/prov-driver-1/identity_test.pdf",
            "filename": "aadhaar_scan.pdf",
        }

        class MockReqTable:
            def __init__(self):
                self.record = None
            def insert(self, rec):
                self.record = rec
                return self
            def execute(self):
                return SimpleNamespace(data=[{"id": "doc-req-123"}])

        mock_req = MockReqTable()
        with patch("app.upload_private_verification_document", return_value=mock_storage_res), \
             patch("app.create_signed_verification_url", return_value="https://supabase.co/signed/doc.pdf"), \
             patch("app.table", return_value=mock_req):
            response = self.client.post(
                "/provider/driver-document/upload",
                headers={"X-CSRFToken": "tok"},
                data=data,
                content_type="multipart/form-data",
            )
        self.assertIn(response.status_code, (200, 201))
        json_data = response.get_json()
        self.assertTrue(json_data.get("success"))
        self.assertTrue("id" in json_data["data"] or "document_id" in json_data["data"])
        self.assertIn("https://supabase.co/signed/doc.pdf", json_data["data"]["signed_url"])

    def test_async_driver_document_upload_rejects_executable(self):
        with self.client.session_transaction() as session:
            session.update(
                user_id="prov-driver-1",
                role="provider",
                _csrf_token="tok",
            )

        bad_bytes = b"MZ\x90\x00\x03\x00\x00\x00"  # Windows executable header
        data = {
            "verification_type": "identity",
            "document": (io.BytesIO(bad_bytes), "payload.exe", "application/octet-stream"),
        }
        response = self.client.post(
            "/provider/driver-document/upload",
            headers={"X-CSRFToken": "tok"},
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        json_data = response.get_json()
        self.assertFalse(json_data.get("success"))
        err_msg = json_data.get("error")
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message", "")
        self.assertIn("Only JPG, PNG and PDF", str(err_msg))

    def test_reviewer_console_renders_uploaded_document_evidence(self):
        reviewer_id = "rev-user-1"
        driver_id = "drv-user-2"

        with self.client.session_transaction() as session:
            session.update(
                user_id=reviewer_id,
                role="provider",
                _csrf_token="tok",
            )

        fake_reviewer = {"user_id": reviewer_id}
        fake_profile = {
            "provider_id": driver_id,
            "experience_years": 5,
            "licence_category": "LMV",
            "languages": ["English", "Hindi"],
            "bio": "Experienced driver",
            "verification_status": "under_review",
            "created_at": "2026-10-19T00:00:00Z",
            "updated_at": "2026-10-20T00:00:00Z",
        }
        fake_doc = {
            "id": "doc-1",
            "verification_type": "identity",
            "status": "pending",
            "document_storage_path": "driver-verification/drv-user-2/identity_123.pdf",
            "submitted_at": "2026-10-20T01:00:00Z",
            "review_notes": None,
        }

        class MockReviewerTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def maybe_single(self): return self
            def execute(self): return SimpleNamespace(data=fake_reviewer)

        class MockProfilesTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def order(self, *a, **k): return self
            def limit(self, *a): return self
            def execute(self): return SimpleNamespace(data=[fake_profile])

        class MockDocsTable:
            def select(self, *a): return self
            def eq(self, *a): return self
            def order(self, *a, **k): return self
            def execute(self): return SimpleNamespace(data=[fake_doc])

        def table_router(name):
            if name == "trusted_driver_reviewers":
                return MockReviewerTable()
            elif name == "driver_profiles":
                return MockProfilesTable()
            elif name == "driver_verification_requests":
                return MockDocsTable()
            return MockReviewerTable()

        with patch("app.table", side_effect=table_router), \
             patch("app.create_signed_verification_url", return_value="https://supabase.co/signed/proof.pdf"):
            response = self.client.get("/reviewer/driver-profiles")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Uploaded Verification Evidence", body)
        self.assertIn("Identity / Aadhaar Document", body)
        self.assertIn("Inspect Document", body)
        self.assertIn("https://supabase.co/signed/proof.pdf", body)


if __name__ == "__main__":
    unittest.main()


