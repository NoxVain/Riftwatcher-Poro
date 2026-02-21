import asyncio
from datetime import datetime, timezone

import requests

from src.discord_recap_worker import process_recap_cycle


def _state_key(riot_id):
    return f"last_announced_match_id::{riot_id.casefold()}"


def _participant(puuid, *, win):
    return {
        "puuid": puuid,
        "win": win,
        "championName": "Ahri",
        "teamPosition": "MIDDLE",
        "kills": 5,
        "deaths": 2,
        "assists": 6,
        "totalMinionsKilled": 150,
        "neutralMinionsKilled": 10,
        "totalDamageDealtToChampions": 21000,
        "damageDealtToObjectives": 4000,
        "totalHeal": 1200,
        "totalDamageTaken": 18000,
        "visionScore": 18,
    }


class FakeChannel:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FakeRiotClient:
    def __init__(self):
        self.puuid_by_riot_id = {}
        self.recent_ids_by_puuid = {}
        self.match_info_by_id = {}
        self.mode_records_by_riot_id = {}
        self.today_mode_calls = []

    async def fetch_puuid(self, riot_id):
        return self.puuid_by_riot_id[riot_id]

    async def fetch_recent_match_ids(self, puuid, count=20):
        return self.recent_ids_by_puuid.get(puuid, [])

    async def fetch_match_info(self, match_id):
        return self.match_info_by_id[match_id]

    async def get_today_mode_records(self, riot_id):
        self.today_mode_calls.append(riot_id)
        return self.mode_records_by_riot_id[riot_id]

    @staticmethod
    def get_participant(match_info, puuid):
        for participant in match_info.get("info", {}).get("participants", []):
            if participant.get("puuid") == puuid:
                return participant
        return None


class FakeMoodService:
    def __init__(self):
        self.invalidated = False

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

    @staticmethod
    def get_cycle_key():
        return "2026-02-19"

    def invalidate_report_cache(self):
        self.invalidated = True


def test_process_recap_cycle_posts_recap_and_syncs_affected_players():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakeMoodService()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a", "Bravo#NA1": "puuid-b"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_2", "EUW1_1"], "puuid-b": ["EUW1_2", "EUW1_1"]}
    riot.match_info_by_id = {
        "EUW1_2": {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": now_ms,
                "participants": [_participant("puuid-a", win=True), _participant("puuid-b", win=False)],
            }
        }
    }
    riot.mode_records_by_riot_id = {
        "Alpha#NA1": (
            {"solo_duo": {"wins": 1, "losses": 0}, "flex": {"wins": 0, "losses": 0}, "arcade": {"wins": 0, "losses": 0}},
            {"cs_total": 100, "minutes_total": 30.0},
        ),
        "Bravo#NA1": (
            {"solo_duo": {"wins": 0, "losses": 1}, "flex": {"wins": 0, "losses": 0}, "arcade": {"wins": 0, "losses": 0}},
            {"cs_total": 90, "minutes_total": 30.0},
        ),
    }

    state = {_state_key("Alpha#NA1"): "EUW1_1", _state_key("Bravo#NA1"): "EUW1_1"}
    upserts = []
    edit_calls = []
    logs = []

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    def db_upsert_daily_stats(cycle_key, riot_id, mode_records, performance_totals):
        upserts.append((cycle_key, riot_id, mode_records, performance_totals))

    async def edit_last_report_message(**kwargs):
        edit_calls.append(kwargs)

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1", "Bravo#NA1"],
            riot_client=riot,
            mood_service=mood,
            report_timezone=timezone.utc,
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_upsert_daily_stats=db_upsert_daily_stats,
            edit_last_report_message=edit_last_report_message,
            log=logs.append,
        )
    )

    assert len(channel.messages) == 1
    assert "New Match Recap" in channel.messages[0]
    assert "`30:00`" in channel.messages[0]
    assert "\n\n❌ **Bravo**" in channel.messages[0]
    assert state[_state_key("Alpha#NA1")] == "EUW1_2"
    assert state[_state_key("Bravo#NA1")] == "EUW1_2"
    assert [row[1] for row in upserts] == ["Alpha#NA1", "Bravo#NA1"]
    assert mood.invalidated is True
    assert edit_calls == [{"bypass_cache": True}]


def test_process_recap_cycle_posts_streak_callout_when_threshold_crossed():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakeMoodService()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_3", "EUW1_2", "EUW1_1"]}
    riot.match_info_by_id = {
        "EUW1_3": {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": now_ms,
                "participants": [_participant("puuid-a", win=True)],
            }
        },
        "EUW1_2": {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": now_ms - 1800,
                "participants": [_participant("puuid-a", win=True)],
            }
        },
        "EUW1_1": {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": now_ms - 3600,
                "participants": [_participant("puuid-a", win=True)],
            }
        },
    }
    riot.mode_records_by_riot_id = {
        "Alpha#NA1": (
            {"solo_duo": {"wins": 3, "losses": 0}, "flex": {"wins": 0, "losses": 0}, "arcade": {"wins": 0, "losses": 0}},
            {"cs_total": 100, "minutes_total": 30.0},
        ),
    }

    state = {_state_key("Alpha#NA1"): "EUW1_2"}

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            mood_service=mood,
            report_timezone=timezone.utc,
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_upsert_daily_stats=lambda *_args, **_kwargs: None,
            edit_last_report_message=lambda **_kwargs: asyncio.sleep(0),
            log=lambda _msg: None,
        )
    )

    assert len(channel.messages) == 1
    assert "New Match Recap" in channel.messages[0]
    assert "Heater Alert" in channel.messages[0] or "Momentum" in channel.messages[0]


def test_process_recap_cycle_no_new_matches_skips_post_and_sync():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakeMoodService()

    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_1", "EUW1_0"]}

    state = {_state_key("Alpha#NA1"): "EUW1_1"}
    upserts = []
    edit_calls = []

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    def db_upsert_daily_stats(cycle_key, riot_id, mode_records, performance_totals):
        upserts.append((cycle_key, riot_id, mode_records, performance_totals))

    async def edit_last_report_message(**kwargs):
        edit_calls.append(kwargs)

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            mood_service=mood,
            report_timezone=timezone.utc,
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_upsert_daily_stats=db_upsert_daily_stats,
            edit_last_report_message=edit_last_report_message,
            log=lambda _msg: None,
        )
    )

    assert channel.messages == []
    assert upserts == []
    assert mood.invalidated is False
    assert edit_calls == []


def test_process_recap_cycle_skips_remake_notifications_and_sync():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakeMoodService()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_2", "EUW1_1"]}
    riot.match_info_by_id = {
        "EUW1_2": {
            "info": {
                "queueId": 420,
                "gameDuration": 240,
                "gameEndTimestamp": now_ms,
                "participants": [_participant("puuid-a", win=True)],
            }
        }
    }

    state = {_state_key("Alpha#NA1"): "EUW1_1"}
    upserts = []
    edit_calls = []

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    async def edit_last_report_message(**kwargs):
        edit_calls.append(kwargs)

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            mood_service=mood,
            report_timezone=timezone.utc,
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_upsert_daily_stats=lambda *_args, **_kwargs: upserts.append(True),
            edit_last_report_message=edit_last_report_message,
            log=lambda _msg: None,
        )
    )

    assert channel.messages == []
    assert state[_state_key("Alpha#NA1")] == "EUW1_2"
    assert upserts == []
    assert edit_calls == []


def test_process_recap_cycle_batches_multiple_matches_into_single_post():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakeMoodService()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_2", "EUW1_1", "EUW1_0"]}
    riot.match_info_by_id = {
        "EUW1_2": {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": now_ms,
                "participants": [_participant("puuid-a", win=True)],
            }
        },
        "EUW1_1": {
            "info": {
                "queueId": 440,
                "gameDuration": 1700,
                "gameEndTimestamp": now_ms - 120000,
                "participants": [_participant("puuid-a", win=False)],
            }
        },
    }
    riot.mode_records_by_riot_id = {
        "Alpha#NA1": (
            {"solo_duo": {"wins": 1, "losses": 1}, "flex": {"wins": 0, "losses": 0}, "arcade": {"wins": 0, "losses": 0}},
            {"cs_total": 100, "minutes_total": 30.0},
        ),
    }

    state = {_state_key("Alpha#NA1"): "EUW1_0"}
    edit_calls = []

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    async def edit_last_report_message(**kwargs):
        edit_calls.append(kwargs)

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            mood_service=mood,
            report_timezone=timezone.utc,
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_upsert_daily_stats=lambda *_args, **_kwargs: None,
            edit_last_report_message=edit_last_report_message,
            log=lambda _msg: None,
        )
    )

    assert len(channel.messages) == 1
    assert channel.messages[0].count("New Match Recap") == 2
    assert "\n\n---\n\n" in channel.messages[0]
    assert edit_calls == [{"bypass_cache": True}]


def test_process_recap_cycle_does_not_advance_state_when_match_fetch_fails():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakeMoodService()

    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_2", "EUW1_1"]}
    riot.mode_records_by_riot_id = {
        "Alpha#NA1": (
            {"solo_duo": {"wins": 1, "losses": 0}, "flex": {"wins": 0, "losses": 0}, "arcade": {"wins": 0, "losses": 0}},
            {"cs_total": 100, "minutes_total": 30.0},
        ),
    }

    async def failing_fetch_match_info(match_id):
        if match_id == "EUW1_2":
            raise requests.RequestException("temporary failure")
        return {"info": {"participants": []}}

    riot.fetch_match_info = failing_fetch_match_info

    state = {_state_key("Alpha#NA1"): "EUW1_1"}

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            mood_service=mood,
            report_timezone=timezone.utc,
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_upsert_daily_stats=lambda *_args, **_kwargs: None,
            edit_last_report_message=lambda **_kwargs: asyncio.sleep(0),
            log=lambda _msg: None,
        )
    )

    assert state[_state_key("Alpha#NA1")] == "EUW1_1"
    assert channel.messages == []
