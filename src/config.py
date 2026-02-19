import os
from zoneinfo import ZoneInfo


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
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TOKEN = require_env("DISCORD_TOKEN")
RIOT_API_KEY = require_env("RIOT_API_KEY")
RIOT_PLATFORM_ROUTING = os.getenv("RIOT_PLATFORM_ROUTING", "euw1").strip().lower() or "euw1"
CHANNEL_ID = int(require_env("DISCORD_CHANNEL_ID"))
REPORT_TIMEZONE_NAME = os.getenv("REPORT_TIMEZONE", "UTC")
try:
    REPORT_TIMEZONE = ZoneInfo(REPORT_TIMEZONE_NAME)
except Exception as exc:
    raise RuntimeError(f"Invalid REPORT_TIMEZONE '{REPORT_TIMEZONE_NAME}': {exc}") from exc

LOG_RIOT_REQUESTS = get_env_bool("LOG_RIOT_REQUESTS", False)
LOG_JSON = get_env_bool("LOG_JSON", False)
REPORT_CACHE_SECONDS = int(os.getenv("REPORT_CACHE_SECONDS", "120"))
MAX_TODAY_MATCH_DETAILS = int(os.getenv("MAX_TODAY_MATCH_DETAILS", "20"))
MAX_MATCH_IDS_SCAN = int(os.getenv("MAX_MATCH_IDS_SCAN", "0"))
MAX_IN_MEMORY_MATCH_CACHE = int(os.getenv("MAX_IN_MEMORY_MATCH_CACHE", "200"))
REPORT_DAY_START_HOUR = int(os.getenv("REPORT_DAY_START_HOUR", "6"))
if REPORT_DAY_START_HOUR < 0 or REPORT_DAY_START_HOUR > 23:
    raise RuntimeError("REPORT_DAY_START_HOUR must be between 0 and 23.")
DAILY_REFRESH_SECONDS = int(os.getenv("DAILY_REFRESH_SECONDS", "300"))
MATCH_CACHE_RETENTION_DAYS = int(os.getenv("MATCH_CACHE_RETENTION_DAYS", "0"))
raw_match_recap_channel_id = os.getenv("MATCH_RECAP_CHANNEL_ID", "").strip()
MATCH_RECAP_CHANNEL_ID = int(raw_match_recap_channel_id) if raw_match_recap_channel_id else None
MATCH_RECAP_POLL_SECONDS = int(os.getenv("MATCH_RECAP_POLL_SECONDS", "90"))
DATABASE_URL = require_env("DATABASE_URL")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
