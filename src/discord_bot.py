import asyncio
import contextvars
import json
import time
import uuid
from datetime import datetime, timedelta

import discord
import requests

from src import config as cfg
from src import db as dbm
from src.constants import ADD_COMMAND, DEBUG_PLAYER_COMMAND, HEALTH_COMMAND, MOOD_COMMAND, RIOT_TEST_COMMAND, TEST_COMMAND
from src.mood_service import MoodService
from src.riot_api import RiotApiClient
from src.report_logic import get_match_end_unix_seconds


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


def create_request_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def report_signature(text):
    lines = [line for line in text.splitlines() if not line.startswith("_Last updated:")]
    return "\n".join(lines)


TOKEN = cfg.TOKEN
RIOT_API_KEY = cfg.RIOT_API_KEY
CHANNEL_ID = cfg.CHANNEL_ID
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
db_get_last_seen_match_id = dbm.db_get_last_seen_match_id
db_get_match_info = dbm.db_get_match_info
db_get_puuid = dbm.db_get_puuid
db_health_stats = dbm.db_health_stats
db_get_daily_stats_for_player = dbm.db_get_daily_stats_for_player
db_load_latest_stats = dbm.db_load_latest_stats
db_load_tracked_players = dbm.db_load_tracked_players
db_set_last_report_message = dbm.db_set_last_report_message
db_set_last_seen_match_id = dbm.db_set_last_seen_match_id
db_set_state = dbm.db_set_state
db_get_state = dbm.db_get_state
db_upsert_daily_stats = dbm.db_upsert_daily_stats
db_upsert_match_info = dbm.db_upsert_match_info
db_upsert_player = dbm.db_upsert_player
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
STARTUP_SCOREBOARD_INIT_DONE = False
BACKGROUND_REFRESH_TASK = None
BACKGROUND_RECAP_TASK = None


async def send_riot_key_expired_alert():
    channel = await resolve_channel(CHANNEL_ID)
    if channel is None:
        return
    await channel.send(
        "@NoxVain âš ï¸ Riot API returned 401 Unauthorized. "
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
    db_upsert_daily_stats=db_upsert_daily_stats,
    db_get_daily_stats_for_player=db_get_daily_stats_for_player,
    db_get_last_seen_match_id=db_get_last_seen_match_id,
    db_set_last_seen_match_id=db_set_last_seen_match_id,
    db_health_stats=db_health_stats,
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


async def daily_report(channel):
    report_text = await mood_service.build_today_win_rate_report()
    report_message = await get_or_create_report_message(channel, report_text)
    if report_message.content != report_text:
        await report_message.edit(content=report_text)
        log(f"[scheduler] Updated scoreboard message {report_message.id}.")
    else:
        log(f"[scheduler] No scoreboard change; skipped update for {report_message.id}.")


def match_recap_state_key(riot_id):
    return f"last_announced_match_id::{riot_id.casefold()}"


def format_recap_queue_name(queue_id):
    if queue_id == 420:
        return "\U0001F3C6 Ranked Solo/Duo"
    if queue_id == 440:
        return "\U0001F3C6 Ranked Flex"
    return f"\U0001F3AF Queue {queue_id}"


def format_recap_player_line(riot_id, participant, match_duration_seconds):
    lol_name = riot_id.split("#", 1)[0]
    won = bool(participant.get("win"))
    result_label = "Win" if won else "Loss"
    result_emoji = "\u2705" if won else "\u274C"
    champion = participant.get("championName", "Unknown")
    kills = int(participant.get("kills", 0) or 0)
    deaths = int(participant.get("deaths", 0) or 0)
    assists = int(participant.get("assists", 0) or 0)
    cs = int(participant.get("totalMinionsKilled", 0) or 0) + int(participant.get("neutralMinionsKilled", 0) or 0)
    minutes = max(1.0, float(match_duration_seconds) / 60.0)
    cs_per_min = cs / minutes
    player_damage = int(participant.get("totalDamageDealtToChampions", 0) or 0)
    objective_damage = int(participant.get("damageDealtToObjectives", 0) or 0)
    vision_score = int(participant.get("visionScore", 0) or 0)
    return (
        f"{result_emoji} **{lol_name}** | **{champion}** ({result_label})\n"
        f"   \u2694\uFE0F `K/D/A {kills}/{deaths}/{assists}`  \U0001F33E `CS/min {cs_per_min:.1f}`\n"
        f"   \U0001F4A5 `Damage {player_damage:,}`  \U0001F3F0 `Objectives {objective_damage:,}`  \U0001F441\uFE0F `Vision {vision_score}`"
    )


async def background_match_recap_notifier():
    if MATCH_RECAP_CHANNEL_ID is None:
        return

    while not client.is_closed():
        token = REQUEST_ID_CONTEXT.set(create_request_id("recap"))
        try:
            channel = await resolve_channel(MATCH_RECAP_CHANNEL_ID)
            if channel is None:
                await asyncio.sleep(max(30, MATCH_RECAP_POLL_SECONDS))
                continue

            puuid_by_riot_id = {}
            matches_to_report = set()
            for riot_id in FRIENDS:
                try:
                    puuid = await riot_client.fetch_puuid(riot_id)
                    puuid_by_riot_id[riot_id] = puuid
                    recent_ids = await riot_client.fetch_recent_match_ids(puuid, count=20)
                    if not recent_ids:
                        continue

                    state_key = match_recap_state_key(riot_id)
                    last_announced = await asyncio.to_thread(db_get_state, state_key)
                    latest_match_id = recent_ids[0]
                    if not last_announced:
                        await asyncio.to_thread(db_set_state, state_key, latest_match_id)
                        continue

                    new_match_ids = mood_service.get_new_match_ids(recent_ids, last_announced)
                    if new_match_ids:
                        matches_to_report.update(new_match_ids)
                    await asyncio.to_thread(db_set_state, state_key, latest_match_id)
                except requests.RequestException as exc:
                    log(f"[recap] Failed while checking {riot_id}: {exc}")

            if matches_to_report:
                puuid_to_riot_id = {puuid: riot_id for riot_id, puuid in puuid_by_riot_id.items()}
                match_entries = []
                for match_id in matches_to_report:
                    try:
                        match_info = await riot_client.fetch_match_info(match_id)
                    except requests.RequestException as exc:
                        log(f"[recap] Failed fetching match {match_id}: {exc}")
                        continue

                    participants = match_info.get("info", {}).get("participants", [])
                    tracked_participants = []
                    for participant in participants:
                        riot_id = puuid_to_riot_id.get(participant.get("puuid"))
                        if riot_id:
                            tracked_participants.append((riot_id, participant))
                    if not tracked_participants:
                        continue

                    end_ts = get_match_end_unix_seconds(match_info)
                    queue_id = int(match_info.get("info", {}).get("queueId", -1))
                    duration_seconds = int(match_info.get("info", {}).get("gameDuration", 0) or 0)
                    if duration_seconds > 10_000:
                        duration_seconds = int(duration_seconds / 1000)
                    match_entries.append((end_ts, match_id, queue_id, duration_seconds, tracked_participants))

                match_entries.sort(key=lambda row: row[0])
                for end_ts, match_id, queue_id, duration_seconds, tracked_participants in match_entries:
                    queue_name = format_recap_queue_name(queue_id)
                    end_local = datetime.fromtimestamp(end_ts, tz=REPORT_TIMEZONE)
                    lines = [
                        "\U0001F3AE **New Match Recap**",
                        f"`{queue_name}` - \U0001F552 `{end_local:%d.%m.%Y %H:%M}`",
                        "",
                    ]
                    for riot_id, participant in sorted(tracked_participants, key=lambda row: row[0].casefold()):
                        lines.append(format_recap_player_line(riot_id, participant, duration_seconds))
                    message = "\n".join(lines)
                    if len(message) > 2000:
                        message = message[:1950] + "\n..."
                    await channel.send(message)
                    log(f"[recap] Posted new match recap for {match_id} in channel {MATCH_RECAP_CHANNEL_ID}.")

                if DB_ENABLED:
                    try:
                        await mood_service.refresh_recent_matches_snapshot(recent_count=20)
                        await edit_last_report_message(bypass_cache=True)
                        log("[recap] Synced daily report after posting new match recap(s).")
                    except Exception as exc:
                        log(f"[recap] Failed to sync daily report after recap: {exc}")
        except Exception as exc:
            log(f"[recap] Unexpected error: {exc}")
        finally:
            REQUEST_ID_CONTEXT.reset(token)
        await asyncio.sleep(max(30, MATCH_RECAP_POLL_SECONDS))


async def background_daily_refresher():
    global LAST_CACHE_CLEANUP_AT
    if not DB_ENABLED:
        return
    while not client.is_closed():
        token = REQUEST_ID_CONTEXT.set(create_request_id("bg"))
        try:
            last_snapshot_push_at = 0.0
            last_snapshot_signature = None
            snapshot_push_interval = 120.0
            changed_push_min_interval = 30.0

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
        await asyncio.sleep(max(30, DAILY_REFRESH_SECONDS))


@client.event
async def on_ready():
    global BACKGROUND_REFRESH_TASK, BACKGROUND_RECAP_TASK, STARTUP_SCOREBOARD_INIT_DONE
    log(f"[startup] Logged in as {client.user} (id={client.user.id})")
    log(f"[startup] Use {TEST_COMMAND} in channel {CHANNEL_ID} to test sending.")
    log(f"[startup] Use {RIOT_TEST_COMMAND} in channel {CHANNEL_ID} to test Riot API access.")
    log(
        f"[startup] Use {MOOD_COMMAND} in channel {CHANNEL_ID} for results "
        f"since {REPORT_DAY_START_HOUR:02d}:00."
    )
    log(f"[startup] Use {ADD_COMMAND} <Name#Tag> to add a player at runtime.")
    log(f"[startup] Use {DEBUG_PLAYER_COMMAND} <Name#Tag> to inspect queue bucket mapping.")
    log(f"[startup] Use {HEALTH_COMMAND} in channel {CHANNEL_ID} for health status.")
    log(f"[startup] Loaded {len(FRIENDS)} tracked players from postgres.")
    log("[startup] Player store: postgres")
    log(f"[startup] Report timezone: {REPORT_TIMEZONE_NAME}")
    log(f"[startup] LOG_RIOT_REQUESTS={LOG_RIOT_REQUESTS}")
    log(f"[startup] LOG_JSON={LOG_JSON}")
    log(f"[startup] MAX_TODAY_MATCH_DETAILS={MAX_TODAY_MATCH_DETAILS}")
    log(f"[startup] REPORT_DAY_START_HOUR={REPORT_DAY_START_HOUR}")
    log(f"[startup] MAX_MATCH_IDS_SCAN={MAX_MATCH_IDS_SCAN}")
    log(f"[startup] MAX_IN_MEMORY_MATCH_CACHE={MAX_IN_MEMORY_MATCH_CACHE}")
    log(f"[startup] REPORT_CACHE_SECONDS={REPORT_CACHE_SECONDS}")
    log(f"[startup] MATCH_CACHE_RETENTION_DAYS={MATCH_CACHE_RETENTION_DAYS}")
    log(f"[startup] MATCH_RECAP_CHANNEL_ID={MATCH_RECAP_CHANNEL_ID if MATCH_RECAP_CHANNEL_ID else 'disabled'}")
    log(f"[startup] MATCH_RECAP_POLL_SECONDS={MATCH_RECAP_POLL_SECONDS}")
    if MATCH_RECAP_CHANNEL_ID and MATCH_RECAP_CHANNEL_ID == CHANNEL_ID:
        log("[startup] Warning: MATCH_RECAP_CHANNEL_ID equals DISCORD_CHANNEL_ID.")
    if DB_ENABLED:
        log(f"[startup] DAILY_REFRESH_SECONDS={DAILY_REFRESH_SECONDS}")
        if BACKGROUND_REFRESH_TASK is None or BACKGROUND_REFRESH_TASK.done():
            BACKGROUND_REFRESH_TASK = client.loop.create_task(background_daily_refresher())
        if MATCH_RECAP_CHANNEL_ID and (BACKGROUND_RECAP_TASK is None or BACKGROUND_RECAP_TASK.done()):
            BACKGROUND_RECAP_TASK = client.loop.create_task(background_match_recap_notifier())
    if not STARTUP_SCOREBOARD_INIT_DONE:
        channel = await resolve_channel(CHANNEL_ID)
        if channel is not None:
            try:
                await daily_report(channel)
                log(f"[startup] Initialized scoreboard in channel {CHANNEL_ID}.")
            except Exception as exc:
                log(f"[startup] Failed to initialize scoreboard: {exc}")
        STARTUP_SCOREBOARD_INIT_DONE = True
@client.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return

    content = message.content.strip()
    content_lower = content.casefold()

    if content == TEST_COMMAND:
        await message.channel.send("API test: MoodBot is online and can send messages.")
        log(f"[test] Sent API test message in channel {CHANNEL_ID}.")
        return

    if content == RIOT_TEST_COMMAND:
        if not FRIENDS:
            await message.channel.send("Riot API test skipped: no tracked players in database. Add one with `!Add Name#Tag`.")
            return
        try:
            riot_id, puuid, match_count = await riot_client.run_riot_connectivity_test(FRIENDS[0])
            await message.channel.send(
                f"Riot API test OK for `{riot_id}`. Retrieved puuid and {match_count} matches."
            )
            log(f"[test] Riot API test succeeded for {riot_id} ({puuid[:8]}...).")
        except (KeyError, requests.RequestException) as exc:
            await message.channel.send(f"Riot API test failed: {exc}")
            log(f"[test] Riot API test failed: {exc}")
        return

    if content_lower == HEALTH_COMMAND.casefold():
        stats = await mood_service.run_health_check(START_MONOTONIC)
        uptime = str(timedelta(seconds=stats["uptime_seconds"]))
        await message.channel.send(
            (
                f"Health OK\n"
                f"- Uptime: `{uptime}`\n"
                f"- Tracked players: `{stats['tracked_players']}`\n"
                f"- DB: `{'ok' if stats['db_ok'] else 'down'}`\n"
                f"- Match cache entries: `{stats['match_cache_entries']}`\n"
                f"- Report cache active: `{'yes' if stats['request_cache_active'] else 'no'}`"
            )
        )
        log("[health] Sent health status message.")
        return

    if content_lower == DEBUG_PLAYER_COMMAND.casefold():
        await message.channel.send("Usage: `!DebugPlayer Name#Tag`")
        return

    if content_lower.startswith(f"{DEBUG_PLAYER_COMMAND.casefold()} "):
        raw_riot_id = content[len(DEBUG_PLAYER_COMMAND):].strip()
        status_message = await message.channel.send(f"â³ Building debug report for `{raw_riot_id}`...")
        try:
            report_text = await riot_client.build_debug_player_report(
                raw_riot_id,
                report_timezone_name=REPORT_TIMEZONE_NAME,
                normalize_riot_id=normalize_riot_id,
            )
            if len(report_text) > 1900:
                report_text = report_text[:1900] + "\n...truncated..."
            await status_message.edit(content=f"```text\n{report_text}\n```")
        except ValueError as exc:
            await status_message.edit(content=f"Debug report failed: {exc}")
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Debug report failed: {exc}")
            log(f"[debug] Debug report failed: {exc}")
        return

    if content_lower == MOOD_COMMAND.casefold():
        request_id = create_request_id("mood")
        token = REQUEST_ID_CONTEXT.set(request_id)
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log(f"[mood] Could not delete command message {message.id}: {exc}")

        try:
            if MOOD_REQUEST_LOCK.locked():
                await message.channel.send("â³ A mood report is already in progress. Please wait for it to finish.")
                return

            async with MOOD_REQUEST_LOCK:
                loading_text = (
                    f"â³ Gathering match results since {REPORT_DAY_START_HOUR:02d}:00 from Riot..."
                )
                status_message = await get_or_create_report_message(message.channel, loading_text)
                if status_message.content != loading_text:
                    await status_message.edit(content=loading_text)
                try:
                    if DB_ENABLED:
                        snapshot_text = await mood_service.build_today_win_rate_report()
                        refresh_note = "_Refreshing latest matches..._"
                        snapshot_with_note = f"{snapshot_text}\n\n{refresh_note}"
                        if len(snapshot_with_note) > 2000:
                            snapshot_with_note = snapshot_text
                        await status_message.edit(content=snapshot_with_note)
                        displayed_text = snapshot_with_note
                        remember_report_message(status_message)
                        log(f"[mood] Sent stored snapshot report in channel {CHANNEL_ID}.")

                        await mood_service.refresh_recent_matches_snapshot(recent_count=20)
                        refreshed_text = await mood_service.build_today_win_rate_report()
                        if refreshed_text != displayed_text:
                            await status_message.edit(content=refreshed_text)
                            log(f"[mood] Updated report after quick refresh in channel {CHANNEL_ID}.")
                        else:
                            log("[mood] Quick refresh produced no visible report change.")
                    else:
                        async def progress(done, total, last_name):
                            await status_message.edit(
                                content=(
                                    f"â³ Gathering match results since {REPORT_DAY_START_HOUR:02d}:00 "
                                    f"from Riot... ({done}/{total}) `{last_name}`"
                                )
                            )

                        report_text = await mood_service.build_today_win_rate_report(progress_callback=progress)
                        await status_message.edit(content=report_text)
                        remember_report_message(status_message)
                        log(
                            f"[mood] Sent cycle win rate report (since {REPORT_DAY_START_HOUR:02d}:00) "
                            f"in channel {CHANNEL_ID}."
                        )
                except (KeyError, requests.RequestException) as exc:
                    await status_message.edit(content=f"Mood report failed: {exc}")
                    log(f"[mood] Mood report failed: {exc}")
                except Exception as exc:
                    await status_message.edit(content=f"Mood report failed unexpectedly: {exc}")
                    log(f"[mood] Unexpected mood report failure: {exc}")
        finally:
            REQUEST_ID_CONTEXT.reset(token)
        return

    if content_lower == ADD_COMMAND.casefold():
        await message.channel.send("Usage: `!Add Name#Tag`")
        return

    if content_lower.startswith(f"{ADD_COMMAND.casefold()} "):
        raw_riot_id = content[len(ADD_COMMAND):].strip()
        try:
            riot_id = normalize_riot_id(raw_riot_id)
        except ValueError as exc:
            await message.channel.send(f"Add failed: {exc}")
            return

        if any(existing.casefold() == riot_id.casefold() for existing in FRIENDS):
            await message.channel.send(f"`{riot_id}` is already tracked.")
            return

        status_message = await message.channel.send(f"â³ Validating `{riot_id}` with Riot API...")
        try:
            await riot_client.fetch_puuid(riot_id)
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Add failed for `{riot_id}`: {exc}")
            return

        FRIENDS.append(riot_id)
        try:
            db_upsert_player(riot_id, None)
        except Exception as exc:
            await status_message.edit(content=f"Added `{riot_id}`, but failed to persist to postgres: {exc}")
            return

        await status_message.edit(
            content=(
                f"âœ… Added `{riot_id}` and saved to postgres. "
                f"Total tracked players: {len(FRIENDS)}"
            )
        )
        mood_service.invalidate_report_cache()
        log(f"[add] Added player {riot_id}.")


def main():
    client.run(TOKEN)


if __name__ == "__main__":
    main()


