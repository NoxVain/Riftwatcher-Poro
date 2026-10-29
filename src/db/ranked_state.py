from src.db.pool import db_execute


_RANKED_STATE_COLS = (
    "queue_type", "tier", "rank_division", "league_points",
    "wins", "losses", "hot_streak", "veteran", "fresh_blood", "inactive", "updated_at",
)


def db_load_ranked_state(riot_id):
    rows = db_execute(
        """
        SELECT
            queue_type, tier, rank_division, league_points,
            wins, losses, hot_streak, veteran, fresh_blood, inactive, updated_at
        FROM player_ranked_state
        WHERE lower(riot_id) = lower(%s)
        ORDER BY queue_type;
        """,
        (riot_id,),
        fetch=True,
    ) or []
    return [dict(zip(_RANKED_STATE_COLS, row)) for row in rows]


def db_upsert_ranked_state(riot_id, queue_type, entry):
    tier = str(entry.get("tier", "") or "").strip().upper() or None
    rank_division = str(entry.get("rank", "") or "").strip().upper() or None
    league_points = int(entry.get("leaguePoints", 0) or 0)
    wins = int(entry.get("wins", 0) or 0)
    losses = int(entry.get("losses", 0) or 0)
    hot_streak = bool(entry.get("hotStreak", False))
    veteran = bool(entry.get("veteran", False))
    fresh_blood = bool(entry.get("freshBlood", False))
    inactive = bool(entry.get("inactive", False))
    db_execute(
        """
        INSERT INTO player_ranked_state (
            riot_id, queue_type, tier, rank_division, league_points,
            wins, losses, hot_streak, veteran, fresh_blood, inactive, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (riot_id, queue_type)
        DO UPDATE SET
            tier = EXCLUDED.tier,
            rank_division = EXCLUDED.rank_division,
            league_points = EXCLUDED.league_points,
            wins = EXCLUDED.wins,
            losses = EXCLUDED.losses,
            hot_streak = EXCLUDED.hot_streak,
            veteran = EXCLUDED.veteran,
            fresh_blood = EXCLUDED.fresh_blood,
            inactive = EXCLUDED.inactive,
            updated_at = NOW();
        """,
        (
            riot_id,
            queue_type,
            tier,
            rank_division,
            league_points,
            wins,
            losses,
            hot_streak,
            veteran,
            fresh_blood,
            inactive,
        ),
    )


def db_delete_ranked_state_queue(riot_id, queue_type):
    db_execute(
        "DELETE FROM player_ranked_state WHERE lower(riot_id) = lower(%s) AND queue_type = %s;",
        (riot_id, queue_type),
    )


_LP_HISTORY_COLS = (
    "riot_id", "queue_type", "won", "wins_delta", "losses_delta", "lp_delta",
    "previous_tier", "previous_rank_division", "previous_league_points",
    "tier", "rank_division", "league_points", "wins", "losses",
)


def db_insert_lp_change(change):
    """Append one observed rank/LP change (a dict with the _LP_HISTORY_COLS keys)."""
    db_execute(
        f"INSERT INTO lp_history ({', '.join(_LP_HISTORY_COLS)}) "
        f"VALUES ({', '.join(['%s'] * len(_LP_HISTORY_COLS))});",
        tuple(change.get(col) for col in _LP_HISTORY_COLS),
    )


def db_load_all_ranked_states():
    """{riot_id: [rank entries]} for every player with stored rank state."""
    rows = db_execute(
        """
        SELECT
            riot_id, queue_type, tier, rank_division, league_points,
            wins, losses, hot_streak, veteran, fresh_blood, inactive, updated_at
        FROM player_ranked_state
        ORDER BY riot_id, queue_type;
        """,
        fetch=True,
    ) or []
    states = {}
    for row in rows:
        states.setdefault(row[0], []).append(dict(zip(_RANKED_STATE_COLS, row[1:])))
    return states
