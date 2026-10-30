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


def db_load_player_analysis_rows(puuid, since, *, until=None):
    """One player's ranked games (Solo/Duo and Flex, no remakes) that ended after `since` (and by `until`)."""
    until_sql = " AND game_end_at <= %s" if until is not None else ""
    rows = db_execute(
        f"SELECT {', '.join(ANALYSIS_ROW_COLUMNS)} FROM player_match_stats "
        f"WHERE puuid = %s AND queue_id = ANY(%s) AND NOT remake AND game_end_at > %s{until_sql} "
        "ORDER BY game_end_at;",
        (puuid, RANKED_QUEUE_IDS, since, *([until] if until is not None else [])),
        fetch=True,
    ) or []
    return [dict(zip(ANALYSIS_ROW_COLUMNS, row)) for row in rows]


def db_load_role_group_totals(queue_id, since, *, until=None):
    """Per-role totals across all tracked players in one queue: the group baseline, same window as the player."""
    until_sql = " AND game_end_at <= %s" if until is not None else ""
    rows = db_execute(
        "SELECT team_position, count(*), sum(kills), sum(deaths), sum(assists), sum(duration_seconds) / 60.0 "
        "FROM player_match_stats "
        f"WHERE queue_id = %s AND NOT remake AND game_end_at > %s{until_sql} AND team_position IS NOT NULL "
        "GROUP BY team_position;",
        (queue_id, since, *([until] if until is not None else [])),
        fetch=True,
    ) or []
    return {
        role: {"games": int(games), "kills": int(kills), "deaths": int(deaths), "assists": int(assists), "minutes": float(minutes)}
        for role, games, kills, deaths, assists, minutes in rows
    }


RECORD_ROW_COLUMNS = (
    "match_id",
    "queue_id",
    "game_end_at",
    "duration_seconds",
    "champion_name",
    "win",
    "remake",
    "kills",
    "deaths",
    "assists",
    "damage_to_champions",
    "damage_share",
    "damage_taken",
    "gold_earned",
    "cs",
    "vision_score",
    "solo_kills",
    "penta_kills",
    "killing_spree",
    "dragon_takedowns",
    "baron_takedowns",
    "objective_steals",
    "enemy_jungle_camps",
    "buffs_stolen",
)
RECORD_PAGE_ROWS = 1000


def _iter_stats_rows(row_columns, where, params, batch_size):
    """Yield tracked players' rows matching `where` as dicts with `puuid` and `riot_id`, oldest first.

    Paged by (game_end_at, match_id, puuid), so only one page is in memory. `row_columns`
    must start with match_id and include game_end_at.
    """
    columns = ", ".join(f"s.{col}" for col in row_columns)
    cursor = None
    page_size = max(1, int(batch_size))
    while True:
        page_where, page_params = where, list(params)
        if cursor is not None:
            page_where += " AND (s.game_end_at, s.match_id, s.puuid) > (%s, %s, %s)"
            page_params.extend(cursor)
        rows = db_execute(
            f"SELECT {columns}, s.puuid, t.riot_id FROM player_match_stats s "
            "JOIN tracked_players t ON t.puuid = s.puuid "
            f"WHERE {page_where} ORDER BY s.game_end_at, s.match_id, s.puuid LIMIT %s;",
            (*page_params, page_size),
            fetch=True,
        ) or []
        for row in rows:
            yield dict(zip((*row_columns, "puuid", "riot_id"), row))
        if len(rows) < page_size:
            return
        last = rows[-1]
        cursor = (last[row_columns.index("game_end_at")], last[0], last[len(row_columns)])


def db_iter_record_rows(queue_ids, *, before=None, exclude_match_ids=(), batch_size=RECORD_PAGE_ROWS):
    """Yield tracked players' non-remake games in `queue_ids` as dicts with `riot_id`, oldest first.

    `before` keeps only games that ended earlier. Consume it in a worker thread.
    """
    params = [list(queue_ids), list(exclude_match_ids)]
    where = "s.queue_id = ANY(%s) AND NOT s.remake AND NOT (s.match_id = ANY(%s))"
    if before is not None:
        where += " AND s.game_end_at < %s"
        params.append(before)
    return _iter_stats_rows(RECORD_ROW_COLUMNS, where, params, batch_size)


WRAPPED_ROW_COLUMNS = (
    *RECORD_ROW_COLUMNS,
    "team_position",
    "pings",
    "time_dead_seconds",
    "tracked_teammates",
    "arena_placement",
)


def db_iter_wrapped_rows(start, end, *, batch_size=RECORD_PAGE_ROWS):
    """Yield tracked players' non-remake games (all queues) that ended in [start, end), oldest first.

    For Poro Wrapped; consume it in a worker thread.
    """
    where = "NOT s.remake AND s.game_end_at >= %s AND s.game_end_at < %s"
    return _iter_stats_rows(WRAPPED_ROW_COLUMNS, where, [start, end], batch_size)


BELT_ROW_COLUMNS = (
    "queue_id",
    "remake",
    "duration_seconds",
    "damage_to_champions",
    "vision_score",
    "turret_takedowns",
    "dragon_takedowns",
    "baron_takedowns",
    "herald_takedowns",
    "ally_saves",
    "outnumbered_kills",
    "skillshots_dodged",
    "pings",
    "time_dead_seconds",
)


def db_load_belt_rows(start, end):
    """Tracked players' ranked, non-remake games that ended in [start, end), with `riot_id`.

    One report week: a few hundred small rows at most.
    """
    rows = db_execute(
        f"SELECT {', '.join('s.' + col for col in BELT_ROW_COLUMNS)}, t.riot_id FROM player_match_stats s "
        "JOIN tracked_players t ON t.puuid = s.puuid "
        "WHERE s.queue_id = ANY(%s) AND NOT s.remake AND s.game_end_at >= %s AND s.game_end_at < %s;",
        ([420, 440, 710], start, end),
        fetch=True,
    ) or []
    return [dict(zip((*BELT_ROW_COLUMNS, "riot_id"), row)) for row in rows]
