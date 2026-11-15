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
