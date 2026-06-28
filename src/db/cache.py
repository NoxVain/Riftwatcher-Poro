import json

from src.db.pool import db_execute
from src import config as cfg


def db_get_match_info(match_id):
    row = db_execute(
        "SELECT payload FROM match_info_cache WHERE match_id = %s LIMIT 1;",
        (match_id,),
        fetchone=True,
    )
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def db_upsert_match_info(match_id, match_info):
    payload = json.dumps(match_info, separators=(",", ":"))
    db_execute(
        """
        INSERT INTO match_info_cache (match_id, payload, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (match_id)
        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW();
        """,
        (match_id, payload),
    )


def db_cleanup_old_match_cache(retention_days):
    if int(retention_days) <= 0:
        return 0
    row = db_execute(
        """
        DELETE FROM match_info_cache
        WHERE updated_at < NOW() - (%s * INTERVAL '1 day')
        RETURNING match_id;
        """,
        (int(retention_days),),
        fetch=True,
    ) or []
    return len(row)


def db_health_stats():
    ping = db_execute("SELECT 1;", fetchone=True)
    count_row = db_execute("SELECT COUNT(*) FROM match_info_cache;", fetchone=True)
    count = int(count_row[0]) if count_row and count_row[0] is not None else 0
    return {"db_ok": bool(ping and ping[0] == 1), "match_cache_entries": count}


def db_load_match_payloads_for_baseline(limit=cfg.BASELINE_MATCH_LIMIT):
    safe_limit = int(limit)
    if safe_limit > 0:
        rows = db_execute(
            "SELECT payload FROM match_info_cache ORDER BY updated_at DESC LIMIT %s;",
            (safe_limit,),
            fetch=True,
        ) or []
    else:
        rows = db_execute(
            "SELECT payload FROM match_info_cache ORDER BY updated_at DESC;",
            fetch=True,
        ) or []
    result = []
    for (payload_text,) in rows:
        if not payload_text:
            continue
        try:
            result.append(json.loads(payload_text))
        except json.JSONDecodeError:
            continue
    return result
