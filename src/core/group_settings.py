"""
Group-global settings — MongoDB-backed.

Each Telegram group/chat has ONE settings document in `group_settings`.
All users in the same group share the same encoding configuration.

Schema:
  chat_id         int (unique index)
  resolutions     list[str]   e.g. ["1080p", "720p"]
  profiles        dict        {res: {codec_key: {crf, preset, audio_bitrate}}}
                              codec_key = "h264" | "h265"
  audio           dict        {mode: "copy"|"encode", codec, bitrate, channels}
  subtitles       dict        {mode: "copy"|"remove"|"reencode"}
  metadata        dict        {title, author, encoder}
  thumbnail_path  str         "__mongo__:thumb" sentinel or ""
  thumbnail_b64   str         base64 JPEG
  watermark       dict
  av1             dict        {codec, crf, preset, pixel_format, threads}
  send_type       str         "media"|"document"
  auto_detect_thumb bool
  parallel_limit  int         max concurrent encodes for this group
  updated_at      str         ISO datetime
  updated_by      int         user_id who last changed it
"""

import asyncio
import base64
import copy
import logging
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_db = None


def init_group_db(database):
    global _db
    _db = database


def _col():
    if _db is None:
        raise RuntimeError("group_settings DB not initialised — call init_group_db()")
    return _db["group_settings"]


# ── Defaults ──────────────────────────────────────────────────────────────────

# profiles[resolution][codec_key] = {crf, preset, audio_bitrate}
# codec_key: "h264" | "h265"
DEFAULT_PROFILES = {
    "1080p": {
        "h264": {"crf": 23, "preset": "medium", "audio_bitrate": "192k"},
        "h265": {"crf": 24, "preset": "medium", "audio_bitrate": "192k"},
    },
    "720p": {
        "h264": {"crf": 26, "preset": "medium", "audio_bitrate": "128k"},
        "h265": {"crf": 26, "preset": "medium", "audio_bitrate": "128k"},
    },
    "480p": {
        "h264": {"crf": 28, "preset": "fast",   "audio_bitrate": "96k"},
        "h265": {"crf": 28, "preset": "fast",   "audio_bitrate": "96k"},
    },
}

DEFAULT_AV1 = {
    "codec":        "libsvtav1",   # detect at runtime
    "crf":          29,
    "preset":       8,
    "pixel_format": "yuv420p10le",
    "threads":      "auto",
}

DEFAULT_AUDIO = {
    "mode":        "auto",    # "auto", "copy", or "encode"
    "codec":       "aac",
    "bitrate":     "128k",
    "channels":    2,
    "sample_rate": 48000,
}

DEFAULT_SUBTITLES = {
    "mode": "copy",   # "copy", "remove", "reencode"
}

DEFAULT_METADATA = {
    "title":       "",
    "artist":      "",
    "author":      "",
    "comment":     "",
    "audio_track": "",
    "video_track": "",
    "subtitle":    "",
}

DEFAULT_WATERMARK = {
    "enabled":      False,
    "text":         "",
    "color":        "white",
    "font_path":    "",
    "font_b64":     "",
    "font_ext":     ".ttf",
    "font_name":    "default",
    "font_size":    24,
    "padding":      7,
    "timing_mode":  "range",
    "start":        0,
    "end":          0,
    "duration":     30,
    "repeat_count": 1,
    "position":     "bot_right",
}


def _default_doc(chat_id: int) -> Dict[str, Any]:
    return {
        "chat_id":           chat_id,
        "resolutions":       ["1080p"],
        "profiles":          {k: {ck: cv.copy() for ck, cv in v.items()} for k, v in DEFAULT_PROFILES.items()},
        "audio":             DEFAULT_AUDIO.copy(),
        "subtitles":         DEFAULT_SUBTITLES.copy(),
        "metadata":          DEFAULT_METADATA.copy(),
        "thumbnail_path":    "",
        "thumbnail_b64":     "",
        "watermark":         DEFAULT_WATERMARK.copy(),
        "av1":               DEFAULT_AV1.copy(),
        "send_type":         "media",
        "auto_detect_thumb": False,
        "parallel_limit":    1,
        "updated_at":        datetime.utcnow().isoformat(),
        "updated_by":        0,
    }


def _apply_defaults(doc: dict) -> dict:
    d = doc
    d.setdefault("resolutions",       ["1080p"])
    d.setdefault("audio",             DEFAULT_AUDIO.copy())
    d.setdefault("subtitles",         DEFAULT_SUBTITLES.copy())
    d.setdefault("metadata",          DEFAULT_METADATA.copy())
    d.setdefault("watermark",         DEFAULT_WATERMARK.copy())
    d.setdefault("send_type",         "media")
    d.setdefault("auto_detect_thumb", False)
    d.setdefault("thumbnail_path",    "")
    d.setdefault("thumbnail_b64",     "")
    d.setdefault("parallel_limit",    1)

    if "profiles" not in d:
        d["profiles"] = {k: {ck: cv.copy() for ck, cv in v.items()} for k, v in DEFAULT_PROFILES.items()}
    else:
        for res, codecs in DEFAULT_PROFILES.items():
            d["profiles"].setdefault(res, {})
            for ck, cv in codecs.items():
                d["profiles"][res].setdefault(ck, cv.copy())
            # Migrate old flat format {crf, preset, codec, audio_bitrate} → new
            if "crf" in d["profiles"][res]:
                old_flat = d["profiles"].pop(res)
                d["profiles"][res] = {
                    ck: {
                        "crf":           old_flat.get("crf", cv["crf"]),
                        "preset":        old_flat.get("preset", cv["preset"]),
                        "audio_bitrate": old_flat.get("audio_bitrate", cv["audio_bitrate"]),
                    }
                    for ck, cv in DEFAULT_PROFILES[res].items()
                }

    if "av1" not in d or not isinstance(d.get("av1"), dict):
        d["av1"] = DEFAULT_AV1.copy()
    else:
        # setdefault only fills *absent* keys; also replace None / wrong-type values
        # so the encoder never receives None for a numeric field.
        from src.utils.av1 import normalize_av1_settings
        merged = {**DEFAULT_AV1, **{k: v for k, v in d["av1"].items() if v is not None}}
        d["av1"] = normalize_av1_settings(merged)

    for k, v in DEFAULT_WATERMARK.items():
        d["watermark"].setdefault(k, v)
    for k, v in DEFAULT_AUDIO.items():
        d["audio"].setdefault(k, v)
    # Migrate old "reencode" mode to "encode" / "auto"
    if d["audio"].get("mode") == "reencode":
        d["audio"]["mode"] = "encode"
    for k, v in DEFAULT_SUBTITLES.items():
        d["subtitles"].setdefault(k, v)
    for k, v in DEFAULT_METADATA.items():
        d["metadata"].setdefault(k, v)
    # Back-compat: migrate old "encoder" field (no longer stored but carry forward)
    d["metadata"].setdefault("encoder", "")

    return d


# ── In-memory cache (evicts at 200 groups) ────────────────────────────────────

_cache: Dict[int, Dict] = {}
_CACHE_MAX = 200
# Tmp file paths for binary assets (keyed by chat_id)
_thumb_tmp:  Dict[int, str] = {}
_font_tmp:   Dict[int, str] = {}


def _evict():
    if len(_cache) >= _CACHE_MAX:
        evicted = next(iter(_cache))
        _cache.pop(evicted, None)
        _thumb_tmp.pop(evicted, None)
        _font_tmp.pop(evicted, None)


# ── Public async API ──────────────────────────────────────────────────────────

async def get_group_settings(chat_id: int) -> Dict[str, Any]:
    if chat_id in _cache:
        return copy.deepcopy(_cache[chat_id])
    try:
        doc = await _col().find_one({"chat_id": chat_id})
    except Exception as e:
        logger.warning("[GroupSettings] load failed for %d: %s", chat_id, e)
        doc = None
    if doc:
        doc.pop("_id", None)
    else:
        doc = _default_doc(chat_id)
    doc = _apply_defaults(doc)
    _evict()
    _cache[chat_id] = copy.deepcopy(doc)
    return copy.deepcopy(doc)


async def save_group_settings(chat_id: int, data: Dict[str, Any], updated_by: int = 0):
    data["chat_id"]    = chat_id
    data["updated_at"] = datetime.utcnow().isoformat()
    data["updated_by"] = updated_by
    _cache[chat_id]    = copy.deepcopy(data)
    try:
        await _col().replace_one({"chat_id": chat_id}, data, upsert=True)
    except Exception as e:
        logger.error("[GroupSettings] save failed for %d: %s", chat_id, e)


async def update_group_field(chat_id: int, key: str, value: Any, updated_by: int = 0):
    doc = await get_group_settings(chat_id)
    doc[key] = value
    await save_group_settings(chat_id, doc, updated_by)


async def set_group_thumbnail(chat_id: int, path: str, updated_by: int = 0):
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
    except Exception as e:
        logger.error("[GroupSettings] thumb encode failed: %s", e)
        return
    doc = await get_group_settings(chat_id)
    doc["thumbnail_b64"]  = b64
    doc["thumbnail_path"] = "__mongo__:thumb"
    _thumb_tmp.pop(chat_id, None)
    await save_group_settings(chat_id, doc, updated_by)


async def clear_group_thumbnail(chat_id: int, updated_by: int = 0):
    _thumb_tmp.pop(chat_id, None)
    doc = await get_group_settings(chat_id)
    doc["thumbnail_b64"]  = ""
    doc["thumbnail_path"] = ""
    await save_group_settings(chat_id, doc, updated_by)


def get_group_thumbnail_path(chat_id: int, settings: dict) -> Optional[str]:
    b64 = settings.get("thumbnail_b64", "")
    if not b64:
        return None
    cached = _thumb_tmp.get(chat_id, "")
    if cached and os.path.exists(cached):
        return cached
    try:
        data = base64.b64decode(b64)
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _thumb_tmp[chat_id] = path
        return path
    except Exception as e:
        logger.error("[GroupSettings] thumb materialise failed: %s", e)
        return None


async def set_group_parallel(chat_id: int, limit: int, updated_by: int = 0):
    doc = await get_group_settings(chat_id)
    doc["parallel_limit"] = max(1, limit)
    await save_group_settings(chat_id, doc, updated_by)


async def set_group_av1(chat_id: int, av1_dict: dict, updated_by: int = 0):
    from src.utils.av1 import normalize_av1_settings
    doc = await get_group_settings(chat_id)
    doc["av1"] = normalize_av1_settings(av1_dict)
    await save_group_settings(chat_id, doc, updated_by)


async def ensure_index(database):
    await database["group_settings"].create_index("chat_id", unique=True)


def get_codec_profile(settings: dict, resolution: str, codec_key: str) -> dict:
    """
    Return the profile dict for a given resolution + codec key ("h264" | "h265").
    Falls back gracefully if missing.
    """
    default_crf = {"h264": 23, "h265": 24}.get(codec_key, 23)
    fallback = {"crf": default_crf, "preset": "medium", "audio_bitrate": "128k"}
    return (
        settings
        .get("profiles", {})
        .get(resolution, {})
        .get(codec_key, fallback)
    )
