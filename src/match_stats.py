"""Per-player match stats: one compact row per tracked player per cached match.

Analysis (e.g. a future `!analyze`) needs a year of a player's games. Parsing the
match cache for that takes minutes and hundreds of MB, so the numbers are extracted
once into `player_match_stats` and queried with plain SQL. No Riot requests: rows
come only from payloads the bot has already cached.

A background task keeps the table in sync. A full rebuild runs when the tracked
players (or STATS_VERSION) change; otherwise each pass only reads matches cached
since the last pass.
"""

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone

from src.report_logic import get_match_duration_seconds, get_match_end_unix_seconds, is_remake_match
from src.runtime.memory import release_freed_memory

# Bump when extraction changes, so the next pass rebuilds every row.
STATS_VERSION = 2
STATS_FINGERPRINT_KEY = "player_match_stats_fingerprint"
STATS_WATERMARK_KEY = "player_match_stats_cached_through"
STATS_SYNC_INTERVAL_SECONDS = 600
# Re-read a little before the watermark: a row cached just before the last pass may
# have committed after it read. Upserts make the overlap harmless.
WATERMARK_OVERLAP_SECONDS = 300
UPSERT_BATCH_ROWS = 200

STATS_COLUMNS = (
    "match_id",
    "puuid",
    "queue_id",
    "game_end_at",
    "duration_seconds",
    "game_version",
    "champion_id",
    "champion_name",
    "team_position",
    "win",
    "remake",
    "surrender",
    "kills",
    "deaths",
    "assists",
    "kill_participation",
    "damage_share",
    "damage_to_champions",
    "damage_taken",
    "gold_earned",
    "cs",
    "vision_score",
    "wards_placed",
    "wards_killed",
    "control_wards_bought",
    "turret_takedowns",
    "dragon_takedowns",
    "baron_takedowns",
    "first_blood",
    "solo_kills",
    "lane_cs_10",
    "max_cs_advantage",
    "opponent_champion_id",
    "opponent_gold_diff",
    "opponent_cs_diff",
    "arena_placement",
    "tracked_teammates",
    "penta_kills",
)


def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _float(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cs(participant):
    return int(participant.get("totalMinionsKilled", 0) or 0) + int(participant.get("neutralMinionsKilled", 0) or 0)


def _share(value, total):
    return round(value / total, 4) if total else None


def extract_player_match_rows(payload, tracked_puuids):
    """Rows (dicts keyed by STATS_COLUMNS) for each tracked player in one match payload.

    Payloads without participants (Riot's aborted-game stubs) give no rows.
    """
    try:
        info = payload["info"]
        participants = info["participants"]
        match_id = payload["metadata"]["matchId"]
    except (KeyError, TypeError):
        return []
    if not participants:
        return []

    end_at = datetime.fromtimestamp(get_match_end_unix_seconds(payload), tz=timezone.utc)
    duration = get_match_duration_seconds(payload)
    remake = is_remake_match(payload)
    team_kills = {}
    team_damage = {}
    for p in participants:
        team = p.get("teamId")
        team_kills[team] = team_kills.get(team, 0) + int(p.get("kills", 0) or 0)
        team_damage[team] = team_damage.get(team, 0) + int(p.get("totalDamageDealtToChampions", 0) or 0)

    rows = []
    for me in participants:
        puuid = me.get("puuid")
        if puuid not in tracked_puuids:
            continue
        team = me.get("teamId")
        position = me.get("teamPosition") or None
        challenges = me.get("challenges") or {}
        opponent = None
        if position:
            opponent = next(
                (p for p in participants if p.get("teamId") != team and p.get("teamPosition") == position),
                None,
            )
        kills = int(me.get("kills", 0) or 0)
        assists = int(me.get("assists", 0) or 0)
        damage = int(me.get("totalDamageDealtToChampions", 0) or 0)
        kill_participation = _float(challenges.get("killParticipation"))
        if kill_participation is None:
            kill_participation = _share(kills + assists, team_kills.get(team, 0))
        damage_share = _float(challenges.get("teamDamagePercentage"))
        if damage_share is None:
            damage_share = _share(damage, team_damage.get(team, 0))
        rows.append({
            "match_id": match_id,
            "puuid": puuid,
            "queue_id": _int(info.get("queueId")),
            "game_end_at": end_at,
            "duration_seconds": duration,
            "game_version": info.get("gameVersion") or None,
            "champion_id": _int(me.get("championId")),
            "champion_name": me.get("championName") or None,
            "team_position": position,
            "win": bool(me.get("win")),
            "remake": remake,
            "surrender": bool(me.get("gameEndedInSurrender")),
            "kills": kills,
            "deaths": int(me.get("deaths", 0) or 0),
            "assists": assists,
            "kill_participation": kill_participation,
            "damage_share": damage_share,
            "damage_to_champions": damage,
            "damage_taken": _int(me.get("totalDamageTaken")),
            "gold_earned": _int(me.get("goldEarned")),
            "cs": _cs(me),
            "vision_score": _int(me.get("visionScore")),
            "wards_placed": _int(me.get("wardsPlaced")),
            "wards_killed": _int(me.get("wardsKilled")),
            "control_wards_bought": _int(me.get("visionWardsBoughtInGame")),
            "turret_takedowns": _int(me.get("turretTakedowns")),
            "dragon_takedowns": _int(challenges.get("dragonTakedowns")),
            "baron_takedowns": _int(challenges.get("baronTakedowns")),
            "first_blood": bool(me.get("firstBloodKill") or me.get("firstBloodAssist")),
            "solo_kills": _int(challenges.get("soloKills")),
            "lane_cs_10": _float(challenges.get("laneMinionsFirst10Minutes")),
            "max_cs_advantage": _float(challenges.get("maxCsAdvantageOnLaneOpponent")),
            "opponent_champion_id": _int(opponent.get("championId")) if opponent else None,
            "opponent_gold_diff": (
                int(me.get("goldEarned", 0) or 0) - int(opponent.get("goldEarned", 0) or 0) if opponent else None
            ),
            "opponent_cs_diff": _cs(me) - _cs(opponent) if opponent else None,
            "arena_placement": _int(me.get("placement")) if info.get("queueId") == 1700 else None,
            "tracked_teammates": sorted(
                p.get("puuid")
                for p in participants
                if p is not me and p.get("teamId") == team and p.get("puuid") in tracked_puuids
            ),
            "penta_kills": _int(me.get("pentaKills")),
        })
    return rows


def stats_fingerprint(tracked_puuids):
    """Changes when tracked players, their PUUIDs, or the extraction version change."""
    lines = "\n".join([f"v{STATS_VERSION}", *sorted(tracked_puuids)])
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def sync_player_match_stats(
    *,
    load_tracked,
    iter_cached_matches,
    upsert_rows,
    delete_rows_except,
    get_state,
    set_state,
):
    """Blocking (pages through cached payloads); run via asyncio.to_thread.

    Full rebuild when the fingerprint changed, else only matches cached since the
    watermark. State is saved only after a pass completes, so a failed pass repeats.
    Returns a summary dict, or None when there are no tracked players.
    """
    tracked_puuids = {puuid for _riot_id, puuid in load_tracked() if puuid}
    if not tracked_puuids:
        return None
    fingerprint = stats_fingerprint(tracked_puuids)
    full = get_state(STATS_FINGERPRINT_KEY) != fingerprint
    watermark = None
    if not full:
        raw_watermark = get_state(STATS_WATERMARK_KEY)
        try:
            watermark = datetime.fromisoformat(raw_watermark) if raw_watermark else None
        except ValueError:
            watermark = None
        full = watermark is None

    started = time.monotonic()
    if full:
        # Rows for removed players or rotated PUUIDs; the rebuild re-adds current ones.
        delete_rows_except(sorted(tracked_puuids))
    match_count = 0
    new_match_count = 0
    row_count = 0
    newest = None
    pending = []
    since = None if full else watermark - timedelta(seconds=WATERMARK_OVERLAP_SECONDS)
    for _match_id, cached_at, payload in iter_cached_matches(since):
        match_count += 1
        if watermark is None or cached_at > watermark:
            new_match_count += 1
        if newest is None or cached_at > newest:
            newest = cached_at
        pending.extend(extract_player_match_rows(payload, tracked_puuids))
        if len(pending) >= UPSERT_BATCH_ROWS:
            upsert_rows(pending)
            row_count += len(pending)
            pending = []
    if pending:
        upsert_rows(pending)
        row_count += len(pending)

    if newest is not None and (watermark is None or newest > watermark):
        set_state(STATS_WATERMARK_KEY, newest.isoformat())
    if full:
        set_state(STATS_FINGERPRINT_KEY, fingerprint)
    return {
        "mode": "full" if full else "incremental",
        "matches": match_count,
        "new_matches": new_match_count,
        "rows": row_count,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


async def run_match_stats_sync_loop(
    *,
    db_load_tracked_puuids,
    db_iter_cached_matches,
    db_upsert_player_match_stats,
    db_delete_player_match_stats_except,
    db_get_state,
    db_set_state,
    log,
    wait_for=None,
    interval_seconds=STATS_SYNC_INTERVAL_SECONDS,
    max_passes=None,
):
    """Background task: never raises, so a failure can't affect the workers.

    `wait_for` (e.g. the PUUID remap task) finishes first, so a full rebuild never
    reads payloads with stale PUUIDs.
    """
    if wait_for is not None:
        try:
            await wait_for
        except Exception:
            pass
    passes = 0
    while max_passes is None or passes < max_passes:
        passes += 1
        summary = None
        try:
            summary = await asyncio.to_thread(
                sync_player_match_stats,
                load_tracked=db_load_tracked_puuids,
                iter_cached_matches=db_iter_cached_matches,
                upsert_rows=db_upsert_player_match_stats,
                delete_rows_except=db_delete_player_match_stats_except,
                get_state=db_get_state,
                set_state=db_set_state,
            )
        except Exception as exc:
            log(f"[stats] Player match stats sync failed; retrying next pass: {exc}")
        # Incremental passes re-read a short overlap, so only newly cached matches count.
        if summary is not None and (summary["mode"] == "full" or summary["new_matches"]):
            log(
                f"[stats] Player match stats {summary['mode']} sync: rows={summary['rows']} "
                f"matches={summary['matches']} new={summary['new_matches']} elapsed={summary['elapsed_ms']}ms"
            )
        if summary is not None and summary["mode"] == "full":
            await asyncio.to_thread(release_freed_memory)
        if max_passes is None or passes < max_passes:
            await asyncio.sleep(interval_seconds)
