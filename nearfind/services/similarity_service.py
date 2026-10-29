"""
services/similarity_service.py

Provides computer-vision based image embedding generation and cosine similarity comparison
between seeker product reference images and provider response product images.
Uses a pretrained, lightweight MobileNetV3 model for fast local CPU execution.
"""

import io
import ipaddress
import logging
import socket
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import httpx
import numpy as np
from PIL import Image, UnidentifiedImageError

from config import MAX_IMAGE_BYTES

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "Similarity score is an automated visual estimate and does not guarantee "
    "that products are identical or authentic."
)

_MODEL = None
_TRANSFORMS = None
_LOCK = threading.Lock()
_EMBEDDING_CACHE: Dict[str, np.ndarray] = {}
_CACHE_LOCK = threading.Lock()
_MAX_CACHE_SIZE = 512


def is_safe_image_url(url: str) -> bool:
    """Validates that a URL is HTTP/HTTPS and does not target localhost or private IP ranges."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_unspecified
                or ip.is_multicast
            ):
                return False
        except ValueError:
            try:
                resolved_ip = socket.gethostbyname(hostname)
                ip = ipaddress.ip_address(resolved_ip)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_unspecified
                    or ip.is_multicast
                ):
                    return False
            except Exception:
                pass
        return True
    except Exception:
        return False


def set_model_for_testing(mock_model: Any, mock_transforms: Any = None) -> None:
    """Helper for unit tests to inject mock models without downloading weights."""
    global _MODEL, _TRANSFORMS
    with _LOCK:
        _MODEL = mock_model
        _TRANSFORMS = mock_transforms or (lambda img: img)


def reset_model() -> None:
    """Helper to reset model singleton to uninitialized state."""
    global _MODEL, _TRANSFORMS
    with _LOCK:
        _MODEL = None
        _TRANSFORMS = None
    with _CACHE_LOCK:
        _EMBEDDING_CACHE.clear()


def get_model() -> Tuple[Any, Any]:
    """
    Lazily loads the pretrained vision model on first invocation.
    Uses MobileNetV3-Small for lightweight CPU inference.
    """
    global _MODEL, _TRANSFORMS
    if _MODEL is not None:
        return _MODEL, _TRANSFORMS

    with _LOCK:
        if _MODEL is not None:
            return _MODEL, _TRANSFORMS
        try:
            import torch

            import torchvision.models as models
            from torchvision.models import MobileNet_V3_Small_Weights

            weights = MobileNet_V3_Small_Weights.DEFAULT
            model = models.mobilenet_v3_small(weights=weights)
            # Replace final classification layer with Identity to output 576-dim feature embedding
            model.classifier = torch.nn.Identity()
            model.eval()
            transforms = weights.transforms()
            _MODEL = model
            _TRANSFORMS = transforms
            return _MODEL, _TRANSFORMS
        except Exception as error:
            logger.warning("Pretrained vision model is unavailable: %s", error)
            raise RuntimeError(f"Computer vision model is unavailable: {error}") from error


MAX_IMAGE_DIM = 384


def _to_rgb_and_downsample(img: Image.Image, max_dim: int = MAX_IMAGE_DIM) -> Image.Image:
    """Converts image to RGB and downsamples if dimensions exceed max_dim."""
    rgb = img.convert("RGB")
    if rgb.width > max_dim or rgb.height > max_dim:
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
        rgb.thumbnail((max_dim, max_dim), resample)
    return rgb


def load_image(image_input: Union[Image.Image, bytes, bytearray, io.BytesIO, str], max_bytes: int = MAX_IMAGE_BYTES) -> Image.Image:
    """
    Loads and validates an image from PIL Image, raw bytes, file path, or HTTP(S) URL.
    Returns RGB PIL Image, downsampled to MAX_IMAGE_DIM if oversized.
    Releases underlying buffers to control memory usage.
    """
    if isinstance(image_input, Image.Image):
        return _to_rgb_and_downsample(image_input)

    if isinstance(image_input, (bytes, bytearray)):
        if len(image_input) == 0:
            raise ValueError("Image file is empty")
        if len(image_input) > max_bytes:
            raise ValueError("Image file exceeds maximum allowed size")
        with Image.open(io.BytesIO(image_input)) as opened:
            return _to_rgb_and_downsample(opened)

    if hasattr(image_input, "read"):
        data = image_input.read()
        if len(data) == 0:
            raise ValueError("Image file is empty")
        if len(data) > max_bytes:
            raise ValueError("Image file exceeds maximum allowed size")
        with Image.open(io.BytesIO(data)) as opened:
            return _to_rgb_and_downsample(opened)

    if isinstance(image_input, str):
        trimmed = image_input.strip()
        if not trimmed:
            raise ValueError("Image source string is empty")

        if trimmed.startswith(("http://", "https://")):
            if not is_safe_image_url(trimmed):
                raise ValueError("Image URL targets an unsafe or restricted host")
            try:
                with httpx.Client(timeout=3.5, follow_redirects=True) as client:
                    with client.stream("GET", trimmed) as resp:
                        if resp.status_code != 200:
                            raise ValueError(f"Failed to fetch image from URL (HTTP {resp.status_code})")
                        cl = resp.headers.get("content-length")
                        if cl and int(cl) > max_bytes:
                            raise ValueError("Downloaded image file exceeds maximum allowed size")
                        content = bytearray()
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            content.extend(chunk)
                            if len(content) > max_bytes:
                                raise ValueError("Downloaded image file exceeds maximum allowed size")
            except ValueError:
                raise
            except Exception as err:
                raise ValueError(f"Could not download image from URL: {err}") from err

            if len(content) == 0:
                raise ValueError("Downloaded image file is empty")
            with Image.open(io.BytesIO(content)) as opened:
                return _to_rgb_and_downsample(opened)

        # Local path check
        path = Path(trimmed)
        if path.is_file():
            size = path.stat().st_size
            if size == 0:
                raise ValueError("Image file is empty")
            if size > max_bytes:
                raise ValueError("Image file exceeds maximum allowed size")
            with Image.open(path) as opened:
                return _to_rgb_and_downsample(opened)

        raise ValueError(f"Image not found at path: {trimmed}")

    raise ValueError(f"Unsupported image input type: {type(image_input)}")



def extract_embedding(image: Image.Image, cache_key: Optional[str] = None) -> np.ndarray:
    """
    Extracts L2-normalized feature embedding from an image.
    Optionally checks and updates the in-memory LRU cache if cache_key is provided.
    """
    if cache_key:
        with _CACHE_LOCK:
            if cache_key in _EMBEDDING_CACHE:
                return _EMBEDDING_CACHE[cache_key]

    model, transforms = get_model()

    # If model is a mock function or custom callable
    if callable(model) and not hasattr(model, "eval"):
        emb = model(image)
        if hasattr(emb, "cpu"):
            emb = emb.cpu().numpy()
        emb = np.asarray(emb, dtype=np.float32).flatten()
        norm = np.linalg.norm(emb)
        if norm > 0:
            emb = emb / norm
    else:
        import torch

        with torch.no_grad():
            tensor = transforms(image).unsqueeze(0)
            emb_tensor = model(tensor)
            norm = torch.norm(emb_tensor, p=2, dim=-1, keepdim=True)
            norm = torch.clamp(norm, min=1e-8)
            emb_tensor = emb_tensor / norm
            emb = emb_tensor.squeeze(0).cpu().numpy().astype(np.float32)
            del tensor, emb_tensor

    if cache_key:
        with _CACHE_LOCK:
            if len(_EMBEDDING_CACHE) >= _MAX_CACHE_SIZE:
                # Evict oldest entry
                _EMBEDDING_CACHE.pop(next(iter(_EMBEDDING_CACHE)))
            _EMBEDDING_CACHE[cache_key] = emb

    return emb


def compute_cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
    """
    Calculates cosine similarity between two feature vectors.
    Returns float in [-1.0, 1.0].
    """
    v1 = np.asarray(vec1, dtype=np.float32).flatten()
    v2 = np.asarray(vec2, dtype=np.float32).flatten()

    norm1 = float(np.linalg.norm(v1))
    norm2 = float(np.linalg.norm(v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0

    dot = float(np.dot(v1, v2))
    return float(dot / (norm1 * norm2))


def normalize_similarity(cosine_sim: float) -> Dict[str, Any]:
    """
    Normalizes cosine similarity to a 0-100 score and qualitative match level.
    """
    raw = float(cosine_sim)
    clamped = max(0.0, min(1.0, raw))
    score = round(clamped * 100.0, 1)

    if score >= 80.0:
        level = "High Match"
    elif score >= 50.0:
        level = "Moderate Match"
    else:
        level = "Low Match"

    return {
        "score": score,
        "raw_similarity": round(raw, 4),
        "level": level,
    }


def compare_images(
    ref_input: Any,
    prod_input: Any,
    ref_cache_key: Optional[str] = None,
    prod_cache_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compares seeker reference image with provider product image.
    Handles missing images, unsupported formats, corrupt files, model failures gracefully.
    Checks embedding cache before downloading or decoding images.
    Guarantees that temporary PIL image objects are properly closed.
    """
    if not ref_input or not prod_input:
        missing = []
        if not ref_input:
            missing.append("reference image")
        if not prod_input:
            missing.append("product image")
        return {
            "success": False,
            "available": False,
            "score": None,
            "level": None,
            "raw_similarity": None,
            "status": "missing_image",
            "error": f"Missing {' and '.join(missing)} for similarity comparison.",
            "disclaimer": DISCLAIMER,
        }

    # Derive cache keys from string URLs or paths if not provided
    if not ref_cache_key and isinstance(ref_input, str):
        ref_cache_key = ref_input
    if not prod_cache_key and isinstance(prod_input, str):
        prod_cache_key = prod_input

    # Check cache early to avoid redundant network download and PIL image decoding
    emb_ref = None
    if isinstance(ref_input, np.ndarray):
        emb_ref = ref_input
    elif ref_cache_key:
        with _CACHE_LOCK:
            emb_ref = _EMBEDDING_CACHE.get(ref_cache_key)

    emb_prod = None
    if isinstance(prod_input, np.ndarray):
        emb_prod = prod_input
    elif prod_cache_key:
        with _CACHE_LOCK:
            emb_prod = _EMBEDDING_CACHE.get(prod_cache_key)

    ref_img = None
    prod_img = None

    try:
        if emb_ref is None:
            try:
                ref_img = load_image(ref_input)
            except (ValueError, UnidentifiedImageError, OSError) as err:
                return {
                    "success": False,
                    "available": False,
                    "score": None,
                    "level": None,
                    "raw_similarity": None,
                    "status": "invalid_reference_image",
                    "error": f"Could not load reference image: {err}",
                    "disclaimer": DISCLAIMER,
                }
            except Exception as err:
                return {
                    "success": False,
                    "available": False,
                    "score": None,
                    "level": None,
                    "raw_similarity": None,
                    "status": "error",
                    "error": f"Unexpected error reading reference image: {err}",
                    "disclaimer": DISCLAIMER,
                }

        if emb_prod is None:
            try:
                prod_img = load_image(prod_input)
            except (ValueError, UnidentifiedImageError, OSError) as err:
                return {
                    "success": False,
                    "available": False,
                    "score": None,
                    "level": None,
                    "raw_similarity": None,
                    "status": "invalid_product_image",
                    "error": f"Could not load provider product image: {err}",
                    "disclaimer": DISCLAIMER,
                }
            except Exception as err:
                return {
                    "success": False,
                    "available": False,
                    "score": None,
                    "level": None,
                    "raw_similarity": None,
                    "status": "error",
                    "error": f"Unexpected error reading product image: {err}",
                    "disclaimer": DISCLAIMER,
                }

        try:
            if emb_ref is None and ref_img is not None:
                emb_ref = extract_embedding(ref_img, cache_key=ref_cache_key)
            if emb_prod is None and prod_img is not None:
                emb_prod = extract_embedding(prod_img, cache_key=prod_cache_key)
        except RuntimeError as err:
            logger.warning("Similarity model runtime error: %s", err)
            return {
                "success": False,
                "available": False,
                "score": None,
                "level": None,
                "raw_similarity": None,
                "status": "model_unavailable",
                "error": "Image comparison model is currently unavailable.",
                "disclaimer": DISCLAIMER,
            }
        except Exception as err:
            logger.warning("Similarity embedding extraction error: %s", err)
            return {
                "success": False,
                "available": False,
                "score": None,
                "level": None,
                "raw_similarity": None,
                "status": "processing_error",
                "error": f"Error during image feature extraction: {err}",
                "disclaimer": DISCLAIMER,
            }

        cos_sim = compute_cosine_similarity(emb_ref, emb_prod)
        norm = normalize_similarity(cos_sim)

        return {
            "success": True,
            "available": True,
            "score": norm["score"],
            "level": norm["level"],
            "raw_similarity": norm["raw_similarity"],
            "status": "computed",
            "disclaimer": DISCLAIMER,
        }
    finally:
        for img in (ref_img, prod_img):
            if img is not None and hasattr(img, "close"):
                try:
                    img.close()
                except Exception:
                    pass
