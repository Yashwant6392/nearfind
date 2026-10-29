"""
tests/test_demo_similarity.py

Automated tests for the standalone NearFind TechExpo ML Demo:
- Web route rendering
- Sample image generation
- File validation (missing, empty, corrupt, oversized)
- Real pipeline execution (576-dim feature vector, cosine score, match level, latency, disclaimer)
"""

import io
import unittest
from PIL import Image

from demo_similarity import demo_app


def _make_test_image_bytes(color=(255, 0, 0), size=(64, 64)):
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestDemoSimilarity(unittest.TestCase):
    def setUp(self):
        demo_app.config.update(TESTING=True)
        self.client = demo_app.test_client()

    def test_index_route_renders_html(self):
        """GET / should render the TechExpo demo HTML template."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"NearFind TechExpo ML Demo", res.data)
        self.assertIn(b"MobileNetV3", res.data)
        self.assertIn(b"Run AI Visual Comparison", res.data)

    def test_sample_endpoints_return_data_urls(self):
        """GET /demo/sample/<type> returns base64 data URLs for quick testing."""
        for sample_type in ("identical", "similar", "different"):
            res = self.client.get(f"/demo/sample/{sample_type}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertIn("ref_url", data)
            self.assertIn("prod_url", data)
            self.assertTrue(data["ref_url"].startswith("data:image/jpeg;base64,"))
            self.assertTrue(data["prod_url"].startswith("data:image/jpeg;base64,"))

    def test_sample_invalid_type_returns_400(self):
        res = self.client.get("/demo/sample/invalid_type")
        self.assertEqual(res.status_code, 400)

    def test_compare_missing_files_returns_400(self):
        """POST /demo/compare without files returns 400 Bad Request."""
        res = self.client.post("/demo/compare", data={})
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])

    def test_compare_empty_file_returns_400(self):
        """POST /demo/compare with 0-byte file returns 400."""
        data = {
            "ref_image": (io.BytesIO(b""), "ref.jpg"),
            "prod_image": (io.BytesIO(_make_test_image_bytes()), "prod.jpg"),
        }
        res = self.client.post("/demo/compare", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        json_data = res.get_json()
        self.assertFalse(json_data["success"])
        self.assertIn("empty", json_data["error"].lower())

    def test_compare_corrupted_file_returns_400(self):
        """POST /demo/compare with corrupt image bytes returns 400."""
        data = {
            "ref_image": (io.BytesIO(b"corrupt-image-data-here"), "ref.jpg"),
            "prod_image": (io.BytesIO(_make_test_image_bytes()), "prod.jpg"),
        }
        res = self.client.post("/demo/compare", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        json_data = res.get_json()
        self.assertFalse(json_data["success"])

    def test_compare_identical_images_runs_real_inference(self):
        """POST /demo/compare with identical images runs real model and returns ~100% score."""
        img_bytes = _make_test_image_bytes(color=(30, 144, 255), size=(64, 64))
        data = {
            "ref_image": (io.BytesIO(img_bytes), "ref.jpg"),
            "prod_image": (io.BytesIO(img_bytes), "prod.jpg"),
        }
        res = self.client.post("/demo/compare", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)
        json_data = res.get_json()
        self.assertTrue(json_data["success"])
        self.assertAlmostEqual(json_data["score"], 100.0, delta=1.0)
        self.assertEqual(json_data["level"], "High Match")
        self.assertEqual(json_data["embedding_dim"], 576)
        self.assertEqual(json_data["device"], "CPU (PyTorch)")
        self.assertGreater(json_data["elapsed_ms"], 0)
        self.assertIn("similarity score is an automated visual estimate", json_data["disclaimer"].lower())


if __name__ == "__main__":
    unittest.main()
