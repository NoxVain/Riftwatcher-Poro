from src.db.pool import db_execute


def db_upsert_player(riot_id, puuid=None):
    db_execute(
        """
        INSERT INTO tracked_players (riot_id, puuid, created_at, updated_at)
        VALUES (%s, %s, NOW(), NOW())
        ON CONFLICT (riot_id)
        DO UPDATE SET
            puuid = COALESCE(EXCLUDED.puuid, tracked_players.puuid),
            updated_at = NOW();
        """,
        (riot_id, puuid),
    )


def db_get_puuid(riot_id):
    row = db_execute(
        "SELECT puuid FROM tracked_players WHERE lower(riot_id) = lower(%s) LIMIT 1;",
        (riot_id,),
        fetchone=True,
    )
    if row and row[0]:
        return row[0]
    return None


def db_load_tracked_players():
    rows = db_execute("SELECT riot_id FROM tracked_players ORDER BY riot_id;", fetch=True) or []
    return [row[0] for row in rows]
