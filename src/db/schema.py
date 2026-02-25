from src.db.pool import db_execute


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
    db_execute("ALTER TABLE player_daily_stats ADD COLUMN IF NOT EXISTS primary_role TEXT NULL;")
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
