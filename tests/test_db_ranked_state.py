from datetime import datetime, timedelta, timezone

from src.db import ranked_state

START = datetime(2026, 11, 2, 5, tzinfo=timezone.utc)
END = START + timedelta(days=7)
QUEUES = ("RANKED_SOLO_5X5", "RANKED_FLEX_SR")


def test_belt_lp_totals_are_keyed_by_player_with_a_reliable_flag(monkeypatch):
    calls = []

    def fake_execute(query, params=None, fetch=False, fetchone=False):
        calls.append((params, fetch))
        # riot_id, sum of LP deltas, games seen, every row had a delta and no counter went down.
        return [("A#EUW", 120, 8, True), ("B#EUW", -45, 5, False), ("C#EUW", None, 1, False)]

    monkeypatch.setattr(ranked_state, "db_execute", fake_execute)
    assert ranked_state.db_load_belt_lp_totals(START, END, QUEUES) == {
        "A#EUW": {"lp_delta": 120, "games": 8, "reliable": True},
        "B#EUW": {"lp_delta": -45, "games": 5, "reliable": False},
        "C#EUW": {"lp_delta": None, "games": 1, "reliable": False},
    }
    assert calls == [((list(QUEUES), START, END), True)]


def test_belt_lp_totals_are_empty_without_history(monkeypatch):
    monkeypatch.setattr(ranked_state, "db_execute", lambda *args, **kwargs: None)
    assert ranked_state.db_load_belt_lp_totals(START, END, QUEUES) == {}


def test_player_lp_changes_are_one_game_ranked_rows_with_the_first_recorded_date(monkeypatch):
    calls = []

    def fake_execute(query, params=None, fetch=False, fetchone=False):
        calls.append((query, params))
        if fetchone:
            return (START,)
        return [("RANKED_SOLO_5X5", True, END, 21)]

    monkeypatch.setattr(ranked_state, "db_execute", fake_execute)
    changes, first = ranked_state.db_load_player_lp_changes("NoxVain#EUW", START, END)

    assert changes == [{"queue_type": "RANKED_SOLO_5X5", "won": True, "observed_at": END, "lp_delta": 21}]
    assert first == START
    assert calls[0][1] == ("NoxVain#EUW", ["RANKED_SOLO_5X5", "RANKED_FLEX_SR"], START, END)
    assert "wins_delta + losses_delta = 1" in calls[0][0] and "lp_delta IS NOT NULL" in calls[0][0]


def test_player_lp_changes_without_history(monkeypatch):
    monkeypatch.setattr(ranked_state, "db_execute", lambda *args, **kwargs: None)
    assert ranked_state.db_load_player_lp_changes("NoxVain#EUW", START, END) == ([], None)
