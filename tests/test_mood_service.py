import asyncio
from datetime import datetime, timedelta, timezone

from src.mood_service import MoodService


class FakeRiotClient:
    def __init__(self):
        self.today_records_by_riot_id = {}
        self.puuid_by_riot_id = {}
        self.puuid_cache = {}
        self.recent_ids_by_puuid = {}
        self.match_info_by_id = {}
        self.clear_match_cache_calls = 0
        self.get_today_mode_records_calls = []

    def clear_match_cache(self):
        self.clear_match_cache_calls += 1

    async def get_today_mode_records(self, riot_id):
        self.get_today_mode_records_calls.append(riot_id)
        return self.today_records_by_riot_id[riot_id]

    async def fetch_puuid(self, riot_id):
        return self.puuid_by_riot_id[riot_id]

    async def fetch_recent_match_ids(self, puuid, count=20, riot_id=None):
        _ = count, riot_id
        return self.recent_ids_by_puuid.get(puuid, [])

    async def fetch_match_info(self, match_id):
        return self.match_info_by_id[match_id]

    @staticmethod
    def get_participant(match_info, puuid):
        for participant in match_info["info"]["participants"]:
            if participant.get("puuid") == puuid:
                return participant
        return None


def _create_service(
    *,
    friends,
    riot_client,
    db_load_latest_stats=lambda _cycle_key: [],
    db_upsert_daily_stats=lambda *_args, **_kwargs: None,
    db_get_daily_stats_for_player=lambda _cycle_key, _riot_id: None,
    db_get_last_seen_match_id=lambda _riot_id: None,
    db_set_last_seen_match_id=lambda _riot_id, _match_id: None,
):
    return MoodService(
        log=lambda _message: None,
        friends=friends,
        riot_client=riot_client,
        report_timezone=timezone.utc,
        report_day_start_hour=0,
        report_cache_seconds=120,
        daily_refresh_seconds=300,
        db_enabled=True,
        db_load_latest_stats=db_load_latest_stats,
        db_upsert_daily_stats=db_upsert_daily_stats,
        db_get_daily_stats_for_player=db_get_daily_stats_for_player,
        db_get_last_seen_match_id=db_get_last_seen_match_id,
        db_set_last_seen_match_id=db_set_last_seen_match_id,
        db_health_stats=lambda: {"db_ok": True, "match_cache_entries": 0},
        db_load_match_payloads_for_baseline=lambda _limit: [],
    )


def _stats_row(riot_id, updated_at, *, solo_wins=0, solo_losses=0, flex_wins=0, flex_losses=0):
    return {
        "riot_id": riot_id,
        "solo_wins": solo_wins,
        "solo_losses": solo_losses,
        "flex_wins": flex_wins,
        "flex_losses": flex_losses,
        "arcade_wins": 0,
        "arcade_losses": 0,
        "total_wins": solo_wins + flex_wins,
        "total_losses": solo_losses + flex_losses,
        "updated_at": updated_at,
        "cs_total": 0,
        "minutes_total": 0.0,
        "objective_damage": 0,
        "player_damage": 0,
        "healing": 0,
        "damage_taken": 0,
        "kills": 0,
        "deaths": 0,
        "vision_score": 0,
        "primary_role": None,
    }


def test_get_new_match_ids_when_last_seen_present():
    recent_ids = ["m5", "m4", "m3", "m2"]
    assert MoodService.get_new_match_ids(recent_ids, "m3") == ["m5", "m4"]


def test_get_new_match_ids_when_last_seen_missing():
    recent_ids = ["m5", "m4"]
    assert MoodService.get_new_match_ids(recent_ids, None) == ["m5", "m4"]


def test_get_new_match_ids_when_no_new_matches():
    recent_ids = ["m5", "m4"]
    assert MoodService.get_new_match_ids(recent_ids, "m5") == []


def test_get_leader_badges_by_player_marks_category_leads():
    ranked_results = [
        (
            "Alpha",
            {},
            5,
            2,
            71.4,
            {
                "cs_total": 180,
                "minutes_total": 30.0,
                "objective_damage": 10000,
                "player_damage": 20000,
                "healing": 18000,
                "damage_taken": 15000,
                "kills": 10,
                "deaths": 4,
                "vision_score": 20,
            },
        ),
        (
            "Bravo",
            {},
            4,
            3,
            57.1,
            {
                "cs_total": 120,
                "minutes_total": 20.0,
                "objective_damage": 9000,
                "player_damage": 22000,
                "healing": 9000,
                "damage_taken": 22000,
                "kills": 8,
                "deaths": 6,
                "vision_score": 25,
            },
        ),
    ]

    badges = MoodService.get_leader_badges_by_player(ranked_results)
    assert "\U0001F33E" in badges["Alpha"]
    assert "\U0001F3F0" in badges["Alpha"]
    assert "\u2764\uFE0F" in badges["Alpha"]
    assert "\U0001F5E1\uFE0F" in badges["Alpha"]
    assert "\U0001F4A5" in badges["Bravo"]
    assert "\U0001F6E1\uFE0F" in badges["Bravo"]
    assert "\u2620\uFE0F" in badges["Bravo"]
    assert "\U0001F441\uFE0F" in badges["Bravo"]


def test_build_report_falls_back_to_live_when_snapshot_stale():
    riot = FakeRiotClient()
    riot.today_records_by_riot_id["Alpha#NA1"] = (
        {"solo_duo": {"wins": 2, "losses": 1}, "flex": {"wins": 0, "losses": 0}},
        {
            "cs_total": 0,
            "minutes_total": 0.0,
            "objective_damage": 0,
            "player_damage": 0,
            "healing": 0,
            "damage_taken": 0,
            "kills": 0,
            "deaths": 0,
            "vision_score": 0,
        },
    )
    stale_row = _stats_row("Alpha#NA1", datetime.now(tz=timezone.utc) - timedelta(hours=2), solo_wins=1, solo_losses=1)
    service = _create_service(
        friends=["Alpha#NA1"],
        riot_client=riot,
        db_load_latest_stats=lambda _cycle_key: [stale_row],
    )

    report = asyncio.run(service.build_today_win_rate_report())

    assert "Alpha" in report
    assert riot.clear_match_cache_calls == 1
    assert riot.get_today_mode_records_calls == ["Alpha#NA1"]


def test_build_report_uses_snapshot_when_only_one_player_has_ranked_games():
    riot = FakeRiotClient()
    riot.today_records_by_riot_id["Alpha#NA1"] = (
        {"solo_duo": {"wins": 2, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
        {
            "cs_total": 0,
            "minutes_total": 0.0,
            "objective_damage": 0,
            "player_damage": 0,
            "healing": 0,
            "damage_taken": 0,
            "kills": 0,
            "deaths": 0,
            "vision_score": 0,
        },
    )
    riot.today_records_by_riot_id["Bravo#NA1"] = (
        {"solo_duo": {"wins": 1, "losses": 1}, "flex": {"wins": 0, "losses": 0}},
        {
            "cs_total": 0,
            "minutes_total": 0.0,
            "objective_damage": 0,
            "player_damage": 0,
            "healing": 0,
            "damage_taken": 0,
            "kills": 0,
            "deaths": 0,
            "vision_score": 0,
        },
    )
    fresh_row = _stats_row("Alpha#NA1", datetime.now(tz=timezone.utc), solo_wins=3, solo_losses=0)
    service = _create_service(
        friends=["Alpha#NA1", "Bravo#NA1"],
        riot_client=riot,
        db_load_latest_stats=lambda _cycle_key: [fresh_row],
    )

    report = asyncio.run(service.build_today_win_rate_report())

    assert "Alpha" in report
    assert "Bravo" not in report
    assert riot.get_today_mode_records_calls == []


def test_build_weekly_report_aggregates_full_week_rows():
    riot = FakeRiotClient()
    weekly_rows = [
        {
            "riot_id": "Alpha#NA1",
            "solo_wins": 3,
            "solo_losses": 1,
            "flex_wins": 2,
            "flex_losses": 0,
            "arcade_wins": 0,
            "arcade_losses": 0,
            "total_wins": 5,
            "total_losses": 1,
            "updated_at": datetime.now(tz=timezone.utc),
            "cs_total": 500,
            "minutes_total": 100.0,
            "objective_damage": 1000,
            "player_damage": 2000,
            "healing": 300,
            "damage_taken": 4000,
            "kills": 20,
            "deaths": 10,
            "vision_score": 30,
        }
    ]
    service = _create_service(
        friends=["Alpha#NA1"],
        riot_client=riot,
    )
    service.db_load_weekly_stats = lambda _start, _end: weekly_rows

    report = asyncio.run(service.build_weekly_win_rate_report())

    assert "LEAGUE MOOD (WEEKLY)" in report
    assert "Alpha" in report
    assert "`5W-1L`" in report


def test_get_week_window_respects_day_start_cutoff():
    service = MoodService(
        log=lambda _message: None,
        friends=[],
        riot_client=FakeRiotClient(),
        report_timezone=timezone.utc,
        report_day_start_hour=6,
        report_cache_seconds=120,
        daily_refresh_seconds=300,
        db_enabled=True,
        db_load_latest_stats=lambda _day: [],
        db_load_weekly_stats=lambda _start, _end: [],
        db_upsert_daily_stats=lambda *_args, **_kwargs: None,
        db_get_daily_stats_for_player=lambda _day, _riot_id: None,
        db_get_last_seen_match_id=lambda _riot_id: None,
        db_set_last_seen_match_id=lambda _riot_id, _match_id: None,
        db_health_stats=lambda: {"db_ok": True, "match_cache_entries": 0},
    )

    before_cutoff = datetime(2026, 2, 23, 5, 30, tzinfo=timezone.utc)  # Monday 05:30, still previous cycle-day
    week_start, week_end_exclusive = service.get_week_window(now_utc=before_cutoff)

    assert week_start.isoformat() == "2026-02-16"
    assert week_end_exclusive.isoformat() == "2026-02-23"


def test_refresh_recent_matches_snapshot_updates_baseline_stats():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id["Alpha#NA1"] = "puuid-alpha"
    riot.recent_ids_by_puuid["puuid-alpha"] = ["m3", "m2", "m1"]
    riot.match_info_by_id["m2"] = {
        "info": {
            "queueId": 420,
            "gameDuration": 1800,
            "gameEndTimestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            "participants": [
                {
                    "puuid": "puuid-alpha",
                    "win": True,
                    "totalMinionsKilled": 100,
                    "neutralMinionsKilled": 20,
                    "damageDealtToObjectives": 1000,
                    "totalDamageDealtToChampions": 2000,
                    "totalHeal": 300,
                    "totalDamageTaken": 4000,
                    "kills": 5,
                    "deaths": 2,
                    "visionScore": 10,
                }
            ],
        }
    }
    riot.match_info_by_id["m3"] = {
        "info": {
            "queueId": 420,
            "gameDuration": 1800,
            "gameEndTimestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            "participants": [
                {
                    "puuid": "puuid-alpha",
                    "win": False,
                    "totalMinionsKilled": 90,
                    "neutralMinionsKilled": 10,
                    "damageDealtToObjectives": 900,
                    "totalDamageDealtToChampions": 1500,
                    "totalHeal": 200,
                    "totalDamageTaken": 3500,
                    "kills": 3,
                    "deaths": 4,
                    "visionScore": 7,
                }
            ],
        }
    }

    upserts = []
    last_seen_writes = []
    baseline_row = {
        "solo_wins": 0, "solo_losses": 0, "flex_wins": 0, "flex_losses": 0,
        "arcade_wins": 0, "arcade_losses": 0, "cs_total": 50, "minutes_total": 10.0,
        "objective_damage": 100, "player_damage": 200, "healing": 30,
        "damage_taken": 40, "kills": 1, "deaths": 1, "vision_score": 2,
    }
    service = _create_service(
        friends=["Alpha#NA1"],
        riot_client=riot,
        db_get_last_seen_match_id=lambda _riot_id: "m1",
        db_get_daily_stats_for_player=lambda _cycle_key, _riot_id: baseline_row,
        db_upsert_daily_stats=lambda cycle_key, riot_id, mode_records, performance_totals, primary_role=None: upserts.append(
            (cycle_key, riot_id, mode_records, performance_totals)
        ),
        db_set_last_seen_match_id=lambda riot_id, match_id: last_seen_writes.append((riot_id, match_id)),
    )
    service.report_cache["text"] = "cached"
    service.report_cache["day"] = service.get_cycle_key()
    service.report_cache["expires_at"] = 9999999999.0

    asyncio.run(service.refresh_recent_matches_snapshot(recent_count=20))

    assert len(upserts) == 1
    _cycle_key, riot_id, mode_records, performance_totals = upserts[0]
    assert riot_id == "Alpha#NA1"
    assert mode_records["solo_duo"]["wins"] == 1
    assert mode_records["solo_duo"]["losses"] == 1
    assert performance_totals["cs_total"] == 270
    assert performance_totals["minutes_total"] == 70.0
    assert last_seen_writes == [("Alpha#NA1", "m3")]
    assert service.report_cache["text"] is None


def test_refresh_recent_matches_snapshot_ignores_remakes():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id["Alpha#NA1"] = "puuid-alpha"
    riot.recent_ids_by_puuid["puuid-alpha"] = ["m3", "m2", "m1"]
    riot.match_info_by_id["m3"] = {
        "info": {
            "queueId": 420,
            "gameDuration": 240,
            "gameEndTimestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            "participants": [{"puuid": "puuid-alpha", "win": True}],
        }
    }
    riot.match_info_by_id["m2"] = {
        "info": {
            "queueId": 420,
            "gameDuration": 1800,
            "gameEndTimestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            "participants": [{"puuid": "puuid-alpha", "win": False}],
        }
    }

    upserts = []
    service = _create_service(
        friends=["Alpha#NA1"],
        riot_client=riot,
        db_get_last_seen_match_id=lambda _riot_id: "m1",
        db_get_daily_stats_for_player=lambda _cycle_key, _riot_id: {
            "solo_wins": 0, "solo_losses": 0, "flex_wins": 0, "flex_losses": 0,
            "arcade_wins": 0, "arcade_losses": 0, "cs_total": 0, "minutes_total": 0.0,
            "objective_damage": 0, "player_damage": 0, "healing": 0, "damage_taken": 0,
            "kills": 0, "deaths": 0, "vision_score": 0,
        },
        db_upsert_daily_stats=lambda cycle_key, riot_id, mode_records, performance_totals, primary_role=None: upserts.append(
            (cycle_key, riot_id, mode_records, performance_totals)
        ),
    )

    asyncio.run(service.refresh_recent_matches_snapshot(recent_count=20))

    assert len(upserts) == 1
    _cycle_key, _riot_id, mode_records, performance_totals = upserts[0]
    assert mode_records["solo_duo"]["wins"] == 0
    assert mode_records["solo_duo"]["losses"] == 1
    assert performance_totals["minutes_total"] == 30.0


def test_refresh_recent_matches_snapshot_falls_back_when_baseline_missing():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id["Alpha#NA1"] = "puuid-alpha"
    riot.recent_ids_by_puuid["puuid-alpha"] = ["m2", "m1"]
    riot.today_records_by_riot_id["Alpha#NA1"] = (
        {"solo_duo": {"wins": 3, "losses": 2}, "flex": {"wins": 0, "losses": 0}},
        {
            "cs_total": 100,
            "minutes_total": 30.0,
            "objective_damage": 1000,
            "player_damage": 2000,
            "healing": 300,
            "damage_taken": 4000,
            "kills": 10,
            "deaths": 5,
            "vision_score": 20,
        },
    )

    upserts = []
    last_seen_writes = []
    service = _create_service(
        friends=["Alpha#NA1"],
        riot_client=riot,
        db_get_last_seen_match_id=lambda _riot_id: "m1",
        db_get_daily_stats_for_player=lambda _cycle_key, _riot_id: None,
        db_upsert_daily_stats=lambda cycle_key, riot_id, mode_records, performance_totals, primary_role=None: upserts.append(
            (cycle_key, riot_id, mode_records, performance_totals)
        ),
        db_set_last_seen_match_id=lambda riot_id, match_id: last_seen_writes.append((riot_id, match_id)),
    )
    service.report_cache["text"] = "cached"
    service.report_cache["day"] = service.get_cycle_key()
    service.report_cache["expires_at"] = 9999999999.0

    asyncio.run(service.refresh_recent_matches_snapshot(recent_count=20))

    assert len(upserts) == 1
    assert upserts[0][1] == "Alpha#NA1"
    assert last_seen_writes == [("Alpha#NA1", "m2")]
    assert riot.get_today_mode_records_calls == ["Alpha#NA1"]
    assert service.report_cache["text"] is None


def test_refresh_daily_stats_once_starts_with_oldest_collected_player():
    riot = FakeRiotClient()
    riot.today_records_by_riot_id["Alpha#NA1"] = (
        {"solo_duo": {"wins": 1, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
        {"cs_total": 0, "minutes_total": 0.0},
    )
    riot.today_records_by_riot_id["Bravo#NA1"] = (
        {"solo_duo": {"wins": 0, "losses": 1}, "flex": {"wins": 0, "losses": 0}},
        {"cs_total": 0, "minutes_total": 0.0},
    )
    riot.today_records_by_riot_id["Charlie#NA1"] = (
        {"solo_duo": {"wins": 2, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
        {"cs_total": 0, "minutes_total": 0.0},
    )

    now = datetime.now(tz=timezone.utc)
    rows = [
        _stats_row("Alpha#NA1", now - timedelta(minutes=5), solo_wins=1),
        _stats_row("Bravo#NA1", now - timedelta(minutes=20), solo_losses=1),
    ]
    service = _create_service(
        friends=["Alpha#NA1", "Bravo#NA1", "Charlie#NA1"],
        riot_client=riot,
        db_load_latest_stats=lambda _cycle_key: rows,
    )

    asyncio.run(service.refresh_daily_stats_once())

    assert riot.get_today_mode_records_calls == ["Charlie#NA1", "Bravo#NA1", "Alpha#NA1"]


def test_run_health_check_includes_backfill_offsets_for_tracked_players():
    service = MoodService(
        log=lambda _message: None,
        friends=["Alpha#NA1", "Bravo#NA1"],
        riot_client=FakeRiotClient(),
        report_timezone=timezone.utc,
        report_day_start_hour=6,
        report_cache_seconds=120,
        daily_refresh_seconds=300,
        db_enabled=True,
        db_load_latest_stats=lambda _day: [],
        db_load_weekly_stats=lambda _start, _end: [],
        db_upsert_daily_stats=lambda *_args, **_kwargs: None,
        db_get_daily_stats_for_player=lambda _day, _riot_id: None,
        db_get_last_seen_match_id=lambda _riot_id: None,
        db_set_last_seen_match_id=lambda _riot_id, _match_id: None,
        db_health_stats=lambda: {"db_ok": True, "match_cache_entries": 42},
        db_load_backfill_offsets=lambda: {
            "alpha#na1": 250,
            "charlie#na1": 900,
        },
    )

    stats = asyncio.run(service.run_health_check(start_monotonic=0.0))

    assert stats["db_ok"] is True
    assert stats["match_cache_entries"] == 42
    assert stats["players_with_backfill_offset"] == 1
    assert stats["max_backfill_offset"] == 250
    assert stats["top_backfill_offsets"] == ["Alpha#NA1=250"]


def test_backfill_daily_stats_from_cache_rebuilds_range_from_cached_payloads():
    riot = FakeRiotClient()
    riot.puuid_cache["alpha#na1"] = "puuid-alpha"

    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    payloads = [
        {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": int(now.timestamp() * 1000),
                "participants": [
                    {
                        "puuid": "puuid-alpha",
                        "teamId": 100,
                        "win": True,
                        "kills": 6,
                        "assists": 4,
                        "deaths": 2,
                        "totalMinionsKilled": 120,
                        "neutralMinionsKilled": 20,
                        "damageDealtToObjectives": 2000,
                        "totalDamageDealtToChampions": 18000,
                        "totalHeal": 500,
                        "totalDamageTaken": 12000,
                        "visionScore": 18,
                        "goldEarned": 12000,
                    },
                    {"puuid": "ally", "teamId": 100, "kills": 8},
                    {"puuid": "enemy", "teamId": 200, "kills": 10},
                ],
            }
        }
    ]

    upserts = []
    service = MoodService(
        log=lambda _msg: None,
        friends=["Alpha#NA1"],
        riot_client=riot,
        report_timezone=timezone.utc,
        report_day_start_hour=6,
        report_cache_seconds=120,
        daily_refresh_seconds=300,
        db_enabled=True,
        db_load_latest_stats=lambda _day: [],
        db_load_weekly_stats=lambda _start, _end: [],
        db_upsert_daily_stats=lambda cycle_key, riot_id, mode_records, performance_totals, primary_role=None: upserts.append(
            (cycle_key, riot_id, mode_records, performance_totals, primary_role)
        ),
        db_get_daily_stats_for_player=lambda _day, _riot_id: None,
        db_get_last_seen_match_id=lambda _riot_id: None,
        db_set_last_seen_match_id=lambda _riot_id, _match_id: None,
        db_health_stats=lambda: {"db_ok": True, "match_cache_entries": 1},
        db_load_match_payloads_for_baseline=lambda _limit: payloads,
    )

    result = asyncio.run(
        service.backfill_daily_stats_from_cache(
            start_day_date=now.date(),
            end_day_date=now.date(),
            max_payloads=0,
        )
    )

    assert result["upserts"] == 1
    assert result["matched_matches"] == 1
    assert result["matched_participants"] == 1
    assert result["players_without_puuid"] == 0
    assert len(upserts) == 1
    cycle_key, riot_id, mode_records, performance_totals, _primary_role = upserts[0]
    assert cycle_key == now.date().isoformat()
    assert riot_id == "Alpha#NA1"
    assert mode_records["solo_duo"]["wins"] == 1
    assert performance_totals["assists"] == 4
    assert performance_totals["gold_earned"] == 12000
    assert performance_totals["kill_participation_num"] == 10
    assert performance_totals["kill_participation_den"] == 14
