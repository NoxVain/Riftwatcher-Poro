import json
import queue
import threading

try:
    import psycopg
except ImportError:
    psycopg = None

from src.config import DATABASE_URL, DB_POOL_SIZE


if psycopg is None:
    raise RuntimeError("DATABASE_URL is set but psycopg is not installed. Add psycopg[binary] to dependencies.")

DB_ENABLED = True
DB_POOL = None
DB_POOL_LOCK = threading.Lock()
DB_POOL_TOTAL = 0


def init_db_pool():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = queue.LifoQueue(maxsize=max(1, DB_POOL_SIZE))


def create_db_connection():
    return psycopg.connect(DATABASE_URL)


def db_acquire_connection():
    global DB_POOL_TOTAL
    if DB_POOL is None:
        init_db_pool()
    try:
        return DB_POOL.get_nowait()
    except queue.Empty:
        pass

    with DB_POOL_LOCK:
        if DB_POOL_TOTAL < max(1, DB_POOL_SIZE):
            DB_POOL_TOTAL += 1
            return create_db_connection()

    return DB_POOL.get()


def db_release_connection(conn, discard=False):
    global DB_POOL_TOTAL
    if conn is None:
        return
    if discard:
        try:
            conn.close()
        finally:
            with DB_POOL_LOCK:
                DB_POOL_TOTAL = max(0, DB_POOL_TOTAL - 1)
        return
    try:
        DB_POOL.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        finally:
            with DB_POOL_LOCK:
                DB_POOL_TOTAL = max(0, DB_POOL_TOTAL - 1)


def db_execute(query, params=None, fetch=False, fetchone=False):
    conn = db_acquire_connection()
    discard_conn = False
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetchone:
                result = cur.fetchone()
                conn.commit()
                return result
            if fetch:
                result = cur.fetchall()
                conn.commit()
                return result
        conn.commit()
    except Exception:
        discard_conn = True
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        db_release_connection(conn, discard=discard_conn)
    return None


def init_db():
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_players (
            riot_id TEXT PRIMARY KEY,
            puuid TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    db_execute("CREATE INDEX IF NOT EXISTS idx_tracked_players_riot_id_lower ON tracked_players (lower(riot_id));")
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS player_daily_stats (
            day_date DATE NOT NULL,
            riot_id TEXT NOT NULL,
            solo_wins INTEGER NOT NULL DEFAULT 0,
            solo_losses INTEGER NOT NULL DEFAULT 0,
            flex_wins INTEGER NOT NULL DEFAULT 0,
            flex_losses INTEGER NOT NULL DEFAULT 0,
            arcade_wins INTEGER NOT NULL DEFAULT 0,
            arcade_losses INTEGER NOT NULL DEFAULT 0,
            total_wins INTEGER NOT NULL DEFAULT 0,
            total_losses INTEGER NOT NULL DEFAULT 0,
            cs_total INTEGER NOT NULL DEFAULT 0,
            minutes_total DOUBLE PRECISION NOT NULL DEFAULT 0,
            objective_damage BIGINT NOT NULL DEFAULT 0,
            player_damage BIGINT NOT NULL DEFAULT 0,
            healing BIGINT NOT NULL DEFAULT 0,
            damage_taken BIGINT NOT NULL DEFAULT 0,
            kills INTEGER NOT NULL DEFAULT 0,
            deaths INTEGER NOT NULL DEFAULT 0,
            vision_score INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (day_date, riot_id)
        );
        """
    )
    db_execute(
        """
        CREATE INDEX IF NOT EXISTS idx_player_daily_stats_day_riot_lower_updated
        ON player_daily_stats (day_date, lower(riot_id), updated_at DESC);
        """
    )
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS cs_total INTEGER NOT NULL DEFAULT 0;")
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS minutes_total DOUBLE PRECISION NOT NULL DEFAULT 0;")
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS objective_damage BIGINT NOT NULL DEFAULT 0;")
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS player_damage BIGINT NOT NULL DEFAULT 0;")
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS healing BIGINT NOT NULL DEFAULT 0;")
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS damage_taken BIGINT NOT NULL DEFAULT 0;")
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS kills INTEGER NOT NULL DEFAULT 0;")
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS deaths INTEGER NOT NULL DEFAULT 0;")
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS vision_score INTEGER NOT NULL DEFAULT 0;")
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS match_info_cache (
            match_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS player_ranked_state (
            riot_id TEXT NOT NULL,
            queue_type TEXT NOT NULL,
            tier TEXT NULL,
            rank_division TEXT NULL,
            league_points INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            hot_streak BOOLEAN NOT NULL DEFAULT FALSE,
            veteran BOOLEAN NOT NULL DEFAULT FALSE,
            fresh_blood BOOLEAN NOT NULL DEFAULT FALSE,
            inactive BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (riot_id, queue_type)
        );
        """
    )
    db_execute(
        """
        CREATE INDEX IF NOT EXISTS idx_player_ranked_state_riot_lower_queue
        ON player_ranked_state (lower(riot_id), queue_type);
        """
    )


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


def db_load_backfill_offsets():
    rows = db_execute(
        """
        SELECT state_key, state_value
        FROM bot_state
        WHERE state_key LIKE 'backfill_offset::%';
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
    return rows


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


def db_upsert_daily_stats(day_date, riot_id, mode_records, performance_totals=None):
    solo_wins = mode_records["solo_duo"]["wins"]
    solo_losses = mode_records["solo_duo"]["losses"]
    flex_wins = mode_records["flex"]["wins"]
    flex_losses = mode_records["flex"]["losses"]
    arcade_wins = mode_records["arcade"]["wins"]
    arcade_losses = mode_records["arcade"]["losses"]
    total_wins = solo_wins + flex_wins
    total_losses = solo_losses + flex_losses
    stats = performance_totals or {}
    cs_total = int(stats.get("cs_total", 0) or 0)
    minutes_total = float(stats.get("minutes_total", 0.0) or 0.0)
    objective_damage = int(stats.get("objective_damage", 0) or 0)
    player_damage = int(stats.get("player_damage", 0) or 0)
    healing = int(stats.get("healing", 0) or 0)
    damage_taken = int(stats.get("damage_taken", 0) or 0)
    kills = int(stats.get("kills", 0) or 0)
    deaths = int(stats.get("deaths", 0) or 0)
    vision_score = int(stats.get("vision_score", 0) or 0)
    db_execute(
        """
        INSERT INTO player_daily_stats (
            day_date, riot_id,
            solo_wins, solo_losses,
            flex_wins, flex_losses,
            arcade_wins, arcade_losses,
            total_wins, total_losses,
            cs_total, minutes_total,
            objective_damage, player_damage, healing, damage_taken,
            kills, deaths, vision_score,
            updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
        )
        ON CONFLICT (day_date, riot_id)
        DO UPDATE SET
            solo_wins = EXCLUDED.solo_wins,
            solo_losses = EXCLUDED.solo_losses,
            flex_wins = EXCLUDED.flex_wins,
            flex_losses = EXCLUDED.flex_losses,
            arcade_wins = EXCLUDED.arcade_wins,
            arcade_losses = EXCLUDED.arcade_losses,
            total_wins = EXCLUDED.total_wins,
            total_losses = EXCLUDED.total_losses,
            cs_total = EXCLUDED.cs_total,
            minutes_total = EXCLUDED.minutes_total,
            objective_damage = EXCLUDED.objective_damage,
            player_damage = EXCLUDED.player_damage,
            healing = EXCLUDED.healing,
            damage_taken = EXCLUDED.damage_taken,
            kills = EXCLUDED.kills,
            deaths = EXCLUDED.deaths,
            vision_score = EXCLUDED.vision_score,
            updated_at = NOW();
        """,
        (
            day_date,
            riot_id,
            solo_wins,
            solo_losses,
            flex_wins,
            flex_losses,
            arcade_wins,
            arcade_losses,
            total_wins,
            total_losses,
            cs_total,
            minutes_total,
            objective_damage,
            player_damage,
            healing,
            damage_taken,
            kills,
            deaths,
            vision_score,
        ),
    )


def db_load_latest_stats(day_date):
    rows = db_execute(
        """
        SELECT DISTINCT ON (lower(riot_id))
            riot_id, solo_wins, solo_losses, flex_wins, flex_losses,
            arcade_wins, arcade_losses, total_wins, total_losses, updated_at,
            cs_total, minutes_total, objective_damage, player_damage, healing, damage_taken, kills, deaths, vision_score
        FROM player_daily_stats
        WHERE day_date = %s
        ORDER BY lower(riot_id), updated_at DESC;
        """,
        (day_date,),
        fetch=True,
    )
    return rows or []


def db_load_weekly_stats(start_day_date, end_day_date_exclusive):
    rows = db_execute(
        """
        SELECT
            MIN(riot_id) AS riot_id,
            COALESCE(SUM(solo_wins), 0) AS solo_wins,
            COALESCE(SUM(solo_losses), 0) AS solo_losses,
            COALESCE(SUM(flex_wins), 0) AS flex_wins,
            COALESCE(SUM(flex_losses), 0) AS flex_losses,
            COALESCE(SUM(arcade_wins), 0) AS arcade_wins,
            COALESCE(SUM(arcade_losses), 0) AS arcade_losses,
            COALESCE(SUM(total_wins), 0) AS total_wins,
            COALESCE(SUM(total_losses), 0) AS total_losses,
            MAX(updated_at) AS updated_at,
            COALESCE(SUM(cs_total), 0) AS cs_total,
            COALESCE(SUM(minutes_total), 0.0) AS minutes_total,
            COALESCE(SUM(objective_damage), 0) AS objective_damage,
            COALESCE(SUM(player_damage), 0) AS player_damage,
            COALESCE(SUM(healing), 0) AS healing,
            COALESCE(SUM(damage_taken), 0) AS damage_taken,
            COALESCE(SUM(kills), 0) AS kills,
            COALESCE(SUM(deaths), 0) AS deaths,
            COALESCE(SUM(vision_score), 0) AS vision_score
        FROM player_daily_stats
        WHERE day_date >= %s AND day_date < %s
        GROUP BY lower(riot_id)
        ORDER BY lower(MIN(riot_id));
        """,
        (start_day_date, end_day_date_exclusive),
        fetch=True,
    )
    return rows or []


def db_get_daily_stats_for_player(day_date, riot_id):
    row = db_execute(
        """
        SELECT
            solo_wins, solo_losses,
            flex_wins, flex_losses,
            arcade_wins, arcade_losses,
            cs_total,
            minutes_total,
            objective_damage,
            player_damage,
            healing,
            damage_taken,
            kills,
            deaths,
            vision_score
        FROM player_daily_stats
        WHERE day_date = %s AND lower(riot_id) = lower(%s)
        LIMIT 1;
        """,
        (day_date, riot_id),
        fetchone=True,
    )
    return row
