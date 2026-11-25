"""
AIGC watermarking module supporting:
1. Invisible watermark (implicit metadata compliant with GB 45438-2025):
   - Enabled by default with platform producer "CAICAI-菜菜AI".
   - Supports images (PNG, JPEG, WebP) and videos (MP4, MOV, M4V, MKV, WebM, AVI, GIF).
   - Exposes extensible hooks for future blind pixel/frame watermarking (e.g., TrustMark, Video Seal).
2. Visible watermark (image overlay):
   - Disabled by default; enabled when specified by caller.
   - Supports watermark image URL (HTTP/HTTPS with caching) or local file path,
     9-grid anchor positioning, flexible sizing (percentage, pixel width, WxH),
     edge margin, and opacity for both images and videos.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

from PIL import ExifTags, Image, PngImagePlugin

TC260_AIGC_NS_URI = "http://www.tc260.org.cn/ns/AIGC/1.0/"
TC260_AIGC_NS_PREFIX = "TC260"

DEFAULT_CONTENT_PRODUCER = "CAICAI-菜菜AI"
VALID_AIGC_LABELS = ("1", "2", "3")

VISIBLE_WATERMARK_POSITIONS = (
    "bottom_right",
    "bottom_left",
    "top_right",
    "top_left",
    "center",
    "top_center",
    "bottom_center",
    "center_left",
    "center_right",
)

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".gif")

# Exif tag IDs
_TAG_IMAGE_DESCRIPTION = 0x010E
_TAG_USER_COMMENT = 0x9286
_EXIF_IFD_TAG = getattr(getattr(ExifTags, "IFD", None), "Exif", 0x8769)

# Pillow resampling compatibility
_RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")

# Optional future blind-watermark processor hooks:
#   image processor: (PIL.Image.Image, AIGCMetadata) -> PIL.Image.Image
#   video processor: (input_path: str, output_path: str, AIGCMetadata) -> str
ImageWatermarkHook = Callable[[Image.Image, "AIGCMetadata"], Image.Image]
VideoWatermarkHook = Callable[[str, str, "AIGCMetadata"], str]

_IMAGE_WATERMARK_HOOKS: List[ImageWatermarkHook] = []
_VIDEO_WATERMARK_HOOKS: List[VideoWatermarkHook] = []

# Simple in-memory URL cache for watermark images: url -> (timestamp, raw_bytes)
_WM_URL_CACHE: Dict[str, Tuple[float, bytes]] = {}
_WM_URL_CACHE_LOCK = threading.Lock()
_WM_URL_CACHE_TTL_SECONDS = 600

# Cached NVENC encoder flags per ffmpeg binary: ffmpeg_bin -> Optional[List[str]]
_NVENC_ARGS_CACHE: Dict[str, Optional[List[str]]] = {}
_NVENC_CACHE_LOCK = threading.Lock()


def register_image_watermark_hook(hook: ImageWatermarkHook) -> None:
    """Register an optional pixel-level blind image watermark processor (for future upgrades)."""
    _IMAGE_WATERMARK_HOOKS.append(hook)


def register_video_watermark_hook(hook: VideoWatermarkHook) -> None:
    """Register an optional frame-level blind video watermark processor (for future upgrades)."""
    _VIDEO_WATERMARK_HOOKS.append(hook)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class AIGCMetadata:
    """GB 45438-2025 compliant invisible watermark (implicit metadata) payload."""

    label: str = "1"
    content_producer: str = DEFAULT_CONTENT_PRODUCER
    produce_id: str = ""
    reserved_code1: str = ""
    content_propagator: str = ""
    propagate_id: str = ""
    reserved_code2: str = ""

    def __post_init__(self) -> None:
        self.label = str(self.label).strip() if self.label is not None else "1"
        if self.label not in VALID_AIGC_LABELS:
            self.label = "1"
        self.content_producer = (
            self.content_producer or ""
        ).strip() or DEFAULT_CONTENT_PRODUCER
        self.produce_id = (self.produce_id or "").strip() or generate_produce_id()
        self.reserved_code1 = (self.reserved_code1 or "").strip()
        self.content_propagator = (self.content_propagator or "").strip()
        self.propagate_id = (self.propagate_id or "").strip()
        self.reserved_code2 = (self.reserved_code2 or "").strip()

    def to_payload_dict(self) -> Dict[str, str]:
        """Standard GB 45438-2025 AIGC metadata dictionary."""
        return {
            "Label": self.label,
            "ContentProducer": self.content_producer,
            "ProduceID": self.produce_id,
            "ReservedCode1": self.reserved_code1,
            "ContentPropagator": self.content_propagator,
            "PropagateID": self.propagate_id,
            "ReservedCode2": self.reserved_code2,
        }

    def to_payload_json(self) -> str:
        """Compact JSON string for the 'AIGC' key value."""
        return json.dumps(
            self.to_payload_dict(), ensure_ascii=False, separators=(",", ":")
        )

    def to_wrapped_json(self) -> str:
        """Compact JSON string wrapped under top-level 'AIGC' key (for generic fields)."""
        return json.dumps(
            {"AIGC": self.to_payload_dict()}, ensure_ascii=False, separators=(",", ":")
        )

    def to_xmp_packet(self) -> bytes:
        """Build an XMP packet using the TC260 AIGC namespace."""
        payload_escaped = xml_escape(
            self.to_payload_json(), entities={'"': "&quot;", "'": "&apos;"}
        )
        xmp = (
            '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description rdf:about="" '
            f'xmlns:{TC260_AIGC_NS_PREFIX}="{TC260_AIGC_NS_URI}" '
            f'{TC260_AIGC_NS_PREFIX}:AIGC="{payload_escaped}"/>'
            "</rdf:RDF>"
            "</x:xmpmeta>"
            '<?xpacket end="w"?>'
        )
        return xmp.encode("utf-8")


@dataclass
class VisibleWatermarkConfig:
    """Configuration for visible image-overlay watermark."""

    image_url: str = ""
    position: str = "bottom_right"
    size: str = "15%"
    margin: int = 20
    opacity: float = 1.0

    def __post_init__(self) -> None:
        self.image_url = (self.image_url or "").strip()
        pos = (self.position or "bottom_right").strip().lower()
        self.position = (
            pos if pos in VISIBLE_WATERMARK_POSITIONS else "bottom_right"
        )
        self.size = (self.size or "15%").strip() or "15%"
        self.margin = max(0, int(self.margin))
        self.opacity = max(0.0, min(1.0, float(self.opacity)))

    @property
    def is_valid(self) -> bool:
        return bool(self.image_url)


def generate_produce_id(prefix: str = "PROD") -> str:
    """Generate a unique ProduceID with timestamp and random UUID suffix."""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:12]}"


def build_aigc_metadata(
    label: str = "1",
    content_producer: str = "",
    produce_id: str = "",
    reserved_code1: str = "",
    content_propagator: str = "",
    propagate_id: str = "",
    reserved_code2: str = "",
) -> AIGCMetadata:
    """
    Create AIGCMetadata resolving node inputs -> environment variables -> default
    ("CAICAI-菜菜AI").
    """
    resolved_producer = (
        (content_producer or "").strip()
        or os.environ.get("AIGC_CONTENT_PRODUCER", "").strip()
        or DEFAULT_CONTENT_PRODUCER
    )
    resolved_propagator = (
        (content_propagator or "").strip()
        or os.environ.get("AIGC_CONTENT_PROPAGATOR", "").strip()
    )
    return AIGCMetadata(
        label=label,
        content_producer=resolved_producer,
        produce_id=produce_id,
        reserved_code1=reserved_code1,
        content_propagator=resolved_propagator,
        propagate_id=propagate_id,
        reserved_code2=reserved_code2,
    )


def build_visible_watermark(
    image_url: str = "",
    position: str = "bottom_right",
    size: str = "15%",
    margin: int = 20,
    opacity: float = 1.0,
) -> Optional[VisibleWatermarkConfig]:
    """Build VisibleWatermarkConfig, returning None if image_url is empty."""
    cfg = VisibleWatermarkConfig(
        image_url=image_url,
        position=position,
        size=size,
        margin=margin,
        opacity=opacity,
    )
    return cfg if cfg.is_valid else None


# ---------------------------------------------------------------------------
# Visible Watermark Loading, Sizing & Compositing
# ---------------------------------------------------------------------------


def _download_watermark_bytes(url: str, timeout: int = 15, retries: int = 3) -> bytes:
    """Download watermark image bytes from an HTTP/HTTPS URL with TTL caching."""
    now = time.time()
    with _WM_URL_CACHE_LOCK:
        cached = _WM_URL_CACHE.get(url)
        if cached and (now - cached[0]) < _WM_URL_CACHE_TTL_SECONDS:
            return cached[1]

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "ComfyUI-OSS-Upload/2.2"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data:
                raise ValueError(f"Empty response when fetching watermark URL: {url}")
            with _WM_URL_CACHE_LOCK:
                _WM_URL_CACHE[url] = (time.time(), data)
            return data
        except Exception as exc:
            last_err = exc
            if attempt + 1 < retries:
                time.sleep(1.0)

    raise RuntimeError(f"Failed to download visible watermark from {url}: {last_err}")


def load_watermark_image(source: str) -> Image.Image:
    """Load a visible watermark image from HTTP/HTTPS URL or local path as RGBA."""
    src = source.strip()
    if src.lower().startswith(("http://", "https://")):
        raw = _download_watermark_bytes(src)
        with Image.open(io.BytesIO(raw)) as img:
            return img.convert("RGBA")

    if not os.path.exists(src):
        raise FileNotFoundError(f"Visible watermark file not found: {src}")
    with Image.open(src) as img:
        return img.convert("RGBA")


def _parse_dimension_token(token: str, base_dim: int) -> Optional[int]:
    """Parse a single dimension token such as '15%', '0.15', '200', or '200px'."""
    t = token.strip().lower()
    if not t:
        return None
    if t.endswith("%"):
        val = float(t[:-1].strip())
        if val <= 0:
            raise ValueError(f"Watermark percentage must be > 0, got: {token}")
        return max(1, round(base_dim * (val / 100.0)))
    if t.endswith("px"):
        t = t[:-2].strip()
    val = float(t)
    if val <= 0:
        raise ValueError(f"Watermark size must be > 0, got: {token}")
    if 0 < val < 1.0:
        return max(1, round(base_dim * val))
    return max(1, round(val))


def compute_watermark_size(
    base_size: Tuple[int, int],
    wm_size: Tuple[int, int],
    size_spec: str,
) -> Tuple[int, int]:
    """
    Compute target (width, height) for a visible watermark.

    Supported `size_spec` formats:
    - Percentage of base width (proportional height): '15%', '0.15'
    - Fixed pixel width (proportional height): '200', '200px'
    - Explicit width x height: '200x80', '200*80', '20%x10%'
    """
    base_w, base_h = max(1, int(base_size[0])), max(1, int(base_size[1]))
    wm_w, wm_h = max(1, int(wm_size[0])), max(1, int(wm_size[1]))
    spec = (size_spec or "15%").strip().lower()
    if spec in ("", "auto"):
        spec = "15%"

    explicit_pair = re.split(r"[x*×]", spec)
    if len(explicit_pair) == 2 and explicit_pair[0].strip() and explicit_pair[1].strip():
        target_w = _parse_dimension_token(explicit_pair[0], base_w)
        target_h = _parse_dimension_token(explicit_pair[1], base_h)
        assert target_w is not None and target_h is not None
        return (min(target_w, base_w), min(target_h, base_h))

    # Single value -> treat as target width, preserve aspect ratio
    target_w = _parse_dimension_token(spec, base_w)
    assert target_w is not None
    scale = target_w / float(wm_w)
    target_h = max(1, round(wm_h * scale))

    # Clamp inside base canvas while preserving aspect ratio
    if target_w > base_w or target_h > base_h:
        fit_scale = min(base_w / float(target_w), base_h / float(target_h))
        target_w = max(1, round(target_w * fit_scale))
        target_h = max(1, round(target_h * fit_scale))

    return (target_w, target_h)


def compute_watermark_position(
    base_size: Tuple[int, int],
    wm_size: Tuple[int, int],
    position: str,
    margin: int = 20,
) -> Tuple[int, int]:
    """Compute (x, y) top-left offset for a 9-grid position clamped inside the canvas."""
    base_w, base_h = base_size
    wm_w, wm_h = wm_size
    pos = (position or "bottom_right").strip().lower()
    m = max(0, int(margin))

    if pos == "top_left":
        x, y = m, m
    elif pos == "top_center":
        x, y = (base_w - wm_w) // 2, m
    elif pos == "top_right":
        x, y = base_w - wm_w - m, m
    elif pos == "center_left":
        x, y = m, (base_h - wm_h) // 2
    elif pos == "center":
        x, y = (base_w - wm_w) // 2, (base_h - wm_h) // 2
    elif pos == "center_right":
        x, y = base_w - wm_w - m, (base_h - wm_h) // 2
    elif pos == "bottom_left":
        x, y = m, base_h - wm_h - m
    elif pos == "bottom_center":
        x, y = (base_w - wm_w) // 2, base_h - wm_h - m
    else:  # bottom_right
        x, y = base_w - wm_w - m, base_h - wm_h - m

    x = max(0, min(x, max(0, base_w - wm_w)))
    y = max(0, min(y, max(0, base_h - wm_h)))
    return (x, y)


def prepare_visible_watermark(
    base_size: Tuple[int, int],
    config: VisibleWatermarkConfig,
) -> Tuple[Image.Image, Tuple[int, int]]:
    """Load, resize, apply opacity, and compute overlay (x, y) for a visible watermark."""
    wm = load_watermark_image(config.image_url)
    target_w, target_h = compute_watermark_size(base_size, wm.size, config.size)
    if (target_w, target_h) != wm.size:
        wm = wm.resize((target_w, target_h), _RESAMPLE_LANCZOS)

    if config.opacity < 1.0:
        r, g, b, a = wm.split()
        a = a.point(lambda p: int(round(p * config.opacity)))
        wm = Image.merge("RGBA", (r, g, b, a))

    pos = compute_watermark_position(
        base_size, wm.size, config.position, config.margin
    )
    return wm, pos


def apply_visible_watermark_to_image(
    pil_image: Image.Image,
    config: VisibleWatermarkConfig,
) -> Image.Image:
    """Composite a visible image watermark onto a PIL Image."""
    wm, (x, y) = prepare_visible_watermark(pil_image.size, config)
    original_mode = pil_image.mode
    composited = pil_image.convert("RGBA")
    composited.alpha_composite(wm, dest=(x, y))

    if original_mode == "RGBA":
        return composited
    if original_mode in ("RGB", "L"):
        return composited.convert(original_mode)
    return composited.convert("RGB")


# ---------------------------------------------------------------------------
# Invisible Metadata Helpers (Images)
# ---------------------------------------------------------------------------


def _build_exif_bytes(metadata: AIGCMetadata) -> bytes:
    """Construct EXIF bytes with GB 45438 AIGC JSON in ImageDescription & UserComment."""
    exif = Image.Exif()
    wrapped = metadata.to_wrapped_json()

    # IFD0 ImageDescription (ASCII-safe JSON for widest parser compatibility)
    wrapped_ascii = json.dumps(
        {"AIGC": metadata.to_payload_dict()}, ensure_ascii=True, separators=(",", ":")
    )
    exif[_TAG_IMAGE_DESCRIPTION] = wrapped_ascii

    # Exif SubIFD UserComment (8-byte encoding prefix per EXIF 2.3 spec)
    exif_ifd = exif.get_ifd(_EXIF_IFD_TAG)
    try:
        wrapped.encode("ascii")
        user_comment = b"ASCII\x00\x00\x00" + wrapped.encode("ascii")
    except UnicodeEncodeError:
        user_comment = b"UNICODE\x00" + wrapped.encode("utf-8")
    exif_ifd[_TAG_USER_COMMENT] = user_comment

    return exif.tobytes()


def _inject_jpeg_xmp_app1(jpeg_path: str, xmp_bytes: bytes) -> None:
    """Fallback APP1 XMP injector for older Pillow versions that lack JPEG xmp= kwarg."""
    header = b"http://ns.adobe.com/xap/1.0/\x00"
    payload = header + xmp_bytes
    if len(payload) + 2 > 0xFFFF:
        return
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload

    with open(jpeg_path, "rb") as f:
        data = f.read()

    if not data.startswith(b"\xff\xd8"):
        return

    new_data = data[:2] + segment + data[2:]
    with open(jpeg_path, "wb") as f:
        f.write(new_data)


def _resolve_image_format(output_path: str) -> str:
    ext = os.path.splitext(output_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return "JPEG"
    if ext == ".webp":
        return "WEBP"
    return "PNG"


def save_image_with_watermark(
    pil_image: Image.Image,
    output_path: str,
    metadata: Optional[AIGCMetadata] = None,
    visible_config: Optional[VisibleWatermarkConfig] = None,
    quality: int = 95,
) -> str:
    """
    Save a PIL Image with optional visible overlay watermark and optional GB 45438-2025
    invisible metadata (PNG tEXt/iTXt + XMP, JPEG EXIF + XMP, WebP EXIF + XMP).
    """
    img = pil_image

    # Step 1: Visible watermark (pixel overlay)
    if visible_config and visible_config.is_valid:
        img = apply_visible_watermark_to_image(img, visible_config)

    # Step 2: Optional future blind pixel watermark hooks
    if metadata is not None:
        for hook in _IMAGE_WATERMARK_HOOKS:
            img = hook(img, metadata)

    fmt = _resolve_image_format(output_path)

    # Step 3: Save without invisible metadata if disabled
    if metadata is None:
        if fmt == "JPEG":
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            img.save(output_path, format="JPEG", quality=quality)
        elif fmt == "WEBP":
            img.save(output_path, format="WEBP", quality=quality)
        else:
            img.save(output_path, format="PNG")
        return output_path

    # Step 4: Save with GB 45438-2025 invisible metadata
    xmp_bytes = metadata.to_xmp_packet()

    if fmt == "PNG":
        pnginfo = PngImagePlugin.PngInfo()
        payload_json = metadata.to_payload_json()
        try:
            payload_json.encode("latin-1")
            pnginfo.add_text("AIGC", payload_json)
        except UnicodeEncodeError:
            pnginfo.add_itxt("AIGC", payload_json)
        pnginfo.add_itxt("XML:com.adobe.xmp", xmp_bytes.decode("utf-8"))
        img.save(output_path, format="PNG", pnginfo=pnginfo)
        return output_path

    exif_bytes = _build_exif_bytes(metadata)

    if fmt == "JPEG":
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        try:
            img.save(
                output_path,
                format="JPEG",
                quality=quality,
                exif=exif_bytes,
                xmp=xmp_bytes,
            )
        except TypeError:
            img.save(output_path, format="JPEG", quality=quality, exif=exif_bytes)
            _inject_jpeg_xmp_app1(output_path, xmp_bytes)
        return output_path

    if fmt == "WEBP":
        try:
            img.save(
                output_path,
                format="WEBP",
                quality=quality,
                exif=exif_bytes,
                xmp=xmp_bytes,
            )
        except TypeError:
            img.save(output_path, format="WEBP", quality=quality, exif=exif_bytes)
        return output_path

    img.save(output_path)
    return output_path


def embed_image_file_watermark(
    input_path: str,
    output_path: str,
    metadata: Optional[AIGCMetadata] = None,
    visible_config: Optional[VisibleWatermarkConfig] = None,
    quality: int = 95,
) -> str:
    """Load an existing image file and write a watermarked copy to output_path."""
    with Image.open(input_path) as img:
        img.load()
        return save_image_with_watermark(
            img,
            output_path,
            metadata=metadata,
            visible_config=visible_config,
            quality=quality,
        )


# ---------------------------------------------------------------------------
# Video Watermarking (Visible Overlay + Invisible Metadata)
# ---------------------------------------------------------------------------


def _find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg executable from PATH or imageio_ffmpeg (common in ComfyUI/VHS)."""
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _probe_video_dimensions(video_path: str, ffmpeg_bin: str) -> Tuple[int, int]:
    """Determine (width, height) of a video file via ffprobe, with ffmpeg stderr fallback."""
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin:
        res = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "quiet",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                video_path,
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            try:
                streams = json.loads(res.stdout).get("streams", [])
                if streams and streams[0].get("width") and streams[0].get("height"):
                    return int(streams[0]["width"]), int(streams[0]["height"])
            except (json.JSONDecodeError, KeyError, ValueError):
                pass

    # Fallback: parse `ffmpeg -i <video>` stderr
    res = subprocess.run([ffmpeg_bin, "-i", video_path], capture_output=True, text=True)
    for line in res.stderr.splitlines():
        if "Video:" in line:
            match = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if match:
                return int(match.group(1)), int(match.group(2))

    raise RuntimeError(f"Could not determine video resolution for: {video_path}")


def _embed_gif_watermark(
    input_path: str,
    output_path: str,
    metadata: Optional[AIGCMetadata] = None,
    visible_config: Optional[VisibleWatermarkConfig] = None,
) -> str:
    """Embed optional visible overlay and/or GB 45438 JSON comment into GIF preserving frames."""
    with Image.open(input_path) as img:
        base_size = img.size
        wm_pair = (
            prepare_visible_watermark(base_size, visible_config)
            if (visible_config and visible_config.is_valid)
            else None
        )

        frames = []
        durations = []
        try:
            while True:
                frame = img.copy()
                if wm_pair is not None:
                    wm_img, (x, y) = wm_pair
                    frame = frame.convert("RGBA")
                    frame.alpha_composite(wm_img, dest=(x, y))
                frames.append(frame)
                durations.append(img.info.get("duration", 100))
                img.seek(img.tell() + 1)
        except EOFError:
            pass

        loop = img.info.get("loop", 0)
        save_kwargs: Dict[str, object] = {"format": "GIF"}
        if metadata is not None:
            save_kwargs["comment"] = metadata.to_wrapped_json().encode("utf-8")

        if len(frames) > 1:
            frames[0].save(
                output_path,
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=loop,
                **save_kwargs,
            )
        else:
            frames[0].save(output_path, **save_kwargs)
    return output_path


def _detect_h264_nvenc_args(ffmpeg_bin: str) -> Optional[List[str]]:
    """
    Probe once per process whether NVIDIA h264_nvenc works in this runtime.
    Returns encoder CLI args if usable, otherwise None (graceful CPU fallback).
    """
    if os.environ.get("AIGC_DISABLE_NVENC", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return None

    with _NVENC_CACHE_LOCK:
        if ffmpeg_bin in _NVENC_ARGS_CACHE:
            return _NVENC_ARGS_CACHE[ffmpeg_bin]

    candidates = [
        ["-c:v", "h264_nvenc", "-preset", "p2", "-rc", "vbr", "-cq", "20", "-pix_fmt", "yuv420p"],
        ["-c:v", "h264_nvenc", "-preset", "fast", "-cq", "20", "-pix_fmt", "yuv420p"],
    ]

    detected: Optional[List[str]] = None
    for args in candidates:
        try:
            res = subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=0.04",
                    "-frames:v",
                    "1",
                    *args,
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                detected = args
                break
        except Exception:
            continue

    with _NVENC_CACHE_LOCK:
        _NVENC_ARGS_CACHE[ffmpeg_bin] = detected
    return detected


def _cpu_h264_args() -> List[str]:
    """Return fast CPU libx264 encoding flags (overridable via AIGC_VIDEO_PRESET)."""
    preset = os.environ.get("AIGC_VIDEO_PRESET", "veryfast").strip() or "veryfast"
    return [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        preset,
        "-crf",
        "20",
        "-threads",
        "0",
    ]


def _build_ffmpeg_video_cmd(
    ffmpeg_bin: str,
    current_input: str,
    output_path: str,
    ext: str,
    has_visible: bool,
    wm_tmp_path: Optional[str],
    overlay_xy: Optional[Tuple[int, int]],
    video_codec_args: List[str],
    metadata: Optional[AIGCMetadata],
) -> List[str]:
    cmd: List[str] = [ffmpeg_bin, "-y", "-i", current_input]

    if has_visible and wm_tmp_path is not None and overlay_xy is not None:
        x, y = overlay_xy
        cmd.extend(["-i", wm_tmp_path])
        cmd.extend(
            [
                "-filter_complex",
                f"[0:v][1:v]overlay=x={x}:y={y}:format=yuv420[vout]",
                "-map",
                "[vout]",
                "-map",
                "0:a?",
                "-map_metadata",
                "0",
                "-c:a",
                "copy",
                *video_codec_args,
            ]
        )
    else:
        cmd.extend(["-map_metadata", "0", "-c", "copy"])

    # MP4 / MOV require use_metadata_tags so custom 'AIGC' tag is written into QuickTime keys
    if ext in (".mp4", ".mov", ".m4v"):
        if has_visible:
            cmd.extend(["-movflags", "+faststart+use_metadata_tags"])
        elif metadata is not None:
            cmd.extend(["-movflags", "use_metadata_tags"])

    if metadata is not None:
        payload_json = metadata.to_payload_json()
        wrapped_json = metadata.to_wrapped_json()
        cmd.extend(
            [
                "-metadata",
                f"AIGC={payload_json}",
                "-metadata",
                f"comment={wrapped_json}",
                "-metadata",
                f"description={wrapped_json}",
            ]
        )

    cmd.append(output_path)
    return cmd


def embed_video_watermark(
    input_path: str,
    output_path: str,
    metadata: Optional[AIGCMetadata] = None,
    visible_config: Optional[VisibleWatermarkConfig] = None,
) -> str:
    """
    Apply optional visible image overlay and/or GB 45438-2025 invisible metadata to a video.

    - When only invisible metadata is enabled: fast lossless stream copy (-c copy).
    - When visible watermark is enabled: single-pass FFmpeg overlay in YUV420 space
      using NVIDIA h264_nvenc when available (with automatic CPU libx264 veryfast fallback)
      and lossless audio stream copy (-c:a copy).
    """
    has_visible = bool(visible_config and visible_config.is_valid)
    has_invisible = metadata is not None

    if not has_visible and not has_invisible:
        shutil.copy2(input_path, output_path)
        return output_path

    current_input = input_path
    intermediate_files: List[str] = []

    try:
        # Optional future frame-level blind watermark hooks
        if has_invisible and metadata is not None:
            for idx, hook in enumerate(_VIDEO_WATERMARK_HOOKS):
                base, ext = os.path.splitext(output_path)
                hook_out = f"{base}_hook{idx}{ext}"
                current_input = hook(current_input, hook_out, metadata)
                if hook_out != output_path:
                    intermediate_files.append(hook_out)

        ext = (
            os.path.splitext(output_path)[1].lower()
            or os.path.splitext(input_path)[1].lower()
        )
        if ext == ".gif":
            return _embed_gif_watermark(
                current_input,
                output_path,
                metadata=metadata,
                visible_config=visible_config,
            )

        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError(
                "ffmpeg not found in PATH or imageio_ffmpeg; cannot process video watermark."
            )

        wm_tmp_path: Optional[str] = None
        overlay_xy: Optional[Tuple[int, int]] = None
        codec_attempts: List[List[str]] = [[]]

        if has_visible and visible_config is not None:
            vid_w, vid_h = _probe_video_dimensions(current_input, ffmpeg_bin)
            wm_img, overlay_xy = prepare_visible_watermark(
                (vid_w, vid_h), visible_config
            )
            wm_tmp_fd, wm_tmp_path = tempfile.mkstemp(
                prefix="aigc_vis_wm_", suffix=".png"
            )
            os.close(wm_tmp_fd)
            wm_img.save(wm_tmp_path, format="PNG")
            intermediate_files.append(wm_tmp_path)

            if ext == ".webm":
                codec_attempts = [["-c:v", "libvpx-vp9", "-crf", "24", "-b:v", "0", "-deadline", "realtime"]]
            else:
                nvenc_args = _detect_h264_nvenc_args(ffmpeg_bin)
                cpu_args = _cpu_h264_args()
                codec_attempts = [nvenc_args, cpu_args] if nvenc_args else [cpu_args]

        last_err = ""
        for attempt_idx, codec_args in enumerate(codec_attempts):
            cmd = _build_ffmpeg_video_cmd(
                ffmpeg_bin=ffmpeg_bin,
                current_input=current_input,
                output_path=output_path,
                ext=ext,
                has_visible=has_visible,
                wm_tmp_path=wm_tmp_path,
                overlay_xy=overlay_xy,
                video_codec_args=codec_args,
                metadata=metadata,
            )
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return output_path
            last_err = result.stderr.strip()
            if attempt_idx + 1 < len(codec_attempts):
                print(
                    "[WARNING] GPU h264_nvenc overlay encode failed; falling back to CPU libx264."
                )

        raise RuntimeError(f"ffmpeg failed to embed video watermark: {last_err}")
    finally:
        for fpath in intermediate_files:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Verification / Readers
# ---------------------------------------------------------------------------


def read_image_watermark(file_path: str) -> Optional[Dict[str, str]]:
    """Read and parse GB 45438 AIGC invisible metadata from PNG, JPEG, WebP, or GIF."""
    with Image.open(file_path) as img:
        # 1. PNG tEXt / iTXt direct 'AIGC' chunk
        raw_aigc = img.info.get("AIGC")
        if raw_aigc:
            try:
                parsed = json.loads(raw_aigc)
                return parsed.get("AIGC", parsed) if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass

        # 2. GIF comment
        raw_comment = img.info.get("comment")
        if raw_comment:
            text = (
                raw_comment.decode("utf-8", errors="ignore")
                if isinstance(raw_comment, bytes)
                else str(raw_comment)
            )
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed.get("AIGC", parsed)
            except json.JSONDecodeError:
                pass

        # 3. EXIF UserComment / ImageDescription
        try:
            exif = img.getexif()
            exif_ifd = exif.get_ifd(_EXIF_IFD_TAG)
            candidates = []
            if _TAG_USER_COMMENT in exif_ifd:
                uc = exif_ifd[_TAG_USER_COMMENT]
                if isinstance(uc, bytes):
                    for prefix in (b"ASCII\x00\x00\x00", b"UNICODE\x00"):
                        if uc.startswith(prefix):
                            uc = uc[len(prefix) :]
                            break
                    candidates.append(uc.decode("utf-8", errors="ignore"))
                else:
                    candidates.append(str(uc))
            if _TAG_IMAGE_DESCRIPTION in exif:
                candidates.append(str(exif[_TAG_IMAGE_DESCRIPTION]))

            for cand in candidates:
                try:
                    parsed = json.loads(cand)
                    if isinstance(parsed, dict):
                        return parsed.get("AIGC", parsed)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

        # 4. XMP fallback (raw regex avoids requiring defusedxml)
        raw_xmp = img.info.get("xmp") or img.info.get("XML:com.adobe.xmp")
        if raw_xmp:
            xmp_str = (
                raw_xmp.decode("utf-8", errors="ignore")
                if isinstance(raw_xmp, bytes)
                else str(raw_xmp)
            )
            match = re.search(r'TC260:AIGC="([^"]+)"', xmp_str)
            if match:
                unescaped = (
                    match.group(1)
                    .replace("&quot;", '"')
                    .replace("&apos;", "'")
                    .replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                )
                try:
                    parsed = json.loads(unescaped)
                    if isinstance(parsed, dict):
                        return parsed.get("AIGC", parsed)
                except json.JSONDecodeError:
                    pass

    return None


def read_video_watermark(file_path: str) -> Optional[Dict[str, str]]:
    """Read and parse GB 45438 AIGC invisible metadata from a video file."""
    if os.path.splitext(file_path)[1].lower() == ".gif":
        return read_image_watermark(file_path)

    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return None

    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            file_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    try:
        info = json.loads(result.stdout)
        tags = info.get("format", {}).get("tags", {})
    except json.JSONDecodeError:
        return None

    lower_tags = {k.lower(): v for k, v in tags.items()}
    for key in ("aigc", "comment", "description"):
        val = lower_tags.get(key)
        if not val:
            continue
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed.get("AIGC", parsed)
        except json.JSONDecodeError:
            continue

    return None
