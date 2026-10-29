"""
demo_similarity.py

NearFind TechExpo ML Demo: Standalone Live Visual Product Similarity Comparison.
Demonstrates the real MobileNetV3-Small vision feature extractor and cosine
similarity comparison without requiring the production Gunicorn web stack or Render deployment.

Run locally:
    python demo_similarity.py
Access at:
    http://127.0.0.1:5050
"""

import base64
import io
import os
import sys
import time
from typing import Tuple

from flask import Flask, jsonify, render_template, request
from PIL import Image, ImageDraw

# Add repository root to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from config import MAX_IMAGE_BYTES
from services.similarity_service import (
    DISCLAIMER,
    compute_cosine_similarity,
    extract_embedding,
    load_image,
    normalize_similarity,
)

demo_app = Flask(__name__)
demo_app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB max request


def _create_sample_image(category: str, variant: int = 1) -> str:
    """Generates an illustrative test image for quick TechExpo demo tests."""
    img = Image.new("RGB", (256, 256), color=(240, 242, 245))
    draw = ImageDraw.Draw(img)

    if category == "identical":
        # Teal product box with yellow accent
        draw.rectangle([48, 64, 208, 192], fill=(20, 80, 75), outline=(197, 243, 107), width=3)
        draw.ellipse([96, 96, 160, 160], fill=(197, 243, 107))
    elif category == "similar":
        if variant == 1:
            # Teal box with circular emblem
            draw.rectangle([50, 70, 200, 190], fill=(22, 78, 72), outline=(180, 230, 90), width=3)
            draw.ellipse([95, 95, 155, 155], fill=(180, 230, 90))
        else:
            # Slightly shifted teal box with rounded emblem
            draw.rectangle([55, 65, 205, 185], fill=(30, 85, 80), outline=(190, 240, 100), width=3)
            draw.rounded_rectangle([90, 90, 160, 160], radius=15, fill=(190, 240, 100))
    elif category == "different":
        if variant == 1:
            # Electric drill / tool silhouette in blue
            draw.rectangle([40, 100, 180, 150], fill=(30, 60, 180))
            draw.rectangle([140, 150, 170, 210], fill=(50, 50, 50))
            draw.polygon([(180, 115), (220, 125), (180, 135)], fill=(180, 180, 180))
        else:
            # Red apple / food item
            draw.ellipse([68, 68, 188, 188], fill=(210, 35, 45))
            draw.rectangle([123, 40, 133, 75], fill=(90, 50, 20))
            draw.ellipse([133, 45, 165, 65], fill=(35, 140, 45))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


@demo_app.route("/")
def index():
    return render_template("demo_similarity.html")


@demo_app.route("/demo/sample/<sample_type>")
def get_sample(sample_type):
    """Returns sample base64 images for quick TechExpo testing without manual uploads."""
    if sample_type == "identical":
        ref_url = _create_sample_image("identical", 1)
        prod_url = _create_sample_image("identical", 1)
    elif sample_type == "similar":
        ref_url = _create_sample_image("similar", 1)
        prod_url = _create_sample_image("similar", 2)
    elif sample_type == "different":
        ref_url = _create_sample_image("different", 1)
        prod_url = _create_sample_image("different", 2)
    else:
        return jsonify({"error": "Unknown sample type"}), 400

    return jsonify({"ref_url": ref_url, "prod_url": prod_url})


# TechExpo Standalone Demo Upload Limit:
# Intentionally preserved at 5 MB to allow live uncompressed camera/phone photo uploads during exhibitions.
DEMO_MAX_IMAGE_BYTES = 5 * 1024 * 1024


@demo_app.route("/demo/compare", methods=["POST"])
def compare():
    """
    Accepts two uploaded images (ref_image, prod_image),
    runs MobileNetV3 feature extraction on CPU, and returns authentic similarity metrics.
    """
    if "ref_image" not in request.files or "prod_image" not in request.files:
        return jsonify({"success": False, "error": "Both seeker reference and provider product images are required."}), 400

    ref_file = request.files["ref_image"]
    prod_file = request.files["prod_image"]

    if not ref_file.filename or not prod_file.filename:
        return jsonify({"success": False, "error": "Please select both image files to compare."}), 400

    ref_bytes = ref_file.read()
    prod_bytes = prod_file.read()

    if len(ref_bytes) == 0 or len(prod_bytes) == 0:
        return jsonify({"success": False, "error": "One or both uploaded image files are empty."}), 400

    if len(ref_bytes) > DEMO_MAX_IMAGE_BYTES or len(prod_bytes) > DEMO_MAX_IMAGE_BYTES:
        return jsonify({"success": False, "error": f"Image exceeds maximum allowed size of {DEMO_MAX_IMAGE_BYTES // (1024*1024)} MB."}), 400

    # Validate image content, format (JPEG, PNG, WEBP), and excessive dimensions
    for label, b in (("seeker reference", ref_bytes), ("provider product", prod_bytes)):
        try:
            with Image.open(io.BytesIO(b)) as test_img:
                fmt = (test_img.format or "").upper()
                if fmt not in ("JPEG", "PNG", "WEBP"):
                    return jsonify({"success": False, "error": f"Unsupported format for {label} image. Only JPG, PNG and WEBP images are allowed."}), 400
                if test_img.width > 4096 or test_img.height > 4096:
                    return jsonify({"success": False, "error": f"{label.capitalize()} image dimensions exceed maximum allowable limit (4096×4096)."}), 400
        except Exception:
            return jsonify({"success": False, "error": f"Invalid {label} image file."}), 400

    # 1. Load and downsample images
    try:
        ref_img = load_image(ref_bytes, max_bytes=DEMO_MAX_IMAGE_BYTES)
    except Exception as err:
        return jsonify({"success": False, "error": f"Invalid seeker reference image: {err}"}), 400

    try:
        prod_img = load_image(prod_bytes, max_bytes=DEMO_MAX_IMAGE_BYTES)
    except Exception as err:
        return jsonify({"success": False, "error": f"Invalid provider product image: {err}"}), 400


    # 2. Extract feature embeddings and measure actual CPU inference duration
    start_time = time.perf_counter()
    try:
        ref_emb = extract_embedding(ref_img)
        prod_emb = extract_embedding(prod_img)
        cos_sim = compute_cosine_similarity(ref_emb, prod_emb)
        norm = normalize_similarity(cos_sim)
    except Exception as err:
        return jsonify({"success": False, "error": f"Model inference error: {err}"}), 500
    finally:
        for img in (ref_img, prod_img):
            if hasattr(img, "close"):
                try:
                    img.close()
                except Exception:
                    pass

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    return jsonify({
        "success": True,
        "score": norm["score"],
        "level": norm["level"],
        "raw_similarity": norm["raw_similarity"],
        "elapsed_ms": elapsed_ms,
        "embedding_dim": len(ref_emb),
        "model_name": "MobileNetV3-Small (Torchvision Pretrained)",
        "device": "CPU (PyTorch)",
        "disclaimer": DISCLAIMER,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"\n=======================================================")
    print(f" NearFind TechExpo ML Demo (MobileNetV3-Small)")
    print(f" Running locally on: http://127.0.0.1:{port}")
    print(f"=======================================================\n")
    demo_app.run(host="127.0.0.1", port=port, debug=False)
