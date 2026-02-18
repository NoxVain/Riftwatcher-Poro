import asyncio
import contextvars
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import discord
import requests
try:
    import psycopg
except ImportError:
    psycopg = None

from src.constants import ADD_COMMAND, DEBUG_PLAYER_COMMAND, HEALTH_COMMAND, MOOD_COMMAND, RIOT_TEST_COMMAND, TEST_COMMAND
from src.report_logic import (
    create_mode_records,
    format_mode_line,
    get_match_end_unix_seconds,
    get_mode_bucket,
    get_mode_totals,
    is_match_in_last_24h,
    rank_sort_key,
    wilson_lower_bound,
)


def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_riot_friends(raw_value):
    friends = [entry.strip() for entry in raw_value.split(",") if entry.strip()]
    if not friends:
        raise RuntimeError("RIOT_FRIENDS cannot be empty.")
    return friends


def normalize_riot_id(raw_riot_id):
    riot_id = raw_riot_id.strip()
    if riot_id.count("#") != 1:
        raise ValueError("Riot ID must be in the format `Name#Tag`.")

    game_name, tag_line = riot_id.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        raise ValueError("Riot ID must include both name and tag, like `Name#Tag`.")
    return f"{game_name}#{tag_line}"


def get_env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


TOKEN = require_env("DISCORD_TOKEN")
RIOT_API_KEY = require_env("RIOT_API_KEY")
CHANNEL_ID = int(require_env("DISCORD_CHANNEL_ID"))
REPORT_TIMEZONE_NAME = os.getenv("REPORT_TIMEZONE", "UTC")
try:
    REPORT_TIMEZONE = ZoneInfo(REPORT_TIMEZONE_NAME)
except Exception as exc:
    raise RuntimeError(f"Invalid REPORT_TIMEZONE '{REPORT_TIMEZONE_NAME}': {exc}") from exc

LOG_RIOT_REQUESTS = get_env_bool("LOG_RIOT_REQUESTS", False)
LOG_JSON = get_env_bool("LOG_JSON", False)
MAX_MATCHES_PER_PLAYER = int(os.getenv("MAX_MATCHES_PER_PLAYER", "25"))
REPORT_CACHE_SECONDS = int(os.getenv("REPORT_CACHE_SECONDS", "120"))
MAX_TODAY_MATCH_DETAILS = int(os.getenv("MAX_TODAY_MATCH_DETAILS", "20"))
DAILY_REFRESH_SECONDS = int(os.getenv("DAILY_REFRESH_SECONDS", "300"))
MATCH_CACHE_RETENTION_DAYS = int(os.getenv("MATCH_CACHE_RETENTION_DAYS", "31"))
DATABASE_URL = require_env("DATABASE_URL")
if psycopg is None:
    raise RuntimeError("DATABASE_URL is set but psycopg is not installed. Add psycopg[binary] to dependencies.")
DB_ENABLED = True
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_POOL = None
DB_POOL_LOCK = threading.Lock()
DB_POOL_TOTAL = 0
REQUEST_ID_CONTEXT = contextvars.ContextVar("request_id", default=None)
START_MONOTONIC = time.monotonic()
LAST_CACHE_CLEANUP_AT = 0.0
RIOT_401_ALERT_SENT = False
RIOT_ALERT_LOCK = threading.Lock()


def create_request_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def send_riot_key_expired_alert():
    channel = await resolve_channel(CHANNEL_ID)
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
    with RIOT_ALERT_LOCK:
        if riot_401_alert_already_sent():
            return
        mark_riot_401_alert_sent()
    try:
        loop = client.loop
        asyncio.run_coroutine_threadsafe(send_riot_key_expired_alert(), loop)
    except Exception as exc:
        log(f"[riot] Could not schedule key-expiry alert: {exc}")


def get_default_friends():
    return parse_riot_friends(
        os.getenv(
            "RIOT_FRIENDS",
            (
                "NoxVain#EUW,"
                "Tamarin#EUW,"
                "Follow The King#EUW,"
                "Reodor Felgen#EUW,"
                "Not a snake#EUW,"
                "\u00C7\u00DB\u039C\u00CC\u0143\u039C\u00DD\u00C4\u0160\u0160#\u00C3\u00CE\u00D0\u015A,"
                "xXsnakemanXx#EUW,"
                "JonastCvuHU#UNC"
            ),
        )
    )


def init_db_pool():
    global DB_POOL
    if not DB_ENABLED:
        return
    if DB_POOL is None:
        DB_POOL = queue.LifoQueue(maxsize=max(1, DB_POOL_SIZE))


def create_db_connection():
    return psycopg.connect(DATABASE_URL)


def db_acquire_connection():
    global DB_POOL_TOTAL
    if DB_POOL is None:
        init_db_pool()
    try:
        return DB_POOL.get_nowait()
    except queue.Empty:
        pass

    with DB_POOL_LOCK:
        if DB_POOL_TOTAL < max(1, DB_POOL_SIZE):
            DB_POOL_TOTAL += 1
            return create_db_connection()

    return DB_POOL.get()


def db_release_connection(conn, discard=False):
    global DB_POOL_TOTAL
    if conn is None:
        return
    if discard:
        try:
            conn.close()
        finally:
            with DB_POOL_LOCK:
                DB_POOL_TOTAL = max(0, DB_POOL_TOTAL - 1)
        return
    try:
        DB_POOL.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        finally:
            with DB_POOL_LOCK:
                DB_POOL_TOTAL = max(0, DB_POOL_TOTAL - 1)


def db_execute(query, params=None, fetch=False, fetchone=False):
    if not DB_ENABLED:
        return None
    conn = db_acquire_connection()
    discard_conn = False
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetchone:
                result = cur.fetchone()
                conn.commit()
                return result
            if fetch:
                result = cur.fetchall()
                conn.commit()
                return result
        conn.commit()
    except Exception:
        discard_conn = True
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        db_release_connection(conn, discard=discard_conn)
    return None


def init_db():
    if not DB_ENABLED:
        return
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_players (
            riot_id TEXT PRIMARY KEY,
            puuid TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS player_daily_stats (
            day_date DATE NOT NULL,
            riot_id TEXT NOT NULL,
            solo_wins INTEGER NOT NULL DEFAULT 0,
            solo_losses INTEGER NOT NULL DEFAULT 0,
            flex_wins INTEGER NOT NULL DEFAULT 0,
            flex_losses INTEGER NOT NULL DEFAULT 0,
            arcade_wins INTEGER NOT NULL DEFAULT 0,
            arcade_losses INTEGER NOT NULL DEFAULT 0,
            total_wins INTEGER NOT NULL DEFAULT 0,
            total_losses INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (day_date, riot_id)
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS match_info_cache (
            match_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def db_set_state(state_key, state_value):
    if not DB_ENABLED:
        return
    db_execute(
        """
        INSERT INTO bot_state (state_key, state_value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (state_key)
        DO UPDATE SET state_value = EXCLUDED.state_value, updated_at = NOW();
        """,
        (state_key, state_value),
    )


def db_get_state(state_key):
    if not DB_ENABLED:
        return None
    row = db_execute(
        "SELECT state_value FROM bot_state WHERE state_key = %s LIMIT 1;",
        (state_key,),
        fetchone=True,
    )
    if not row:
        return None
    return row[0]


def db_set_last_report_message(channel_id, message_id):
    if not DB_ENABLED:
        return
    db_set_state("last_report_channel_id", str(channel_id))
    db_set_state("last_report_message_id", str(message_id))


def db_get_last_report_message():
    if not DB_ENABLED:
        return None, None
    channel_id = db_get_state("last_report_channel_id")
    message_id = db_get_state("last_report_message_id")
    if not channel_id or not message_id:
        return None, None
    try:
        return int(channel_id), int(message_id)
    except ValueError:
        return None, None


def db_last_seen_match_key(riot_id):
    return f"last_seen_match_id::{riot_id.casefold()}"


def db_get_last_seen_match_id(riot_id):
    if not DB_ENABLED:
        return None
    return db_get_state(db_last_seen_match_key(riot_id))


def db_set_last_seen_match_id(riot_id, match_id):
    if not DB_ENABLED:
        return
    db_set_state(db_last_seen_match_key(riot_id), match_id)


def db_get_match_info(match_id):
    if not DB_ENABLED:
        return None
    row = db_execute(
        "SELECT payload FROM match_info_cache WHERE match_id = %s LIMIT 1;",
        (match_id,),
        fetchone=True,
    )
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def db_upsert_match_info(match_id, match_info):
    if not DB_ENABLED:
        return
    payload = json.dumps(match_info, separators=(",", ":"))
    db_execute(
        """
        INSERT INTO match_info_cache (match_id, payload, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (match_id)
        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW();
        """,
        (match_id, payload),
    )


def db_cleanup_old_match_cache(retention_days):
    if not DB_ENABLED:
        return 0
    row = db_execute(
        """
        DELETE FROM match_info_cache
        WHERE updated_at < NOW() - (%s * INTERVAL '1 day')
        RETURNING match_id;
        """,
        (max(1, retention_days),),
        fetch=True,
    ) or []
    return len(row)


def db_health_stats():
    if not DB_ENABLED:
        return {"db_ok": False, "match_cache_entries": 0}
    ping = db_execute("SELECT 1;", fetchone=True)
    count_row = db_execute("SELECT COUNT(*) FROM match_info_cache;", fetchone=True)
    count = int(count_row[0]) if count_row and count_row[0] is not None else 0
    return {"db_ok": bool(ping and ping[0] == 1), "match_cache_entries": count}


def db_upsert_player(riot_id, puuid=None):
    if not DB_ENABLED:
        return
    db_execute(
        """
        INSERT INTO tracked_players (riot_id, puuid, created_at, updated_at)
        VALUES (%s, %s, NOW(), NOW())
        ON CONFLICT (riot_id)
        DO UPDATE SET
            puuid = COALESCE(EXCLUDED.puuid, tracked_players.puuid),
            updated_at = NOW();
        """,
        (riot_id, puuid),
    )


def db_get_puuid(riot_id):
    if not DB_ENABLED:
        return None
    row = db_execute(
        "SELECT puuid FROM tracked_players WHERE lower(riot_id) = lower(%s) LIMIT 1;",
        (riot_id,),
        fetchone=True,
    )
    if row and row[0]:
        return row[0]
    return None


def db_load_tracked_players():
    if not DB_ENABLED:
        return []
    rows = db_execute("SELECT riot_id FROM tracked_players ORDER BY riot_id;", fetch=True) or []
    return [row[0] for row in rows]


def db_upsert_daily_stats(day_date, riot_id, mode_records):
    if not DB_ENABLED:
        return
    solo_wins = mode_records["solo_duo"]["wins"]
    solo_losses = mode_records["solo_duo"]["losses"]
    flex_wins = mode_records["flex"]["wins"]
    flex_losses = mode_records["flex"]["losses"]
    arcade_wins = mode_records["arcade"]["wins"]
    arcade_losses = mode_records["arcade"]["losses"]
    total_wins = solo_wins + flex_wins + arcade_wins
    total_losses = solo_losses + flex_losses + arcade_losses
    db_execute(
        """
        INSERT INTO player_daily_stats (
            day_date, riot_id,
            solo_wins, solo_losses,
            flex_wins, flex_losses,
            arcade_wins, arcade_losses,
            total_wins, total_losses,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (day_date, riot_id)
        DO UPDATE SET
            solo_wins = EXCLUDED.solo_wins,
            solo_losses = EXCLUDED.solo_losses,
            flex_wins = EXCLUDED.flex_wins,
            flex_losses = EXCLUDED.flex_losses,
            arcade_wins = EXCLUDED.arcade_wins,
            arcade_losses = EXCLUDED.arcade_losses,
            total_wins = EXCLUDED.total_wins,
            total_losses = EXCLUDED.total_losses,
            updated_at = NOW();
        """,
        (
            day_date,
            riot_id,
            solo_wins,
            solo_losses,
            flex_wins,
            flex_losses,
            arcade_wins,
            arcade_losses,
            total_wins,
            total_losses,
        ),
    )


def db_load_latest_stats():
    if not DB_ENABLED:
        return []
    rows = db_execute(
        """
        SELECT DISTINCT ON (lower(riot_id))
            riot_id, solo_wins, solo_losses, flex_wins, flex_losses,
            arcade_wins, arcade_losses, total_wins, total_losses, updated_at
        FROM player_daily_stats
        ORDER BY lower(riot_id), updated_at DESC;
        """,
        fetch=True,
    )
    return rows or []


def load_tracked_players():
    players = db_load_tracked_players()
    if players:
        return players
    defaults = get_default_friends()
    for riot_id in defaults:
        db_upsert_player(riot_id, None)
    return defaults


def save_tracked_players(players):
    for riot_id in players:
        db_upsert_player(riot_id, None)


init_db()
FRIENDS = load_tracked_players()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
MOOD_REQUEST_LOCK = asyncio.Lock()
PUUID_CACHE = {}
MATCH_INFO_CACHE = {}
REPORT_CACHE = {"text": None, "expires_at": 0.0, "day": None}
LAST_REPORT_MESSAGE = {"channel_id": None, "message_id": None}
STARTUP_SCOREBOARD_INIT_DONE = False
BACKGROUND_REFRESH_TASK = None
SCHEDULER_TASK = None


def remember_report_message(message):
    LAST_REPORT_MESSAGE["channel_id"] = message.channel.id
    LAST_REPORT_MESSAGE["message_id"] = message.id
    if DB_ENABLED:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(db_set_last_report_message, message.channel.id, message.id))
        except RuntimeError:
            db_set_last_report_message(message.channel.id, message.id)


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


def riot_get_json(url):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    max_attempts = 4

    for attempt in range(1, max_attempts + 1):
        start_time = time.perf_counter()
        response = requests.get(url, headers=headers, timeout=20)
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if LOG_RIOT_REQUESTS:
            log(f"[riot] {response.status_code} in {elapsed_ms}ms: {url}")

        if response.status_code != 429:
            if response.status_code == 401:
                trigger_riot_key_alert()
            response.raise_for_status()
            return response.json()

        retry_after_header = response.headers.get("Retry-After", "1")
        try:
            retry_after = float(retry_after_header)
        except ValueError:
            retry_after = 1.0

        if attempt == max_attempts:
            response.raise_for_status()

        sleep_seconds = max(1.0, retry_after)
        log(f"[riot] 429 received. attempt={attempt}/{max_attempts}, sleep={sleep_seconds}s")
        time.sleep(sleep_seconds)


async def riot_get_json_async(url):
    return await asyncio.to_thread(riot_get_json, url)


def split_riot_id(riot_id):
    game_name, tag_line = riot_id.split("#", 1)
    return game_name, tag_line


def get_lol_name(riot_id):
    game_name, _ = split_riot_id(riot_id)
    return game_name


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


async def fetch_puuid(riot_id):
    cache_key = riot_id.casefold()
    if cache_key in PUUID_CACHE:
        return PUUID_CACHE[cache_key]

    persisted_puuid = await asyncio.to_thread(db_get_puuid, riot_id)
    if persisted_puuid:
        PUUID_CACHE[cache_key] = persisted_puuid
        return persisted_puuid

    game_name, tag_line = split_riot_id(riot_id)
    encoded_name = quote(game_name, safe="")
    encoded_tag = quote(tag_line, safe="")
    url = (
        "https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{encoded_name}/{encoded_tag}"
    )
    data = await riot_get_json_async(url)
    puuid = data["puuid"]
    PUUID_CACHE[cache_key] = puuid
    await asyncio.to_thread(db_upsert_player, riot_id, puuid)
    return puuid


def get_last_24h_start_unix_seconds():
    now_utc = datetime.now(tz=timezone.utc)
    return int((now_utc - timedelta(hours=24)).timestamp())


async def fetch_match_ids(puuid, start_time_unix):
    count = max(1, min(MAX_MATCHES_PER_PLAYER, 100))
    url = (
        "https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/"
        f"{puuid}/ids?startTime={start_time_unix}&count={count}"
    )
    return await riot_get_json_async(url)


async def fetch_recent_match_ids(puuid, count=20):
    safe_count = max(1, min(count, 100))
    url = (
        "https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/"
        f"{puuid}/ids?count={safe_count}"
    )
    return await riot_get_json_async(url)


async def fetch_match_info(match_id):
    cached = MATCH_INFO_CACHE.get(match_id)
    if cached is not None:
        return cached

    persisted = await asyncio.to_thread(db_get_match_info, match_id)
    if persisted is not None:
        MATCH_INFO_CACHE[match_id] = persisted
        return persisted

    url = f"https://europe.api.riotgames.com/lol/match/v5/matches/{match_id}"
    match_info = await riot_get_json_async(url)
    MATCH_INFO_CACHE[match_id] = match_info
    await asyncio.to_thread(db_upsert_match_info, match_id, match_info)
    return match_info


def get_participant_win(match_info, puuid):
    for participant in match_info["info"]["participants"]:
        if participant["puuid"] == puuid:
            return participant["win"]
    return None


def append_mode_line_if_games(report_lines, label, wins, losses):
    if wins + losses == 0:
        return
    report_lines.append(format_mode_line(label, wins, losses))


async def get_today_mode_records(riot_id):
    player_start = time.perf_counter()
    puuid = await fetch_puuid(riot_id)
    start_time_unix = get_last_24h_start_unix_seconds()
    match_ids = await fetch_match_ids(puuid, start_time_unix)
    if match_ids:
        await asyncio.to_thread(db_set_last_seen_match_id, riot_id, match_ids[0])

    mode_records = create_mode_records()
    today_details_processed = 0
    for match_id in match_ids:
        match_info = await fetch_match_info(match_id)
        if not is_match_in_last_24h(match_info):
            continue

        if today_details_processed >= max(1, MAX_TODAY_MATCH_DETAILS):
            log(
                f"[mood] {riot_id}: reached MAX_TODAY_MATCH_DETAILS={MAX_TODAY_MATCH_DETAILS}, "
                "stopping further today match processing."
            )
            break

        queue_id = match_info["info"].get("queueId", -1)
        bucket_name = get_mode_bucket(queue_id)
        result = get_participant_win(match_info, puuid)
        if result is True:
            mode_records[bucket_name]["wins"] += 1
        elif result is False:
            mode_records[bucket_name]["losses"] += 1
        today_details_processed += 1

    wins, losses = get_mode_totals(mode_records)
    elapsed_ms = int((time.perf_counter() - player_start) * 1000)
    log(
        f"[mood] {riot_id}: matches={len(match_ids)} total={wins}W-{losses}L "
        f"solo={mode_records['solo_duo']['wins']}W-{mode_records['solo_duo']['losses']}L "
        f"flex={mode_records['flex']['wins']}W-{mode_records['flex']['losses']}L "
        f"arcade={mode_records['arcade']['wins']}W-{mode_records['arcade']['losses']}L "
        f"elapsed={elapsed_ms}ms"
    )
    today_key = datetime.now(tz=REPORT_TIMEZONE).date().isoformat()
    await asyncio.to_thread(db_upsert_daily_stats, today_key, riot_id, mode_records)
    return mode_records


def format_report_from_results(ranked_results, error_results, report_start):
    report_lines = ["\u2728------ **LEAGUE MOOD (LAST 24 HOURS)** ------\u2728", ""]
    updated_at = datetime.now(tz=REPORT_TIMEZONE).strftime("%d.%m.%Y %H:%M")
    if not ranked_results and not error_results:
        report_lines.append("Looks like everyone has a life today.")
        report_lines.append("We will keep you up to date of anyone crawls back into the hole.")
        report_lines.append("")
        report_lines.append("\u2728--------------------------------------------\u2728")
        report_lines.append(f"_Last updated: {updated_at}_")
        total_elapsed_ms = int((time.perf_counter() - report_start) * 1000)
        log(
            f"[mood] Report complete: players={len(FRIENDS)} "
            "ranked=0 hidden_no_matches=all errors=0 "
            f"elapsed={total_elapsed_ms}ms"
        )
        return "\n".join(report_lines)

    for index, (lol_name, mode_records, wins, losses, win_rate) in enumerate(ranked_results):
        wilson_score = wilson_lower_bound(wins, losses)
        gamer_score = wilson_score * 100
        if wins + losses > 0 and wilson_score <= 0:
            mood_emoji = "\U0001F480"
        elif wilson_score >= 0.75:
            mood_emoji = "\U0001F601"
        elif wilson_score >= 0.60:
            mood_emoji = "\U0001F60A"
        elif wilson_score >= 0.50:
            mood_emoji = "\U0001F642"
        elif wilson_score >= 0.40:
            mood_emoji = "\U0001F610"
        elif wilson_score >= 0.30:
            mood_emoji = "\U0001F615"
        elif wilson_score >= 0.20:
            mood_emoji = "\U0001F61E"
        else:
            mood_emoji = "\U0001F62D"

        display_emoji = "\u2B50" if index == 0 else mood_emoji
        report_lines.append(f"{display_emoji}  **{lol_name}**  |  Gamer Score: **{gamer_score:.1f}**")
        append_mode_line_if_games(
            report_lines,
            "Ranked Solo/Duo",
            mode_records["solo_duo"]["wins"],
            mode_records["solo_duo"]["losses"],
        )
        append_mode_line_if_games(
            report_lines,
            "Ranked Flex",
            mode_records["flex"]["wins"],
            mode_records["flex"]["losses"],
        )
        append_mode_line_if_games(
            report_lines,
            "Arcade",
            mode_records["arcade"]["wins"],
            mode_records["arcade"]["losses"],
        )
        report_lines.append(f"   Total: `{wins}W-{losses}L` - **{win_rate:.1f}%**")
        report_lines.append("")

    for lol_name, error_text in sorted(error_results, key=lambda row: row[0].lower()):
        report_lines.append(f"\u26AB  **{lol_name}**")
        report_lines.append(f"   Riot error: `{error_text}`")
        report_lines.append("")

    report_lines.append("\u2728--------------------------------------------\u2728")
    report_lines.append(f"_Last updated: {updated_at}_")
    total_elapsed_ms = int((time.perf_counter() - report_start) * 1000)
    log(
        f"[mood] Report complete: players={len(FRIENDS)} "
        f"ranked={len(ranked_results)} hidden_no_matches={len(FRIENDS) - len(ranked_results) - len(error_results)} "
        f"errors={len(error_results)} elapsed={total_elapsed_ms}ms"
    )
    report_text = "\n".join(report_lines)
    if len(report_text) <= 2000:
        return report_text

    compact_lines = ["\u2728------ **LEAGUE MOOD (LAST 24 HOURS)** ------\u2728", ""]
    for index, (lol_name, mode_records, wins, losses, win_rate) in enumerate(ranked_results):
        display_emoji = "\u2B50" if index == 0 else "\U0001F642"
        compact_lines.append(f"{display_emoji}  **{lol_name}**  **`{wins}W-{losses}L` - {win_rate:.1f}%**")

    if error_results:
        compact_lines.append("")
        error_names = ", ".join(name for name, _ in error_results[:6])
        more = "" if len(error_results) <= 6 else f" (+{len(error_results) - 6} more)"
        compact_lines.append(f"\u26AB Riot errors for {len(error_results)} players: {error_names}{more}")

    compact_lines.append("")
    compact_lines.append("\u2728--------------------------------------------\u2728")
    compact_lines.append(f"_Last updated: {updated_at}_")
    compact_text = "\n".join(compact_lines)
    if len(compact_text) > 2000:
        compact_text = compact_text[:1950] + "\n..."
    return compact_text


def simplify_riot_error(exc):
    text = str(exc)
    if "401" in text and "Unauthorized" in text:
        return "401 Unauthorized (check RIOT_API_KEY)"
    if len(text) > 140:
        return text[:137] + "..."
    return text


async def build_today_win_rate_report(progress_callback=None):
    today_key = datetime.now(tz=REPORT_TIMEZONE).date().isoformat()
    now_monotonic = time.monotonic()
    if (
        REPORT_CACHE["text"] is not None
        and REPORT_CACHE["day"] == today_key
        and now_monotonic < REPORT_CACHE["expires_at"]
    ):
        log("[mood] Returning cached report.")
        return REPORT_CACHE["text"]

    report_start = time.perf_counter()
    ranked_results = []
    error_results = []

    if DB_ENABLED:
        stored_rows = await asyncio.to_thread(db_load_latest_stats)
        if stored_rows:
            latest_updated_at = None
            for row in stored_rows:
                row_updated_at = row[9]
                if row_updated_at is None:
                    continue
                if latest_updated_at is None or row_updated_at > latest_updated_at:
                    latest_updated_at = row_updated_at

            snapshot_max_age_seconds = max(600, DAILY_REFRESH_SECONDS * 2)
            snapshot_stale = True
            if latest_updated_at is not None:
                latest_updated_utc = latest_updated_at.astimezone(timezone.utc)
                snapshot_age = datetime.now(tz=timezone.utc) - latest_updated_utc
                snapshot_stale = snapshot_age > timedelta(seconds=snapshot_max_age_seconds)

            if snapshot_stale:
                log("[mood] Snapshot data is stale; falling back to live rebuild.")
                stored_rows = []

        if stored_rows:
            for row in stored_rows:
                riot_id = row[0]
                if not any(p.casefold() == riot_id.casefold() for p in FRIENDS):
                    continue
                mode_records = {
                    "solo_duo": {"wins": row[1], "losses": row[2]},
                    "flex": {"wins": row[3], "losses": row[4]},
                    "arcade": {"wins": row[5], "losses": row[6]},
                }
                wins = row[7]
                losses = row[8]
                total = wins + losses
                if total == 0:
                    continue
                win_rate = (wins / total) * 100
                ranked_results.append((get_lol_name(riot_id), mode_records, wins, losses, win_rate))

            ranked_results.sort(key=rank_sort_key)
            if ranked_results:
                if len(ranked_results) == 1 and len(FRIENDS) > 1:
                    log("[mood] Snapshot looked sparse (1 ranked player); falling back to live rebuild.")
                else:
                    log("[mood] Returning report from postgres daily stats.")
                    report_text = format_report_from_results(ranked_results, error_results, report_start)
                    REPORT_CACHE["text"] = report_text
                    REPORT_CACHE["day"] = today_key
                    REPORT_CACHE["expires_at"] = time.monotonic() + max(0, REPORT_CACHE_SECONDS)
                    return report_text

    MATCH_INFO_CACHE.clear()

    total_players = len(FRIENDS)
    processed_players = 0

    for riot_id in FRIENDS:
        lol_name = get_lol_name(riot_id)
        log(f"[mood] Processing player {lol_name} ({riot_id})")
        try:
            mode_records = await get_today_mode_records(riot_id)
        except requests.RequestException as exc:
            error_results.append((lol_name, simplify_riot_error(exc)))
            log(f"[mood] Player failed {lol_name}: {exc}")
            processed_players += 1
            if progress_callback is not None:
                await progress_callback(processed_players, total_players, lol_name)
            continue

        wins, losses = get_mode_totals(mode_records)
        total = wins + losses
        if total == 0:
            processed_players += 1
            if progress_callback is not None:
                await progress_callback(processed_players, total_players, lol_name)
            continue

        win_rate = (wins / total) * 100
        ranked_results.append((lol_name, mode_records, wins, losses, win_rate))
        processed_players += 1
        if progress_callback is not None:
            await progress_callback(processed_players, total_players, lol_name)

    ranked_results.sort(key=rank_sort_key)

    report_text = format_report_from_results(ranked_results, error_results, report_start)
    REPORT_CACHE["text"] = report_text
    REPORT_CACHE["day"] = today_key
    REPORT_CACHE["expires_at"] = time.monotonic() + max(0, REPORT_CACHE_SECONDS)
    return report_text


async def refresh_daily_stats_once():
    if not DB_ENABLED:
        return
    log("[refresh] Starting daily stats refresh.")
    MATCH_INFO_CACHE.clear()
    for riot_id in FRIENDS:
        try:
            await get_today_mode_records(riot_id)
        except requests.RequestException as exc:
            log(f"[refresh] Failed for {riot_id}: {exc}")
    REPORT_CACHE["text"] = None
    REPORT_CACHE["day"] = None
    REPORT_CACHE["expires_at"] = 0.0
    log("[refresh] Daily stats refresh complete.")


async def refresh_recent_matches_snapshot(recent_count=3):
    if not DB_ENABLED:
        return
    log(f"[refresh] Running on-demand recent refresh (count={recent_count})")
    for riot_id in FRIENDS:
        try:
            puuid = await fetch_puuid(riot_id)
            recent_ids = await fetch_recent_match_ids(puuid, count=max(1, recent_count))
            if not recent_ids:
                continue

            latest_match_id = recent_ids[0]
            last_seen_match_id = await asyncio.to_thread(db_get_last_seen_match_id, riot_id)
            if last_seen_match_id == latest_match_id:
                continue

            await get_today_mode_records(riot_id)
        except requests.RequestException as exc:
            log(f"[refresh] On-demand refresh failed for {riot_id}: {exc}")
    REPORT_CACHE["text"] = None
    REPORT_CACHE["day"] = None
    REPORT_CACHE["expires_at"] = 0.0


async def edit_last_report_message():
    channel_id = LAST_REPORT_MESSAGE["channel_id"]
    message_id = LAST_REPORT_MESSAGE["message_id"]
    if not channel_id or not message_id:
        return

    channel = await resolve_channel(channel_id)
    if channel is None:
        return

    try:
        report_text = await build_today_win_rate_report()
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


async def background_daily_refresher():
    global LAST_CACHE_CLEANUP_AT
    if not DB_ENABLED:
        return
    while not client.is_closed():
        token = REQUEST_ID_CONTEXT.set(create_request_id("bg"))
        try:
            await refresh_daily_stats_once()
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


async def run_riot_connectivity_test():
    riot_id = FRIENDS[0]
    puuid = await fetch_puuid(riot_id)
    start_time_unix = get_last_24h_start_unix_seconds()
    match_ids = await fetch_match_ids(puuid, start_time_unix)
    return riot_id, puuid, len(match_ids)


async def run_health_check():
    uptime_seconds = int(time.monotonic() - START_MONOTONIC)
    try:
        db_stats = await asyncio.to_thread(db_health_stats)
        db_ok = db_stats["db_ok"]
        cache_entries = db_stats["match_cache_entries"]
    except Exception as exc:
        db_ok = False
        cache_entries = 0
        log(f"[health] DB health check failed: {exc}")

    return {
        "uptime_seconds": uptime_seconds,
        "tracked_players": len(FRIENDS),
        "db_ok": db_ok,
        "match_cache_entries": cache_entries,
        "request_cache_active": REPORT_CACHE["text"] is not None,
    }


async def build_debug_player_report(riot_id):
    window_start_unix = get_last_24h_start_unix_seconds()
    normalized = normalize_riot_id(riot_id)
    puuid = await fetch_puuid(normalized)
    recent_ids = await fetch_recent_match_ids(puuid, count=20)
    lines = [
        f"Player debug (timezone={REPORT_TIMEZONE_NAME}, window=last 24h):",
        normalized,
        "match_id | queue | end_time | bucket | in_last_24h",
    ]
    inspected = 0
    for match_id in recent_ids:
        match_info = await fetch_match_info(match_id)
        end_ts = get_match_end_unix_seconds(match_info)
        end_local = datetime.fromtimestamp(end_ts, tz=timezone.utc).astimezone(REPORT_TIMEZONE)
        queue_id = match_info["info"].get("queueId", -1)
        bucket = get_mode_bucket(queue_id)
        in_window = "yes" if end_ts >= window_start_unix else "no"
        lines.append(f"{match_id} | {queue_id} | {end_local:%d.%m.%Y %H:%M} | {bucket} | {in_window}")
        inspected += 1
        if inspected >= 12:
            break

    return "\n".join(lines)


async def daily_report(channel):
    report_text = await build_today_win_rate_report()
    report_message = await get_or_create_report_message(channel, report_text)
    if report_message.content != report_text:
        await report_message.edit(content=report_text)
        log(f"[scheduler] Updated scoreboard message {report_message.id}.")
    else:
        log(f"[scheduler] No scoreboard change; skipped update for {report_message.id}.")


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
        try:
            riot_id, puuid, match_count = await run_riot_connectivity_test()
            await message.channel.send(
                f"Riot API test OK for `{riot_id}`. Retrieved puuid and {match_count} matches."
            )
            log(f"[test] Riot API test succeeded for {riot_id} ({puuid[:8]}...).")
        except (KeyError, requests.RequestException) as exc:
            await message.channel.send(f"Riot API test failed: {exc}")
            log(f"[test] Riot API test failed: {exc}")
        return

    if content_lower == HEALTH_COMMAND.casefold():
        stats = await run_health_check()
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
        status_message = await message.channel.send(f"\u23F3 Building debug report for `{raw_riot_id}`...")
        try:
            report_text = await build_debug_player_report(raw_riot_id)
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
                await message.channel.send("\u23F3 A mood report is already in progress. Please wait for it to finish.")
                return

            async with MOOD_REQUEST_LOCK:
                loading_text = "\u23F3 Gathering last 24h match results from Riot..."
                status_message = await get_or_create_report_message(message.channel, loading_text)
                if status_message.content != loading_text:
                    await status_message.edit(content=loading_text)
                try:
                    if DB_ENABLED:
                        # Show stored snapshot immediately.
                        snapshot_text = await build_today_win_rate_report()
                        refresh_note = "_Refreshing latest matches..._"
                        snapshot_with_note = f"{snapshot_text}\n\n{refresh_note}"
                        if len(snapshot_with_note) > 2000:
                            snapshot_with_note = snapshot_text
                        await status_message.edit(content=snapshot_with_note)
                        displayed_text = snapshot_with_note
                        remember_report_message(status_message)
                        log(f"[mood] Sent stored snapshot report in channel {CHANNEL_ID}.")

                        # Refresh latest matches and update the same message after refresh completes.
                        await refresh_recent_matches_snapshot(recent_count=1)
                        refreshed_text = await build_today_win_rate_report()
                        if refreshed_text != displayed_text:
                            await status_message.edit(content=refreshed_text)
                            log(f"[mood] Updated report after quick refresh in channel {CHANNEL_ID}.")
                        else:
                            log(f"[mood] Quick refresh produced no visible report change.")
                    else:
                        async def progress(done, total, last_name):
                            await status_message.edit(
                                content=f"\u23F3 Gathering last 24h match results from Riot... ({done}/{total}) `{last_name}`"
                            )

                        report_text = await build_today_win_rate_report(progress_callback=progress)
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

        status_message = await message.channel.send(f"\u23F3 Validating `{riot_id}` with Riot API...")
        try:
            await fetch_puuid(riot_id)
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
                f"\u2705 Added `{riot_id}` and saved to postgres. "
                f"Total tracked players: {len(FRIENDS)}"
            )
        )
        REPORT_CACHE["text"] = None
        REPORT_CACHE["day"] = None
        REPORT_CACHE["expires_at"] = 0.0
        log(f"[add] Added player {riot_id}.")


def main():
    client.run(TOKEN)


if __name__ == "__main__":
    main()
