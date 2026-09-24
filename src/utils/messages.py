"""
OCEAN theme — centralised message templates.
All user-facing strings live here; handlers import what they need.
"""

from __future__ import annotations

from html import escape as _he

_SEP  = "━━━━━━━━━━━━━━━━━━━━━"
_SEPs = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"


# ── Generic helpers ───────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 12) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _size(b: int) -> str:
    if b >= 1 << 30:
        return f"{b/(1<<30):.2f} GB"
    if b >= 1 << 20:
        return f"{b/(1<<20):.2f} MB"
    return f"{b/1024:.2f} KB"


def _elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    h, r = divmod(seconds, 3600)
    return f"{h}h {r//60}m {r%60}s"


# ── Start / Welcome ───────────────────────────────────────────────────────────

START_TEXT = (
    "🌊 <b>TOJI-Encode</b>  <i>— Video Encoding Bot</i>\n"
    "\n"
    "<i>Drop a video · select quality · receive your file.</i>\n"
    "\n"
    f"{_SEP}\n"
    "\n"
    "◆  Multi-resolution encoding  <i>(480p / 720p / 1080p)</i>\n"
    "◆  AV1 encoding via <code>/eav1</code>\n"
    "◆  Group settings via <code>/gs</code>  ·  Personal settings via <code>/us</code>\n"
    "◆  Live queue &amp; progress\n"
    "◆  Metadata · Thumbnail · Watermark\n"
    "\n"
    f"{_SEP}\n"
    "\n"
    "<code>/gs</code> — group config  ·  <code>/us</code> — your settings  ·  <code>/help</code> — all commands"
)


# ── Queue ─────────────────────────────────────────────────────────────────────

def queued_msg(task_id: str, position: int, filename: str) -> str:
    pos_str = f"Position <b>#{position}</b> in queue" if position > 1 else "Up <b>next</b>"
    return (
        f"🌊 <b>Queued</b>\n"
        f"{_SEP}\n"
        f"📄 <code>{_he(filename)}</code>\n"
        f"🆔 <code>{_he(task_id)}</code>\n"
        f"📍 {pos_str}\n"
        f"{_SEPs}\n"
        f"Cancel: <code>/cancel {_he(task_id)}</code>"
    )


# ── Download ──────────────────────────────────────────────────────────────────

def downloading_msg(filename: str, pct: float, speed: str = "", eta: str = "") -> str:
    extra = f"\n⚡ {_he(speed)}  ·  ⏱ {_he(eta)}" if speed else ""
    return (
        f"🌊 <b>Downloading</b>\n"
        f"{_SEP}\n"
        f"📥 <code>{_he(filename)}</code>\n"
        f"[{_bar(pct)}] {pct:.1f}%{extra}"
    )


# ── Encoding ──────────────────────────────────────────────────────────────────

def encoding_msg(filename: str, resolution: str, pct: float,
                 job_n: int = 1, job_total: int = 1, codec: str = "") -> str:
    codec_tag = f"  <code>{_he(codec)}</code>" if codec else ""
    job_tag   = f"  Job {job_n}/{job_total}" if job_total > 1 else ""
    return (
        f"🎬 <b>Encoding</b>{job_tag}\n"
        f"{_SEP}\n"
        f"📄 <code>{_he(filename)}</code>\n"
        f"🔹 <b>{_he(resolution)}</b>{codec_tag}\n"
        f"[{_bar(pct)}] {pct:.1f}%"
    )


def encoding_av1_msg(filename: str, resolution: str, pct: float,
                     job_n: int = 1, job_total: int = 1) -> str:
    return encoding_msg(filename, resolution, pct, job_n, job_total, codec="AV1")


# ── Upload ────────────────────────────────────────────────────────────────────

def uploading_msg(filename: str, pct: float, speed: str = "") -> str:
    extra = f"\n⚡ {_he(speed)}" if speed else ""
    return (
        f"🌊 <b>Uploading</b>\n"
        f"{_SEP}\n"
        f"📤 <code>{_he(filename)}</code>\n"
        f"[{_bar(pct)}] {pct:.1f}%{extra}"
    )


# ── Success ───────────────────────────────────────────────────────────────────

def success_msg(filename: str, size_bytes: int, elapsed_s: int,
                resolution: str = "", codec: str = "") -> str:
    tags = []
    if resolution:
        tags.append(f"<b>{_he(resolution)}</b>")
    if codec:
        tags.append(f"<code>{_he(codec)}</code>")
    tag_str = "  ".join(tags)
    return (
        f"✅ <b>Done</b>  {tag_str}\n"
        f"{_SEP}\n"
        f"📄 <code>{_he(filename)}</code>\n"
        f"📦 {_size(size_bytes)}  ·  ⏱ {_elapsed(elapsed_s)}"
    )


def group_success_msg(filename: str, elapsed_s: int) -> str:
    return (
        f"✅ <b>Encoded &amp; delivered</b>\n"
        f"{_SEP}\n"
        f"<code>{_he(filename)}</code>\n"
        f"⏱ {_elapsed(elapsed_s)}  ·  Sent to PM"
    )


# ── Failure ───────────────────────────────────────────────────────────────────

def failure_msg(task_id: str, reason: str = "") -> str:
    detail = f"\n<i>{_he(reason[:200])}</i>" if reason else ""
    return (
        f"❌ <b>Failed</b>\n"
        f"{_SEP}\n"
        f"🆔 <code>{_he(task_id)}</code>{detail}\n"
        f"{_SEPs}\n"
        f"Check your settings with <code>/gs</code> and try again."
    )


# ── Cancellation ──────────────────────────────────────────────────────────────

def cancelled_msg(task_id: str) -> str:
    return (
        f"⚠️ <b>Cancelled</b>\n"
        f"{_SEP}\n"
        f"Task <code>{task_id}</code> has been stopped and cleaned up."
    )


# ── AV1 ───────────────────────────────────────────────────────────────────────

def av1_no_encoder_msg() -> str:
    return (
        "❌ <b>No AV1 encoder found</b>\n"
        f"{_SEP}\n"
        "FFmpeg on this server does not have <code>libsvtav1</code>, "
        "<code>libaom-av1</code> or <code>rav1e</code>.\n"
        "Ask your admin to rebuild FFmpeg with AV1 support."
    )


def av1_encoder_found_msg(encoder: str) -> str:
    return f"🎬 AV1 encoder: <code>{encoder}</code>"


# ── /gs + /us display ──────────────────────────────────────────────────────────────

def _res_toggle(res: str, enabled: bool) -> str:
    mark = "✓" if enabled else "✗"
    return f"  <code>{res}</code>  {'<u>On</u>' if enabled else '<i>Off</i>'}  {mark}"


def _profile_line(res: str, profiles_for_res: dict) -> str:
    """profiles_for_res = {h264: {crf, preset, audio_bitrate}, h265: {...}}"""
    lines = [f"  <b>{res}</b>"]
    for ck, label in (("h264", "H.264"), ("h265", "H.265")):
        p = profiles_for_res.get(ck) or {}
        lines.append(
            f"    {label}  "
            f"CRF <code>{p.get('crf', '?')}</code>  "
            f"<code>{p.get('preset', '?')}</code>  "
            f"<code>{p.get('audio_bitrate', '?')}</code>"
        )
    return "\n".join(lines)


def build_es_text(chat_title: str, settings: dict) -> str:
    chat_title = _he(chat_title)
    resolutions = settings.get("resolutions", ["1080p"])
    profiles    = settings.get("profiles", {})
    audio       = settings.get("audio", {})
    subs        = settings.get("subtitles", {})
    meta        = settings.get("metadata", {})
    av1         = settings.get("av1", {})
    wm          = settings.get("watermark", {})
    parallel    = settings.get("parallel_limit", 1)
    thumb_set   = bool(settings.get("thumbnail_b64", ""))

    all_res = ["480p", "720p", "1080p"]

    res_lines = "\n".join(_res_toggle(r, r in resolutions) for r in all_res)
    profile_lines = "\n".join(
        _profile_line(r, profiles.get(r, {})) for r in all_res if r in resolutions
    )

    wm_state = "On ✓" if wm.get("enabled") else "Off"
    thumb_state = "Set ✓" if thumb_set else "Not set"
    audio_mode = audio.get("mode", "auto")
    if audio_mode == "auto":
        audio_summary = "Auto (copy if compatible, else encode)"
    elif audio_mode == "copy":
        audio_summary = "Copy (no re-encode)"
    else:
        audio_summary = (
            f"Encode  <code>{audio.get('codec','aac')}</code>  "
            f"<code>{audio.get('bitrate','128k')}</code>  "
            f"{audio.get('channels',2)}ch  "
            f"<code>{audio.get('sample_rate',48000)} Hz</code>"
        )
    sub_mode = subs.get("mode", "copy")

    av1_threads = av1.get("threads", "auto")

    return (
        f"⚙️ <b>Group Settings</b>  —  <i>{chat_title}</i>\n"
        f"{_SEP}\n"
        "\n"
        f"<b>🎞 VIDEO</b>\n"
        f"{profile_lines if profile_lines else '  <i>(no resolutions enabled)</i>'}\n"
        "\n"
        f"<b>🔊 AUDIO</b>\n"
        f"  {audio_summary}\n"
        "\n"
        f"<b>💬 SUBTITLES</b>\n"
        f"  Mode: <code>{sub_mode}</code>\n"
        "\n"
        f"<b>🏷 METADATA</b>\n"
        f"┃  🎬 Title  ·  <code>{_he(meta.get('title') or '—')}</code>\n"
        f"┃  🎨 Artist  ·  <code>{_he(meta.get('artist') or '—')}</code>\n"
        f"┃  ✍️ Author  ·  <code>{_he(meta.get('author') or '—')}</code>\n"
        f"┃  💬 Comment  ·  <code>{_he(meta.get('comment') or '—')}</code>\n"
        f"┃  🔊 Audio Track  ·  <code>{_he(meta.get('audio_track') or '—')}</code>\n"
        f"┃  🎥 Video Track  ·  <code>{_he(meta.get('video_track') or '—')}</code>\n"
        f"┃  📝 Subtitle  ·  <code>{_he(meta.get('subtitle') or '—')}</code>\n"
        "\n"
        f"<b>♾ AV1</b>\n"
        f"  Encoder: <code>{av1.get('codec','libsvtav1')}</code>\n"
        f"  CRF:     <code>{av1.get('crf',29)}</code>  "
        f"Preset: <code>{av1.get('preset',8)}</code>\n"
        f"  Pixel:   <code>{av1.get('pixel_format','yuv420p10le')}</code>  "
        f"Threads: <code>{av1_threads}</code>\n"
        "\n"
        f"<b>📐 RESOLUTIONS</b>\n"
        f"{res_lines}\n"
        "\n"
        f"<b>🖼 THUMBNAIL</b>  {thumb_state}\n"
        "\n"
        f"<b>🌀 WATERMARK</b>  {wm_state}\n"
        "\n"
        f"<b>⚡ PARALLEL LIMIT</b>  <code>{parallel}</code>\n"
        f"{_SEP}"
    )


# ── Quality-selection keyboard text ──────────────────────────────────────────

def quality_select_text(filename: str, mode: str = "h264") -> str:
    mode_tag = "AV1" if mode == "av1" else "H.264/H.265"
    return (
        f"🎬 <b>Select Quality</b>  <i>({mode_tag})</i>\n"
        f"{_SEP}\n"
        f"📄 <code>{_he(filename)}</code>\n"
        f"{_SEPs}\n"
        "Choose a resolution or encode all enabled qualities."
    )


# ── /parallel ────────────────────────────────────────────────────────────────

def parallel_set_msg(limit: int) -> str:
    return (
        f"⚡ <b>Parallel limit set to {limit}</b>\n"
        f"{_SEP}\n"
        "This group's queue will now run up to "
        f"<b>{limit}</b> encode(s) simultaneously."
    )


def parallel_denied_msg(requested: int, max_allowed: int) -> str:
    return (
        f"❌ <b>Parallel limit too high</b>\n"
        f"{_SEP}\n"
        f"Requested: <b>{requested}</b>  ·  "
        f"Machine maximum: <b>{max_allowed}</b>\n"
        "Lower the value or upgrade the server."
    )


# ── Errors ────────────────────────────────────────────────────────────────────

def access_denied_msg() -> str:
    return "⛔ You don't have permission to do that."


def admin_only_msg() -> str:
    return "🔒 Only group admins can change these settings."


def no_reply_msg(cmd: str) -> str:
    return f"↩️ Reply to a video file with <code>{cmd}</code>."


def resolution_disabled_msg(res: str) -> str:
    return (
        f"❌ <b>{res} is disabled</b> for this group.\n"
        f"An admin can enable it via <code>/gs</code>."
    )


def disk_space_msg(free_mb: float, needed_mb: int) -> str:
    return (
        f"💾 <b>Insufficient disk space</b>\n"
        f"{_SEP}\n"
        f"Free: <b>{free_mb:.0f} MB</b>  ·  Needed: <b>{needed_mb} MB</b>"
    )

# ── Codec display labels ──────────────────────────────────────────────────────
CODEC_LABELS = {
    "h264": "H.264",
    "h265": "H.265",
    "av1":  "AV1",
}


# ── Dump channel completion message ──────────────────────────────────────────

def dump_info_msg(
    original_name: str,
    output_name: str,
    user_mention: str,
    user_id: int,
    original_size_bytes: int,
    encoded_size_bytes: int,
    status: str = "✅ Done",
) -> str:
    """Formatted FILE INFO block sent to the dump channel after every job."""

    def _sz(b: int) -> str:
        if b <= 0:
            return "—"
        if b >= 1 << 30:
            return f"{b / (1 << 30):.2f} GB"
        if b >= 1 << 20:
            return f"{b / (1 << 20):.2f} MB"
        return f"{b / (1 << 10):.1f} KB"

    ratio_str = ""
    if original_size_bytes > 0 and encoded_size_bytes > 0:
        ratio = encoded_size_bytes / original_size_bytes * 100
        ratio_str = f"  <i>({ratio:.1f}%)</i>"

    return (
        "╭━━━〔 📂 <b>FILE INFO</b> 〕━━━╮\n"
        f"┃  <b>Original  :</b>  <code>{original_name}</code>\n"
        f"┃  <b>Renamed   :</b>  <code>{output_name}</code>\n"
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"┃  <b>Orig Size :</b>  {_sz(original_size_bytes)}\n"
        f"┃  <b>Enc  Size :</b>  {_sz(encoded_size_bytes)}{ratio_str}\n"
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"┃  <b>Requested :</b>  {user_mention}  <code>[{user_id}]</code>\n"
        f"┃  <b>Status    :</b>  {status}\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )
