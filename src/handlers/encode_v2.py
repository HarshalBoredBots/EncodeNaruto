"""
encode_v2.py — encode handler with two-step menu.

/e flow:
  Step 1: codec menu  → [H.264] [H.265] [AV1]
  Step 2: resolution  → [480p] [720p] [1080p] [Encode All]

/480p /720p /1080p flow:
  Step 1: codec menu  → [H.264] [H.265]   (no AV1 for these — use /480pav1)
  Step 2: encode immediately

/eav1               → skip to resolution menu (AV1 fixed)
/480pav1 /720pav1 /1080pav1 → encode immediately with AV1

All callbacks carry a HMAC-style token binding them to the original file/user.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

from html import escape as _he

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.core.group_settings import (
    get_group_settings,
    get_group_thumbnail_path,
    get_codec_profile,
)
from src.utils.av1 import (
    detect_av1_encoder,
    get_cached_av1_encoder,
    build_av1_video_args,
)
from src.utils.messages import (
    queued_msg,
    av1_no_encoder_msg,
    resolution_disabled_msg,
    no_reply_msg,
    CODEC_LABELS,
    _SEP,
)
from src.utils.resources import check_disk_space, get_min_free_disk_mb

logger = logging.getLogger(__name__)

ALL_RESOLUTIONS = ["480p", "720p", "1080p"]



# ── Media helpers ─────────────────────────────────────────────────────────────

def _get_media(message: Message):
    return (
        message.video
        or message.document
        or message.audio
        or message.animation
    )


def _video_filename(message: Message) -> str:
    """Return an HTML-safe filename for use inside <code> tags."""
    media = _get_media(message)
    if not media:
        return "video"
    raw = getattr(media, "file_name", None) or f"file_{media.file_id[:8]}"
    return _he(raw)


def _token(file_id: str, chat_id: int, user_id: int) -> str:
    """Short token binding a callback to a specific file + user + chat."""
    return hashlib.sha256(f"{file_id}:{chat_id}:{user_id}".encode()).hexdigest()[:8]


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _codec_keyboard(
    file_id: str,
    chat_id: int,
    user_id: int,
    include_av1: bool = True,
    fixed_res: str | None = None,      # set for /480p /720p /1080p
) -> InlineKeyboardMarkup:
    """
    Step-1 keyboard: pick codec.
    Callback: ec:{codec}:{token}:{chat_id}:{user_id}[:{fixed_res}]
    """
    tok = _token(file_id, chat_id, user_id)
    def _cb(codec: str) -> str:
        suffix = f":{fixed_res}" if fixed_res else ""
        return f"ec:{codec}:{tok}:{chat_id}:{user_id}{suffix}"

    rows = [
        [
            InlineKeyboardButton("H.264  (libx264)", callback_data=_cb("h264")),
            InlineKeyboardButton("H.265  (libx265)", callback_data=_cb("h265")),
        ]
    ]
    if include_av1:
        rows.append([InlineKeyboardButton("AV1  (libsvtav1)", callback_data=_cb("av1"))])
    rows.append([InlineKeyboardButton("✗ Cancel", callback_data="ec_cancel")])
    return InlineKeyboardMarkup(rows)


def _resolution_keyboard(
    enabled: list[str],
    file_id: str,
    chat_id: int,
    user_id: int,
    codec: str,
) -> InlineKeyboardMarkup:
    """
    Step-2 keyboard: pick resolution.
    Callback: er:{codec}:{token}:{chat_id}:{user_id}:{res_or_all}
    """
    tok = _token(file_id, chat_id, user_id)
    prefix = f"er:{codec}:{tok}:{chat_id}:{user_id}:"

    row = []
    rows = []
    for res in ALL_RESOLUTIONS:
        if res in enabled:
            row.append(InlineKeyboardButton(res, callback_data=f"{prefix}{res}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("🌊 Encode All", callback_data=f"{prefix}all")])
    rows.append([InlineKeyboardButton("✗ Cancel", callback_data="ec_cancel")])
    return InlineKeyboardMarkup(rows)


# ── Queue a job ───────────────────────────────────────────────────────────────

async def _queue_encode(
    client: Client,
    file_msg: Message,
    trigger_msg: Message,
    resolutions: list[str],
    codec: str,           # "h264" | "h265" | "av1"
    task_queue,
    config,
    requester_id: int | None = None,  # explicit caller; use cb.from_user.id from callbacks
):
    chat_id = (trigger_msg.chat or file_msg.chat).id

    # ── Determine the requesting user ─────────────────────────────────────────
    # IMPORTANT: trigger_msg is sometimes the bot's own keyboard message
    # (when called from a CallbackQuery handler).  Using trigger_msg.from_user
    # in that case returns the bot itself, which then becomes the upload
    # destination → USER_IS_BOT crash.
    #
    # Resolution order:
    #   1. requester_id (passed explicitly by callback handlers via cb.from_user.id)
    #   2. file_msg.from_user.id  (the person who sent/forwarded the file — always a human)
    #   3. trigger_msg.from_user.id only as last resort, and ONLY if it is not a bot
    if requester_id is not None:
        user_id = requester_id
    elif file_msg.from_user and not getattr(file_msg.from_user, "is_bot", False):
        user_id = file_msg.from_user.id
    elif trigger_msg.from_user and not getattr(trigger_msg.from_user, "is_bot", False):
        user_id = trigger_msg.from_user.id
    else:
        logger.error(
            "[_queue_encode] Cannot determine a human requester: "
            "trigger_from=%s file_from=%s requester_id=%s",
            getattr(trigger_msg.from_user, "id", None),
            getattr(file_msg.from_user, "id", None),
            requester_id,
        )
        await trigger_msg.reply_text("❌ Could not identify the requesting user.")
        return

    logger.info(
        "[_queue_encode] job_create user_id=%s chat_id=%s codec=%s resolutions=%s",
        user_id, chat_id, codec, resolutions,
    )

    settings = await get_group_settings(chat_id)

    media     = _get_media(file_msg)
    file_id   = media.file_id
    file_size = getattr(media, "file_size", 0) or 0
    filename  = _video_filename(file_msg)
    base_name = os.path.splitext(filename)[0]
    ext       = os.path.splitext(filename)[1].lower() or ".mkv"

    # Disk pre-check
    min_mb  = get_min_free_disk_mb()
    need_mb = max(min_mb, int(file_size * 2.5 / (1 << 20)))
    ok, msg_ = check_disk_space(config.paths.tmp, need_mb)
    if not ok:
        await trigger_msg.reply_text(f"💾 Not enough disk space.\n{msg_}")
        return

    thumb = get_group_thumbnail_path(chat_id, settings) or ""
    meta  = settings.get("metadata", {})

    jobs = []
    for res in resolutions:
        if res not in settings.get("resolutions", ["1080p"]):
            continue

        out_name = f"{base_name} [{res}]{ext}"

        if codec == "av1":
            from src.utils.av1 import normalize_av1_settings
            av1_cfg  = normalize_av1_settings(settings.get("av1") or {})
            av1_args = build_av1_video_args(av1_cfg)
            job = {
                "resolution":      res,
                "output_filename": out_name,
                "processing_mode": "av1",
                "av1_args":        av1_args,
                "av1_settings":    av1_cfg,
                "codec":           av1_cfg.get("codec", "libsvtav1"),
                "audio_bitrate":   settings.get("audio", {}).get("bitrate", "128k"),
                "audio_mode":      settings.get("audio", {}).get("mode", "auto"),
                "audio_codec":     settings.get("audio", {}).get("codec", "aac"),
                "audio_sample_rate": settings.get("audio", {}).get("sample_rate", 48000),
                "sub_mode":        settings.get("subtitles", {}).get("mode", "copy"),
            }
        else:
            prof    = get_codec_profile(settings, res, codec)   # codec = "h264"|"h265"
            ffcodec = "libx264" if codec == "h264" else "libx265"
            job = {
                "resolution":      res,
                "output_filename": out_name,
                "processing_mode": "encode",
                "crf":             prof.get("crf", 23),
                "preset":          prof.get("preset", "medium"),
                "codec":           ffcodec,
                "audio_bitrate":   prof.get("audio_bitrate", "128k"),
                "audio_mode":      settings.get("audio", {}).get("mode", "auto"),
                "audio_codec":     settings.get("audio", {}).get("codec", "aac"),
                "audio_sample_rate": settings.get("audio", {}).get("sample_rate", 48000),
                "sub_mode":        settings.get("subtitles", {}).get("mode", "copy"),
            }

        job["metadata"]       = meta
        job["send_type"]      = settings.get("send_type", "media")
        job["thumbnail_path"] = thumb
        job["source_chat_id"] = chat_id
        jobs.append(job)

    if not jobs:
        await trigger_msg.reply_text("❌ No enabled resolutions to encode.")
        return

    task_data = {
        "file_id":            file_id,
        "file_size":          file_size,
        "user_id":            user_id,
        "destination_chat_id": user_id,   # explicit: always the human requester's DM
        "chat_id":            chat_id,
        "source_chat_id":     chat_id,
        "message_id":         file_msg.id,
        "output_filename":    jobs[0]["output_filename"],
        "original_filename":  filename,
        "jobs":               jobs,
        "total_jobs":         len(jobs),
        "current_job":        0,
        "current_stage":      "queued",
        "watermark":          settings.get("watermark", {}),
        "metadata":           meta,
        "send_type":          settings.get("send_type", "media"),
        "thumbnail_path":     thumb,
        "encode_mode":        codec,
        "username":           (trigger_msg.from_user.username or "") if trigger_msg.from_user else "",
        "first_name":         (trigger_msg.from_user.first_name or "User") if trigger_msg.from_user else "User",
    }

    task_id  = task_queue.create_task(task_data)
    position = task_queue.get_queue_position(task_id)

    await trigger_msg.reply_text(
        queued_msg(task_id, position, jobs[0]["output_filename"]),
        parse_mode=ParseMode.HTML,
    )


# ── /e and /eav1 entry points ─────────────────────────────────────────────────

async def _cmd_e(client: Client, message: Message, task_queue, config):
    """Show Step-1 codec menu (H.264 / H.265 / AV1)."""
    replied = message.reply_to_message
    if not replied or not _get_media(replied):
        await message.reply_text(no_reply_msg("/e"))
        return

    media    = _get_media(replied)
    chat_id  = message.chat.id
    user_id  = message.from_user.id
    filename = _video_filename(replied)

    kb = _codec_keyboard(media.file_id, chat_id, user_id, include_av1=True)
    await message.reply_to_message.reply_text(
        f"🎬 <b>Select Codec</b>\n"
        f"{_SEP}\n"
        f"📄 <code>{filename}</code>\n"
        f"{_SEP}\n"
        "Choose the codec, then select resolution.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def _cmd_eav1(client: Client, message: Message, task_queue, config):
    """Show Step-2 resolution menu locked to AV1."""
    replied = message.reply_to_message
    if not replied or not _get_media(replied):
        await message.reply_text(no_reply_msg("/eav1"))
        return

    # AV1 availability check
    enc = get_cached_av1_encoder() or await detect_av1_encoder(config.paths.ffmpeg)
    if not enc:
        await message.reply_text(av1_no_encoder_msg(), parse_mode=ParseMode.HTML)
        return

    media    = _get_media(replied)
    chat_id  = message.chat.id
    user_id  = message.from_user.id
    settings = await get_group_settings(chat_id)
    enabled  = settings.get("resolutions", ["1080p"])
    filename = _video_filename(replied)

    kb = _resolution_keyboard(enabled, media.file_id, chat_id, user_id, "av1")
    await message.reply_to_message.reply_text(
        f"♾ <b>AV1 — Select Resolution</b>\n"
        f"{_SEP}\n"
        f"📄 <code>{filename}</code>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def _cmd_direct_res(
    client: Client, message: Message, resolution: str,
    task_queue, config,
):
    """
    /480p /720p /1080p — show codec menu (H.264 / H.265 only).
    Resolution is fixed; codec is chosen via button.
    """
    replied = message.reply_to_message
    if not replied or not _get_media(replied):
        await message.reply_text(no_reply_msg(f"/{resolution}"))
        return

    chat_id  = message.chat.id
    settings = await get_group_settings(chat_id)
    enabled  = settings.get("resolutions", ["1080p"])

    if resolution not in enabled:
        await message.reply_text(resolution_disabled_msg(resolution), parse_mode=ParseMode.HTML)
        return

    media    = _get_media(replied)
    user_id  = message.from_user.id
    filename = _video_filename(replied)

    # fixed_res baked into callback so Step-2 is skipped
    kb = _codec_keyboard(
        media.file_id, chat_id, user_id,
        include_av1=False,       # /480p etc. → use /480pav1 for AV1
        fixed_res=resolution,
    )
    await message.reply_to_message.reply_text(
        f"🎬 <b>Select Codec  [{resolution}]</b>\n"
        f"{_SEP}\n"
        f"📄 <code>{filename}</code>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def _cmd_direct_av1(
    client: Client, message: Message, resolution: str,
    task_queue, config,
):
    """/480pav1 /720pav1 /1080pav1 — encode immediately with AV1."""
    replied = message.reply_to_message
    if not replied or not _get_media(replied):
        await message.reply_text(no_reply_msg(f"/{resolution}av1"))
        return

    chat_id  = message.chat.id
    settings = await get_group_settings(chat_id)
    enabled  = settings.get("resolutions", ["1080p"])

    if resolution not in enabled:
        await message.reply_text(resolution_disabled_msg(resolution), parse_mode=ParseMode.HTML)
        return

    enc = get_cached_av1_encoder() or await detect_av1_encoder(config.paths.ffmpeg)
    if not enc:
        await message.reply_text(av1_no_encoder_msg(), parse_mode=ParseMode.HTML)
        return

    await _queue_encode(
        client, replied, message, [resolution], "av1", task_queue, config
    )


# ── Callback: Step-1 codec chosen ─────────────────────────────────────────────

async def _cb_codec(client: Client, cb: CallbackQuery, task_queue, config):
    """
    ec:{codec}:{token}:{chat_id}:{user_id}[:{fixed_res}]
    If fixed_res present → queue immediately.
    Else → show resolution keyboard (Step 2).
    """
    parts     = cb.data.split(":")
    codec     = parts[1]
    tok       = parts[2]
    chat_id   = int(parts[3])
    user_id   = int(parts[4])
    fixed_res = parts[5] if len(parts) > 5 else None

    # Only the triggering user may press this
    if cb.from_user.id != user_id:
        await cb.answer("⚠️ Not your menu.", show_alert=True)
        return

    file_msg = cb.message.reply_to_message
    if not file_msg or not _get_media(file_msg):
        await cb.answer("Original file not found.", show_alert=True)
        return

    media = _get_media(file_msg)

    # Verify token
    if _token(media.file_id, chat_id, user_id) != tok:
        await cb.answer("⚠️ Invalid session.", show_alert=True)
        return

    # AV1 check
    if codec == "av1":
        enc = get_cached_av1_encoder() or await detect_av1_encoder(config.paths.ffmpeg)
        if not enc:
            await cb.answer("No AV1 encoder available.", show_alert=True)
            await cb.message.edit_text(av1_no_encoder_msg(), parse_mode=ParseMode.HTML)
            return

    if fixed_res:
        # Direct resolution command — encode immediately, no Step-2
        try:
            await cb.message.delete()
        except Exception:
            pass
        await _queue_encode(
            client, file_msg, cb.message, [fixed_res], codec, task_queue, config,
            requester_id=cb.from_user.id,
        )
        await cb.answer()
        return

    # /e path — show Step-2 resolution keyboard
    settings = await get_group_settings(chat_id)
    enabled  = settings.get("resolutions", ["1080p"])
    filename = _video_filename(file_msg)
    codec_label = CODEC_LABELS.get(codec, codec.upper())

    kb = _resolution_keyboard(enabled, media.file_id, chat_id, user_id, codec)
    try:
        await cb.message.edit_text(
            f"🎬 <b>{codec_label} — Select Resolution</b>\n"
            f"{_SEP}\n"
            f"📄 <code>{filename}</code>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except MessageNotModified:
        pass
    await cb.answer()


# ── Callback: Step-2 resolution chosen ───────────────────────────────────────

async def _cb_resolution(client: Client, cb: CallbackQuery, task_queue, config):
    """
    er:{codec}:{token}:{chat_id}:{user_id}:{res_or_all}
    """
    parts   = cb.data.split(":")
    codec   = parts[1]
    tok     = parts[2]
    chat_id = int(parts[3])
    user_id = int(parts[4])
    quality = parts[5]

    if cb.from_user.id != user_id:
        await cb.answer("⚠️ Not your menu.", show_alert=True)
        return

    file_msg = cb.message.reply_to_message
    if not file_msg or not _get_media(file_msg):
        await cb.answer("Original file not found.", show_alert=True)
        return

    media = _get_media(file_msg)
    if _token(media.file_id, chat_id, user_id) != tok:
        await cb.answer("⚠️ Invalid session.", show_alert=True)
        return

    settings = await get_group_settings(chat_id)
    enabled  = settings.get("resolutions", ["1080p"])

    if quality == "all":
        resolutions = [r for r in ALL_RESOLUTIONS if r in enabled]
    else:
        if quality not in enabled:
            await cb.answer(f"{quality} is disabled.", show_alert=True)
            return
        resolutions = [quality]

    try:
        await cb.message.delete()
    except Exception:
        pass

    await _queue_encode(
        client, file_msg, cb.message, resolutions, codec, task_queue, config,
        requester_id=cb.from_user.id,
    )
    await cb.answer()


# ── Handler registration ──────────────────────────────────────────────────────

def setup_encode_v2_handlers(app: Client, task_queue, config):

    @app.on_message(filters.command(["e", "encode"]) & (filters.group | filters.private))
    async def cmd_e(client, message: Message):
        logger.info(
            "[Command] /e received user_id=%s chat_id=%s",
            message.from_user.id if message.from_user else "?",
            message.chat.id,
        )
        await _cmd_e(client, message, task_queue, config)

    @app.on_message(filters.command(["eav1"]) & (filters.group | filters.private))
    async def cmd_eav1(client, message: Message):
        logger.info(
            "[Command] /eav1 received user_id=%s chat_id=%s",
            message.from_user.id if message.from_user else "?",
            message.chat.id,
        )
        await _cmd_eav1(client, message, task_queue, config)

    # Direct resolution → codec menu (H.264 / H.265)
    for _res in ("480p", "720p", "1080p"):
        _r = _res
        @app.on_message(filters.command([_r]) & (filters.group | filters.private))
        async def cmd_direct_res(client, message: Message, _r=_r):
            await _cmd_direct_res(client, message, _r, task_queue, config)

    # Direct AV1 encode
    for _res in ("480p", "720p", "1080p"):
        _cmd = f"{_res}av1"
        _r   = _res
        @app.on_message(filters.command([_cmd]) & (filters.group | filters.private))
        async def cmd_direct_av1(client, message: Message, _r=_r):
            await _cmd_direct_av1(client, message, _r, task_queue, config)

    # Step-1: codec chosen
    @app.on_callback_query(filters.regex(r"^ec:"))
    async def cb_codec(client, cb: CallbackQuery):
        try:
            await _cb_codec(client, cb, task_queue, config)
        except Exception:
            logger.exception("[Callback] ec: unhandled error data=%s", cb.data)
            try:
                await cb.answer("⚠️ An error occurred.", show_alert=True)
            except Exception:
                pass

    # Step-2: resolution chosen
    @app.on_callback_query(filters.regex(r"^er:"))
    async def cb_resolution(client, cb: CallbackQuery):
        try:
            await _cb_resolution(client, cb, task_queue, config)
        except Exception:
            logger.exception("[Callback] er: unhandled error data=%s", cb.data)
            try:
                await cb.answer("⚠️ An error occurred.", show_alert=True)
            except Exception:
                pass

    # Cancel
    @app.on_callback_query(filters.regex(r"^ec_cancel$"))
    async def cb_cancel(client, cb: CallbackQuery):
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.answer("Cancelled.")
