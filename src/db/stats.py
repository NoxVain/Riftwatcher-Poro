from src.db.pool import db_execute


def db_upsert_daily_stats(day_date, riot_id, mode_records, performance_totals=None, primary_role=None):
    solo_wins = mode_records["solo_duo"]["wins"]
    solo_losses = mode_records["solo_duo"]["losses"]
    flex_wins = mode_records["flex"]["wins"]
    flex_losses = mode_records["flex"]["losses"]
    arcade_wins = 0
    arcade_losses = 0
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
    role = str(primary_role).upper() if primary_role else None
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
            primary_role,
            updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
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
            primary_role = COALESCE(EXCLUDED.primary_role, player_daily_stats.primary_role),
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
            role,
        ),
    )


_LATEST_STATS_COLS = (
    "riot_id", "solo_wins", "solo_losses", "flex_wins", "flex_losses",
    "arcade_wins", "arcade_losses", "total_wins", "total_losses", "updated_at",
    "cs_total", "minutes_total", "objective_damage", "player_damage", "healing",
    "damage_taken", "kills", "deaths", "vision_score", "primary_role",
)


def db_load_latest_stats(day_date):
    rows = db_execute(
        """
        SELECT DISTINCT ON (lower(riot_id))
            riot_id, solo_wins, solo_losses, flex_wins, flex_losses,
            arcade_wins, arcade_losses, total_wins, total_losses, updated_at,
            cs_total, minutes_total, objective_damage, player_damage, healing, damage_taken,
            kills, deaths, vision_score, primary_role
        FROM player_daily_stats
        WHERE day_date = %s
        ORDER BY lower(riot_id), updated_at DESC;
        """,
        (day_date,),
        fetch=True,
    )
    return [dict(zip(_LATEST_STATS_COLS, row)) for row in rows] if rows else []


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
    return [dict(zip(_LATEST_STATS_COLS, row)) for row in rows] if rows else []


_DAILY_STATS_PLAYER_COLS = (
    "solo_wins", "solo_losses", "flex_wins", "flex_losses",
    "arcade_wins", "arcade_losses", "cs_total", "minutes_total",
    "objective_damage", "player_damage", "healing", "damage_taken",
    "kills", "deaths", "vision_score", "primary_role",
)


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
            vision_score,
            primary_role
        FROM player_daily_stats
        WHERE day_date = %s AND lower(riot_id) = lower(%s)
        LIMIT 1;
        """,
        (day_date, riot_id),
        fetchone=True,
    )
    if row is None:
        return None
    return dict(zip(_DAILY_STATS_PLAYER_COLS, row))
