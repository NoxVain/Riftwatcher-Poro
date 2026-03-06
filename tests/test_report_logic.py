from datetime import datetime, timezone

from src.report_logic import (
    accumulate_participant_performance,
    compute_gamer_score,
    compute_perf_percentile,
    create_performance_totals,
    create_mode_records,
    format_mode_line,
    gamer_score_weights_for_games,
    get_match_duration_seconds,
    get_report_cycle_key,
    get_report_cycle_start_unix_seconds,
    get_match_end_unix_seconds,
    get_mode_bucket,
    get_mode_totals,
    is_remake_match,
    is_match_in_report_cycle,
    rank_sort_key,
    wilson_lower_bound,
)


def test_get_mode_bucket_maps_ranked_and_ignores_other_queues():
    assert get_mode_bucket(420) == "solo_duo"
    assert get_mode_bucket(440) == "flex"
    assert get_mode_bucket(450) is None
    assert get_mode_bucket(1700) is None


def test_wilson_lower_bound_prefers_consistent_sample():
    strong_large_sample = wilson_lower_bound(9, 1)
    perfect_tiny_sample = wilson_lower_bound(1, 0)
    assert strong_large_sample > perfect_tiny_sample


def test_wilson_default_is_stricter_than_legacy_z():
    default_score = wilson_lower_bound(9, 1)
    legacy_score = wilson_lower_bound(9, 1, z=1.28)
    assert default_score < legacy_score


def test_gamer_score_weights_ramp_perf_component():
    win_w_1, perf_w_1 = gamer_score_weights_for_games(1)
    win_w_4, perf_w_4 = gamer_score_weights_for_games(4)
    win_w_8, perf_w_8 = gamer_score_weights_for_games(8)
    win_w_20, perf_w_20 = gamer_score_weights_for_games(20)

    assert win_w_1 > win_w_4 > win_w_8
    assert perf_w_1 < perf_w_4 < perf_w_8
    assert perf_w_8 == perf_w_20


def test_compute_perf_percentile_uses_metric_weights():
    baselines = {
        "MIDDLE": {
            "cs_per_min": [4.0, 6.0],  # weighted higher
            "healing_per_min": [100.0, 200.0],  # weighted lower
        }
    }
    player = {
        "cs_per_min": 6.0,  # top percentile on high-weight stat
        "healing_per_min": 100.0,  # mid percentile on low-weight stat
    }

    weighted = compute_perf_percentile(player, "MIDDLE", baselines)
    equal_avg = (1.0 + 0.5) / 2.0
    assert weighted > equal_avg


def test_compute_gamer_score_ramps_performance_weight_by_volume():
    baselines = {
        "MIDDLE": {
            "cs_per_min": [4.0, 5.0, 6.0],
            "player_damage_per_min": [300.0, 500.0, 700.0],
        }
    }
    perf_totals = {
        "minutes_total": 30.0,
        "cs_total": 180,
        "player_damage": 21000,
        "objective_damage": 0,
        "healing": 0,
        "damage_taken": 0,
        "kills": 0,
        "deaths": 0,
        "vision_score": 0,
    }

    low_volume = compute_gamer_score(1, 0, perf_totals, "MIDDLE", baselines)
    high_volume = compute_gamer_score(12, 0, perf_totals, "MIDDLE", baselines)
    low_volume_wilson = wilson_lower_bound(1, 0) * 100
    high_volume_wilson = wilson_lower_bound(12, 0) * 100

    # Perf contribution exists in both, but high-volume score should pull further away
    # from pure Wilson because perf weight has fully ramped in.
    assert abs(high_volume - high_volume_wilson) > abs(low_volume - low_volume_wilson)


def test_rank_sort_key_orders_by_wilson_then_volume():
    rows = [
        ("OneGame", {}, 1, 0, 100.0),
        ("Solid", {}, 9, 1, 90.0),
        ("Mid", {}, 5, 5, 50.0),
    ]
    ranked = sorted(rows, key=rank_sort_key)
    assert [row[0] for row in ranked] == ["Solid", "OneGame", "Mid"]


def test_format_mode_line_handles_empty_and_non_empty():
    assert format_mode_line("Arcade", 0, 0) == "   Arcade: `0W-0L` - **N/A**"
    assert format_mode_line("Arcade", 3, 1) == "   Arcade: `3W-1L` - **75.0%**"


def test_get_mode_totals_counts_ranked_only():
    records = create_mode_records()
    records["solo_duo"]["wins"] = 2
    records["flex"]["losses"] = 1
    assert get_mode_totals(records) == (2, 1)


def test_accumulate_participant_performance_collects_extended_stats_and_kp():
    totals = create_performance_totals()
    match_info = {
        "info": {
            "participants": [
                {"teamId": 100, "kills": 5},
                {"teamId": 100, "kills": 7},
                {"teamId": 200, "kills": 9},
            ]
        }
    }
    participant = {
        "teamId": 100,
        "totalMinionsKilled": 120,
        "neutralMinionsKilled": 20,
        "damageDealtToObjectives": 3000,
        "totalDamageDealtToChampions": 25000,
        "totalHeal": 1500,
        "totalDamageTaken": 18000,
        "kills": 8,
        "assists": 6,
        "deaths": 3,
        "visionScore": 22,
        "goldEarned": 14000,
        "wardsPlaced": 10,
        "wardsKilled": 4,
        "turretTakedowns": 2,
        "dragonTakedowns": 1,
        "baronTakedowns": 1,
        "doubleKills": 1,
        "tripleKills": 0,
        "quadraKills": 0,
        "pentaKills": 0,
        "teamPosition": "MIDDLE",
    }

    accumulate_participant_performance(totals, participant, 1800, match_info=match_info)

    assert totals["minutes_total"] == 30.0
    assert totals["cs_total"] == 140
    assert totals["assists"] == 6
    assert totals["gold_earned"] == 14000
    assert totals["wards_placed"] == 10
    assert totals["turret_takedowns"] == 2
    assert totals["double_kills"] == 1
    assert totals["kill_participation_num"] == 14
    assert totals["kill_participation_den"] == 12


def test_get_match_end_unix_seconds_falls_back_when_end_timestamp_missing():
    match_info = {"info": {"gameCreation": 1_000_000, "gameDuration": 120}}
    assert get_match_end_unix_seconds(match_info) == 1120



def test_report_cycle_start_uses_previous_day_before_cutoff():
    tz = timezone.utc
    now_utc = datetime(2026, 2, 18, 5, 30, tzinfo=timezone.utc)
    start_ts = get_report_cycle_start_unix_seconds(tz, day_start_hour=6, now_utc=now_utc)
    expected = int(datetime(2026, 2, 17, 6, 0, tzinfo=timezone.utc).timestamp())
    assert start_ts == expected


def test_report_cycle_key_changes_at_cutoff():
    tz = timezone.utc
    before = datetime(2026, 2, 18, 5, 59, tzinfo=timezone.utc)
    after = datetime(2026, 2, 18, 6, 0, tzinfo=timezone.utc)
    assert get_report_cycle_key(tz, day_start_hour=6, now_utc=before) == "2026-02-17"
    assert get_report_cycle_key(tz, day_start_hour=6, now_utc=after) == "2026-02-18"


def test_is_match_in_report_cycle_uses_cutoff_window():
    tz = timezone.utc
    now_utc = datetime(2026, 2, 18, 10, 0, tzinfo=timezone.utc)
    in_cycle = {"info": {"gameEndTimestamp": int(datetime(2026, 2, 18, 6, 0, tzinfo=timezone.utc).timestamp() * 1000)}}
    old = {"info": {"gameEndTimestamp": int(datetime(2026, 2, 18, 5, 59, tzinfo=timezone.utc).timestamp() * 1000)}}
    assert is_match_in_report_cycle(in_cycle, tz, day_start_hour=6, now_utc=now_utc)
    assert not is_match_in_report_cycle(old, tz, day_start_hour=6, now_utc=now_utc)


def test_get_match_duration_seconds_normalizes_seconds_or_milliseconds():
    assert get_match_duration_seconds({"info": {"gameDuration": 1800}}) == 1800
    assert get_match_duration_seconds({"info": {"gameDuration": 1_800_000}}) == 1800


def test_is_remake_match_detects_early_surrender_or_short_duration():
    early_surrender = {
        "info": {
            "gameDuration": 1300,
            "participants": [{"gameEndedInEarlySurrender": True}],
        }
    }
    short_game = {"info": {"gameDuration": 240, "participants": [{}]}}
    normal_game = {"info": {"gameDuration": 1800, "participants": [{}]}}

    assert is_remake_match(early_surrender)
    assert is_remake_match(short_game)
    assert not is_remake_match(normal_game)
