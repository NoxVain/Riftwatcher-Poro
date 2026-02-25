from src.db.pool import db_execute


def db_set_state(state_key, state_value):
    db_execute(
        """
        INSERT INTO bot_state (state_key, state_value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (state_key)
        DO UPDATE SET state_value = EXCLUDED.state_value, updated_at = NOW();
        """,
        (state_key, state_value),
    )


def db_get_state(state_key):
    row = db_execute(
        "SELECT state_value FROM bot_state WHERE state_key = %s LIMIT 1;",
        (state_key,),
        fetchone=True,
    )
    if not row:
        return None
    return row[0]


def db_set_last_report_message(channel_id, message_id):
    db_set_state("last_report_channel_id", str(channel_id))
    db_set_state("last_report_message_id", str(message_id))


def db_set_last_weekly_report_message(channel_id, message_id):
    db_set_state("last_weekly_report_channel_id", str(channel_id))
    db_set_state("last_weekly_report_message_id", str(message_id))


def db_get_last_report_message():
    channel_id = db_get_state("last_report_channel_id")
    message_id = db_get_state("last_report_message_id")
    if not channel_id or not message_id:
        return None, None
    try:
        return int(channel_id), int(message_id)
    except ValueError:
        return None, None


def db_get_last_weekly_report_message():
    channel_id = db_get_state("last_weekly_report_channel_id")
    message_id = db_get_state("last_weekly_report_message_id")
    if not channel_id or not message_id:
        return None, None
    try:
        return int(channel_id), int(message_id)
    except ValueError:
        return None, None


def db_last_seen_match_key(riot_id):
    return f"last_seen_match_id::{riot_id.casefold()}"


def db_get_last_seen_match_id(riot_id):
    return db_get_state(db_last_seen_match_key(riot_id))


def db_set_last_seen_match_id(riot_id, match_id):
    db_set_state(db_last_seen_match_key(riot_id), match_id)


def db_load_backfill_offsets():
    rows = db_execute(
        """
        SELECT state_key, state_value
        FROM bot_state
        WHERE state_key LIKE 'backfill_offset::%%';
        """,
        fetch=True,
    ) or []
    offsets = {}
    for state_key, state_value in rows:
        riot_key = str(state_key).split("::", 1)[1]
        try:
            offsets[riot_key] = max(0, int(state_value))
        except (TypeError, ValueError):
            continue
    return offsets
