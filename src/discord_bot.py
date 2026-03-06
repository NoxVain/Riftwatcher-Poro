import asyncio
import contextvars
import json
import time
from datetime import datetime

import discord

from src import config as cfg
from src import db as dbm
from src.discord_command_handlers import handle_incoming_message
from src.discord_text import create_request_id
from src.constants import ADD_COMMAND, BACKFILL_COMMAND, DEBUG_PLAYER_COMMAND, HEALTH_COMMAND, MOOD_COMMAND, REMOVE_COMMAND, RIOT_TEST_COMMAND, TEST_COMMAND, WEEK_COMMAND
from src.mood_service import MoodService
from src.riot_api import RiotApiClient
from src.runtime.alerts import RiotAlertState, trigger_riot_key_alert as runtime_trigger_riot_key_alert
from src.runtime.alerts import check_and_notify_worker_stalls as runtime_check_and_notify_worker_stalls
from src.runtime.message_store import (
    create_message_state,
    get_or_create_report_message as runtime_get_or_create_report_message,
    get_or_create_weekly_report_message as runtime_get_or_create_weekly_report_message,
    remember_previous_report_message as runtime_remember_previous_report_message,
    remember_report_message as runtime_remember_report_message,
    remember_weekly_report_message as runtime_remember_weekly_report_message,
)
from src.runtime.workers import (
    background_daily_refresher as runtime_background_daily_refresher,
    background_match_cache_backfiller as runtime_background_match_cache_backfiller,
    background_match_recap_notifier as runtime_background_match_recap_notifier,
    background_rank_notifier as runtime_background_rank_notifier,
    evaluate_rank_changes_and_notify as runtime_evaluate_rank_changes_and_notify,
)


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
RIOT_REGIONAL_ROUTING = cfg.RIOT_REGIONAL_ROUTING
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
db_load_match_payloads_for_baseline = dbm.db_load_match_payloads_for_baseline
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
db_remove_player = dbm.db_remove_player
db_upsert_ranked_state = dbm.db_upsert_ranked_state
init_db = dbm.init_db

REQUEST_ID_CONTEXT = contextvars.ContextVar("request_id", default=None)
START_MONOTONIC = time.monotonic()
RIOT_ALERT_STATE = RiotAlertState()
WORKER_STATS = {
    "refresh": {"cycles": 0, "errors": 0, "runs": 0, "elapsed_ms_last": 0, "elapsed_ms_avg": 0, "elapsed_ms_max": 0, "elapsed_ms_total": 0, "last_success_at": START_MONOTONIC},
    "rank": {"cycles": 0, "errors": 0, "runs": 0, "elapsed_ms_last": 0, "elapsed_ms_avg": 0, "elapsed_ms_max": 0, "elapsed_ms_total": 0, "last_success_at": START_MONOTONIC},
    "recap": {"cycles": 0, "errors": 0, "runs": 0, "elapsed_ms_last": 0, "elapsed_ms_avg": 0, "elapsed_ms_max": 0, "elapsed_ms_total": 0, "last_success_at": START_MONOTONIC},
    "backfill": {"cycles": 0, "errors": 0, "runs": 0, "elapsed_ms_last": 0, "elapsed_ms_avg": 0, "elapsed_ms_max": 0, "elapsed_ms_total": 0, "last_success_at": START_MONOTONIC},
}
WORKER_STALL_STATE = {"alerted_by_worker": {}}
WORKER_STALE_THRESHOLDS = {
    "refresh": max(90, max(30, DAILY_REFRESH_SECONDS) * 3),
    "rank": max(90, max(30, DAILY_REFRESH_SECONDS) * 3),
    "recap": max(90, max(30, MATCH_RECAP_POLL_SECONDS) * 3),
    "backfill": max(300, max(120, DAILY_REFRESH_SECONDS * 2) * 3),
}
def load_tracked_players():
    return db_load_tracked_players()


init_db()
FRIENDS = load_tracked_players()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
MOOD_REQUEST_LOCK = asyncio.Lock()

MESSAGE_STATE = create_message_state()
LAST_REPORT_MESSAGE = MESSAGE_STATE["last_report_message"]
LAST_WEEKLY_REPORT_MESSAGE = MESSAGE_STATE["last_weekly_report_message"]
REPORT_RUNTIME_STATE = {"last_cache_cleanup_at": 0.0}
STARTUP_SCOREBOARD_INIT_DONE = False
BACKGROUND_REFRESH_TASK = None
BACKGROUND_RECAP_TASK = None
BACKGROUND_RANK_TASK = None
BACKGROUND_BACKFILL_TASK = None
BACKGROUND_STALL_TASK = None


def trigger_riot_key_alert():
    runtime_trigger_riot_key_alert(
        state=RIOT_ALERT_STATE,
        client=client,
        resolve_channel=resolve_channel,
        events_channel_id=EVENTS_CHANNEL_ID,
        db_get_state=db_get_state,
        db_set_state=db_set_state,
        log=log,
    )


riot_client = RiotApiClient(
    riot_api_key=RIOT_API_KEY,
    riot_platform_routing=RIOT_PLATFORM_ROUTING,
    riot_regional_routing=RIOT_REGIONAL_ROUTING,
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
    db_load_match_payloads_for_baseline=db_load_match_payloads_for_baseline,
)


def remember_report_message(message):
    runtime_remember_report_message(
        state=MESSAGE_STATE,
        message=message,
        db_enabled=DB_ENABLED,
        db_set_last_report_message=db_set_last_report_message,
    )


def remember_previous_report_message(message, cycle_key=None):
    runtime_remember_previous_report_message(
        state=MESSAGE_STATE,
        message=message,
        db_enabled=DB_ENABLED,
        db_set_state=db_set_state,
        cycle_key=cycle_key,
    )


def remember_weekly_report_message(message):
    runtime_remember_weekly_report_message(
        state=MESSAGE_STATE,
        message=message,
        db_enabled=DB_ENABLED,
        db_set_last_weekly_report_message=db_set_last_weekly_report_message,
    )


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
    return await runtime_get_or_create_report_message(
        state=MESSAGE_STATE,
        channel=channel,
        initial_content=initial_content,
        mood_service=mood_service,
        db_enabled=DB_ENABLED,
        db_get_state=db_get_state,
        db_set_state=db_set_state,
        db_get_last_report_message=db_get_last_report_message,
        db_set_last_report_message=db_set_last_report_message,
        remember_report_message_fn=remember_report_message,
        remember_previous_report_message_fn=remember_previous_report_message,
    )


async def get_or_create_weekly_report_message(channel, initial_content):
    return await runtime_get_or_create_weekly_report_message(
        state=MESSAGE_STATE,
        channel=channel,
        initial_content=initial_content,
        db_enabled=DB_ENABLED,
        db_get_last_weekly_report_message=db_get_last_weekly_report_message,
        db_set_last_weekly_report_message=db_set_last_weekly_report_message,
        remember_weekly_report_message_fn=remember_weekly_report_message,
    )


async def edit_last_report_message(prefer_snapshot=False, bypass_cache=False):
    channel = await resolve_channel(DAILY_REPORT_CHANNEL_ID)
    if channel is None:
        return

    try:
        report_text = await mood_service.build_today_win_rate_report(
            prefer_snapshot=prefer_snapshot,
            bypass_cache=bypass_cache,
        )
        message = await get_or_create_report_message(channel, report_text)
        if message.content == report_text:
            log(f"[refresh] No report change; skipped editing message {message.id}.")
            return
        await message.edit(content=report_text)
        log(f"[refresh] Updated last report message {message.id} in channel {channel.id}.")
    except (discord.NotFound, discord.Forbidden) as exc:
        log(f"[refresh] Could not edit last report message: {exc}")
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
    return await runtime_evaluate_rank_changes_and_notify(
        resolve_channel=resolve_channel,
        events_channel_id=EVENTS_CHANNEL_ID,
        friends=FRIENDS,
        riot_client=riot_client,
        db_load_ranked_state=db_load_ranked_state,
        db_upsert_ranked_state=db_upsert_ranked_state,
        db_delete_ranked_state_queue=db_delete_ranked_state_queue,
        log=log,
    )


async def background_rank_notifier():
    return await runtime_background_rank_notifier(
        db_enabled=DB_ENABLED,
        daily_refresh_seconds=DAILY_REFRESH_SECONDS,
        client=client,
        request_id_context=REQUEST_ID_CONTEXT,
        worker_stats=WORKER_STATS,
        evaluate_rank_changes_and_notify_fn=evaluate_rank_changes_and_notify,
        log=log,
    )


async def background_match_recap_notifier():
    return await runtime_background_match_recap_notifier(
        client=client,
        match_recap_poll_seconds=MATCH_RECAP_POLL_SECONDS,
        request_id_context=REQUEST_ID_CONTEXT,
        friends=FRIENDS,
        riot_client=riot_client,
        mood_service=mood_service,
        report_timezone=REPORT_TIMEZONE,
        match_recap_channel_id=MATCH_RECAP_CHANNEL_ID,
        db_enabled=DB_ENABLED,
        db_get_state=db_get_state,
        db_set_state=db_set_state,
        db_upsert_daily_stats=db_upsert_daily_stats,
        edit_last_report_message=edit_last_report_message,
        edit_last_weekly_report_message=edit_last_weekly_report_message,
        resolve_channel=resolve_channel,
        worker_stats=WORKER_STATS,
        log=log,
    )


async def background_match_cache_backfiller():
    return await runtime_background_match_cache_backfiller(
        db_enabled=DB_ENABLED,
        daily_refresh_seconds=DAILY_REFRESH_SECONDS,
        client=client,
        request_id_context=REQUEST_ID_CONTEXT,
        friends=FRIENDS,
        riot_client=riot_client,
        db_get_state=db_get_state,
        db_set_state=db_set_state,
        db_get_match_info=db_get_match_info,
        db_load_backfill_offsets=db_load_backfill_offsets,
        worker_stats=WORKER_STATS,
        log=log,
    )


async def background_daily_refresher():
    await runtime_background_daily_refresher(
        db_enabled=DB_ENABLED,
        daily_refresh_seconds=DAILY_REFRESH_SECONDS,
        client=client,
        request_id_context=REQUEST_ID_CONTEXT,
        mood_service=mood_service,
        resolve_channel=resolve_channel,
        daily_report_channel_id=DAILY_REPORT_CHANNEL_ID,
        get_or_create_report_message=get_or_create_report_message,
        edit_last_weekly_report_message=edit_last_weekly_report_message,
        db_cleanup_old_match_cache=db_cleanup_old_match_cache,
        match_cache_retention_days=MATCH_CACHE_RETENTION_DAYS,
        db_set_last_report_message=db_set_last_report_message,
        report_state=LAST_REPORT_MESSAGE,
        runtime_state=REPORT_RUNTIME_STATE,
        worker_stats=WORKER_STATS,
        log=log,
    )


async def background_worker_stall_notifier():
    sleep_seconds = 30
    await asyncio.sleep(15)
    while not client.is_closed():
        token = REQUEST_ID_CONTEXT.set(create_request_id("stall"))
        try:
            await runtime_check_and_notify_worker_stalls(
                state=WORKER_STALL_STATE,
                resolve_channel=resolve_channel,
                events_channel_id=EVENTS_CHANNEL_ID,
                worker_stats=WORKER_STATS,
                stale_thresholds_seconds=WORKER_STALE_THRESHOLDS,
                db_get_state=db_get_state if DB_ENABLED else None,
                db_set_state=db_set_state if DB_ENABLED else None,
                now_monotonic=time.monotonic(),
                log=log,
            )
        except Exception as exc:
            log(f"[health] Worker stall monitor error: {exc}")
        finally:
            REQUEST_ID_CONTEXT.reset(token)
        await asyncio.sleep(sleep_seconds)


@client.event
async def on_ready():
    global BACKGROUND_REFRESH_TASK, BACKGROUND_RECAP_TASK, BACKGROUND_RANK_TASK, BACKGROUND_BACKFILL_TASK, BACKGROUND_STALL_TASK, STARTUP_SCOREBOARD_INIT_DONE
    log(f"[startup] Logged in as {client.user} (id={client.user.id})")
    log(f"[startup] Use {TEST_COMMAND} in channel {EVENTS_CHANNEL_ID} to test sending.")
    log(f"[startup] Use {RIOT_TEST_COMMAND} in channel {EVENTS_CHANNEL_ID} to test Riot API access.")
    log(
        f"[startup] Use {MOOD_COMMAND} in channel {DAILY_REPORT_CHANNEL_ID} for results "
        f"since {REPORT_DAY_START_HOUR:02d}:00."
    )
    log(
        f"[startup] Use {WEEK_COMMAND} in channel {WEEKLY_REPORT_CHANNEL_ID}; "
        f"it publishes in {WEEKLY_REPORT_CHANNEL_ID} "
        f"for Monday {REPORT_DAY_START_HOUR:02d}:00 -> next Monday {REPORT_DAY_START_HOUR:02d}:00."
    )
    log(f"[startup] Use {ADD_COMMAND} <Name#Tag> in channel {EVENTS_CHANNEL_ID} to add a player at runtime.")
    log(f"[startup] Use {REMOVE_COMMAND} <Name#Tag> in channel {EVENTS_CHANNEL_ID} to remove a player at runtime.")
    log(f"[startup] Use {DEBUG_PLAYER_COMMAND} <Name#Tag> in channel {EVENTS_CHANNEL_ID} to inspect queue bucket mapping.")
    log(f"[startup] Use {HEALTH_COMMAND} in channel {EVENTS_CHANNEL_ID} for health status.")
    log(f"[startup] Use {BACKFILL_COMMAND} <YYYY-MM-DD> <YYYY-MM-DD> in channel {EVENTS_CHANNEL_ID} for cache backfill.")
    log(f"[startup] Loaded {len(FRIENDS)} tracked players from postgres.")
    log("[startup] Player store: postgres")
    log(f"[startup] Report timezone: {REPORT_TIMEZONE_NAME}")
    log(f"[startup] LOG_RIOT_REQUESTS={LOG_RIOT_REQUESTS}")
    log(f"[startup] LOG_JSON={LOG_JSON}")
    log(f"[startup] RIOT_PLATFORM_ROUTING={RIOT_PLATFORM_ROUTING}")
    log(f"[startup] RIOT_REGIONAL_ROUTING={RIOT_REGIONAL_ROUTING}")
    log(f"[startup] MAX_TODAY_MATCH_DETAILS={MAX_TODAY_MATCH_DETAILS}")
    log(f"[startup] REPORT_DAY_START_HOUR={REPORT_DAY_START_HOUR}")
    log(f"[startup] MAX_MATCH_IDS_SCAN={MAX_MATCH_IDS_SCAN}")
    log(f"[startup] MAX_IN_MEMORY_MATCH_CACHE={MAX_IN_MEMORY_MATCH_CACHE}")
    log(f"[startup] REPORT_CACHE_SECONDS={REPORT_CACHE_SECONDS}")
    log(f"[startup] MATCH_CACHE_RETENTION_DAYS={MATCH_CACHE_RETENTION_DAYS}")
    log(f"[startup] EVENTS_CHANNEL_ID={EVENTS_CHANNEL_ID}")
    log(f"[startup] WEEKLY_REPORT_CHANNEL_ID={WEEKLY_REPORT_CHANNEL_ID}")
    log(f"[startup] MATCH_RECAP_CHANNEL_ID={MATCH_RECAP_CHANNEL_ID}")
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
        if BACKGROUND_STALL_TASK is None or BACKGROUND_STALL_TASK.done():
            BACKGROUND_STALL_TASK = client.loop.create_task(background_worker_stall_notifier())
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
        db_remove_player=db_remove_player,
        log=log,
        weekly_report_channel_id=WEEKLY_REPORT_CHANNEL_ID,
        events_channel_id=EVENTS_CHANNEL_ID,
        resolve_channel=resolve_channel,
        worker_stats=WORKER_STATS,
        db_get_state=db_get_state,
        db_set_state=db_set_state,
        match_recap_channel_id=MATCH_RECAP_CHANNEL_ID,
    )


def main():
    client.run(TOKEN)


if __name__ == "__main__":
    main()















