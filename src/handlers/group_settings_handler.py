"""
/gs — group-global settings panel
/parallel N — set encoding concurrency limit
/st — set group thumbnail (admin only)
"""

from __future__ import annotations

import logging
import os

from html import escape as _he

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.core.group_settings import (
    get_group_settings,
    save_group_settings,
    update_group_field,
    set_group_thumbnail,
    clear_group_thumbnail,
    set_group_parallel,
    set_group_av1,
    get_codec_profile,
    DEFAULT_PROFILES,
)
from src.utils.messages import (
    build_es_text,
    parallel_set_msg,
    parallel_denied_msg,
    admin_only_msg,
    access_denied_msg,
    _SEP,
)
from src.utils.admin_check import is_group_admin
from src.utils.resources import detect_max_concurrent_encodings

logger = logging.getLogger(__name__)

# ── Keyboard builders ─────────────────────────────────────────────────────────

RES_OPTIONS     = ["480p", "720p", "1080p"]
PRESET_OPTIONS  = ["ultrafast", "superfast", "veryfast", "faster",
                   "fast", "medium", "slow", "slower", "veryslow"]
AUDIO_BR        = ["48k", "64k", "96k", "128k", "192k", "256k", "320k"]
AUDIO_MODES     = ["auto", "copy", "encode"]
AUDIO_CODECS    = ["aac", "opus", "ac3", "eac3"]
AUDIO_SR        = [44100, 48000]
SUB_MODES       = ["copy", "remove"]
SEND_TYPES      = ["media", "document"]
AV1_CODECS      = ["libsvtav1", "libaom-av1", "rav1e"]
AV1_PIXFMTS     = ["yuv420p10le", "yuv420p"]


def _main_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    resolutions = settings.get("resolutions", ["1080p"])
    res_label = " / ".join(resolutions) if resolutions else "none"
    wm = settings.get("watermark", {})
    wm_label = "On ✓" if wm.get("enabled") else "Off"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📐 Resolutions  [{res_label}]", callback_data=f"gs:res:{chat_id}")],
        [InlineKeyboardButton("🎞 Video Profiles",       callback_data=f"gs:video:{chat_id}")],
        [InlineKeyboardButton("🔊 Audio",                callback_data=f"gs:audio:{chat_id}")],
        [InlineKeyboardButton("💬 Subtitles",            callback_data=f"gs:subs:{chat_id}")],
        [InlineKeyboardButton("🏷 Metadata",             callback_data=f"gs:meta:{chat_id}")],
        [InlineKeyboardButton("♾ AV1 Settings",         callback_data=f"gs:av1:{chat_id}")],
        [InlineKeyboardButton("🖼 Thumbnail",            callback_data=f"gs:thumb:{chat_id}")],
        [InlineKeyboardButton(f"🌀 Watermark  [{wm_label}]", callback_data=f"gs:wm:{chat_id}")],
        [InlineKeyboardButton("✗ Close",                 callback_data=f"gs:close:{chat_id}")],
    ])


def _back_btn(chat_id: int) -> list:
    return [InlineKeyboardButton("⬅ Back", callback_data=f"gs:main:{chat_id}")]


def _res_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    enabled = settings.get("resolutions", ["1080p"])
    rows = []
    for r in RES_OPTIONS:
        mark = "✓" if r in enabled else "✗"
        rows.append([InlineKeyboardButton(
            f"{mark}  {r}", callback_data=f"gs:res_toggle:{chat_id}:{r}"
        )])
    rows.append(_back_btn(chat_id))
    return InlineKeyboardMarkup(rows)


def _video_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    resolutions = settings.get("resolutions", ["1080p"])
    rows = []
    for r in resolutions:
        rows.append([
            InlineKeyboardButton(f"{r}  H.264", callback_data=f"gs:vprofile:{chat_id}:{r}:h264"),
            InlineKeyboardButton(f"{r}  H.265", callback_data=f"gs:vprofile:{chat_id}:{r}:h265"),
        ])
    rows.append(_back_btn(chat_id))
    return InlineKeyboardMarkup(rows)


def _profile_keyboard(chat_id: int, res: str, ck: str, settings: dict) -> InlineKeyboardMarkup:
    """ck = "h264" | "h265" """
    prof   = get_codec_profile(settings, res, ck)
    crf    = prof.get("crf", 23)
    preset = prof.get("preset", "medium")
    abr    = prof.get("audio_bitrate", "128k")
    pi = PRESET_OPTIONS.index(preset) if preset in PRESET_OPTIONS else 5
    ai = AUDIO_BR.index(abr) if abr in AUDIO_BR else 3
    label  = "H.264" if ck == "h264" else "H.265"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"CRF ◀  {crf}  ▶",
            callback_data=f"gs:crf:{chat_id}:{res}:{ck}:dec"),
         InlineKeyboardButton(f"▶",
            callback_data=f"gs:crf:{chat_id}:{res}:{ck}:inc")],
        [InlineKeyboardButton(f"Preset ◀  {preset}",
            callback_data=f"gs:preset:{chat_id}:{res}:{ck}:dec"),
         InlineKeyboardButton(f"▶",
            callback_data=f"gs:preset:{chat_id}:{res}:{ck}:inc")],
        [InlineKeyboardButton(f"Audio ◀  {abr}",
            callback_data=f"gs:abr:{chat_id}:{res}:{ck}:dec"),
         InlineKeyboardButton(f"▶",
            callback_data=f"gs:abr:{chat_id}:{res}:{ck}:inc")],
        [InlineKeyboardButton(f"↩ Reset {label} defaults",
            callback_data=f"gs:reset_profile:{chat_id}:{res}:{ck}")],
        [InlineKeyboardButton("⬅ Back to Video", callback_data=f"gs:video:{chat_id}")],
    ])


def _audio_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    audio  = settings.get("audio", {})
    mode   = audio.get("mode", "auto")
    if mode not in AUDIO_MODES:
        mode = "auto"
    mi     = AUDIO_MODES.index(mode)
    br     = audio.get("bitrate", "128k")
    ai     = AUDIO_BR.index(br) if br in AUDIO_BR else 3
    ch     = audio.get("channels", 2)
    codec  = audio.get("codec", "aac")
    if codec not in AUDIO_CODECS:
        codec = "aac"
    ci     = AUDIO_CODECS.index(codec)
    sr     = audio.get("sample_rate", 48000)
    sri    = AUDIO_SR.index(sr) if sr in AUDIO_SR else 1

    rows = [
        [InlineKeyboardButton(
            f"Mode: {mode}",
            callback_data=f"gs:audio_mode:{chat_id}:{(mi+1)%len(AUDIO_MODES)}"
        )],
    ]
    # Only show codec/bitrate/sample_rate/channels when mode is "encode"
    if mode == "encode":
        rows += [
            [InlineKeyboardButton(
                f"Codec: {codec}",
                callback_data=f"gs:audio_codec:{chat_id}:{(ci+1)%len(AUDIO_CODECS)}"
            )],
            [InlineKeyboardButton(f"Bitrate: {br}  ◀",
                callback_data=f"gs:audio_br:{chat_id}:dec"),
             InlineKeyboardButton(f"▶",
                callback_data=f"gs:audio_br:{chat_id}:inc")],
            [InlineKeyboardButton(
                f"Sample Rate: {sr} Hz",
                callback_data=f"gs:audio_sr:{chat_id}:{(sri+1)%len(AUDIO_SR)}"
            )],
            [InlineKeyboardButton(f"Channels: {ch}",
                callback_data=f"gs:audio_ch:{chat_id}:{2 if ch==6 else 6}")],
        ]
    rows.append(_back_btn(chat_id))
    return InlineKeyboardMarkup(rows)


def _subs_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    mode = settings.get("subtitles", {}).get("mode", "copy")
    mi   = SUB_MODES.index(mode) if mode in SUB_MODES else 0
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"Subtitle mode: {mode}",
            callback_data=f"gs:sub_mode:{chat_id}:{(mi+1)%len(SUB_MODES)}"
        )],
        _back_btn(chat_id),
    ])


def _av1_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    from src.utils.av1 import _safe_int, _safe_str, AV1_DEFAULTS
    av1    = settings.get("av1", {})
    codec  = _safe_str(av1.get("codec"),        AV1_DEFAULTS["codec"])
    crf    = _safe_int(av1.get("crf"),           AV1_DEFAULTS["crf"])
    preset = _safe_int(av1.get("preset"),        AV1_DEFAULTS["preset"])
    pf     = _safe_str(av1.get("pixel_format"),  AV1_DEFAULTS["pixel_format"])
    ci     = AV1_CODECS.index(codec) if codec in AV1_CODECS else 0
    pfi    = AV1_PIXFMTS.index(pf) if pf in AV1_PIXFMTS else 0
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"Encoder: {codec}",
            callback_data=f"gs:av1_codec:{chat_id}:{(ci+1)%len(AV1_CODECS)}"
        )],
        [InlineKeyboardButton(f"CRF: {crf}  ◀",
            callback_data=f"gs:av1_crf:{chat_id}:dec"),
         InlineKeyboardButton(f"▶",
            callback_data=f"gs:av1_crf:{chat_id}:inc")],
        [InlineKeyboardButton(f"Preset: {preset}  ◀",
            callback_data=f"gs:av1_preset:{chat_id}:dec"),
         InlineKeyboardButton(f"▶",
            callback_data=f"gs:av1_preset:{chat_id}:inc")],
        [InlineKeyboardButton(
            f"Pixel fmt: {pf}",
            callback_data=f"gs:av1_pf:{chat_id}:{(pfi+1)%len(AV1_PIXFMTS)}"
        )],
        _back_btn(chat_id),
    ])


def _thumb_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    has_thumb = bool(settings.get("thumbnail_b64", ""))
    rows = []
    if has_thumb:
        rows.append([InlineKeyboardButton(
            "🗑 Clear thumbnail", callback_data=f"gs:thumb_clear:{chat_id}"
        )])
    rows.append([InlineKeyboardButton(
        "ℹ To set: reply to a photo with /st", callback_data="gs:noop"
    )])
    rows.append(_back_btn(chat_id))
    return InlineKeyboardMarkup(rows)


WM_COLORS     = ["white", "black", "red", "green", "blue", "yellow"]
WM_POSITIONS  = ["top_left", "top_mid", "top_right", "mid_left", "mid_right", "bot_left", "bot_right"]
WM_TIMING     = ["full", "range", "random_duration"]
WM_POS_LABELS = {
    "top_left":  "🡔 Top Left",  "top_mid":  "🡑 Top Mid",  "top_right": "🡕 Top Right",
    "mid_left":  "🡐 Mid Left",  "mid_right": "🡒 Mid Right",
    "bot_left":  "🡗 Bot Left",  "bot_right": "🡖 Bot Right",
}


def _wm_text(wm: dict, note: str = "") -> str:
    enabled  = wm.get("enabled", False)
    text     = wm.get("text", "") or "—"
    color    = wm.get("color", "white").capitalize()
    pos      = WM_POS_LABELS.get(wm.get("position", "bot_right"), "Bot Right")
    size     = wm.get("font_size", 24)
    timing   = wm.get("timing_mode", "range").replace("_", " ").title()
    note_str = f"\n<i>{note}</i>" if note else ""
    return (
        f"🌀 <b>Group Watermark</b>  {'<u>On ✓</u>' if enabled else '<i>Off</i>'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Text:     <code>{text}</code>\n"
        f"Color:    <code>{color}</code>   Size: <code>{size}</code>\n"
        f"Position: {pos}\n"
        f"Timing:   <code>{timing}</code>{note_str}"
    )


def _wm_keyboard(chat_id: int, wm: dict) -> InlineKeyboardMarkup:
    enabled = wm.get("enabled", False)
    toggle_lbl = "✅ Enabled  →  Disable" if enabled else "☐ Disabled  →  Enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_lbl,        callback_data=f"gs:wm_toggle:{chat_id}")],
        [InlineKeyboardButton("📝 Set Text",      callback_data=f"gs:wm_text:{chat_id}"),
         InlineKeyboardButton("🎨 Color",         callback_data=f"gs:wm_color:{chat_id}")],
        [InlineKeyboardButton("📍 Position",      callback_data=f"gs:wm_pos:{chat_id}"),
         InlineKeyboardButton("⏱ Timing",         callback_data=f"gs:wm_timing:{chat_id}")],
        [InlineKeyboardButton("🔤 Font Size  ◀",  callback_data=f"gs:wm_fs:{chat_id}:dec"),
         InlineKeyboardButton(str(wm.get("font_size", 24)),  callback_data="gs:noop"),
         InlineKeyboardButton("▶",               callback_data=f"gs:wm_fs:{chat_id}:inc")],
        [InlineKeyboardButton("↩ Reset",          callback_data=f"gs:wm_reset:{chat_id}"),
         InlineKeyboardButton("⬅ Back",           callback_data=f"gs:main:{chat_id}")],
    ])


def _wm_color_keyboard(chat_id: int, current: str) -> InlineKeyboardMarkup:
    def lbl(c): return f"● {c.capitalize()}" if c == current else c.capitalize()
    rows = [
        [InlineKeyboardButton(lbl("white"),  callback_data=f"gs:wm_color_set:{chat_id}:white"),
         InlineKeyboardButton(lbl("black"),  callback_data=f"gs:wm_color_set:{chat_id}:black")],
        [InlineKeyboardButton(lbl("red"),    callback_data=f"gs:wm_color_set:{chat_id}:red"),
         InlineKeyboardButton(lbl("green"),  callback_data=f"gs:wm_color_set:{chat_id}:green")],
        [InlineKeyboardButton(lbl("yellow"), callback_data=f"gs:wm_color_set:{chat_id}:yellow"),
         InlineKeyboardButton(lbl("blue"),   callback_data=f"gs:wm_color_set:{chat_id}:blue")],
        [InlineKeyboardButton("⬅ Back",      callback_data=f"gs:wm:{chat_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def _wm_pos_keyboard(chat_id: int, current: str) -> InlineKeyboardMarkup:
    def lbl(k): return f"● {WM_POS_LABELS[k]}" if k == current else WM_POS_LABELS[k]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl("top_left"),  callback_data=f"gs:wm_pos_set:{chat_id}:top_left"),
         InlineKeyboardButton(lbl("top_mid"),   callback_data=f"gs:wm_pos_set:{chat_id}:top_mid"),
         InlineKeyboardButton(lbl("top_right"), callback_data=f"gs:wm_pos_set:{chat_id}:top_right")],
        [InlineKeyboardButton(lbl("mid_left"),  callback_data=f"gs:wm_pos_set:{chat_id}:mid_left"),
         InlineKeyboardButton(lbl("mid_right"), callback_data=f"gs:wm_pos_set:{chat_id}:mid_right")],
        [InlineKeyboardButton(lbl("bot_left"),  callback_data=f"gs:wm_pos_set:{chat_id}:bot_left"),
         InlineKeyboardButton(lbl("bot_right"), callback_data=f"gs:wm_pos_set:{chat_id}:bot_right")],
        [InlineKeyboardButton("⬅ Back",         callback_data=f"gs:wm:{chat_id}")],
    ])


def _wm_timing_keyboard(chat_id: int, current: str) -> InlineKeyboardMarkup:
    def lbl(k, t): return f"● {t}" if k == current else t
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl("full", "Full Duration"),       callback_data=f"gs:wm_timing_set:{chat_id}:full")],
        [InlineKeyboardButton(lbl("range", "Start → End"),        callback_data=f"gs:wm_timing_set:{chat_id}:range")],
        [InlineKeyboardButton(lbl("random_duration", "Random"),   callback_data=f"gs:wm_timing_set:{chat_id}:random_duration")],
        [InlineKeyboardButton("⬅ Back",                           callback_data=f"gs:wm:{chat_id}")],
    ])


# ── Display helpers ───────────────────────────────────────────────────────────

def _profile_text(res: str, ck: str, settings: dict) -> str:
    prof  = get_codec_profile(settings, res, ck)
    label = "H.264 (libx264)" if ck == "h264" else "H.265 (libx265)"
    return (
        f"⚙️ <b>{res}  ·  {label}</b>\n"
        f"{_SEP}\n"
        f"CRF:    <code>{prof.get('crf', 23)}</code>\n"
        f"Preset: <code>{prof.get('preset', 'medium')}</code>\n"
        f"Audio:  <code>{prof.get('audio_bitrate', '128k')}</code>"
    )


async def _show_main(msg_or_cb, chat_id: int, chat_title: str, edit: bool = False):
    settings = await get_group_settings(chat_id)
    text = build_es_text(chat_title, settings)
    kb   = _main_keyboard(chat_id, settings)
    if edit:
        try:
            await msg_or_cb.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    else:
        await msg_or_cb.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ── /gs command ───────────────────────────────────────────────────────────────

async def _cmd_es(client: Client, message: Message, config):
    chat_id = message.chat.id
    user_id = message.from_user.id

    # /gs is group-only
    if message.chat.id == user_id or getattr(message.chat, "type", None) in ("private", None):
        await message.reply_text("This command can only be used in groups.")
        return

    if not await is_group_admin(client, chat_id, user_id, config):
        await message.reply_text(admin_only_msg())
        return

    chat_title = message.chat.title or "this group"
    await _show_main(message, chat_id, chat_title)


# ── /parallel N ───────────────────────────────────────────────────────────────

async def _cmd_parallel(client: Client, message: Message, config):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if not await is_group_admin(client, chat_id, user_id, config):
        await message.reply_text(admin_only_msg())
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply_text(
            "Usage: <code>/parallel 1</code>  (1–4)", parse_mode=ParseMode.HTML
        )
        return

    try:
        requested = int(parts[1])
    except ValueError:
        await message.reply_text("❌ Please provide an integer, e.g. <code>/parallel 2</code>", parse_mode=ParseMode.HTML)
        return

    machine_max = detect_max_concurrent_encodings()
    # AV1 is heavier — cap group limit at machine_max
    if requested > machine_max:
        await message.reply_text(
            parallel_denied_msg(requested, machine_max), parse_mode=ParseMode.HTML
        )
        return

    await set_group_parallel(chat_id, requested, updated_by=user_id)
    await message.reply_text(parallel_set_msg(requested), parse_mode=ParseMode.HTML)


# ── /st — set thumbnail ───────────────────────────────────────────────────────

async def _cmd_st(client: Client, message: Message, config):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if not await is_group_admin(client, chat_id, user_id, config):
        await message.reply_text(admin_only_msg())
        return

    replied = message.reply_to_message
    photo = None
    if replied:
        if replied.photo:
            photo = replied.photo
        elif replied.document and replied.document.mime_type and \
                "image" in replied.document.mime_type:
            photo = replied.document

    if not photo:
        await message.reply_text("↩️ Reply to a photo with <code>/st</code>.", parse_mode=ParseMode.HTML)
        return

    tmp_path = os.path.join(config.paths.tmp, f"thumb_grp_{chat_id}.jpg")
    try:
        await client.download_media(photo, file_name=tmp_path)
        await set_group_thumbnail(chat_id, tmp_path, updated_by=user_id)
        await message.reply_text("✅ <b>Group thumbnail saved.</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("[GroupSettings] thumbnail save failed: %s", e)
        await message.reply_text(f"❌ Failed to save thumbnail: {e}")


# ── Callback dispatcher ───────────────────────────────────────────────────────

async def _handle_gs_callback(client: Client, cb: CallbackQuery, config):
    data    = cb.data  # gs:<action>:<chat_id>[:<extra>...]
    parts   = data.split(":")
    action  = parts[1] if len(parts) > 1 else ""
    chat_id = int(parts[2]) if len(parts) > 2 else cb.message.chat.id
    user_id = cb.from_user.id

    # Admin check
    if action not in ("noop",) and not await is_group_admin(client, chat_id, user_id, config):
        await cb.answer(admin_only_msg(), show_alert=True)
        return

    settings = await get_group_settings(chat_id)
    chat_title = cb.message.chat.title or "this group"

    # ── Main menu ──────────────────────────────────────────────────────────────
    if action == "main":
        await _show_main(cb.message, chat_id, chat_title, edit=True)

    elif action == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass

    elif action == "noop":
        pass

    # ── Resolutions ────────────────────────────────────────────────────────────
    elif action == "res":
        kb = _res_keyboard(chat_id, settings)
        try:
            await cb.message.edit_text(
                "📐 <b>Resolutions</b>\nToggle which qualities are available for /e.",
                reply_markup=kb, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    elif action == "res_toggle":
        res = parts[3] if len(parts) > 3 else ""
        enabled = list(settings.get("resolutions", ["1080p"]))
        if res in enabled:
            if len(enabled) == 1:
                await cb.answer("At least one resolution must be enabled.", show_alert=True)
                return
            enabled.remove(res)
        else:
            enabled.append(res)
        settings["resolutions"] = [r for r in RES_OPTIONS if r in enabled]
        await save_group_settings(chat_id, settings, user_id)
        kb = _res_keyboard(chat_id, settings)
        try:
            await cb.message.edit_reply_markup(kb)
        except Exception:
            pass

    # ── Video profiles ─────────────────────────────────────────────────────────
    elif action == "video":
        kb = _video_keyboard(chat_id, settings)
        try:
            await cb.message.edit_text(
                "🎞 <b>Video Profiles</b>\nEdit per-resolution encoding settings.",
                reply_markup=kb, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    elif action == "vprofile":
        res = parts[3] if len(parts) > 3 else "1080p"
        ck  = parts[4] if len(parts) > 4 else "h264"
        kb  = _profile_keyboard(chat_id, res, ck, settings)
        try:
            await cb.message.edit_text(
                _profile_text(res, ck, settings), reply_markup=kb, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    elif action == "crf":
        res = parts[3]; ck = parts[4]; direction = parts[5]
        prof = settings.setdefault("profiles", {}).setdefault(res, {}).setdefault(ck, {})
        crf  = int(prof.get("crf", 23))
        prof["crf"] = max(0, min(51, crf + (-1 if direction == "dec" else 1)))
        await save_group_settings(chat_id, settings, user_id)
        kb = _profile_keyboard(chat_id, res, ck, settings)
        try:
            await cb.message.edit_text(_profile_text(res, ck, settings), reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "preset":
        res = parts[3]; ck = parts[4]; direction = parts[5]
        prof = settings.setdefault("profiles", {}).setdefault(res, {}).setdefault(ck, {})
        cur  = prof.get("preset", "medium")
        idx  = PRESET_OPTIONS.index(cur) if cur in PRESET_OPTIONS else 5
        idx  = max(0, min(len(PRESET_OPTIONS)-1, idx + (-1 if direction == "dec" else 1)))
        prof["preset"] = PRESET_OPTIONS[idx]
        await save_group_settings(chat_id, settings, user_id)
        kb = _profile_keyboard(chat_id, res, ck, settings)
        try:
            await cb.message.edit_text(_profile_text(res, ck, settings), reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    # "codec" action removed — codec is now implicit in the ck key (h264/h265)

    elif action == "abr":
        res = parts[3]; ck = parts[4]; direction = parts[5]
        prof = settings.setdefault("profiles", {}).setdefault(res, {}).setdefault(ck, {})
        cur  = prof.get("audio_bitrate", "128k")
        idx  = AUDIO_BR.index(cur) if cur in AUDIO_BR else 3
        idx  = max(0, min(len(AUDIO_BR)-1, idx + (-1 if direction == "dec" else 1)))
        prof["audio_bitrate"] = AUDIO_BR[idx]
        await save_group_settings(chat_id, settings, user_id)
        kb = _profile_keyboard(chat_id, res, ck, settings)
        try:
            await cb.message.edit_text(_profile_text(res, ck, settings), reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "reset_profile":
        res = parts[3]; ck = parts[4] if len(parts) > 4 else "h264"
        defaults = DEFAULT_PROFILES.get(res, {}).get(ck, {})
        settings.setdefault("profiles", {}).setdefault(res, {})[ck] = defaults.copy()
        await save_group_settings(chat_id, settings, user_id)
        kb = _profile_keyboard(chat_id, res, ck, settings)
        try:
            await cb.message.edit_text(_profile_text(res, ck, settings), reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    # ── Audio ──────────────────────────────────────────────────────────────────
    elif action == "audio":
        kb = _audio_keyboard(chat_id, settings)
        try:
            await cb.message.edit_text(
                "🔊 <b>Audio Settings</b>", reply_markup=kb, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    elif action == "audio_mode":
        idx = int(parts[3])
        settings.setdefault("audio", {})["mode"] = AUDIO_MODES[idx]
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_text(
                "🔊 <b>Audio Settings</b>",
                reply_markup=_audio_keyboard(chat_id, settings),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "audio_br":
        direction = parts[3]
        cur = settings.setdefault("audio", {}).get("bitrate", "128k")
        idx = AUDIO_BR.index(cur) if cur in AUDIO_BR else 3
        idx = max(0, min(len(AUDIO_BR)-1, idx + (-1 if direction == "dec" else 1)))
        settings["audio"]["bitrate"] = AUDIO_BR[idx]
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_audio_keyboard(chat_id, settings))
        except Exception:
            pass

    elif action == "audio_ch":
        ch = int(parts[3])
        settings.setdefault("audio", {})["channels"] = ch
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_audio_keyboard(chat_id, settings))
        except Exception:
            pass

    elif action == "audio_codec":
        idx = int(parts[3])
        settings.setdefault("audio", {})["codec"] = AUDIO_CODECS[idx]
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_audio_keyboard(chat_id, settings))
        except Exception:
            pass

    elif action == "audio_sr":
        idx = int(parts[3])
        settings.setdefault("audio", {})["sample_rate"] = AUDIO_SR[idx]
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_audio_keyboard(chat_id, settings))
        except Exception:
            pass

    # ── Subtitles ──────────────────────────────────────────────────────────────
    elif action == "subs":
        kb = _subs_keyboard(chat_id, settings)
        try:
            await cb.message.edit_text("💬 <b>Subtitle Settings</b>", reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "sub_mode":
        idx = int(parts[3])
        settings.setdefault("subtitles", {})["mode"] = SUB_MODES[idx]
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_subs_keyboard(chat_id, settings))
        except Exception:
            pass

    # ── Metadata ───────────────────────────────────────────────────────────────
    elif action == "meta":
        meta = settings.get("metadata", {})
        try:
            await cb.message.edit_text(
                f"🏷 <b>Metadata</b>\n{_SEP}\n"
                f"┃  🎬 Title  ·  <code>{_he(meta.get('title') or '—')}</code>\n"
                f"┃  🎨 Artist  ·  <code>{_he(meta.get('artist') or '—')}</code>\n"
                f"┃  ✍️ Author  ·  <code>{_he(meta.get('author') or '—')}</code>\n"
                f"┃  💬 Comment  ·  <code>{_he(meta.get('comment') or '—')}</code>\n"
                f"┃  🔊 Audio Track  ·  <code>{_he(meta.get('audio_track') or '—')}</code>\n"
                f"┃  🎥 Video Track  ·  <code>{_he(meta.get('video_track') or '—')}</code>\n"
                f"┃  📝 Subtitle  ·  <code>{_he(meta.get('subtitle') or '—')}</code>\n\n"
                "Use <code>/meta &lt;field&gt; &lt;value&gt;</code> to set:\n"
                "<code>title</code> · <code>artist</code> · <code>author</code> · "
                "<code>comment</code> · <code>audio_track</code> · "
                "<code>video_track</code> · <code>subtitle</code>",
                reply_markup=InlineKeyboardMarkup([_back_btn(chat_id)]),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    # ── AV1 ────────────────────────────────────────────────────────────────────
    elif action == "av1":
        kb = _av1_keyboard(chat_id, settings)
        av1 = settings.get("av1", {})
        try:
            await cb.message.edit_text(
                f"♾ <b>AV1 Settings</b>\n{_SEP}\n"
                f"Encoder: <code>{av1.get('codec','libsvtav1')}</code>\n"
                f"CRF:     <code>{av1.get('crf',29)}</code>\n"
                f"Preset:  <code>{av1.get('preset',8)}</code>\n"
                f"Pixel:   <code>{av1.get('pixel_format','yuv420p10le')}</code>",
                reply_markup=kb, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    elif action == "av1_codec":
        idx = int(parts[3])
        settings.setdefault("av1", {})["codec"] = AV1_CODECS[idx]
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_av1_keyboard(chat_id, settings))
        except Exception:
            pass

    elif action == "av1_crf":
        from src.utils.av1 import _safe_int
        direction = parts[3]
        av1 = settings.setdefault("av1", {})
        cur_crf = _safe_int(av1.get("crf"), 29)
        av1["crf"] = max(0, min(63, cur_crf + (-1 if direction == "dec" else 1)))
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_av1_keyboard(chat_id, settings))
        except Exception:
            pass

    elif action == "av1_preset":
        from src.utils.av1 import _safe_int
        direction = parts[3]
        av1 = settings.setdefault("av1", {})
        cur_preset = _safe_int(av1.get("preset"), 8)
        av1["preset"] = max(0, min(13, cur_preset + (-1 if direction == "dec" else 1)))
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_av1_keyboard(chat_id, settings))
        except Exception:
            pass

    elif action == "av1_pf":
        idx = int(parts[3])
        settings.setdefault("av1", {})["pixel_format"] = AV1_PIXFMTS[idx]
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_av1_keyboard(chat_id, settings))
        except Exception:
            pass

    # ── Thumbnail ──────────────────────────────────────────────────────────────
    elif action == "thumb":
        kb = _thumb_keyboard(chat_id, settings)
        has_thumb = bool(settings.get("thumbnail_b64", ""))
        thumb_text = "Set ✓" if has_thumb else "Not set"
        try:
            await cb.message.edit_text(
                f"🖼 <b>Thumbnail</b>  {thumb_text}\n{_SEP}\n"
                "Use <code>/st</code> (reply to a photo) to set the group thumbnail.",
                reply_markup=kb, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    elif action == "thumb_clear":
        await clear_group_thumbnail(chat_id, user_id)
        settings = await get_group_settings(chat_id)
        kb = _thumb_keyboard(chat_id, settings)
        try:
            await cb.message.edit_text(
                f"🖼 <b>Thumbnail</b>  Not set\n{_SEP}\nThumbnail cleared.",
                reply_markup=kb, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    # ── Watermark ──────────────────────────────────────────────────────────────
    elif action == "wm":
        wm = settings.get("watermark", {})
        try:
            await cb.message.edit_text(
                _wm_text(wm),
                reply_markup=_wm_keyboard(chat_id, wm),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_toggle":
        wm = settings.setdefault("watermark", {})
        wm["enabled"] = not wm.get("enabled", False)
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_text(
                _wm_text(wm),
                reply_markup=_wm_keyboard(chat_id, wm),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_text":
        # Prompt user to send watermark text
        try:
            await cb.message.edit_text(
                "✏️ <b>Set Watermark Text</b>\n"
                "Reply to this message with the text you want as watermark.\n\n"
                "Use <code>/wm_text &lt;your text&gt;</code>  — or send it as a reply.\n"
                "Example: <code>/wm_text Ocean Encode</code>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ Back", callback_data=f"gs:wm:{chat_id}")
                ]]),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_color":
        wm = settings.get("watermark", {})
        try:
            await cb.message.edit_text(
                "🎨 <b>Watermark Color</b>",
                reply_markup=_wm_color_keyboard(chat_id, wm.get("color", "white")),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_color_set":
        color = parts[3] if len(parts) > 3 else "white"
        if color not in WM_COLORS:
            color = "white"
        settings.setdefault("watermark", {})["color"] = color
        await save_group_settings(chat_id, settings, user_id)
        wm = settings.get("watermark", {})
        try:
            await cb.message.edit_text(
                _wm_text(wm, f"Color set to {color} ✓"),
                reply_markup=_wm_keyboard(chat_id, wm),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_pos":
        wm = settings.get("watermark", {})
        try:
            await cb.message.edit_text(
                "📍 <b>Watermark Position</b>",
                reply_markup=_wm_pos_keyboard(chat_id, wm.get("position", "bot_right")),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_pos_set":
        pos = parts[3] if len(parts) > 3 else "bot_right"
        if pos not in WM_POSITIONS:
            pos = "bot_right"
        settings.setdefault("watermark", {})["position"] = pos
        await save_group_settings(chat_id, settings, user_id)
        wm = settings.get("watermark", {})
        try:
            await cb.message.edit_text(
                _wm_text(wm, "Position updated ✓"),
                reply_markup=_wm_keyboard(chat_id, wm),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_timing":
        wm = settings.get("watermark", {})
        try:
            await cb.message.edit_text(
                "⏱ <b>Watermark Timing</b>\n"
                "Choose when the watermark is visible:",
                reply_markup=_wm_timing_keyboard(chat_id, wm.get("timing_mode", "range")),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_timing_set":
        mode = parts[3] if len(parts) > 3 else "range"
        if mode not in WM_TIMING:
            mode = "range"
        settings.setdefault("watermark", {})["timing_mode"] = mode
        await save_group_settings(chat_id, settings, user_id)
        wm = settings.get("watermark", {})
        try:
            await cb.message.edit_text(
                _wm_text(wm, "Timing updated ✓"),
                reply_markup=_wm_keyboard(chat_id, wm),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "wm_fs":
        direction = parts[3] if len(parts) > 3 else "inc"
        wm = settings.setdefault("watermark", {})
        cur = int(wm.get("font_size", 24))
        wm["font_size"] = max(8, min(120, cur + (-2 if direction == "dec" else 2)))
        await save_group_settings(chat_id, settings, user_id)
        try:
            await cb.message.edit_reply_markup(_wm_keyboard(chat_id, wm))
        except Exception:
            pass

    elif action == "wm_reset":
        from src.core.group_settings import DEFAULT_WATERMARK
        settings["watermark"] = DEFAULT_WATERMARK.copy()
        await save_group_settings(chat_id, settings, user_id)
        wm = settings["watermark"]
        try:
            await cb.message.edit_text(
                _wm_text(wm, "Watermark reset to defaults ✓"),
                reply_markup=_wm_keyboard(chat_id, wm),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    await cb.answer()


# ── /meta command ──────────────────────────────────────────────────────────────

_META_FIELDS = {
    "title", "artist", "author", "comment", "audio_track", "video_track", "subtitle"
}


async def _cmd_meta(client: Client, message: Message, config):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_group_admin(client, chat_id, user_id, config):
        await message.reply_text(admin_only_msg())
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        fields_list = " · ".join(f"<code>{f}</code>" for f in sorted(_META_FIELDS))
        await message.reply_text(
            f"Usage: <code>/meta &lt;field&gt; &lt;value&gt;</code>\n\n"
            f"Fields: {fields_list}\n\n"
            "Example: <code>/meta title My Anime</code>",
            parse_mode=ParseMode.HTML
        )
        return
    field = parts[1].lower()
    value = parts[2].strip()
    if field not in _META_FIELDS:
        fields_list = ", ".join(f"<code>{f}</code>" for f in sorted(_META_FIELDS))
        await message.reply_text(
            f"❌ Unknown field. Valid: {fields_list}",
            parse_mode=ParseMode.HTML
        )
        return
    settings = await get_group_settings(chat_id)
    settings.setdefault("metadata", {})[field] = value
    await save_group_settings(chat_id, settings, user_id)
    await message.reply_text(
        f"✅ Metadata <b>{_he(field)}</b> set to <code>{_he(value)}</code>.",
        parse_mode=ParseMode.HTML
    )


async def _cmd_wm_text(client: Client, message: Message, config):
    """Set group watermark text via /wm_text <text>"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_group_admin(client, chat_id, user_id, config):
        await message.reply_text(admin_only_msg())
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text(
            "Usage: <code>/wm_text Your Watermark Text</code>",
            parse_mode=ParseMode.HTML
        )
        return
    text = parts[1].strip()[:200]
    settings = await get_group_settings(chat_id)
    settings.setdefault("watermark", {})["text"] = text
    await save_group_settings(chat_id, settings, user_id)
    await message.reply_text(
        f"✅ Watermark text set to: <code>{_he(text)}</code>",
        parse_mode=ParseMode.HTML
    )


# ── Handler registration ──────────────────────────────────────────────────────

def setup_group_settings_handlers(app: Client, config):

    # NOTE: /us and /settings are owned by settings.py (user encode settings).
    # Group settings uses /gs and /groupsettings to avoid competing with them.
    # When the /gs redesign lands this becomes the canonical entry point.
    @app.on_message(
        filters.command(["gs", "groupsettings"])
    )
    async def cmd_es(client, message: Message):
        await _cmd_es(client, message, config)

    @app.on_message(
        filters.command(["parallel"])
        & (filters.group | filters.private)
    )
    async def cmd_parallel(client, message: Message):
        await _cmd_parallel(client, message, config)

    @app.on_message(
        filters.command(["st", "setthumb"])
        & (filters.group | filters.private)
    )
    async def cmd_st(client, message: Message):
        await _cmd_st(client, message, config)

    @app.on_message(
        filters.command(["meta"])
        & (filters.group | filters.private)
    )
    async def cmd_meta(client, message: Message):
        await _cmd_meta(client, message, config)

    @app.on_message(
        filters.command(["wm_text", "wmtext"])
        & (filters.group | filters.private)
    )
    async def cmd_wm_text(client, message: Message):
        await _cmd_wm_text(client, message, config)

    @app.on_callback_query(filters.regex(r"^gs:"))
    async def cb_gs(client, callback_query: CallbackQuery):
        import logging as _lg
        _lg.getLogger(__name__).debug(
            "[Callback] gs: data=%s user_id=%s",
            callback_query.data,
            callback_query.from_user.id if callback_query.from_user else "?",
        )
        try:
            await _handle_gs_callback(client, callback_query, config)
        except Exception:
            _lg.getLogger(__name__).exception("[Callback] gs: unhandled error data=%s", callback_query.data)
            try:
                await callback_query.answer("⚠️ An error occurred.", show_alert=True)
            except Exception:
                pass
