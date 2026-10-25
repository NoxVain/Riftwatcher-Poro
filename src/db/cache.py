import json

from src.db.pool import db_acquire_connection, db_execute, db_release_connection
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


MATCH_PAYLOAD_BATCH_SIZE = 200


def db_load_match_payloads_for_baseline(limit=cfg.BASELINE_MATCH_LIMIT, batch_size=MATCH_PAYLOAD_BATCH_SIZE):
    """Yield cached match payloads, newest first; limit <= 0 means all.

    Streams through a server-side cursor in batches so only one batch of payloads is
    in memory at a time (loading thousands at once cost GBs of RAM). This does
    blocking DB I/O while iterated, so consume it in a worker thread.
    """
    safe_limit = int(limit)
    query = "SELECT payload FROM match_info_cache ORDER BY updated_at DESC"
    params = ()
    if safe_limit > 0:
        query += " LIMIT %s"
        params = (safe_limit,)
    conn = db_acquire_connection()
    discard_conn = False
    try:
        # A named cursor is server-side: rows arrive itersize at a time.
        with conn.cursor(name="match_payload_scan") as cur:
            cur.itersize = max(1, int(batch_size))
            cur.execute(query, params)
            for (payload_text,) in cur:
                if not payload_text:
                    continue
                try:
                    yield json.loads(payload_text)
                except json.JSONDecodeError:
                    continue
        conn.commit()
    except BaseException as exc:
        # GeneratorExit (caller stopped early) is not a DB fault: roll back and reuse the
        # connection. Anything else is treated like db_execute does: discard it.
        discard_conn = not isinstance(exc, GeneratorExit)
        try:
            conn.rollback()
        except Exception:
            discard_conn = True
        raise
    finally:
        db_release_connection(conn, discard=discard_conn)
