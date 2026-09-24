import asyncio
import sys
import os
import logging

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

_original_get_event_loop = asyncio.get_event_loop

def _patched_get_event_loop():
    try:
        return _original_get_event_loop()
    except RuntimeError:
        return _loop

asyncio.get_event_loop = _patched_get_event_loop

from pyrogram import Client, idle, filters
from pyrogram.types import BotCommand, Message

import motor.motor_asyncio
from src import Config, TaskQueue, UserSettings, FFmpeg
from src.core.user_setting import init_db
from src.utils.resources import (
    detect_ffmpeg_threads,
    resource_summary,
    get_temp_dir,
)
from src.services import Worker
# NOTE: encode.py is intentionally NOT imported — encode_v2 owns /e and /encode
from src.handlers.settings import setup_settings_handlers
from src.handlers.status import setup_status_handlers
from src.handlers.shift import setup_shift_handlers
from src.handlers.start import setup_start_handler
from src.handlers.cancel import setup_cancel_handlers, set_worker_instance, set_admin_ids
from src.handlers.mi import setup_mediainfo_handlers
from src.handlers.ocean import setup_ocean_handlers
from src.handlers.encode_v2 import setup_encode_v2_handlers
from src.handlers.group_settings_handler import setup_group_settings_handlers
from src.core.group_settings import init_group_db, ensure_index as gs_ensure_index
from src.utils.av1 import detect_av1_encoder
from src.handlers.rename import setup_rename_handler
from src.handlers.set import setup_set_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)


async def main():
    config = Config()
    config.paths.makedirs()

    # ── MongoDB ───────────────────────────────────────────────────────────────
    _mongo_uri = config.mongo_uri
    _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
        _mongo_uri,
        serverSelectionTimeoutMS=10_000,
    )
    _mongo_db = _mongo_client[config.mongo_db_name]
    init_db(_mongo_db)
    init_group_db(_mongo_db)

    await _mongo_db["user_settings"].create_index("user_id", unique=True)
    await gs_ensure_index(_mongo_db)
    logger.info("[Boot] MongoDB connected: %s / %s", _mongo_uri.split("@")[-1], config.mongo_db_name)

    import os as _os
    _workers = int(_os.getenv('PYROGRAM_WORKERS', '8'))
    _max_tx  = int(_os.getenv('PYROGRAM_MAX_TX',  '4'))
    app = Client(
        "encode_bot_session",
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        workdir=config.paths.logs,
        workers=_workers,
        max_concurrent_transmissions=_max_tx,
    )
    logger.info("[Boot] Pyrogram client created (workers=%d)", _workers)

    task_queue = TaskQueue()
    _ffmpeg_threads = detect_ffmpeg_threads()
    logger.info("[Boot] Resource profile: %s", resource_summary())
    logger.info("[Boot] FFmpeg threads: %d", _ffmpeg_threads)
    ffmpeg = FFmpeg(
        ffmpeg_path=config.paths.ffmpeg,
        ffprobe_path=config.paths.ffprobe,
        threads=_ffmpeg_threads,
    )

    _user_settings_cache: dict[int, UserSettings] = {}
    _SETTINGS_CACHE_MAX = 500

    def get_user_settings(user_id: int) -> UserSettings:
        if user_id not in _user_settings_cache:
            if len(_user_settings_cache) >= _SETTINGS_CACHE_MAX:
                _user_settings_cache.pop(next(iter(_user_settings_cache)))
            _user_settings_cache[user_id] = UserSettings(user_id, config.paths)
        return _user_settings_cache[user_id]

    # ── Register handlers BEFORE start() ─────────────────────────────────────
    # In Pyrogram 2.x this is the safe order:
    #   Create Client → Register handlers → Start Client → idle
    logger.info("[Handlers] Loading handlers...")
    _total = 0

    def _setup(name, fn, *args, **kwargs):
        nonlocal _total
        try:
            before = sum(
                len(g) for g in getattr(app, "dispatcher", None) and
                getattr(app.dispatcher, "groups", {}).values() or []
            ) if hasattr(app, "dispatcher") and app.dispatcher else 0
            fn(*args, **kwargs)
            after = sum(
                len(g) for g in getattr(app, "dispatcher", None) and
                getattr(app.dispatcher, "groups", {}).values() or []
            ) if hasattr(app, "dispatcher") and app.dispatcher else 0
            added = after - before
            _total += max(added, 1)  # count at least 1 per setup call
            logger.info("[Handlers] %s loaded", name)
        except Exception:
            logger.exception("[Handlers] %s FAILED", name)

    _setup("ocean",          setup_ocean_handlers,
           app=app, user_settings=get_user_settings, config=config)
    _setup("group_settings", setup_group_settings_handlers,
           app=app, config=config)
    _setup("encode_v2",      setup_encode_v2_handlers,
           app=app, task_queue=task_queue, config=config)
    _setup("set",            setup_set_handlers,
           app=app, user_settings=get_user_settings, config=config)
    # NOTE: setup_encode_handlers (old encode.py) intentionally omitted —
    #       it would create a duplicate /e handler competing with encode_v2.
    _setup("rename",         setup_rename_handler,
           app, task_queue, get_user_settings, config)
    _setup("cancel",         setup_cancel_handlers,
           app, task_queue, config)
    _setup("shift",          setup_shift_handlers,
           app=app, task_queue=task_queue, config=config)
    _setup("status",         setup_status_handlers,
           app=app, task_queue=task_queue, admin_ids=config.admin_ids, config=config)
    _setup("start",          setup_start_handler,
           app, config)
    _setup("mediainfo",      setup_mediainfo_handlers,
           app=app, config=config)
    _setup("settings",       setup_settings_handlers,
           app=app, user_settings=get_user_settings, config=config)

    logger.info("[Handlers] Total setup calls completed: %d", _total)
    logger.info("[Boot] allowed_group_ids = %s", config.allowed_group_ids)
    logger.info("[Boot] admin_ids         = %s", config.admin_ids)

    # ── Start client AFTER handlers are registered ────────────────────────────
    await app.start()
    logger.info("[Boot] Pyrogram client started")

    # ── Worker ────────────────────────────────────────────────────────────────
    worker = Worker(task_queue, get_user_settings, ffmpeg, app, config)
    set_worker_instance(worker)
    set_admin_ids(config.admin_ids)

    _av1_enc = await detect_av1_encoder(config.paths.ffmpeg)
    logger.info("[Boot] AV1 encoder: %s", _av1_enc or 'none')

    me = await app.get_me()

    # ── Register bot commands (shows up in Telegram's / menu) ─────────────────
    _bot_commands = [
        BotCommand("start",       "Welcome & help"),
        BotCommand("e",           "Encode video — codec + resolution menu"),
        BotCommand("eav1",        "Encode video with AV1"),
        BotCommand("480p",        "Encode at 480p — choose codec"),
        BotCommand("720p",        "Encode at 720p — choose codec"),
        BotCommand("1080p",       "Encode at 1080p — choose codec"),
        BotCommand("480pav1",     "Encode at 480p with AV1"),
        BotCommand("720pav1",     "Encode at 720p with AV1"),
        BotCommand("1080pav1",    "Encode at 1080p with AV1"),
        BotCommand("us",          "Your personal settings & thumbnail (DM only)"),
        BotCommand("gs",          "Group encoding settings (admin, groups only)"),
        BotCommand("parallel",    "Set parallel encode limit (admin)"),
        BotCommand("rename",      "Rename a file"),
        BotCommand("status",      "Show encode queue"),
        BotCommand("cancel",      "Cancel a queued task"),
        BotCommand("mi",          "Media info for a file"),
        BotCommand("shift",       "Shift subtitle timing"),
    ]
    try:
        await app.set_bot_commands(_bot_commands)
        logger.info("[Boot] Bot commands registered (%d)", len(_bot_commands))
    except Exception:
        logger.exception("[Boot] Failed to register bot commands (non-fatal)")

    logger.info("[Boot] Bot ready — @%s", me.username)
    print(f"""
    ╔══════════════════════════════════╗
    ║  @{me.username:<31}║
    ╠══════════════════════════════════╣
    ║  /start   – Welcome              ║
    ║  /gs      – Group settings       ║
    ║  /us      – Personal settings    ║
    ║  /e       – Encode (v2 menu)     ║
    ║  /rename  – Rename a file        ║
    ║  /status  – Queue status         ║
    ║  /mi      – Media Info           ║
    ║  /eav1    – AV1 Encode           ║
    ║  /parallel– Set concurrency      ║
    ║  /cancel  – Cancel a task        ║
    ╚══════════════════════════════════╝
    """)

    import signal

    _worker_task = asyncio.create_task(worker.start())

    def _graceful_shutdown(signum, frame):
        logger.info("[Boot] Received signal %d — shutting down", signum)
        _worker_task.cancel()

    try:
        signal.signal(signal.SIGTERM, _graceful_shutdown)
        signal.signal(signal.SIGINT,  _graceful_shutdown)
    except Exception:
        pass

    await idle()
    await worker.stop()
    await app.stop()


if __name__ == "__main__":
    try:
        _loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        _loop.close()
