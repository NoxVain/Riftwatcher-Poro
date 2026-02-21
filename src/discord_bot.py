import asyncio
import contextvars
import json
import random
import time
from datetime import datetime

import discord
import requests

from src import config as cfg
from src import db as dbm
from src.discord_backfill_worker import process_backfill_cycle
from src.discord_command_handlers import handle_incoming_message
from src.discord_rank_worker import process_rank_cycle
from src.discord_recap_worker import process_recap_cycle
from src.discord_text import (
    create_request_id,
    report_signature,
)
from src.constants import ADD_COMMAND, DEBUG_PLAYER_COMMAND, HEALTH_COMMAND, MOOD_COMMAND, RIOT_TEST_COMMAND, TEST_COMMAND, WEEK_COMMAND
from src.mood_service import MoodService
from src.riot_api import RiotApiClient


def log(message):
    timestamp = datetime.now().isoformat(timespec="seconds")
    request_id = REQUEST_ID_CONTEXT.get()
    if LOG_JSON:
        payload = {"ts": timestamp, "msg": message}
        if request_id:
            payload["request_id"] = request_id
        print(json.dumps(payload, ensure_ascii=True))
        return
    if request_id:
        print(f"[{timestamp}] [{request_id}] {message}")
        return
    print(f"[{timestamp}] {message}")


TOKEN = cfg.TOKEN
RIOT_API_KEY = cfg.RIOT_API_KEY
RIOT_PLATFORM_ROUTING = cfg.RIOT_PLATFORM_ROUTING
DAILY_REPORT_CHANNEL_ID = cfg.DAILY_REPORT_CHANNEL_ID
WEEKLY_REPORT_CHANNEL_ID = cfg.WEEKLY_REPORT_CHANNEL_ID
EVENTS_CHANNEL_ID = cfg.EVENTS_CHANNEL_ID
REPORT_TIMEZONE_NAME = cfg.REPORT_TIMEZONE_NAME
REPORT_TIMEZONE = cfg.REPORT_TIMEZONE
LOG_RIOT_REQUESTS = cfg.LOG_RIOT_REQUESTS
LOG_JSON = cfg.LOG_JSON
REPORT_CACHE_SECONDS = cfg.REPORT_CACHE_SECONDS
REPORT_DAY_START_HOUR = cfg.REPORT_DAY_START_HOUR
MAX_TODAY_MATCH_DETAILS = cfg.MAX_TODAY_MATCH_DETAILS
MAX_MATCH_IDS_SCAN = cfg.MAX_MATCH_IDS_SCAN
MAX_IN_MEMORY_MATCH_CACHE = cfg.MAX_IN_MEMORY_MATCH_CACHE
DAILY_REFRESH_SECONDS = cfg.DAILY_REFRESH_SECONDS
MATCH_CACHE_RETENTION_DAYS = cfg.MATCH_CACHE_RETENTION_DAYS
MATCH_RECAP_CHANNEL_ID = cfg.MATCH_RECAP_CHANNEL_ID
MATCH_RECAP_POLL_SECONDS = cfg.MATCH_RECAP_POLL_SECONDS
DB_ENABLED = dbm.DB_ENABLED

normalize_riot_id = cfg.normalize_riot_id

db_cleanup_old_match_cache = dbm.db_cleanup_old_match_cache
db_get_last_report_message = dbm.db_get_last_report_message
db_get_last_weekly_report_message = dbm.db_get_last_weekly_report_message
db_get_last_seen_match_id = dbm.db_get_last_seen_match_id
db_get_match_info = dbm.db_get_match_info
db_get_puuid = dbm.db_get_puuid
db_health_stats = dbm.db_health_stats
db_get_daily_stats_for_player = dbm.db_get_daily_stats_for_player
db_load_latest_stats = dbm.db_load_latest_stats
db_load_weekly_stats = dbm.db_load_weekly_stats
db_load_backfill_offsets = dbm.db_load_backfill_offsets
db_load_ranked_state = dbm.db_load_ranked_state
db_load_tracked_players = dbm.db_load_tracked_players
db_delete_ranked_state_queue = dbm.db_delete_ranked_state_queue
db_set_last_report_message = dbm.db_set_last_report_message
db_set_last_weekly_report_message = dbm.db_set_last_weekly_report_message
db_set_last_seen_match_id = dbm.db_set_last_seen_match_id
db_set_state = dbm.db_set_state
db_get_state = dbm.db_get_state
db_upsert_daily_stats = dbm.db_upsert_daily_stats
db_upsert_match_info = dbm.db_upsert_match_info
db_upsert_player = dbm.db_upsert_player
db_upsert_ranked_state = dbm.db_upsert_ranked_state
init_db = dbm.init_db

REQUEST_ID_CONTEXT = contextvars.ContextVar("request_id", default=None)
START_MONOTONIC = time.monotonic()
LAST_CACHE_CLEANUP_AT = 0.0
RIOT_401_ALERT_SENT = False
RIOT_ALERT_LOCK = asyncio.Lock()


def load_tracked_players():
    return db_load_tracked_players()


init_db()
FRIENDS = load_tracked_players()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
MOOD_REQUEST_LOCK = asyncio.Lock()

LAST_REPORT_MESSAGE = {"channel_id": None, "message_id": None}
LAST_WEEKLY_REPORT_MESSAGE = {"channel_id": None, "message_id": None}
STARTUP_SCOREBOARD_INIT_DONE = False
BACKGROUND_REFRESH_TASK = None
BACKGROUND_RECAP_TASK = None
BACKGROUND_RANK_TASK = None
BACKGROUND_BACKFILL_TASK = None


async def send_riot_key_expired_alert():
    channel = await resolve_channel(EVENTS_CHANNEL_ID)
    if channel is None:
        return
    await channel.send(
        "@NoxVain \u26A0\uFE0F Riot API returned 401 Unauthorized. "
        "Your RIOT_API_KEY is likely expired or invalid. "
        "Update the Railway variable `RIOT_API_KEY`."
    )
    log("[riot] Sent RIOT_API_KEY expiry alert.")


def riot_401_alert_already_sent():
    global RIOT_401_ALERT_SENT
    if RIOT_401_ALERT_SENT:
        return True
    persisted = db_get_state("riot_401_alert_sent")
    if persisted == "1":
        RIOT_401_ALERT_SENT = True
        return True
    return False


def mark_riot_401_alert_sent():
    global RIOT_401_ALERT_SENT
    RIOT_401_ALERT_SENT = True
    db_set_state("riot_401_alert_sent", "1")


def trigger_riot_key_alert():
    async def _inner():
        async with RIOT_ALERT_LOCK:
            if riot_401_alert_already_sent():
                return
            mark_riot_401_alert_sent()
        await send_riot_key_expired_alert()

    try:
        loop = client.loop
        asyncio.run_coroutine_threadsafe(_inner(), loop)
    except Exception as exc:
        log(f"[riot] Could not schedule key-expiry alert: {exc}")


riot_client = RiotApiClient(
    riot_api_key=RIOT_API_KEY,
    riot_platform_routing=RIOT_PLATFORM_ROUTING,
    log=log,
    log_riot_requests=LOG_RIOT_REQUESTS,
    report_timezone=REPORT_TIMEZONE,
    report_day_start_hour=REPORT_DAY_START_HOUR,
    max_today_match_details=MAX_TODAY_MATCH_DETAILS,
    max_match_ids_scan=MAX_MATCH_IDS_SCAN,
    max_in_memory_match_cache=MAX_IN_MEMORY_MATCH_CACHE,
    db_get_puuid=db_get_puuid,
    db_upsert_player=db_upsert_player,
    db_get_match_info=db_get_match_info,
    db_upsert_match_info=db_upsert_match_info,
    db_set_last_seen_match_id=db_set_last_seen_match_id,
    on_unauthorized=trigger_riot_key_alert,
)

mood_service = MoodService(
    log=log,
    friends=FRIENDS,
    riot_client=riot_client,
    report_timezone=REPORT_TIMEZONE,
    report_day_start_hour=REPORT_DAY_START_HOUR,
    report_cache_seconds=REPORT_CACHE_SECONDS,
    daily_refresh_seconds=DAILY_REFRESH_SECONDS,
    db_enabled=DB_ENABLED,
    db_load_latest_stats=db_load_latest_stats,
    db_load_weekly_stats=db_load_weekly_stats,
    db_upsert_daily_stats=db_upsert_daily_stats,
    db_get_daily_stats_for_player=db_get_daily_stats_for_player,
    db_get_last_seen_match_id=db_get_last_seen_match_id,
    db_set_last_seen_match_id=db_set_last_seen_match_id,
    db_health_stats=db_health_stats,
    db_load_backfill_offsets=db_load_backfill_offsets,
)


def remember_report_message(message):
    LAST_REPORT_MESSAGE["channel_id"] = message.channel.id
    LAST_REPORT_MESSAGE["message_id"] = message.id
    if DB_ENABLED:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(db_set_last_report_message, message.channel.id, message.id))
        except RuntimeError:
            db_set_last_report_message(message.channel.id, message.id)


def remember_weekly_report_message(message):
    LAST_WEEKLY_REPORT_MESSAGE["channel_id"] = message.channel.id
    LAST_WEEKLY_REPORT_MESSAGE["message_id"] = message.id
    if DB_ENABLED:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(db_set_last_weekly_report_message, message.channel.id, message.id))
        except RuntimeError:
            db_set_last_weekly_report_message(message.channel.id, message.id)


async def resolve_channel(channel_id):
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel

    try:
        return await client.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden) as exc:
        log(f"[channel] Could not access channel {channel_id}: {exc}")
    except discord.HTTPException as exc:
        log(f"[channel] Discord API error while fetching channel {channel_id}: {exc}")
    return None


async def get_or_create_report_message(channel, initial_content):
    channel_id = LAST_REPORT_MESSAGE["channel_id"]
    message_id = LAST_REPORT_MESSAGE["message_id"]

    if (not channel_id or not message_id) and DB_ENABLED:
        persisted_channel_id, persisted_message_id = await asyncio.to_thread(db_get_last_report_message)
        if persisted_channel_id and persisted_message_id:
            LAST_REPORT_MESSAGE["channel_id"] = persisted_channel_id
            LAST_REPORT_MESSAGE["message_id"] = persisted_message_id
            channel_id = persisted_channel_id
            message_id = persisted_message_id

    if channel_id == channel.id and message_id:
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LAST_REPORT_MESSAGE["channel_id"] = None
            LAST_REPORT_MESSAGE["message_id"] = None
            if DB_ENABLED:
                await asyncio.to_thread(db_set_last_report_message, 0, 0)

    message = await channel.send(initial_content)
    remember_report_message(message)
    return message


async def get_or_create_weekly_report_message(channel, initial_content):
    channel_id = LAST_WEEKLY_REPORT_MESSAGE["channel_id"]
    message_id = LAST_WEEKLY_REPORT_MESSAGE["message_id"]

    if (not channel_id or not message_id) and DB_ENABLED:
        persisted_channel_id, persisted_message_id = await asyncio.to_thread(db_get_last_weekly_report_message)
        if persisted_channel_id and persisted_message_id:
            LAST_WEEKLY_REPORT_MESSAGE["channel_id"] = persisted_channel_id
            LAST_WEEKLY_REPORT_MESSAGE["message_id"] = persisted_message_id
            channel_id = persisted_channel_id
            message_id = persisted_message_id

    if channel_id == channel.id and message_id:
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LAST_WEEKLY_REPORT_MESSAGE["channel_id"] = None
            LAST_WEEKLY_REPORT_MESSAGE["message_id"] = None
            if DB_ENABLED:
                await asyncio.to_thread(db_set_last_weekly_report_message, 0, 0)

    message = await channel.send(initial_content)
    remember_weekly_report_message(message)
    return message


async def edit_last_report_message(prefer_snapshot=False, bypass_cache=False):
    channel_id = LAST_REPORT_MESSAGE["channel_id"]
    message_id = LAST_REPORT_MESSAGE["message_id"]
    if not channel_id or not message_id:
        return

    channel = await resolve_channel(channel_id)
    if channel is None:
        return

    try:
        report_text = await mood_service.build_today_win_rate_report(
            prefer_snapshot=prefer_snapshot,
            bypass_cache=bypass_cache,
        )
        message = await channel.fetch_message(message_id)
        if message.content == report_text:
            log(f"[refresh] No report change; skipped editing message {message_id}.")
            return
        await message.edit(content=report_text)
        log(f"[refresh] Updated last report message {message_id} in channel {channel_id}.")
    except (discord.NotFound, discord.Forbidden) as exc:
        log(f"[refresh] Could not edit last report message {message_id}: {exc}")
        LAST_REPORT_MESSAGE["channel_id"] = None
        LAST_REPORT_MESSAGE["message_id"] = None
        if DB_ENABLED:
            await asyncio.to_thread(db_set_last_report_message, 0, 0)
    except discord.HTTPException as exc:
        log(f"[refresh] Discord API error while editing last report message: {exc}")


async def edit_last_weekly_report_message(bypass_cache=False):
    channel_id = LAST_WEEKLY_REPORT_MESSAGE["channel_id"]
    message_id = LAST_WEEKLY_REPORT_MESSAGE["message_id"]
    if not channel_id or not message_id:
        return

    channel = await resolve_channel(channel_id)
    if channel is None:
        return

    try:
        report_text = await mood_service.build_weekly_win_rate_report(bypass_cache=bypass_cache)
        message = await channel.fetch_message(message_id)
        if message.content == report_text:
            log(f"[refresh] No weekly report change; skipped editing message {message_id}.")
            return
        await message.edit(content=report_text)
        log(f"[refresh] Updated weekly report message {message_id} in channel {channel_id}.")
    except (discord.NotFound, discord.Forbidden) as exc:
        log(f"[refresh] Could not edit weekly report message {message_id}: {exc}")
        LAST_WEEKLY_REPORT_MESSAGE["channel_id"] = None
        LAST_WEEKLY_REPORT_MESSAGE["message_id"] = None
        if DB_ENABLED:
            await asyncio.to_thread(db_set_last_weekly_report_message, 0, 0)
    except discord.HTTPException as exc:
        log(f"[refresh] Discord API error while editing weekly report message: {exc}")


async def daily_report(channel):
    report_text = await mood_service.build_today_win_rate_report()
    report_message = await get_or_create_report_message(channel, report_text)
    if report_message.content != report_text:
        await report_message.edit(content=report_text)
        log(f"[scheduler] Updated scoreboard message {report_message.id}.")
    else:
        log(f"[scheduler] No scoreboard change; skipped update for {report_message.id}.")


async def weekly_report(channel):
    report_text = await mood_service.build_weekly_win_rate_report()
    report_message = await get_or_create_weekly_report_message(channel, report_text)
    if report_message.content != report_text:
        await report_message.edit(content=report_text)
        log(f"[scheduler] Updated weekly scoreboard message {report_message.id}.")
    else:
        log(f"[scheduler] No weekly scoreboard change; skipped update for {report_message.id}.")


async def evaluate_rank_changes_and_notify():
    channel = await resolve_channel(EVENTS_CHANNEL_ID)
    if channel is None:
        return
    await process_rank_cycle(
        friends=FRIENDS,
        channel=channel,
        riot_client=riot_client,
        db_load_ranked_state=db_load_ranked_state,
        db_upsert_ranked_state=db_upsert_ranked_state,
        db_delete_ranked_state_queue=db_delete_ranked_state_queue,
        log=log,
    )


async def background_rank_notifier():
    if not DB_ENABLED:
        return
    sleep_seconds = max(30, DAILY_REFRESH_SECONDS)
    initial_jitter = random.uniform(0.0, min(30.0, sleep_seconds / 2))
    if initial_jitter > 0:
        log(f"[rank] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)
    while not client.is_closed():
        cycle_start = time.monotonic()
        token = REQUEST_ID_CONTEXT.set(create_request_id("rank"))
        try:
            await evaluate_rank_changes_and_notify()
        except Exception as exc:
            log(f"[rank] Unexpected background error: {exc}")
        finally:
            REQUEST_ID_CONTEXT.reset(token)
        elapsed = int((time.monotonic() - cycle_start) * 1000)
        log(f"[rank] Cycle complete elapsed={elapsed}ms next_sleep={sleep_seconds}s")
        await asyncio.sleep(sleep_seconds)


async def background_match_recap_notifier():
    sleep_seconds = max(30, MATCH_RECAP_POLL_SECONDS)
    initial_jitter = random.uniform(0.0, min(30.0, sleep_seconds / 2))
    if initial_jitter > 0:
        log(f"[recap] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)
    while not client.is_closed():
        cycle_start = time.monotonic()
        token = REQUEST_ID_CONTEXT.set(create_request_id("recap"))
        try:
            channel = await resolve_channel(MATCH_RECAP_CHANNEL_ID)
            if channel is None:
                await asyncio.sleep(max(30, MATCH_RECAP_POLL_SECONDS))
                continue

            await process_recap_cycle(
                friends=FRIENDS,
                riot_client=riot_client,
                mood_service=mood_service,
                report_timezone=REPORT_TIMEZONE,
                match_recap_channel_id=MATCH_RECAP_CHANNEL_ID,
                channel=channel,
                db_enabled=DB_ENABLED,
                db_get_state=db_get_state,
                db_set_state=db_set_state,
                db_upsert_daily_stats=db_upsert_daily_stats,
                edit_last_report_message=edit_last_report_message,
                edit_last_weekly_report_message=edit_last_weekly_report_message,
                log=log,
            )
        except Exception as exc:
            log(f"[recap] Unexpected error: {exc}")
        finally:
            REQUEST_ID_CONTEXT.reset(token)
        elapsed = int((time.monotonic() - cycle_start) * 1000)
        log(f"[recap] Cycle complete elapsed={elapsed}ms next_sleep={sleep_seconds}s")
        await asyncio.sleep(sleep_seconds)


async def background_match_cache_backfiller():
    if not DB_ENABLED:
        return

    backfill_recent_ids_count = 100
    backfill_per_player_limit = 3
    backfill_interval_seconds = max(120, DAILY_REFRESH_SECONDS * 2)
    initial_jitter = random.uniform(0.0, min(60.0, backfill_interval_seconds / 2))
    if initial_jitter > 0:
        log(f"[backfill] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)

    while not client.is_closed():
        cycle_start = time.monotonic()
        token = REQUEST_ID_CONTEXT.set(create_request_id("backfill"))
        try:
            total_backfilled = await process_backfill_cycle(
                friends=FRIENDS,
                riot_client=riot_client,
                db_get_state=db_get_state,
                db_set_state=db_set_state,
                db_get_match_info=db_get_match_info,
                recent_ids_count=backfill_recent_ids_count,
                per_player_limit=backfill_per_player_limit,
                log=log,
            )
            offsets = await asyncio.to_thread(db_load_backfill_offsets)
            active_offsets = sum(1 for riot_id in FRIENDS if int(offsets.get(riot_id.casefold(), 0) or 0) > 0)
            max_offset = max((int(offsets.get(riot_id.casefold(), 0) or 0) for riot_id in FRIENDS), default=0)
            log(
                f"[backfill] Cycle summary: cached={total_backfilled}, "
                f"active_offsets={active_offsets}/{len(FRIENDS)}, max_offset={max_offset}."
            )
        except Exception as exc:
            log(f"[backfill] Unexpected background error: {exc}")
        finally:
            REQUEST_ID_CONTEXT.reset(token)
        elapsed = int((time.monotonic() - cycle_start) * 1000)
        log(f"[backfill] Cycle complete elapsed={elapsed}ms next_sleep={backfill_interval_seconds}s")
        await asyncio.sleep(backfill_interval_seconds)


async def background_daily_refresher():
    global LAST_CACHE_CLEANUP_AT
    if not DB_ENABLED:
        return
    sleep_seconds = max(30, DAILY_REFRESH_SECONDS)
    initial_jitter = random.uniform(0.0, min(30.0, sleep_seconds / 2))
    if initial_jitter > 0:
        log(f"[refresh] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)
    last_snapshot_push_at = 0.0
    last_snapshot_signature = None
    snapshot_push_interval = 120.0
    changed_push_min_interval = 30.0
    while not client.is_closed():
        cycle_start = time.monotonic()
        token = REQUEST_ID_CONTEXT.set(create_request_id("bg"))
        try:
            async def push_snapshot_update(force=False):
                nonlocal last_snapshot_push_at, last_snapshot_signature
                now_mono = time.monotonic()

                channel_id = LAST_REPORT_MESSAGE["channel_id"]
                message_id = LAST_REPORT_MESSAGE["message_id"]
                if not channel_id or not message_id:
                    return

                channel = await resolve_channel(channel_id)
                if channel is None:
                    return

                try:
                    snapshot_text = await mood_service.build_today_win_rate_report(
                        prefer_snapshot=True,
                        bypass_cache=True,
                    )
                    interval_elapsed = (now_mono - last_snapshot_push_at) >= snapshot_push_interval
                    signature = report_signature(snapshot_text)
                    changed = signature != last_snapshot_signature
                    changed_interval_elapsed = (now_mono - last_snapshot_push_at) >= changed_push_min_interval
                    should_push = force or interval_elapsed or (changed and changed_interval_elapsed)
                    if not should_push:
                        return

                    message = await channel.fetch_message(message_id)
                    if message.content != snapshot_text:
                        await message.edit(content=snapshot_text)
                        log(
                            f"[refresh] Updated last report message {message_id} in channel {channel_id} "
                            f"(force={force})."
                        )
                    else:
                        log(f"[refresh] Snapshot unchanged in Discord for message {message_id}.")

                    last_snapshot_signature = signature
                    last_snapshot_push_at = now_mono
                except (discord.NotFound, discord.Forbidden) as exc:
                    log(f"[refresh] Could not edit last report message {message_id}: {exc}")
                    LAST_REPORT_MESSAGE["channel_id"] = None
                    LAST_REPORT_MESSAGE["message_id"] = None
                    if DB_ENABLED:
                        await asyncio.to_thread(db_set_last_report_message, 0, 0)
                except discord.HTTPException as exc:
                    log(f"[refresh] Discord API error while editing last report message: {exc}")

            async def on_player_refreshed(_processed, _total, _riot_id):
                await push_snapshot_update(force=False)

            await mood_service.refresh_daily_stats_once(progress_callback=on_player_refreshed)
            await push_snapshot_update(force=True)
            await edit_last_weekly_report_message(bypass_cache=True)
            now_mono = time.monotonic()
            if (now_mono - LAST_CACHE_CLEANUP_AT) >= max(3600, DAILY_REFRESH_SECONDS):
                deleted = await asyncio.to_thread(db_cleanup_old_match_cache, MATCH_CACHE_RETENTION_DAYS)
                LAST_CACHE_CLEANUP_AT = now_mono
                log(
                    f"[refresh] Match cache cleanup complete: deleted={deleted}, "
                    f"retention_days={MATCH_CACHE_RETENTION_DAYS}"
                )
        except Exception as exc:
            log(f"[refresh] Unexpected error: {exc}")
        finally:
            REQUEST_ID_CONTEXT.reset(token)
        elapsed = int((time.monotonic() - cycle_start) * 1000)
        log(f"[refresh] Cycle complete elapsed={elapsed}ms next_sleep={sleep_seconds}s")
        await asyncio.sleep(sleep_seconds)


@client.event
async def on_ready():
    global BACKGROUND_REFRESH_TASK, BACKGROUND_RECAP_TASK, BACKGROUND_RANK_TASK, BACKGROUND_BACKFILL_TASK, STARTUP_SCOREBOARD_INIT_DONE
    log(f"[startup] Logged in as {client.user} (id={client.user.id})")
    log(f"[startup] Use {TEST_COMMAND} in channel {DAILY_REPORT_CHANNEL_ID} to test sending.")
    log(f"[startup] Use {RIOT_TEST_COMMAND} in channel {DAILY_REPORT_CHANNEL_ID} to test Riot API access.")
    log(
        f"[startup] Use {MOOD_COMMAND} in channel {DAILY_REPORT_CHANNEL_ID} for results "
        f"since {REPORT_DAY_START_HOUR:02d}:00."
    )
    log(
        f"[startup] Use {WEEK_COMMAND} in channel {DAILY_REPORT_CHANNEL_ID}; "
        f"it publishes in {WEEKLY_REPORT_CHANNEL_ID} "
        f"for Monday {REPORT_DAY_START_HOUR:02d}:00 -> next Monday {REPORT_DAY_START_HOUR:02d}:00."
    )
    log(f"[startup] Use {ADD_COMMAND} <Name#Tag> to add a player at runtime.")
    log(f"[startup] Use {DEBUG_PLAYER_COMMAND} <Name#Tag> to inspect queue bucket mapping.")
    log(f"[startup] Use {HEALTH_COMMAND} in channel {DAILY_REPORT_CHANNEL_ID} for health status.")
    log(f"[startup] Loaded {len(FRIENDS)} tracked players from postgres.")
    log("[startup] Player store: postgres")
    log(f"[startup] Report timezone: {REPORT_TIMEZONE_NAME}")
    log(f"[startup] LOG_RIOT_REQUESTS={LOG_RIOT_REQUESTS}")
    log(f"[startup] LOG_JSON={LOG_JSON}")
    log(f"[startup] RIOT_PLATFORM_ROUTING={RIOT_PLATFORM_ROUTING}")
    log(f"[startup] MAX_TODAY_MATCH_DETAILS={MAX_TODAY_MATCH_DETAILS}")
    log(f"[startup] REPORT_DAY_START_HOUR={REPORT_DAY_START_HOUR}")
    log(f"[startup] MAX_MATCH_IDS_SCAN={MAX_MATCH_IDS_SCAN}")
    log(f"[startup] MAX_IN_MEMORY_MATCH_CACHE={MAX_IN_MEMORY_MATCH_CACHE}")
    log(f"[startup] REPORT_CACHE_SECONDS={REPORT_CACHE_SECONDS}")
    log(f"[startup] MATCH_CACHE_RETENTION_DAYS={MATCH_CACHE_RETENTION_DAYS}")
    log(f"[startup] EVENTS_CHANNEL_ID={EVENTS_CHANNEL_ID}")
    log(f"[startup] WEEKLY_REPORT_CHANNEL_ID={WEEKLY_REPORT_CHANNEL_ID}")
    log(f"[startup] MATCH_RECAP_CHANNEL_ID={MATCH_RECAP_CHANNEL_ID}")
    log(f"[startup] RANK_ALERT_CHANNEL_ID={EVENTS_CHANNEL_ID}")
    log(f"[startup] MATCH_RECAP_POLL_SECONDS={MATCH_RECAP_POLL_SECONDS}")
    if MATCH_RECAP_CHANNEL_ID and MATCH_RECAP_CHANNEL_ID == DAILY_REPORT_CHANNEL_ID:
        log("[startup] Warning: MATCH_RECAP_CHANNEL_ID equals DAILY_REPORT_CHANNEL_ID.")
    if WEEKLY_REPORT_CHANNEL_ID and WEEKLY_REPORT_CHANNEL_ID == DAILY_REPORT_CHANNEL_ID:
        log("[startup] Info: WEEKLY_REPORT_CHANNEL_ID equals DAILY_REPORT_CHANNEL_ID.")
    if DB_ENABLED:
        log(f"[startup] DAILY_REFRESH_SECONDS={DAILY_REFRESH_SECONDS}")
        if BACKGROUND_REFRESH_TASK is None or BACKGROUND_REFRESH_TASK.done():
            BACKGROUND_REFRESH_TASK = client.loop.create_task(background_daily_refresher())
        if BACKGROUND_RANK_TASK is None or BACKGROUND_RANK_TASK.done():
            BACKGROUND_RANK_TASK = client.loop.create_task(background_rank_notifier())
        if BACKGROUND_RECAP_TASK is None or BACKGROUND_RECAP_TASK.done():
            BACKGROUND_RECAP_TASK = client.loop.create_task(background_match_recap_notifier())
        if BACKGROUND_BACKFILL_TASK is None or BACKGROUND_BACKFILL_TASK.done():
            BACKGROUND_BACKFILL_TASK = client.loop.create_task(background_match_cache_backfiller())
    if not STARTUP_SCOREBOARD_INIT_DONE:
        daily_channel = await resolve_channel(DAILY_REPORT_CHANNEL_ID)
        weekly_channel = await resolve_channel(WEEKLY_REPORT_CHANNEL_ID)
        if daily_channel is not None:
            try:
                await daily_report(daily_channel)
                log(f"[startup] Initialized daily scoreboard in channel {DAILY_REPORT_CHANNEL_ID}.")
            except Exception as exc:
                log(f"[startup] Failed to initialize daily scoreboard: {exc}")
        if weekly_channel is not None:
            try:
                await weekly_report(weekly_channel)
                log(f"[startup] Initialized weekly scoreboard in channel {WEEKLY_REPORT_CHANNEL_ID}.")
            except Exception as exc:
                log(f"[startup] Failed to initialize weekly scoreboard: {exc}")
        STARTUP_SCOREBOARD_INIT_DONE = True
@client.event
async def on_message(message):
    if message.author.bot:
        return
    await handle_incoming_message(
        message=message,
        channel_id=DAILY_REPORT_CHANNEL_ID,
        friends=FRIENDS,
        riot_client=riot_client,
        mood_service=mood_service,
        report_timezone_name=REPORT_TIMEZONE_NAME,
        report_day_start_hour=REPORT_DAY_START_HOUR,
        db_enabled=DB_ENABLED,
        start_monotonic=START_MONOTONIC,
        mood_request_lock=MOOD_REQUEST_LOCK,
        request_id_context=REQUEST_ID_CONTEXT,
        create_request_id=create_request_id,
        get_or_create_report_message=get_or_create_report_message,
        remember_report_message=remember_report_message,
        get_or_create_weekly_report_message=get_or_create_weekly_report_message,
        remember_weekly_report_message=remember_weekly_report_message,
        normalize_riot_id=normalize_riot_id,
        db_upsert_player=db_upsert_player,
        log=log,
        weekly_report_channel_id=WEEKLY_REPORT_CHANNEL_ID,
        resolve_channel=resolve_channel,
    )


def main():
    client.run(TOKEN)


if __name__ == "__main__":
    main()







