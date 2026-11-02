from src.db import stats


def test_weekly_stats_take_the_role_played_most_that_week(monkeypatch):
    week_row = [0] * len(stats._LATEST_STATS_COLS)
    week_row[0] = "Alpha#NA1"
    calls = []

    def fake_execute(query, params=None, fetch=False, fetchone=False):
        calls.append(params)
        if len(calls) == 1:
            return [tuple(week_row)]
        # Role query: lower(riot_id), primary_role, ranked games on days with that role.
        return [("alpha#na1", "JUNGLE", 3), ("alpha#na1", "MIDDLE", 4), ("other#na1", "TOP", 9)]

    monkeypatch.setattr(stats, "db_execute", fake_execute)
    rows = stats.db_load_weekly_stats("2026-11-02", "2026-11-09")

    assert rows[0]["riot_id"] == "Alpha#NA1"
    assert rows[0]["primary_role"] == "MIDDLE"
    assert calls == [("2026-11-02", "2026-11-09")] * 2


def test_weekly_stats_skip_the_role_query_for_an_empty_week(monkeypatch):
    calls = []
    monkeypatch.setattr(stats, "db_execute", lambda *args, **kwargs: calls.append(args) or [])
    assert stats.db_load_weekly_stats("2026-11-02", "2026-11-09") == []
    assert len(calls) == 1
