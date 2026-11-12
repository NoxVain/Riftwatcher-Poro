from datetime import datetime, timedelta, timezone

from src.db import ranked_state


def test_weekly_lp_query_scopes_tracked_players_queues_and_observation_window(monkeypatch):
    calls = []

    def execute(query, params, *, fetch):
        calls.append((query, params, fetch))
        return [("A#EUW", 120, 8), ("B#EUW", -45, 5)]

    monkeypatch.setattr(ranked_state, "db_execute", execute)
    start = datetime(2026, 11, 2, 5, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    assert ranked_state.db_load_belt_lp_totals(start, end) == {
        "A#EUW": {"lp_delta": 120, "games": 8}, "B#EUW": {"lp_delta": -45, "games": 5},
    }
    query, params, fetch = calls[0]
    assert fetch and params == (["RANKED_SOLO_5X5", "RANKED_FLEX_SR"], start, end)
    assert "JOIN lp_history" in query and "lower(h.riot_id) = lower(t.riot_id)" in query
    assert "observed_at >= %s" in query and "observed_at < %s" in query
    assert "sum(h.lp_delta)" in query and "sum(h.wins_delta + h.losses_delta)" in query
    # Reject a player's whole week if any included observation is unknown or a reset.
    assert "bool_and(h.lp_delta IS NOT NULL AND h.wins_delta >= 0 AND h.losses_delta >= 0)" in query
    # Multi-game polls, dodges and decay contribute to net LP; do not filter on won.
    assert "h.won" not in query


def test_no_stored_lp_is_an_empty_result(monkeypatch):
    monkeypatch.setattr(ranked_state, "db_execute", lambda *args, **kwargs: None)
    assert ranked_state.db_load_belt_lp_totals(None, None) == {}
