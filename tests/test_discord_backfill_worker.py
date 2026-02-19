import asyncio

from src.discord_backfill_worker import process_backfill_cycle


class FakeRiotClient:
    def __init__(self):
        self.puuid_by_riot_id = {}
        self.recent_ids_by_puuid = {}
        self.match_info_by_id = {}
        self.fetched_match_ids = []

    async def fetch_puuid(self, riot_id):
        return self.puuid_by_riot_id[riot_id]

    async def fetch_recent_match_ids(self, puuid, count=100):
        _ = count
        return self.recent_ids_by_puuid.get(puuid, [])

    async def fetch_match_info(self, match_id, *, cache_in_memory=True):
        _ = cache_in_memory
        self.fetched_match_ids.append(match_id)
        return self.match_info_by_id.get(match_id, {"info": {}})


class FakeMoodService:
    @staticmethod
    def get_new_match_ids(recent_ids, last_seen_match_id):
        if not recent_ids:
            return []
        if not last_seen_match_id:
            return list(recent_ids)
        new_ids = []
        for match_id in recent_ids:
            if match_id == last_seen_match_id:
                break
            new_ids.append(match_id)
        return new_ids


def test_backfill_skips_when_new_matches_detected():
    riot = FakeRiotClient()
    mood = FakeMoodService()
    riot.puuid_by_riot_id["Alpha#EUW"] = "puuid-alpha"
    riot.recent_ids_by_puuid["puuid-alpha"] = ["m3", "m2", "m1"]
    db_cache = {}

    total = asyncio.run(
        process_backfill_cycle(
            friends=["Alpha#EUW"],
            riot_client=riot,
            mood_service=mood,
            db_get_last_seen_match_id=lambda _riot_id: "m2",
            db_get_match_info=lambda match_id: db_cache.get(match_id),
            recent_ids_count=100,
            per_player_limit=3,
            log=lambda _msg: None,
        )
    )

    assert total == 0
    assert riot.fetched_match_ids == []


def test_backfill_fetches_missing_older_matches_up_to_limit():
    riot = FakeRiotClient()
    mood = FakeMoodService()
    riot.puuid_by_riot_id["Alpha#EUW"] = "puuid-alpha"
    riot.recent_ids_by_puuid["puuid-alpha"] = ["m5", "m4", "m3", "m2", "m1"]
    db_cache = {"m1": {"cached": True}, "m2": {"cached": True}}

    total = asyncio.run(
        process_backfill_cycle(
            friends=["Alpha#EUW"],
            riot_client=riot,
            mood_service=mood,
            db_get_last_seen_match_id=lambda _riot_id: "m5",
            db_get_match_info=lambda match_id: db_cache.get(match_id),
            recent_ids_count=100,
            per_player_limit=2,
            log=lambda _msg: None,
        )
    )

    assert total == 2
    assert riot.fetched_match_ids == ["m3", "m4"]
