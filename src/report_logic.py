import bisect
import math
from datetime import datetime, timedelta, timezone


VALID_POSITIONS = frozenset({"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"})
GAMER_SCORE_WIN_WEIGHT = 0.65
GAMER_SCORE_PERF_WEIGHT = 0.35
GAMER_SCORE_PERF_RAMP_GAMES = 8
WILSON_Z = 1.40

_BASELINE_STATS = (
    "cs_per_min",
    "player_damage_per_min",
    "objective_damage_per_min",
    "healing_per_min",
    "damage_taken_per_min",
    "kills_per_min",
    "deaths_per_min",
    "vision_per_min",
)
_INVERTED_BASELINE_STATS = frozenset({"deaths_per_min"})
_PERF_STAT_WEIGHTS = {
    "cs_per_min": 0.16,
    "player_damage_per_min": 0.20,
    "objective_damage_per_min": 0.16,
    "healing_per_min": 0.06,
    "damage_taken_per_min": 0.10,
    "kills_per_min": 0.14,
    "deaths_per_min": 0.12,
    "vision_per_min": 0.06,
}


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
        "assists": 0,
        "deaths": 0,
        "vision_score": 0,
        "gold_earned": 0,
        "wards_placed": 0,
        "wards_killed": 0,
        "turret_takedowns": 0,
        "dragon_takedowns": 0,
        "baron_takedowns": 0,
        "double_kills": 0,
        "triple_kills": 0,
        "quadra_kills": 0,
        "penta_kills": 0,
        "kill_participation_num": 0,
        "kill_participation_den": 0,
    }


def get_mode_totals(mode_records):
    wins = mode_records["solo_duo"]["wins"] + mode_records["flex"]["wins"]
    losses = mode_records["solo_duo"]["losses"] + mode_records["flex"]["losses"]
    return wins, losses


def accumulate_participant_performance(performance_totals, participant, duration_seconds, match_info=None):
    performance_totals["minutes_total"] += max(0.0, duration_seconds / 60.0)
    performance_totals["cs_total"] += int(participant.get("totalMinionsKilled", 0) or 0)
    performance_totals["cs_total"] += int(participant.get("neutralMinionsKilled", 0) or 0)
    performance_totals["objective_damage"] += int(participant.get("damageDealtToObjectives", 0) or 0)
    performance_totals["player_damage"] += int(participant.get("totalDamageDealtToChampions", 0) or 0)
    performance_totals["healing"] += int(participant.get("totalHeal", 0) or 0)
    performance_totals["damage_taken"] += int(participant.get("totalDamageTaken", 0) or 0)
    kills = int(participant.get("kills", 0) or 0)
    assists = int(participant.get("assists", 0) or 0)
    performance_totals["kills"] += kills
    performance_totals["assists"] += assists
    performance_totals["deaths"] += int(participant.get("deaths", 0) or 0)
    performance_totals["vision_score"] += int(participant.get("visionScore", 0) or 0)
    performance_totals["gold_earned"] += int(participant.get("goldEarned", 0) or 0)
    performance_totals["wards_placed"] += int(participant.get("wardsPlaced", 0) or 0)
    performance_totals["wards_killed"] += int(participant.get("wardsKilled", 0) or 0)
    performance_totals["turret_takedowns"] += int(participant.get("turretTakedowns", 0) or 0)
    performance_totals["dragon_takedowns"] += int(participant.get("dragonTakedowns", 0) or 0)
    performance_totals["baron_takedowns"] += int(participant.get("baronTakedowns", 0) or 0)
    performance_totals["double_kills"] += int(participant.get("doubleKills", 0) or 0)
    performance_totals["triple_kills"] += int(participant.get("tripleKills", 0) or 0)
    performance_totals["quadra_kills"] += int(participant.get("quadraKills", 0) or 0)
    performance_totals["penta_kills"] += int(participant.get("pentaKills", 0) or 0)

    team_id = participant.get("teamId")
    if match_info is not None and team_id is not None:
        team_kills = 0
        participants = match_info.get("info", {}).get("participants", []) or []
        for row in participants:
            if row.get("teamId") == team_id:
                team_kills += int(row.get("kills", 0) or 0)
        performance_totals["kill_participation_num"] += (kills + assists)
        performance_totals["kill_participation_den"] += team_kills
    position = str(participant.get("teamPosition", "") or "").upper()
    if position in VALID_POSITIONS:
        role_votes = performance_totals.setdefault("_role_votes", {})
        role_votes[position] = role_votes.get(position, 0) + 1


def derive_primary_role(performance_totals):
    role_votes = performance_totals.get("_role_votes") or {}
    if not role_votes:
        return None
    return max(role_votes, key=role_votes.get)


def compute_role_baselines(match_payloads):
    """Build per-role, per-minute stat distributions from cached match payloads.

    Returns {role: {stat_name: sorted_list_of_floats}}.
    Only ranked queues (solo/duo, flex) and non-remake matches are included.
    A stat list must have at least 20 samples to be included.
    """
    role_data = {role: {stat: [] for stat in _BASELINE_STATS} for role in VALID_POSITIONS}
    for match_info in match_payloads:
        if not isinstance(match_info, dict):
            continue
        info = match_info.get("info", {}) or {}
        queue_id = int(info.get("queueId", -1) or -1)
        if get_mode_bucket(queue_id) is None:
            continue
        duration_seconds = get_match_duration_seconds(match_info)
        if duration_seconds < 300:
            continue
        minutes = max(1.0, duration_seconds / 60.0)
        for participant in info.get("participants", []) or []:
            position = str(participant.get("teamPosition", "") or "").upper()
            if position not in VALID_POSITIONS:
                continue
            cs = int(participant.get("totalMinionsKilled", 0) or 0)
            cs += int(participant.get("neutralMinionsKilled", 0) or 0)
            player_damage = int(participant.get("totalDamageDealtToChampions", 0) or 0)
            objective_damage = int(participant.get("damageDealtToObjectives", 0) or 0)
            healing = int(participant.get("totalHeal", 0) or 0)
            damage_taken = int(participant.get("totalDamageTaken", 0) or 0)
            kills = int(participant.get("kills", 0) or 0)
            deaths = int(participant.get("deaths", 0) or 0)
            vision_score = int(participant.get("visionScore", 0) or 0)
            role_data[position]["cs_per_min"].append(cs / minutes)
            role_data[position]["player_damage_per_min"].append(player_damage / minutes)
            role_data[position]["objective_damage_per_min"].append(objective_damage / minutes)
            role_data[position]["healing_per_min"].append(healing / minutes)
            role_data[position]["damage_taken_per_min"].append(damage_taken / minutes)
            role_data[position]["kills_per_min"].append(kills / minutes)
            role_data[position]["deaths_per_min"].append(deaths / minutes)
            role_data[position]["vision_per_min"].append(vision_score / minutes)

    baselines = {}
    for role, stats in role_data.items():
        valid_stats = {stat: sorted(values) for stat, values in stats.items() if len(values) >= 20}
        if valid_stats:
            baselines[role] = valid_stats
    return baselines


def compute_perf_percentile(player_perf_per_min, role, baselines):
    """Compute performance percentile (0.0–1.0) vs same-role players in the baseline.

    For inverted stats (deaths), lower values yield higher percentile.
    Returns 0.5 (neutral) when no baseline data is available for this role.
    """
    if not baselines or role not in baselines:
        return 0.5
    role_baselines = baselines[role]
    weighted_sum = 0.0
    weight_total = 0.0
    stat_percentiles = []
    for stat_name, values in role_baselines.items():
        player_value = float(player_perf_per_min.get(stat_name, 0.0) or 0.0)
        percentile = bisect.bisect_right(values, player_value) / len(values)
        if stat_name in _INVERTED_BASELINE_STATS:
            percentile = 1.0 - percentile
        weight = float(_PERF_STAT_WEIGHTS.get(stat_name, 0.0) or 0.0)
        weighted_sum += percentile * weight
        weight_total += weight
        stat_percentiles.append(percentile)
    if weight_total > 0:
        return weighted_sum / weight_total
    if not stat_percentiles:
        return 0.5
    return sum(stat_percentiles) / len(stat_percentiles)


def gamer_score_weights_for_games(total_games):
    if total_games <= 0:
        return 1.0, 0.0
    perf_factor = min(1.0, float(total_games) / float(GAMER_SCORE_PERF_RAMP_GAMES))
    perf_weight = GAMER_SCORE_PERF_WEIGHT * perf_factor
    win_weight = 1.0 - perf_weight
    return win_weight, perf_weight


def compute_gamer_score(wins, losses, performance_totals, primary_role, baselines):
    """Composite Gamer Score (0-100): Wilson confidence + role-adjusted performance percentile.

    Performance weight ramps in over the first N ranked games, then caps at GAMER_SCORE_PERF_WEIGHT.
    Falls back to pure Wilson * 100 when baselines or role are unavailable.
    """
    wilson = wilson_lower_bound(wins, losses)
    total_games = wins + losses
    win_weight, perf_weight = gamer_score_weights_for_games(total_games)
    if baselines and primary_role and (wins + losses) > 0:
        minutes = float(performance_totals.get("minutes_total", 0.0) or 0.0)
        if minutes > 0:
            cs = int(performance_totals.get("cs_total", 0) or 0)
            player_damage = int(performance_totals.get("player_damage", 0) or 0)
            objective_damage = int(performance_totals.get("objective_damage", 0) or 0)
            healing = int(performance_totals.get("healing", 0) or 0)
            damage_taken = int(performance_totals.get("damage_taken", 0) or 0)
            kills = int(performance_totals.get("kills", 0) or 0)
            deaths = int(performance_totals.get("deaths", 0) or 0)
            vision_score = int(performance_totals.get("vision_score", 0) or 0)
            perf_per_min = {
                "cs_per_min": cs / minutes,
                "player_damage_per_min": player_damage / minutes,
                "objective_damage_per_min": objective_damage / minutes,
                "healing_per_min": healing / minutes,
                "damage_taken_per_min": damage_taken / minutes,
                "kills_per_min": kills / minutes,
                "deaths_per_min": deaths / minutes,
                "vision_per_min": vision_score / minutes,
            }
            perf = compute_perf_percentile(perf_per_min, primary_role, baselines)
            return (wilson * win_weight + perf * perf_weight) * 100
    return wilson * 100


def wilson_lower_bound(wins, losses, z=WILSON_Z):
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
    gamer_score = row[6] if len(row) > 6 else None
    wins = row[2]
    losses = row[3]
    win_rate = row[4]
    if gamer_score is not None:
        return (-gamer_score, -win_rate, -(wins + losses), row[0].lower())
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
