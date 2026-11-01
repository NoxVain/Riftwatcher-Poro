import asyncio

from src.discord_backfill_worker import BACKFILL_REVISIT_SECONDS, BackfillCycleResult, process_backfill_cycle


class FakeRiotClient:
    def __init__(self):
        self.puuid_by_riot_id = {}
        self.page_ids_by_puuid = {}
        self.match_info_by_id = {}
        self.fetched_match_ids = []
        self.puuid_requests = []
        self.page_requests = []

    async def fetch_puuid(self, riot_id, *, request_tier="priority"):
        _ = request_tier
        self.puuid_requests.append(riot_id)
        return self.puuid_by_riot_id[riot_id]

    async def fetch_match_ids_page(self, puuid, *, start=0, count=100, request_tier="priority", riot_id=None):
        _ = count, request_tier, riot_id
        self.page_requests.append((puuid, start))
        pages = self.page_ids_by_puuid.get(puuid, {})
        return pages.get(start, [])

    async def fetch_match_info(self, match_id, *, cache_in_memory=True, request_tier="priority"):
        _ = cache_in_memory, request_tier
        self.fetched_match_ids.append(match_id)
        return self.match_info_by_id.get(match_id, {"info": {}})


def test_backfill_fetches_missing_older_matches_up_to_limit_and_advances_offset():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id["Alpha#EUW"] = "puuid-alpha"
    riot.page_ids_by_puuid["puuid-alpha"] = {
        0: ["m5", "m4", "m3", "m2", "m1"],
    }
    state = {}
    db_cache = {"m1": {"cached": True}, "m2": {"cached": True}}

    total = asyncio.run(
        process_backfill_cycle(
            friends=["Alpha#EUW"],
            riot_client=riot,
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.__setitem__(key, value),
            db_get_match_info=lambda match_id: db_cache.get(match_id),
            recent_ids_count=100,
            per_player_limit=3,
            log=lambda _msg: None,
        )
    )

    assert riot.fetched_match_ids == ["m3", "m4", "m5"]
    assert total.cached == 3
    assert state["backfill_offset::alpha#euw"] == "5"


NOW = 1_800_000_000


def run_cycle(riot, state, *, now=NOW, logs=None, cached=None, friends=("Alpha#EUW",)):
    cached = cached if cached is not None else {}
    return asyncio.run(
        process_backfill_cycle(
            friends=list(friends),
            riot_client=riot,
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.__setitem__(key, value),
            db_get_match_info=lambda match_id: cached.get(match_id),
            recent_ids_count=100,
            per_player_limit=2,
            log=(logs.append if logs is not None else (lambda _msg: None)),
            now=now,
        )
    )


def test_end_of_history_marks_the_player_done_instead_of_starting_over():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id["Alpha#EUW"] = "puuid-alpha"
    riot.page_ids_by_puuid["puuid-alpha"] = {5: []}
    state = {"backfill_offset::alpha#euw": "5"}
    logs = []

    result = run_cycle(riot, state, logs=logs)

    assert result == BackfillCycleResult(cached=0, resting=0)
    assert state["backfill_offset::alpha#euw"] == "0"
    assert state["backfill_done_at::alpha#euw"] == str(NOW)
    assert logs == ["[backfill] History fully cached for Alpha#EUW (5 matches); next pass in 7 days."]


def test_done_players_rest_without_any_riot_request():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id = {"Alpha#EUW": "puuid-alpha", "Bravo#EUW": "puuid-bravo"}
    riot.page_ids_by_puuid["puuid-bravo"] = {0: ["b2", "b1"]}
    state = {"backfill_done_at::alpha#euw": str(NOW - 3600)}

    result = run_cycle(riot, state, friends=("Alpha#EUW", "Bravo#EUW"), cached={"b1": {}, "b2": {}})

    assert result == BackfillCycleResult(cached=0, resting=1)
    # Only Bravo, who has no finished pass yet, was looked up.
    assert riot.puuid_requests == ["Bravo#EUW"]
    assert riot.page_requests == [("puuid-bravo", 0)]
    assert state["backfill_offset::bravo#euw"] == "2"


def test_a_new_pass_starts_from_the_newest_games_after_the_rest():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id["Alpha#EUW"] = "puuid-alpha"
    riot.page_ids_by_puuid["puuid-alpha"] = {0: ["m2", "m1"]}
    state = {"backfill_done_at::alpha#euw": str(NOW - BACKFILL_REVISIT_SECONDS - 1)}

    result = run_cycle(riot, state, cached={"m1": {}})

    # The gap (m2) is filled, and the pass carries on until it reaches the end again.
    assert result == BackfillCycleResult(cached=1, resting=0)
    assert riot.fetched_match_ids == ["m2"]
    assert riot.page_requests == [("puuid-alpha", 0)]
    assert state["backfill_offset::alpha#euw"] == "2"


def test_backfill_does_not_skip_when_limit_reached_mid_page():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id["Alpha#EUW"] = "puuid-alpha"
    riot.page_ids_by_puuid["puuid-alpha"] = {
        0: ["m5", "m4", "m3", "m2", "m1"],
    }
    state = {}
    db_cache = {"m1": {"cached": True}}

    total_first = asyncio.run(
        process_backfill_cycle(
            friends=["Alpha#EUW"],
            riot_client=riot,
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.__setitem__(key, value),
            db_get_match_info=lambda match_id: db_cache.get(match_id),
            recent_ids_count=100,
            per_player_limit=2,
            log=lambda _msg: None,
        )
    )

    assert total_first.cached == 2
    assert riot.fetched_match_ids == ["m2", "m3"]
    assert state["backfill_offset::alpha#euw"] == "0"

    for match_id in riot.fetched_match_ids:
        db_cache[match_id] = {"cached": True}
    riot.fetched_match_ids = []

    total_second = asyncio.run(
        process_backfill_cycle(
            friends=["Alpha#EUW"],
            riot_client=riot,
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.__setitem__(key, value),
            db_get_match_info=lambda match_id: db_cache.get(match_id),
            recent_ids_count=100,
            per_player_limit=3,
            log=lambda _msg: None,
        )
    )

    assert total_second.cached == 2
    assert riot.fetched_match_ids == ["m4", "m5"]
    assert state["backfill_offset::alpha#euw"] == "5"


def test_removing_a_player_clears_cursors_and_rank_snapshot_but_keeps_history(monkeypatch):
    import src.db.players as db_players

    calls = []
    monkeypatch.setattr(db_players, "db_execute", lambda query, params=None, **_kwargs: calls.append((query, params)))
    db_players.db_remove_player("Alpha#EUW")
    state_keys = next(params for query, params in calls if "bot_state" in query)
    assert "backfill_done_at::alpha#euw" in state_keys and "backfill_offset::alpha#euw" in state_keys
    assert "last_seen_match_id::alpha#euw" in state_keys
    # History stays: daily stats, per-game stats and LP history are never deleted.
    deleted_tables = {query.split("DELETE FROM", 1)[1].split()[0] for query, _params in calls if "DELETE FROM" in query}
    assert deleted_tables == {"tracked_players", "player_ranked_state", "bot_state"}
