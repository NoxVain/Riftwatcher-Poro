from src.db.pool import db_execute
from src.match_stats import STATS_COLUMNS

_UPDATE_COLUMNS = [col for col in STATS_COLUMNS if col not in ("match_id", "puuid")]


def db_upsert_player_match_stats(rows):
    """Insert or refresh rows (dicts keyed by STATS_COLUMNS) in one statement."""
    if not rows:
        return
    placeholders = "(" + ", ".join(["%s"] * len(STATS_COLUMNS)) + ")"
    params = []
    for row in rows:
        params.extend(row.get(col) for col in STATS_COLUMNS)
    db_execute(
        f"INSERT INTO player_match_stats ({', '.join(STATS_COLUMNS)}) "
        f"VALUES {', '.join([placeholders] * len(rows))} "
        "ON CONFLICT (puuid, match_id) DO UPDATE SET "
        + ", ".join(f"{col} = EXCLUDED.{col}" for col in _UPDATE_COLUMNS)
        + ", updated_at = NOW();",
        tuple(params),
    )


def db_delete_player_match_stats_except(puuids):
    """Drop rows for PUUIDs that are no longer tracked (removed players, rotated keys)."""
    db_execute("DELETE FROM player_match_stats WHERE NOT (puuid = ANY(%s));", (list(puuids),))


ANALYSIS_ROW_COLUMNS = (
    "queue_id",
    "game_end_at",
    "duration_seconds",
    "champion_name",
    "team_position",
    "win",
    "kills",
    "deaths",
    "assists",
    "opponent_gold_diff",
    "opponent_cs_diff",
    "tracked_teammates",
)
RANKED_QUEUE_IDS = [420, 440]


def db_load_player_analysis_rows(puuid, since):
    """One player's ranked games (Solo/Duo and Flex, no remakes) that ended after `since`."""
    rows = db_execute(
        f"SELECT {', '.join(ANALYSIS_ROW_COLUMNS)} FROM player_match_stats "
        "WHERE puuid = %s AND queue_id = ANY(%s) AND NOT remake AND game_end_at > %s "
        "ORDER BY game_end_at;",
        (puuid, RANKED_QUEUE_IDS, since),
        fetch=True,
    ) or []
    return [dict(zip(ANALYSIS_ROW_COLUMNS, row)) for row in rows]


def db_load_role_group_totals(queue_id, since):
    """Per-role totals across all tracked players in one queue: the group baseline."""
    rows = db_execute(
        "SELECT team_position, count(*), sum(kills), sum(deaths), sum(assists), sum(duration_seconds) / 60.0 "
        "FROM player_match_stats "
        "WHERE queue_id = %s AND NOT remake AND game_end_at > %s AND team_position IS NOT NULL "
        "GROUP BY team_position;",
        (queue_id, since),
        fetch=True,
    ) or []
    return {
        role: {"games": int(games), "kills": int(kills), "deaths": int(deaths), "assists": int(assists), "minutes": float(minutes)}
        for role, games, kills, deaths, assists, minutes in rows
    }
