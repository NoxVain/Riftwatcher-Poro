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


MATCH_PAYLOAD_BATCH_SIZE = 50


def db_load_match_payloads_for_baseline(limit=cfg.BASELINE_MATCH_LIMIT, batch_size=MATCH_PAYLOAD_BATCH_SIZE):
    """Yield cached match payloads, newest first; limit <= 0 means all.

    Reads in pages so only one page of payloads is in memory at a time (loading
    thousands at once cost GBs of RAM). One small query lists the match IDs in order;
    each page of payloads is then its own short query by primary key. No server-side
    cursor or long transaction: a streaming cursor stalled mid-FETCH in production
    (2026-10-26) and froze every caller. This does blocking DB I/O while iterated,
    so consume it in a worker thread.
    """
    safe_limit = int(limit)
    query = "SELECT match_id FROM match_info_cache ORDER BY updated_at DESC"
    params = ()
    if safe_limit > 0:
        query += " LIMIT %s"
        params = (safe_limit,)
    match_ids = [row[0] for row in (db_execute(f"{query};", params, fetch=True) or [])]
    page_size = max(1, int(batch_size))
    for start in range(0, len(match_ids), page_size):
        page_ids = match_ids[start:start + page_size]
        rows = db_execute(
            "SELECT match_id, payload FROM match_info_cache WHERE match_id = ANY(%s);",
            (page_ids,),
            fetch=True,
        ) or []
        payload_by_id = dict(rows)
        # Keep newest-first order; skip rows deleted (cache cleanup) since the ID query.
        for match_id in page_ids:
            payload_text = payload_by_id.get(match_id)
            if not payload_text:
                continue
            try:
                yield json.loads(payload_text)
            except json.JSONDecodeError:
                continue


PUUID_REWRITE_BATCH_SIZE = 500


def db_rewrite_cached_puuids(remap, batch_size=PUUID_REWRITE_BATCH_SIZE):
    """Replace PUUIDs in cached payloads ({old: new}); returns the number of rows changed.

    The replacement runs in Postgres on the payload text (quoted, so only whole PUUID
    values match), so payloads never pass through the bot's memory. Short updates by
    pages of match IDs keep each transaction brief. `updated_at` is left alone: it
    drives cache retention and baseline ordering.
    """
    if not remap:
        return 0
    pairs = [(f'"{old}"', f'"{new}"') for old, new in remap.items()]
    expression = "payload"
    replace_params = []
    for old, new in pairs:
        expression = f"replace({expression}, %s, %s)"
        replace_params.extend((old, new))
    condition = " OR ".join("strpos(payload, %s) > 0" for _ in pairs)
    condition_params = [old for old, _ in pairs]
    query = (
        f"UPDATE match_info_cache SET payload = {expression} "
        f"WHERE match_id = ANY(%s) AND ({condition}) RETURNING match_id;"
    )
    match_ids = [row[0] for row in (db_execute("SELECT match_id FROM match_info_cache;", fetch=True) or [])]
    page_size = max(1, int(batch_size))
    changed = 0
    for start in range(0, len(match_ids), page_size):
        rows = db_execute(
            query,
            (*replace_params, match_ids[start:start + page_size], *condition_params),
            fetch=True,
        ) or []
        changed += len(rows)
    return changed


def db_iter_cached_matches(since=None, batch_size=MATCH_PAYLOAD_BATCH_SIZE):
    """Yield (match_id, updated_at, payload) oldest-cached first, optionally only rows
    cached after `since` (a datetime). Paged like db_load_match_payloads_for_baseline,
    so only one page of payloads is in memory; consume it in a worker thread.
    """
    if since is None:
        id_rows = db_execute("SELECT match_id FROM match_info_cache ORDER BY updated_at, match_id;", fetch=True)
    else:
        id_rows = db_execute(
            "SELECT match_id FROM match_info_cache WHERE updated_at > %s ORDER BY updated_at, match_id;",
            (since,),
            fetch=True,
        )
    match_ids = [row[0] for row in (id_rows or [])]
    page_size = max(1, int(batch_size))
    for start in range(0, len(match_ids), page_size):
        page_ids = match_ids[start:start + page_size]
        rows = db_execute(
            "SELECT match_id, updated_at, payload FROM match_info_cache WHERE match_id = ANY(%s);",
            (page_ids,),
            fetch=True,
        ) or []
        by_id = {match_id: (updated_at, payload) for match_id, updated_at, payload in rows}
        for match_id in page_ids:
            found = by_id.get(match_id)
            if not found or not found[1]:
                continue
            try:
                yield match_id, found[0], json.loads(found[1])
            except json.JSONDecodeError:
                continue
