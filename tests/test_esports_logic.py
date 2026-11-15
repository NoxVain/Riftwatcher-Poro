from datetime import date, datetime, timezone

from src.esports_logic import (
    COMPLETION_GRACE_SECONDS,
    align_event,
    events_in_window,
    format_announcement,
    format_daily_schedule,
    format_weekly_schedule,
    match_winner_index,
    parse_schedule_event,
    reconcile_events,
    restore_resolved_teams,
    snapshot_entry,
)


def diff_events(previous_matches, events, *, now_unix):
    """Announcements only."""
    return reconcile_events(previous_matches, events, now_unix=now_unix)[0]


def raw_event(match_id="m1", state="inProgress", wins=(0, 0), outcomes=(None, None), start="2026-10-15T18:00:00Z", best_of=5, block="Play-Ins", event_type="match"):
    return {
        "startTime": start,
        "state": state,
        "type": event_type,
        "blockName": block,
        "league": {"name": "Worlds", "slug": "worlds"},
        "match": {
            "id": match_id,
            "teams": [
                {"name": "Karmine Corp", "code": "KC", "result": {"outcome": outcomes[0], "gameWins": wins[0]}},
                {"name": "Cloud9 Kia", "code": "C9", "result": {"outcome": outcomes[1], "gameWins": wins[1]}},
            ],
            "strategy": {"type": "bestOf", "count": best_of},
        },
    }


def event(**kwargs):
    return parse_schedule_event(raw_event(**kwargs))


NOW = int(datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc).timestamp())


# --- parsing ----------------------------------------------------------------


def test_parse_schedule_event_normalizes_fields():
    parsed = event(wins=(1, 2))
    assert parsed["match_id"] == "m1"
    assert parsed["start_unix"] == int(datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc).timestamp())
    assert parsed["league"] == "Worlds"
    assert parsed["block"] == "Play-Ins"
    assert parsed["best_of"] == 5
    assert [t["code"] for t in parsed["teams"]] == ["KC", "C9"]
    assert [t["wins"] for t in parsed["teams"]] == [1, 2]


def test_parse_schedule_event_skips_non_match_and_malformed():
    assert parse_schedule_event(raw_event(event_type="show")) is None
    broken = raw_event()
    broken["match"]["teams"] = broken["match"]["teams"][:1]
    assert parse_schedule_event(broken) is None
    assert parse_schedule_event(None) is None


def test_parse_schedule_event_handles_tbd_teams_without_results():
    raw = raw_event(state="unstarted")
    raw["match"]["teams"] = [{"name": "TBD", "code": "TBD"}, {"name": "TBD", "code": "TBD"}]
    parsed = parse_schedule_event(raw)
    assert [t["wins"] for t in parsed["teams"]] == [0, 0]


def test_match_winner_prefers_outcome_then_game_wins():
    assert match_winner_index(event(state="completed", wins=(2, 3), outcomes=("loss", "win"))) == 1
    assert match_winner_index(event(state="completed", wins=(3, 1))) == 0
    assert match_winner_index(event(state="completed", wins=(1, 1))) is None


# --- diffing ----------------------------------------------------------------


def test_diff_announces_live_when_match_starts():
    previous = {"m1": snapshot_entry(event(state="unstarted"))}
    result = diff_events(previous, [event(state="inProgress")], now_unix=NOW)
    assert [a["kind"] for a in result] == ["live"]


def test_diff_announces_game_win_with_winner_and_game_number():
    previous = {"m1": snapshot_entry(event(wins=(0, 1)))}
    result = diff_events(previous, [event(wins=(1, 1))], now_unix=NOW)
    assert len(result) == 1
    assert result[0]["kind"] == "game"
    assert result[0]["winner_index"] == 0
    assert result[0]["game_number"] == 2


def test_diff_live_and_game_when_start_and_first_game_missed():
    previous = {"m1": snapshot_entry(event(state="unstarted"))}
    result = diff_events(previous, [event(wins=(0, 1))], now_unix=NOW)
    assert [a["kind"] for a in result] == ["live", "game"]
    assert result[1]["winner_index"] == 1


def test_diff_score_update_when_both_teams_gained():
    previous = {"m1": snapshot_entry(event(wins=(0, 0)))}
    result = diff_events(previous, [event(wins=(1, 1))], now_unix=NOW)
    assert result[0]["kind"] == "game"
    assert result[0]["winner_index"] is None


def test_diff_announces_match_win_once():
    previous = {"m1": snapshot_entry(event(wins=(2, 1)))}
    completed = event(state="completed", wins=(3, 1), outcomes=("win", "loss"))
    result = diff_events(previous, [completed], now_unix=NOW)
    assert [a["kind"] for a in result] == ["match"]
    assert result[0]["winner_index"] == 0

    previous = {"m1": snapshot_entry(completed)}
    assert diff_events(previous, [completed], now_unix=NOW) == []


def test_diff_no_changes_no_announcements():
    current = event(wins=(1, 0))
    assert diff_events({"m1": snapshot_entry(current)}, [current], now_unix=NOW) == []
    unstarted = event(state="unstarted")
    assert diff_events({"m1": snapshot_entry(unstarted)}, [unstarted], now_unix=NOW) == []


def test_diff_new_inprogress_match_announces_live_without_game_spam():
    result = diff_events({}, [event(wins=(1, 1))], now_unix=NOW)
    assert [a["kind"] for a in result] == ["live"]


def test_diff_new_completed_match_only_if_recent():
    recent = event(state="completed", wins=(3, 0), start="2026-10-15T12:00:00Z")
    old = event(match_id="old", state="completed", wins=(3, 0), start="2025-11-09T07:00:00Z")
    result = diff_events({}, [recent, old], now_unix=NOW)
    assert [a["event"]["match_id"] for a in result] == ["m1"]


# --- regressions from the Worlds 2026 Play-Ins log --------------------------


def swapped(parsed):
    return {**parsed, "teams": [parsed["teams"][1], parsed["teams"][0]]}


def test_stale_state_regression_keeps_snapshot_and_does_not_repost():
    completed = event(state="completed", wins=(3, 0), outcomes=("win", "loss"))
    stored = snapshot_entry(completed)
    announcements, entries = reconcile_events({"m1": stored}, [event(wins=(2, 0))], now_unix=NOW)
    assert announcements == []
    assert entries["m1"] == stored
    # The fresh response afterwards must not re-announce the series.
    assert diff_events(entries, [completed], now_unix=NOW) == []


def test_stale_live_regression_does_not_repost_live():
    stored = snapshot_entry(event(state="inProgress"))
    announcements, entries = reconcile_events({"m1": stored}, [event(state="unstarted")], now_unix=NOW)
    assert announcements == []
    assert diff_events(entries, [event(state="inProgress")], now_unix=NOW) == []


def test_stale_lower_score_does_not_repost_game():
    stored = snapshot_entry(event(wins=(0, 1)))
    announcements, entries = reconcile_events({"m1": stored}, [event(wins=(0, 0))], now_unix=NOW)
    assert announcements == []
    assert diff_events(entries, [event(wins=(0, 1))], now_unix=NOW) == []


def test_align_event_keeps_first_seen_team_order():
    stored = snapshot_entry(event(state="unstarted"))  # KC vs C9
    flipped = swapped(event(wins=(0, 1)))  # API now lists C9 first; C9 won game 1
    aligned = align_event(flipped, stored)
    assert [t["code"] for t in aligned["teams"]] == ["KC", "C9"]
    result = diff_events({"m1": stored}, [aligned], now_unix=NOW)
    game = [a for a in result if a["kind"] == "game"][0]
    assert aligned["teams"][game["winner_index"]]["code"] == "C9"
    assert "**Karmine Corp** vs **Cloud9 Kia**" in format_announcement(game)


def test_align_event_ignores_tbd_and_unrelated_orders():
    tbd = parse_schedule_event({**raw_event(state="unstarted"), "match": {**raw_event()["match"], "teams": [{"name": "TBD", "code": "TBD"}, {"name": "TBD", "code": "TBD"}]}})
    real = event(state="unstarted")
    assert align_event(real, snapshot_entry(tbd)) is real
    assert align_event(real, None) is real


def test_completed_with_lagging_score_is_held_until_final():
    previous = {"m1": snapshot_entry(event(wins=(2, 1)))}
    lagging = event(state="completed", wins=(2, 1), outcomes=("win", "loss"))
    announcements, entries = reconcile_events(previous, [lagging], now_unix=NOW)
    assert announcements == []
    assert entries["m1"]["state"] == "inProgress"
    assert entries["m1"]["pending_since"] == NOW

    # Still lagging a minute later: keep waiting from the original timestamp.
    announcements, entries = reconcile_events(entries, [lagging], now_unix=NOW + 60)
    assert announcements == []
    assert entries["m1"]["pending_since"] == NOW

    final = event(state="completed", wins=(3, 1), outcomes=("win", "loss"))
    announcements, entries = reconcile_events(entries, [final], now_unix=NOW + 120)
    assert [a["kind"] for a in announcements] == ["match"]
    assert "wins the series 3-1" in format_announcement(announcements[0])
    assert entries["m1"]["state"] == "completed"


def test_completed_with_lagging_score_posts_settled_score_after_grace():
    previous = {"m1": {**snapshot_entry(event(wins=(2, 1))), "pending_since": NOW - COMPLETION_GRACE_SECONDS}}
    lagging = event(state="completed", wins=(2, 1), outcomes=("win", "loss"))
    announcements, _ = reconcile_events(previous, [lagging], now_unix=NOW)
    assert len(announcements) == 1
    assert "**Karmine Corp** wins the series 3-1" in format_announcement(announcements[0])


def test_bo1_completed_before_score_is_held():
    previous = {"m1": snapshot_entry(event(best_of=1))}
    lagging = event(state="completed", wins=(0, 0), best_of=1)
    assert diff_events(previous, [lagging], now_unix=NOW) == []
    final = event(state="completed", wins=(0, 1), outcomes=("loss", "win"), best_of=1)
    result = diff_events(previous, [final], now_unix=NOW)
    assert "**Cloud9 Kia** wins" in format_announcement(result[0])


def test_deciding_game_is_left_to_the_series_announcement():
    previous = {"m1": snapshot_entry(event(wins=(2, 0)))}
    assert diff_events(previous, [event(wins=(3, 0))], now_unix=NOW) == []
    bo1_previous = {"m1": snapshot_entry(event(best_of=1))}
    assert diff_events(bo1_previous, [event(wins=(1, 0), best_of=1)], now_unix=NOW) == []
    bo3_previous = {"m1": snapshot_entry(event(wins=(1, 0), best_of=3))}
    result = diff_events(bo3_previous, [event(wins=(1, 1), best_of=3)], now_unix=NOW)
    assert [a["kind"] for a in result] == ["game"]


def tbd_event(**kwargs):
    raw = raw_event(state="unstarted", **kwargs)
    raw["match"]["teams"] = [{"name": "TBD", "code": "TBD"}, {"name": "TBD", "code": "TBD"}]
    return parse_schedule_event(raw)


def test_tbd_resolving_is_accepted_but_reverting_is_stale():
    resolved = event(state="unstarted")
    announcements, entries = reconcile_events({"m1": snapshot_entry(tbd_event())}, [resolved], now_unix=NOW)
    assert announcements == []
    assert entries["m1"]["codes"] == ["KC", "C9"]

    announcements, entries = reconcile_events(entries, [tbd_event()], now_unix=NOW)
    assert announcements == []
    assert entries["m1"]["codes"] == ["KC", "C9"]


def test_restore_resolved_teams_uses_stored_names():
    entry = snapshot_entry(event(state="unstarted"))
    restored = restore_resolved_teams(tbd_event(), entry)
    assert [t["name"] for t in restored["teams"]] == ["Karmine Corp", "Cloud9 Kia"]
    plain = tbd_event()
    assert restore_resolved_teams(plain, snapshot_entry(tbd_event())) is plain
    assert restore_resolved_teams(plain, {"codes": ["KC", "C9"]}) is plain  # legacy entry without names
    resolved = event(state="unstarted")
    assert restore_resolved_teams(resolved, entry) is resolved


def test_weekly_schedule_without_status_omits_live_and_scores():
    days = [(date(2026, 10, 23), [event(state="completed", wins=(3, 1), start="2026-10-23T17:00:00Z")])]
    text = format_weekly_schedule(days, date(2026, 10, 19), show_status=False)[0]
    assert "KC 3-1 C9" not in text and "\u2705" not in text


# --- formatting -------------------------------------------------------------


def test_format_game_announcement_with_spoilers():
    text = format_announcement(
        {"kind": "game", "event": event(wins=(1, 1)), "winner_index": 0, "game_number": 2},
        spoilers=True,
    )
    assert "Worlds Play-Ins" in text
    assert "**Karmine Corp** vs **Cloud9 Kia**" in text
    assert "Game 2: ||**Karmine Corp** wins \u00B7 KC 1-1 C9||" in text


def test_format_match_announcement_bo5_and_bo1_without_spoilers():
    bo5 = event(state="completed", wins=(1, 3), outcomes=("loss", "win"))
    text = format_announcement({"kind": "match", "event": bo5, "winner_index": 1}, spoilers=False)
    assert "**Cloud9 Kia** wins the series 3-1" in text
    assert "||" not in text

    bo1 = event(state="completed", wins=(1, 0), best_of=1)
    text = format_announcement({"kind": "match", "event": bo1, "winner_index": 0}, spoilers=True)
    assert "||**Karmine Corp** wins||" in text


def test_format_live_announcement():
    text = format_announcement({"kind": "live", "event": event()})
    assert text.startswith("\U0001F534 **LIVE**")
    assert "Bo5" in text


def test_daily_schedule_groups_and_marks_state():
    events = [
        event(match_id="a", state="completed", wins=(3, 1), start="2026-10-15T12:00:00Z"),
        event(match_id="b", state="inProgress", wins=(1, 1), start="2026-10-15T18:00:00Z"),
        event(match_id="c", state="unstarted", start="2026-10-15T23:00:00Z", block="Swiss"),
    ]
    text = format_daily_schedule(events, "2026-10-15", spoilers=True)
    assert "Esports today" in text and "2026-10-15" in text
    assert "**Worlds Play-Ins**" in text and "**Worlds Swiss**" in text
    assert "||KC 3-1 C9||" in text
    assert "LIVE" in text
    assert f"<t:{events[2]['start_unix']}:t>" in text


def test_daily_schedule_none_when_empty():
    assert format_daily_schedule([], "2026-10-15") is None


def test_weekly_schedule_groups_by_day_with_stages():
    friday = [
        event(match_id="a", state="unstarted", start="2026-10-23T17:00:00Z", block="Swiss", best_of=1),
        event(match_id="b", state="unstarted", start="2026-10-23T18:00:00Z", block="Swiss", best_of=1),
    ]
    sunday = [event(match_id="c", state="completed", wins=(2, 1), start="2026-10-25T19:00:00Z", block="Swiss", best_of=3)]
    days = [(date(2026, 10, 19), []), (date(2026, 10, 23), friday), (date(2026, 10, 25), sunday)]
    messages = format_weekly_schedule(days, date(2026, 10, 19), spoilers=True)
    assert len(messages) == 1
    text = messages[0]
    assert text.startswith("\U0001F5D3\uFE0F **Esports this week** \u00B7 Mon 19 Oct \u2013 Sun 25 Oct")
    assert "**Mon 19 Oct**" not in text
    assert "**Fri 23 Oct** \u00B7 Worlds Swiss" in text
    assert "**Sun 25 Oct** \u00B7 Worlds Swiss" in text
    assert text.count("\u2022 ") == 3
    assert "||KC 2-1 C9||" in text
    assert text.endswith("_Times shown in your local timezone._")


def test_weekly_schedule_empty_week_returns_no_messages():
    assert format_weekly_schedule([(date(2026, 10, 19), [])], date(2026, 10, 19)) == []


def test_weekly_schedule_splits_long_weeks_between_days():
    days = []
    for offset in range(4):
        day = date(2026, 10, 23 + offset)
        events = [
            event(match_id=f"{offset}-{i}", state="unstarted", start=f"2026-10-{23 + offset}T{17 + i}:00:00Z")
            for i in range(4)
        ]
        days.append((day, events))
    messages = format_weekly_schedule(days, date(2026, 10, 19), limit=600)
    assert len(messages) > 1
    assert all(len(m) <= 600 for m in messages)
    assert messages[0].startswith("\U0001F5D3")
    assert messages[-1].endswith("_Times shown in your local timezone._")
    assert sum(m.count("\u2022 ") for m in messages) == 16
    # Splits land on day boundaries, so every follow-up message starts with a day header.
    assert all(m.startswith("**") for m in messages[1:])


def test_events_in_window_filters_and_sorts():
    late = event(match_id="late", start="2026-10-16T05:00:00Z")
    early = event(match_id="early", start="2026-10-15T07:00:00Z")
    outside = event(match_id="outside", start="2026-10-16T07:00:00Z")
    start = int(datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc).timestamp())
    result = events_in_window([late, outside, early], start, start + 24 * 3600)
    assert [e["match_id"] for e in result] == ["early", "late"]
