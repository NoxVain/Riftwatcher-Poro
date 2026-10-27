"""Per-player match stats: extraction from cached payloads and the background sync."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src import match_stats
from src.db import cache as db_cache
from src.db import match_stats as db_match_stats
from src.match_stats import (
    STATS_COLUMNS,
    STATS_FINGERPRINT_KEY,
    STATS_WATERMARK_KEY,
    WATERMARK_OVERLAP_SECONDS,
    extract_player_match_rows,
    run_match_stats_sync_loop,
    stats_fingerprint,
    sync_player_match_stats,
)

NOX = "nox-puuid"
FRESH = "fresh-puuid"
TRACKED = {NOX, FRESH}
END_MS = 1_790_000_000_000


def player(puuid, team, position, **overrides):
    base = {
        "puuid": puuid,
        "teamId": team,
        "teamPosition": position,
        "championId": 1,
        "championName": "Annie",
        "win": team == 100,
        "kills": 2,
        "deaths": 3,
        "assists": 4,
        "totalDamageDealtToChampions": 10_000,
        "totalDamageTaken": 15_000,
        "goldEarned": 9_000,
        "totalMinionsKilled": 20,
        "neutralMinionsKilled": 0,
        "visionScore": 40,
        "wardsPlaced": 12,
        "wardsKilled": 3,
        "visionWardsBoughtInGame": 4,
        "turretTakedowns": 1,
        "firstBloodKill": False,
        "firstBloodAssist": False,
        "gameEndedInEarlySurrender": False,
        "gameEndedInSurrender": False,
    }
    base.update(overrides)
    return base


def match(match_id, participants, *, queue=420, duration=1800, end_ms=END_MS):
    return {
        "metadata": {"matchId": match_id, "participants": [p["puuid"] for p in participants]},
        "info": {
            "queueId": queue,
            "gameDuration": duration,
            "gameEndTimestamp": end_ms,
            "gameVersion": "16.21.1",
            "participants": participants,
        },
    }


def ranked_match(match_id="EUW1_1", **kwargs):
    return match(match_id, [
        player(NOX, 100, "UTILITY", championName="Zyra", championId=143, goldEarned=8_000, totalMinionsKilled=30,
               firstBloodAssist=True, challenges={
                   "killParticipation": 0.55, "teamDamagePercentage": 0.12, "dragonTakedowns": 2,
                   "baronTakedowns": 1, "soloKills": 0, "laneMinionsFirst10Minutes": 8,
                   "maxCsAdvantageOnLaneOpponent": 5,
               }),
        player(FRESH, 100, "BOTTOM"),
        player("ally", 100, "MIDDLE"),
        player("enemy-sup", 200, "UTILITY", championId=412, goldEarned=7_000, totalMinionsKilled=25),
        player("enemy-adc", 200, "BOTTOM"),
    ], **kwargs)


def test_extract_rows_for_each_tracked_player():
    rows = extract_player_match_rows(ranked_match(), TRACKED)

    assert sorted(row["puuid"] for row in rows) == [FRESH, NOX]
    assert all(set(row) == set(STATS_COLUMNS) for row in rows)
    nox = next(row for row in rows if row["puuid"] == NOX)
    assert nox["match_id"] == "EUW1_1"
    assert nox["queue_id"] == 420
    assert nox["game_end_at"] == datetime.fromtimestamp(END_MS / 1000, tz=timezone.utc)
    assert nox["duration_seconds"] == 1800
    assert nox["champion_name"] == "Zyra" and nox["team_position"] == "UTILITY"
    assert nox["win"] is True and nox["remake"] is False
    assert nox["kill_participation"] == 0.55 and nox["damage_share"] == 0.12
    assert nox["dragon_takedowns"] == 2 and nox["baron_takedowns"] == 1
    assert nox["first_blood"] is True
    assert nox["lane_cs_10"] == 8.0 and nox["max_cs_advantage"] == 5.0
    assert nox["opponent_champion_id"] == 412
    assert nox["opponent_gold_diff"] == 1_000
    assert nox["opponent_cs_diff"] == 5
    assert nox["arena_placement"] is None
    assert nox["tracked_teammates"] == [FRESH]


def test_extract_computes_shares_without_challenges():
    rows = extract_player_match_rows(ranked_match(), {FRESH})

    fresh = rows[0]
    # Team 100: 3 players x 2 kills; Fresh has 2 kills + 4 assists.
    assert fresh["kill_participation"] == 1.0
    assert fresh["damage_share"] == round(10_000 / 30_000, 4)
    assert fresh["dragon_takedowns"] is None and fresh["lane_cs_10"] is None
    assert fresh["tracked_teammates"] == []


def test_extract_marks_remakes_and_arena_placement():
    remake = match("EUW1_2", [player(NOX, 100, "TOP", gameEndedInEarlySurrender=True)], duration=200)
    arena = match("EUW1_3", [player(NOX, 100, "", placement=3)], queue=1700)

    assert extract_player_match_rows(remake, TRACKED)[0]["remake"] is True
    arena_row = extract_player_match_rows(arena, TRACKED)[0]
    assert arena_row["arena_placement"] == 3
    assert arena_row["team_position"] is None and arena_row["opponent_gold_diff"] is None


def test_extract_skips_stubs_and_untracked_matches():
    stub = {"metadata": {"matchId": "EUW1_4", "participants": [NOX]}, "info": {"queueId": 0, "participants": []}}

    assert extract_player_match_rows(stub, TRACKED) == []
    assert extract_player_match_rows({"info": {}}, TRACKED) == []
    assert extract_player_match_rows(ranked_match(), {"someone-else"}) == []


def test_stats_fingerprint_tracks_players_and_version(monkeypatch):
    base = stats_fingerprint(TRACKED)

    assert stats_fingerprint(set(reversed(sorted(TRACKED)))) == base
    assert stats_fingerprint({NOX}) != base
    monkeypatch.setattr(match_stats, "STATS_VERSION", match_stats.STATS_VERSION + 1)
    assert stats_fingerprint(TRACKED) != base


class FakeStore:
    def __init__(self, cached, state=None, fail_upsert=False):
        self.cached = cached  # [(match_id, cached_at, payload)]
        self.state = dict(state or {})
        self.fail_upsert = fail_upsert
        self.upserts = []
        self.deletes = []
        self.since_calls = []

    def load_tracked(self):
        return [("NoxVain#EUW", NOX), ("Freshwhite#EUW", FRESH), ("Pending#EUW", None)]

    def iter_cached(self, since):
        self.since_calls.append(since)
        return iter([entry for entry in self.cached if since is None or entry[1] > since])

    def upsert(self, rows):
        if self.fail_upsert:
            raise RuntimeError("db down")
        self.upserts.append(list(rows))

    def sync(self):
        return sync_player_match_stats(
            load_tracked=self.load_tracked,
            iter_cached_matches=self.iter_cached,
            upsert_rows=self.upsert,
            delete_rows_except=self.deletes.append,
            get_state=self.state.get,
            set_state=self.state.__setitem__,
        )


T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_first_sync_rebuilds_everything_and_saves_state():
    store = FakeStore([("EUW1_1", T0, ranked_match("EUW1_1")), ("EUW1_2", T0 + timedelta(minutes=5), ranked_match("EUW1_2"))])

    summary = store.sync()

    assert summary["mode"] == "full" and summary["matches"] == 2 and summary["rows"] == 4
    assert store.since_calls == [None]
    assert store.deletes == [sorted(TRACKED)]
    assert store.state[STATS_FINGERPRINT_KEY] == stats_fingerprint(TRACKED)
    assert store.state[STATS_WATERMARK_KEY] == (T0 + timedelta(minutes=5)).isoformat()


def test_later_sync_reads_only_recent_matches_with_overlap():
    watermark = T0 + timedelta(minutes=5)
    store = FakeStore(
        [("EUW1_1", T0, ranked_match("EUW1_1")), ("EUW1_3", T0 + timedelta(minutes=30), ranked_match("EUW1_3"))],
        state={STATS_FINGERPRINT_KEY: stats_fingerprint(TRACKED), STATS_WATERMARK_KEY: watermark.isoformat()},
    )

    summary = store.sync()

    assert summary["mode"] == "incremental" and summary["matches"] == 1 and summary["new_matches"] == 1
    assert summary["rows"] == 2
    assert store.since_calls == [watermark - timedelta(seconds=WATERMARK_OVERLAP_SECONDS)]
    assert store.deletes == []
    assert store.state[STATS_WATERMARK_KEY] == (T0 + timedelta(minutes=30)).isoformat()


def test_overlap_rereads_are_not_counted_as_new():
    store = FakeStore(
        [("EUW1_1", T0, ranked_match("EUW1_1"))],
        state={STATS_FINGERPRINT_KEY: stats_fingerprint(TRACKED), STATS_WATERMARK_KEY: T0.isoformat()},
    )

    summary = store.sync()

    assert summary["matches"] == 1 and summary["new_matches"] == 0
    assert store.state[STATS_WATERMARK_KEY] == T0.isoformat()


def test_sync_with_nothing_new_keeps_watermark():
    watermark = T0.isoformat()
    store = FakeStore([], state={STATS_FINGERPRINT_KEY: stats_fingerprint(TRACKED), STATS_WATERMARK_KEY: watermark})

    summary = store.sync()

    assert summary == {"mode": "incremental", "matches": 0, "new_matches": 0, "rows": 0, "elapsed_ms": summary["elapsed_ms"]}
    assert store.upserts == [] and store.state[STATS_WATERMARK_KEY] == watermark


@pytest.mark.parametrize("state", [
    {STATS_FINGERPRINT_KEY: "old-players", STATS_WATERMARK_KEY: T0.isoformat()},
    {STATS_FINGERPRINT_KEY: stats_fingerprint(TRACKED), STATS_WATERMARK_KEY: "garbage"},
    {STATS_FINGERPRINT_KEY: stats_fingerprint(TRACKED)},
])
def test_sync_rebuilds_when_players_change_or_watermark_is_unusable(state):
    store = FakeStore([("EUW1_1", T0, ranked_match("EUW1_1"))], state=state)

    assert store.sync()["mode"] == "full"
    assert store.since_calls == [None]


def test_failed_sync_saves_no_state_so_it_repeats():
    store = FakeStore([("EUW1_1", T0, ranked_match("EUW1_1"))], fail_upsert=True)

    with pytest.raises(RuntimeError):
        store.sync()

    assert STATS_FINGERPRINT_KEY not in store.state and STATS_WATERMARK_KEY not in store.state


def test_sync_upserts_in_batches(monkeypatch):
    monkeypatch.setattr(match_stats, "UPSERT_BATCH_ROWS", 3)
    store = FakeStore([(f"EUW1_{i}", T0 + timedelta(seconds=i), ranked_match(f"EUW1_{i}")) for i in range(4)])

    summary = store.sync()

    assert summary["rows"] == 8
    assert [len(batch) for batch in store.upserts] == [4, 4]


def test_sync_without_tracked_puuids_does_nothing():
    store = FakeStore([("EUW1_1", T0, ranked_match())])
    store.load_tracked = lambda: [("Pending#EUW", None)]

    assert store.sync() is None
    assert store.since_calls == [] and store.deletes == []


def run_loop(store, logs, **kwargs):
    return asyncio.run(run_match_stats_sync_loop(
        db_load_tracked_puuids=store.load_tracked,
        db_iter_cached_matches=store.iter_cached,
        db_upsert_player_match_stats=store.upsert,
        db_delete_player_match_stats_except=store.deletes.append,
        db_get_state=store.state.get,
        db_set_state=store.state.__setitem__,
        log=logs.append,
        interval_seconds=0,
        **kwargs,
    ))


def test_sync_loop_waits_for_remap_then_logs_full_pass(monkeypatch):
    monkeypatch.setattr(match_stats, "release_freed_memory", lambda: False)
    order = []
    store = FakeStore([("EUW1_1", T0, ranked_match())])
    original_iter = store.iter_cached
    store.iter_cached = lambda since: (order.append("sync"), original_iter(since))[1]
    logs = []

    async def remap():
        order.append("remap")

    async def main():
        await run_match_stats_sync_loop(
            db_load_tracked_puuids=store.load_tracked,
            db_iter_cached_matches=store.iter_cached,
            db_upsert_player_match_stats=store.upsert,
            db_delete_player_match_stats_except=store.deletes.append,
            db_get_state=store.state.get,
            db_set_state=store.state.__setitem__,
            log=logs.append,
            wait_for=asyncio.ensure_future(remap()),
            interval_seconds=0,
            max_passes=2,
        )

    asyncio.run(main())

    assert order == ["remap", "sync", "sync"]
    # The second pass only re-reads the overlap (no new matches), so it stays quiet.
    assert len(logs) == 1
    assert logs[0].startswith("[stats] Player match stats full sync: rows=2 matches=1 new=1 elapsed=")


def test_sync_loop_logs_failures_and_keeps_going(monkeypatch):
    monkeypatch.setattr(match_stats, "release_freed_memory", lambda: False)
    store = FakeStore([("EUW1_1", T0, ranked_match())], fail_upsert=True)
    logs = []

    run_loop(store, logs, max_passes=2)

    assert logs == ["[stats] Player match stats sync failed; retrying next pass: db down"] * 2


class FakeDb:
    def __init__(self, cached=()):
        self.cached = list(cached)  # [(match_id, updated_at, payload_text)]
        self.queries = []

    def execute(self, query, params=None, fetch=False, fetchone=False):
        self.queries.append((query, params))
        if query.startswith("SELECT match_id FROM match_info_cache"):
            since = params[0] if params else None
            return [(mid,) for mid, at, _ in sorted(self.cached, key=lambda e: e[1]) if since is None or at > since]
        if query.startswith("SELECT match_id, updated_at, payload FROM match_info_cache WHERE match_id = ANY"):
            return [(mid, at, text) for mid, at, text in reversed(self.cached) if mid in params[0]]
        return None


def test_db_iter_cached_matches_pages_oldest_first_and_filters_by_since(monkeypatch):
    fake = FakeDb([
        ("EUW1_3", T0 + timedelta(minutes=3), json.dumps({"n": 3})),
        ("EUW1_1", T0 + timedelta(minutes=1), json.dumps({"n": 1})),
        ("EUW1_2", T0 + timedelta(minutes=2), "not json"),
    ])
    monkeypatch.setattr(db_cache, "db_execute", fake.execute)

    everything = list(db_cache.db_iter_cached_matches(batch_size=2))
    recent = list(db_cache.db_iter_cached_matches(since=T0 + timedelta(minutes=1)))

    assert [(mid, payload["n"]) for mid, _at, payload in everything] == [("EUW1_1", 1), ("EUW1_3", 3)]
    assert sum(1 for q, _ in fake.queries if "ANY" in q) == 3  # two pages, then one page for `recent`
    assert [mid for mid, _at, _payload in recent] == ["EUW1_3"]


def test_db_upsert_player_match_stats_builds_one_statement(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(db_match_stats, "db_execute", fake.execute)
    rows = extract_player_match_rows(ranked_match(), TRACKED)

    db_match_stats.db_upsert_player_match_stats(rows)
    db_match_stats.db_upsert_player_match_stats([])

    assert len(fake.queries) == 1
    query, params = fake.queries[0]
    assert query.startswith(f"INSERT INTO player_match_stats ({', '.join(STATS_COLUMNS)}) VALUES (")
    assert "ON CONFLICT (puuid, match_id) DO UPDATE SET" in query and "match_id = EXCLUDED" not in query
    assert len(params) == 2 * len(STATS_COLUMNS) == query.count("%s")


def test_db_delete_player_match_stats_except_passes_puuid_list(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(db_match_stats, "db_execute", fake.execute)

    db_match_stats.db_delete_player_match_stats_except([NOX, FRESH])

    assert fake.queries == [("DELETE FROM player_match_stats WHERE NOT (puuid = ANY(%s));", ([NOX, FRESH],))]
