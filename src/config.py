import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


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
    value = raw.strip()
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def get_env_int(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {raw!r}") from exc


def get_env_str(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    return value


TOKEN = require_env("DISCORD_TOKEN")
RIOT_API_KEY = require_env("RIOT_API_KEY")
RIOT_PLATFORM_ROUTING = get_env_str("RIOT_PLATFORM_ROUTING", "euw1").lower()
RIOT_REGIONAL_ROUTING = get_env_str("RIOT_REGIONAL_ROUTING", "europe").lower()
DAILY_REPORT_CHANNEL_ID = int(require_env("DAILY_REPORT_CHANNEL_ID"))
WEEKLY_REPORT_CHANNEL_ID = int(require_env("WEEKLY_REPORT_CHANNEL_ID"))
EVENTS_CHANNEL_ID = int(require_env("EVENTS_CHANNEL_ID"))
REPORT_TIMEZONE_NAME = get_env_str("REPORT_TIMEZONE", "UTC")
try:
    REPORT_TIMEZONE = ZoneInfo(REPORT_TIMEZONE_NAME)
except Exception as exc:
    raise RuntimeError(f"Invalid REPORT_TIMEZONE '{REPORT_TIMEZONE_NAME}': {exc}") from exc

LOG_RIOT_REQUESTS = get_env_bool("LOG_RIOT_REQUESTS", False)
LOG_JSON = get_env_bool("LOG_JSON", False)
REPORT_CACHE_SECONDS = get_env_int("REPORT_CACHE_SECONDS", 120)
MAX_TODAY_MATCH_DETAILS = get_env_int("MAX_TODAY_MATCH_DETAILS", 100)
MAX_MATCH_IDS_SCAN = get_env_int("MAX_MATCH_IDS_SCAN", 2000)
MAX_IN_MEMORY_MATCH_CACHE = get_env_int("MAX_IN_MEMORY_MATCH_CACHE", 200)
REPORT_DAY_START_HOUR = get_env_int("REPORT_DAY_START_HOUR", 6)
if REPORT_DAY_START_HOUR < 0 or REPORT_DAY_START_HOUR > 23:
    raise RuntimeError("REPORT_DAY_START_HOUR must be between 0 and 23.")
DAILY_REFRESH_SECONDS = get_env_int("DAILY_REFRESH_SECONDS", 300)
MATCH_CACHE_RETENTION_DAYS = get_env_int("MATCH_CACHE_RETENTION_DAYS", 730)
MATCH_RECAP_CHANNEL_ID = int(require_env("MATCH_RECAP_CHANNEL_ID"))
MATCH_RECAP_POLL_SECONDS = get_env_int("MATCH_RECAP_POLL_SECONDS", 90)
DATABASE_URL = require_env("DATABASE_URL")
DB_POOL_SIZE = get_env_int("DB_POOL_SIZE", 5)
# --- Low-resource optimizations ---
STARTUP_PREFER_SNAPSHOT = get_env_bool("STARTUP_PREFER_SNAPSHOT", False)
BASELINE_MATCH_LIMIT = get_env_int("BASELINE_MATCH_LIMIT", 5000)
RIOT_REQUEST_YIELD_SECONDS = float(get_env_str("RIOT_REQUEST_YIELD_SECONDS", "0.0"))
ASYNCIO_THREAD_POOL_SIZE = get_env_int("ASYNCIO_THREAD_POOL_SIZE", 0)
WORKER_STAGGER_SECONDS = get_env_int("WORKER_STAGGER_SECONDS", 0)
