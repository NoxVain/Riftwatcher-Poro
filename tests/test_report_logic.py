from datetime import datetime, timezone

from src.report_logic import (
    create_mode_records,
    format_mode_line,
    get_match_duration_seconds,
    get_report_cycle_key,
    get_report_cycle_start_unix_seconds,
    get_match_end_unix_seconds,
    get_mode_bucket,
    get_mode_totals,
    is_remake_match,
    is_match_in_report_cycle,
    is_match_in_last_24h,
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
    records["arcade"]["wins"] = 99
    assert get_mode_totals(records) == (2, 1)


def test_get_match_end_unix_seconds_falls_back_when_end_timestamp_missing():
    match_info = {"info": {"gameCreation": 1_000_000, "gameDuration": 120}}
    assert get_match_end_unix_seconds(match_info) == 1120


def test_is_match_in_last_24h_uses_game_end_timestamp():
    now_ts = 2_000_000
    recent_match = {"info": {"gameEndTimestamp": (now_ts - 60) * 1000}}
    old_match = {"info": {"gameEndTimestamp": (now_ts - (24 * 60 * 60) - 1) * 1000}}

    assert is_match_in_last_24h(recent_match, now_ts=now_ts)
    assert not is_match_in_last_24h(old_match, now_ts=now_ts)


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
