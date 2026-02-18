import math
import time
from datetime import datetime, timedelta, timezone


def get_mode_bucket(queue_id):
    if queue_id == 420:
        return "solo_duo"
    if queue_id == 440:
        return "flex"
    return None


def create_mode_records():
    return {
        "solo_duo": {"wins": 0, "losses": 0},
        "flex": {"wins": 0, "losses": 0},
        "arcade": {"wins": 0, "losses": 0},
    }


def create_performance_totals():
    return {
        "cs_total": 0,
        "minutes_total": 0.0,
        "objective_damage": 0,
        "player_damage": 0,
        "kills": 0,
        "deaths": 0,
        "vision_score": 0,
    }


def get_mode_totals(mode_records):
    wins = mode_records["solo_duo"]["wins"] + mode_records["flex"]["wins"]
    losses = mode_records["solo_duo"]["losses"] + mode_records["flex"]["losses"]
    return wins, losses


def wilson_lower_bound(wins, losses, z=1.28):
    n = wins + losses
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    denominator = 1 + (z2 / n)
    center = p + (z2 / (2 * n))
    margin = z * math.sqrt((p * (1 - p) + (z2 / (4 * n))) / n)
    return (center - margin) / denominator


def rank_sort_key(row):
    wins = row[2]
    losses = row[3]
    win_rate = row[4]
    return (-wilson_lower_bound(wins, losses), -win_rate, -(wins + losses), row[0].lower())


def format_mode_line(label, wins, losses):
    total = wins + losses
    if total == 0:
        return f"   {label}: `0W-0L` - **N/A**"
    win_rate = (wins / total) * 100
    return f"   {label}: `{wins}W-{losses}L` - **{win_rate:.1f}%**"


def get_match_end_unix_seconds(match_info):
    end_ms = match_info["info"].get("gameEndTimestamp")
    if not end_ms:
        creation_ms = match_info["info"].get("gameCreation", 0)
        duration_s = match_info["info"].get("gameDuration", 0)
        end_ms = creation_ms + (duration_s * 1000)
    return int(end_ms / 1000)


def get_report_cycle_start_unix_seconds(report_timezone, day_start_hour=6, now_utc=None):
    safe_day_start_hour = max(0, min(23, int(day_start_hour)))
    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)
    now_local = now_utc.astimezone(report_timezone)
    cycle_start_local = now_local.replace(hour=safe_day_start_hour, minute=0, second=0, microsecond=0)
    if now_local < cycle_start_local:
        cycle_start_local -= timedelta(days=1)
    return int(cycle_start_local.astimezone(timezone.utc).timestamp())


def get_report_cycle_key(report_timezone, day_start_hour=6, now_utc=None):
    safe_day_start_hour = max(0, min(23, int(day_start_hour)))
    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)
    now_local = now_utc.astimezone(report_timezone)
    cycle_start_local = now_local.replace(hour=safe_day_start_hour, minute=0, second=0, microsecond=0)
    if now_local < cycle_start_local:
        cycle_start_local -= timedelta(days=1)
    return cycle_start_local.date().isoformat()


def is_match_in_report_cycle(match_info, report_timezone, day_start_hour=6, now_utc=None):
    window_start = get_report_cycle_start_unix_seconds(report_timezone, day_start_hour=day_start_hour, now_utc=now_utc)
    end_ts = get_match_end_unix_seconds(match_info)
    return end_ts >= window_start


def is_match_in_last_24h(match_info, now_ts=None):
    if now_ts is None:
        now_ts = int(time.time())
    window_start = now_ts - (24 * 60 * 60)
    end_ts = get_match_end_unix_seconds(match_info)
    return end_ts >= window_start
