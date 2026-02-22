import math
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
    }


def create_performance_totals():
    return {
        "cs_total": 0,
        "minutes_total": 0.0,
        "objective_damage": 0,
        "player_damage": 0,
        "healing": 0,
        "damage_taken": 0,
        "kills": 0,
        "deaths": 0,
        "vision_score": 0,
    }


def get_mode_totals(mode_records):
    wins = mode_records["solo_duo"]["wins"] + mode_records["flex"]["wins"]
    losses = mode_records["solo_duo"]["losses"] + mode_records["flex"]["losses"]
    return wins, losses


def accumulate_participant_performance(performance_totals, participant, duration_seconds):
    performance_totals["minutes_total"] += max(0.0, duration_seconds / 60.0)
    performance_totals["cs_total"] += int(participant.get("totalMinionsKilled", 0) or 0)
    performance_totals["cs_total"] += int(participant.get("neutralMinionsKilled", 0) or 0)
    performance_totals["objective_damage"] += int(participant.get("damageDealtToObjectives", 0) or 0)
    performance_totals["player_damage"] += int(participant.get("totalDamageDealtToChampions", 0) or 0)
    performance_totals["healing"] += int(participant.get("totalHeal", 0) or 0)
    performance_totals["damage_taken"] += int(participant.get("totalDamageTaken", 0) or 0)
    performance_totals["kills"] += int(participant.get("kills", 0) or 0)
    performance_totals["deaths"] += int(participant.get("deaths", 0) or 0)
    performance_totals["vision_score"] += int(participant.get("visionScore", 0) or 0)


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


def get_match_duration_seconds(match_info):
    duration_seconds = int(match_info.get("info", {}).get("gameDuration", 0) or 0)
    if duration_seconds > 10_000:
        duration_seconds = int(duration_seconds / 1000)
    return max(0, duration_seconds)


def is_remake_match(match_info):
    info = match_info.get("info", {})
    participants = info.get("participants", []) or []
    if any(bool(p.get("gameEndedInEarlySurrender")) for p in participants):
        return True
    return get_match_duration_seconds(match_info) < 300


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


