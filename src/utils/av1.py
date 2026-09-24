"""
AV1 encoder detection and FFmpeg command building.

Detection priority: libsvtav1 → libaom-av1 → rav1e
"""

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

AV1_CANDIDATES = ["libsvtav1", "libaom-av1", "rav1e"]

_detected_encoder: Optional[str] = None   # cached after first detection
_detection_done   = False


async def detect_av1_encoder(ffmpeg_path: str = "ffmpeg") -> Optional[str]:
    """Return the first available AV1 encoder or None."""
    global _detected_encoder, _detection_done
    if _detection_done:
        return _detected_encoder

    for enc in AV1_CANDIDATES:
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_path, "-hide_banner", "-encoders",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            text = stdout.decode("utf-8", errors="ignore")
            if enc in text:
                _detected_encoder = enc
                _detection_done   = True
                logger.info("[AV1] Detected encoder: %s", enc)
                return enc
        except Exception as e:
            logger.warning("[AV1] Detection error for %s: %s", enc, e)

    _detected_encoder = None
    _detection_done   = True
    logger.warning("[AV1] No AV1 encoder found in FFmpeg")
    return None


def get_cached_av1_encoder() -> Optional[str]:
    return _detected_encoder


AV1_DEFAULTS: dict = {
    "codec":        "libsvtav1",
    "crf":          29,
    "preset":       8,
    "pixel_format": "yuv420p10le",
    "threads":      "auto",
}

_VALID_CODECS  = frozenset(AV1_CANDIDATES)
_VALID_PIXFMTS = frozenset({"yuv420p10le", "yuv420p"})


def _safe_int(value, default: int) -> int:
    """Return int(value) or *default* when value is None, empty, or non-numeric."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value, default: str, valid: frozenset | None = None) -> str:
    """Return str(value) or *default* when value is None, empty, or not in *valid*."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    s = str(value).strip()
    if valid is not None and s not in valid:
        logger.warning("[AV1] Unrecognised value %r; falling back to default %r", s, default)
        return default
    return s


def normalize_av1_settings(raw: dict) -> dict:
    """
    Return a sanitised copy of an AV1 settings dict.
    Every None / empty / invalid value is replaced with the correct default.
    Call this before persisting to MongoDB *and* before building the FFmpeg command
    so the encoder never receives None for a numeric argument.
    """
    threads_raw = raw.get("threads", AV1_DEFAULTS["threads"])
    # Keep "auto" as a string; convert anything else to int safely.
    if threads_raw is None or (isinstance(threads_raw, str) and threads_raw.strip().lower() == "auto"):
        threads_clean: str | int = "auto"
    else:
        threads_clean = _safe_int(threads_raw, 0)

    return {
        "codec":        _safe_str(raw.get("codec"),        AV1_DEFAULTS["codec"],        _VALID_CODECS),
        "crf":          _safe_int(raw.get("crf"),          AV1_DEFAULTS["crf"]),
        "preset":       _safe_int(raw.get("preset"),       AV1_DEFAULTS["preset"]),
        "pixel_format": _safe_str(raw.get("pixel_format"), AV1_DEFAULTS["pixel_format"], _VALID_PIXFMTS),
        "threads":      threads_clean,
    }


def build_av1_video_args(av1_settings: dict, threads_override: Optional[int] = None) -> list[str]:
    """
    Return the FFmpeg video stream arguments for AV1 encoding.
    Does NOT include -i or output path.
    Normalises all values so None / empty / invalid never reach FFmpeg.
    """
    safe        = normalize_av1_settings(av1_settings)
    codec       = safe["codec"]
    crf         = safe["crf"]
    preset      = safe["preset"]
    pixfmt      = safe["pixel_format"]
    threads_cfg = safe["threads"]

    if threads_override is not None:
        threads = int(threads_override)
    elif str(threads_cfg).lower() == "auto":
        threads = 0   # let FFmpeg decide per-encoder
    else:
        threads = _safe_int(threads_cfg, 0)

    args = ["-c:v", codec, "-crf", str(crf), "-pix_fmt", pixfmt]

    if codec == "libsvtav1":
        # libsvtav1 uses -preset (0 = slowest, 13 = fastest)
        args += ["-preset", str(preset)]
        if threads > 0:
            args += ["-svtav1-params", f"lp={threads}"]
    elif codec == "libaom-av1":
        # libaom uses -cpu-used (0–8)
        args += ["-cpu-used", str(preset)]
        if threads > 0:
            args += ["-threads", str(threads)]
    elif codec == "rav1e":
        # rav1e uses -speed (0–10)
        args += ["-speed", str(preset)]
        if threads > 0:
            args += ["-threads", str(threads)]
    else:
        args += ["-preset", str(preset)]
        if threads > 0:
            args += ["-threads", str(threads)]

    return args


def av1_pixel_format_compatible(source_pix_fmt: str, target: str = "yuv420p10le") -> bool:
    """
    Return True if the target pixel format is compatible with the source.
    Falls back to yuv420p if 10-bit is not available.
    """
    ten_bit_capable = {"yuv420p10le", "yuv422p10le", "yuv444p10le"}
    ten_bit_sources = {
        "yuv420p10le", "yuv422p10le", "yuv444p10le",
        "yuv420p10be", "yuv422p10be", "yuv444p10be",
        "p010le", "p010be",
    }
    if target in ten_bit_capable and source_pix_fmt not in ten_bit_sources:
        return False
    return True


def safe_av1_pixel_format(source_pix_fmt: str, requested: str = "yuv420p10le") -> str:
    """Return requested pixel format if safe, else fall back to yuv420p."""
    if av1_pixel_format_compatible(source_pix_fmt, requested):
        return requested
    logger.info(
        "[AV1] Source pix_fmt=%s incompatible with %s; falling back to yuv420p",
        source_pix_fmt, requested,
    )
    return "yuv420p"
