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
