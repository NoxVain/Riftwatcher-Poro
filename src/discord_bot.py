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


TOKEN = cfg.TOKEN
RIOT_API_KEY = cfg.RIOT_API_KEY
CHANNEL_ID = cfg.CHANNEL_ID
REPORT_TIMEZONE_NAME = cfg.REPORT_TIMEZONE_NAME
REPORT_TIMEZONE = cfg.REPORT_TIMEZONE
LOG_RIOT_REQUESTS = cfg.LOG_RIOT_REQUESTS
LOG_JSON = cfg.LOG_JSON
MAX_MATCHES_PER_PLAYER = cfg.MAX_MATCHES_PER_PLAYER
REPORT_CACHE_SECONDS = cfg.REPORT_CACHE_SECONDS
MAX_TODAY_MATCH_DETAILS = cfg.MAX_TODAY_MATCH_DETAILS
DAILY_REFRESH_SECONDS = cfg.DAILY_REFRESH_SECONDS
MATCH_CACHE_RETENTION_DAYS = cfg.MATCH_CACHE_RETENTION_DAYS
DB_ENABLED = dbm.DB_ENABLED

normalize_riot_id = cfg.normalize_riot_id

db_cleanup_old_match_cache = dbm.db_cleanup_old_match_cache
db_get_last_report_message = dbm.db_get_last_report_message
db_get_last_seen_match_id = dbm.db_get_last_seen_match_id
db_get_match_info = dbm.db_get_match_info
db_get_puuid = dbm.db_get_puuid
db_health_stats = dbm.db_health_stats
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


def save_tracked_players(players):
    for riot_id in players:
        db_upsert_player(riot_id, None)


init_db()
FRIENDS = load_tracked_players()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
MOOD_REQUEST_LOCK = asyncio.Lock()

LAST_REPORT_MESSAGE = {"channel_id": None, "message_id": None}
STARTUP_SCOREBOARD_INIT_DONE = False
BACKGROUND_REFRESH_TASK = None
SCHEDULER_TASK = None


async def send_riot_key_expired_alert():
    channel = await resolve_channel(CHANNEL_ID)
    if channel is None:
        return
    await channel.send(
        "@NoxVain ⚠️ Riot API returned 401 Unauthorized. "
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
    max_matches_per_player=MAX_MATCHES_PER_PLAYER,
    max_today_match_details=MAX_TODAY_MATCH_DETAILS,
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
    report_cache_seconds=REPORT_CACHE_SECONDS,
    daily_refresh_seconds=DAILY_REFRESH_SECONDS,
    db_enabled=DB_ENABLED,
    db_load_latest_stats=db_load_latest_stats,
    db_upsert_daily_stats=db_upsert_daily_stats,
    db_get_last_seen_match_id=db_get_last_seen_match_id,
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


async def edit_last_report_message():
    channel_id = LAST_REPORT_MESSAGE["channel_id"]
    message_id = LAST_REPORT_MESSAGE["message_id"]
    if not channel_id or not message_id:
        return

    channel = await resolve_channel(channel_id)
    if channel is None:
        return

    try:
        report_text = await mood_service.build_today_win_rate_report()
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


async def background_daily_refresher():
    global LAST_CACHE_CLEANUP_AT
    if not DB_ENABLED:
        return
    while not client.is_closed():
        token = REQUEST_ID_CONTEXT.set(create_request_id("bg"))
        try:
            await mood_service.refresh_daily_stats_once()
            await edit_last_report_message()
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


async def scheduler():
    await client.wait_until_ready()
    channel = await resolve_channel(CHANNEL_ID)
    if channel is None:
        return

    while not client.is_closed():
        now = datetime.now()
        target = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now > target:
            target += timedelta(days=1)

        await asyncio.sleep((target - now).total_seconds())
        await daily_report(channel)


@client.event
async def on_ready():
    global BACKGROUND_REFRESH_TASK, SCHEDULER_TASK, STARTUP_SCOREBOARD_INIT_DONE
    log(f"[startup] Logged in as {client.user} (id={client.user.id})")
    log(f"[startup] Use {TEST_COMMAND} in channel {CHANNEL_ID} to test sending.")
    log(f"[startup] Use {RIOT_TEST_COMMAND} in channel {CHANNEL_ID} to test Riot API access.")
    log(f"[startup] Use {MOOD_COMMAND} in channel {CHANNEL_ID} for last 24h win rates.")
    log(f"[startup] Use {ADD_COMMAND} <Name#Tag> to add a player at runtime.")
    log(f"[startup] Use {DEBUG_PLAYER_COMMAND} <Name#Tag> to inspect queue bucket mapping.")
    log(f"[startup] Use {HEALTH_COMMAND} in channel {CHANNEL_ID} for health status.")
    log(f"[startup] Loaded {len(FRIENDS)} tracked players from postgres.")
    log("[startup] Player store: postgres")
    log(f"[startup] Report timezone: {REPORT_TIMEZONE_NAME}")
    log(f"[startup] LOG_RIOT_REQUESTS={LOG_RIOT_REQUESTS}")
    log(f"[startup] LOG_JSON={LOG_JSON}")
    log(f"[startup] MAX_MATCHES_PER_PLAYER={MAX_MATCHES_PER_PLAYER}")
    log(f"[startup] MAX_TODAY_MATCH_DETAILS={MAX_TODAY_MATCH_DETAILS}")
    log(f"[startup] REPORT_CACHE_SECONDS={REPORT_CACHE_SECONDS}")
    log(f"[startup] MATCH_CACHE_RETENTION_DAYS={MATCH_CACHE_RETENTION_DAYS}")
    if DB_ENABLED:
        log(f"[startup] DAILY_REFRESH_SECONDS={DAILY_REFRESH_SECONDS}")
        if BACKGROUND_REFRESH_TASK is None or BACKGROUND_REFRESH_TASK.done():
            BACKGROUND_REFRESH_TASK = client.loop.create_task(background_daily_refresher())
    if not STARTUP_SCOREBOARD_INIT_DONE:
        channel = await resolve_channel(CHANNEL_ID)
        if channel is not None:
            try:
                await daily_report(channel)
                log(f"[startup] Initialized scoreboard in channel {CHANNEL_ID}.")
            except Exception as exc:
                log(f"[startup] Failed to initialize scoreboard: {exc}")
        STARTUP_SCOREBOARD_INIT_DONE = True
    if SCHEDULER_TASK is None or SCHEDULER_TASK.done():
        SCHEDULER_TASK = client.loop.create_task(scheduler())


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
        status_message = await message.channel.send(f"⏳ Building debug report for `{raw_riot_id}`...")
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
                await message.channel.send("⏳ A mood report is already in progress. Please wait for it to finish.")
                return

            async with MOOD_REQUEST_LOCK:
                loading_text = "⏳ Gathering last 24h match results from Riot..."
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

                        await mood_service.refresh_recent_matches_snapshot(recent_count=1)
                        refreshed_text = await mood_service.build_today_win_rate_report()
                        if refreshed_text != displayed_text:
                            await status_message.edit(content=refreshed_text)
                            log(f"[mood] Updated report after quick refresh in channel {CHANNEL_ID}.")
                        else:
                            log("[mood] Quick refresh produced no visible report change.")
                    else:
                        async def progress(done, total, last_name):
                            await status_message.edit(
                                content=f"⏳ Gathering last 24h match results from Riot... ({done}/{total}) `{last_name}`"
                            )

                        report_text = await mood_service.build_today_win_rate_report(progress_callback=progress)
                        await status_message.edit(content=report_text)
                        remember_report_message(status_message)
                        log(f"[mood] Sent last 24h win rate report in channel {CHANNEL_ID}.")
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

        status_message = await message.channel.send(f"⏳ Validating `{riot_id}` with Riot API...")
        try:
            await riot_client.fetch_puuid(riot_id)
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Add failed for `{riot_id}`: {exc}")
            return

        FRIENDS.append(riot_id)
        try:
            save_tracked_players(FRIENDS)
        except Exception as exc:
            await status_message.edit(content=f"Added `{riot_id}`, but failed to persist to postgres: {exc}")
            return

        await status_message.edit(
            content=(
                f"✅ Added `{riot_id}` and saved to postgres. "
                f"Total tracked players: {len(FRIENDS)}"
            )
        )
        mood_service.invalidate_report_cache()
        log(f"[add] Added player {riot_id}.")


def main():
    client.run(TOKEN)


if __name__ == "__main__":
    main()
