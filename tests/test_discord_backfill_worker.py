import asyncio

from src.discord_backfill_worker import process_backfill_cycle


class FakeRiotClient:
    def __init__(self):
        self.puuid_by_riot_id = {}
        self.page_ids_by_puuid = {}
        self.match_info_by_id = {}
        self.fetched_match_ids = []

    async def fetch_puuid(self, riot_id, *, request_tier="priority"):
        _ = request_tier
        return self.puuid_by_riot_id[riot_id]

    async def fetch_match_ids_page(self, puuid, *, start=0, count=100, request_tier="priority"):
        _ = count, request_tier
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
    assert total == 3
    assert state["backfill_offset::alpha#euw"] == "5"


def test_backfill_resets_offset_when_no_more_ids():
    riot = FakeRiotClient()
    riot.puuid_by_riot_id["Alpha#EUW"] = "puuid-alpha"
    riot.page_ids_by_puuid["puuid-alpha"] = {
        5: [],
    }
    state = {"backfill_offset::alpha#euw": "5"}

    total = asyncio.run(
        process_backfill_cycle(
            friends=["Alpha#EUW"],
            riot_client=riot,
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.__setitem__(key, value),
            db_get_match_info=lambda _match_id: None,
            recent_ids_count=100,
            per_player_limit=2,
            log=lambda _msg: None,
        )
    )

    assert total == 0
    assert state["backfill_offset::alpha#euw"] == "0"


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

    assert total_first == 2
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

    assert total_second == 2
    assert riot.fetched_match_ids == ["m4", "m5"]
    assert state["backfill_offset::alpha#euw"] == "5"
