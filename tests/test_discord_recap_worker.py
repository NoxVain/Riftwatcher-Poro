import asyncio
import json
import time
from datetime import datetime, timezone

import discord
import pytest
import requests

from discord_fakes import ComponentMessage, view_text
from src.champion_icons import CHAMPION_DISPLAY_STATE_KEY
from src.discord_recap_worker import process_recap_cycle
from src.recap_cards import ACCENT_WIN
from src.recap_lp import handle_lp_event, lp_events_state_key, lp_pending_state_key


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

    async def send(self, message=None, tts=False, *, view=None):
        # Cards are recorded with their text so assertions read like the plain recap.
        self.messages.append(
            {"content": view_text(view) if view is not None else message, "view": view, "tts": bool(tts)}
        )


class FakeRiotClient:
    def __init__(self):
        self.puuid_by_riot_id = {}
        self.recent_ids_by_puuid = {}
        self.match_info_by_id = {}
        self.mode_records_by_riot_id = {}
        self.today_mode_calls = []

    async def fetch_puuid(self, riot_id):
        return self.puuid_by_riot_id[riot_id]

    async def fetch_recent_match_ids(self, puuid, count=20, riot_id=None):
        _ = count, riot_id
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


class FakePoroService:
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
    mood = FakePoroService()
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
            {"solo_duo": {"wins": 1, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
            {"cs_total": 100, "minutes_total": 30.0},
        ),
        "Bravo#NA1": (
            {"solo_duo": {"wins": 0, "losses": 1}, "flex": {"wins": 0, "losses": 0}},
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

    def db_upsert_daily_stats(cycle_key, riot_id, mode_records, performance_totals, primary_role=None):
        upserts.append((cycle_key, riot_id, mode_records, performance_totals))

    async def edit_last_report_message(**kwargs):
        edit_calls.append(kwargs)

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1", "Bravo#NA1"],
            riot_client=riot,
            poro_service=mood,
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
    assert "-# " in channel.messages[0]["content"]
    assert " · 30:00\n" in channel.messages[0]["content"]
    # Mixed result: the card edge can't say who won, so each player names their result.
    assert "**Alpha** · **Ahri** Mid · **Win**\n" in channel.messages[0]["content"]
    assert "\n\n**Bravo** · **Ahri** Mid · **Loss**\n" in channel.messages[0]["content"]
    assert channel.messages[0]["tts"] is False
    assert state[_state_key("Alpha#NA1")] == "EUW1_2"
    assert state[_state_key("Bravo#NA1")] == "EUW1_2"
    assert [row[1] for row in upserts] == ["Alpha#NA1", "Bravo#NA1"]
    assert mood.invalidated is True
    assert edit_calls == [{"bypass_cache": True}]


def test_process_recap_cycle_posts_streak_callout_when_threshold_crossed():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakePoroService()
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
            {"solo_duo": {"wins": 3, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
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
            poro_service=mood,
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

    assert len(channel.messages) == 2
    assert "-# " in channel.messages[0]["content"]
    assert channel.messages[0]["tts"] is False
    assert "Heater Alert" in channel.messages[1]["content"] or "Momentum" in channel.messages[1]["content"]
    assert channel.messages[1]["tts"] is True


def test_process_recap_cycle_posts_streak_callout_without_tts_when_disabled():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakePoroService()
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
            {"solo_duo": {"wins": 3, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
            {"cs_total": 100, "minutes_total": 30.0},
        ),
    }

    state = {
        _state_key("Alpha#NA1"): "EUW1_2",
        "streak_tts_enabled": "0",
    }

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            poro_service=mood,
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

    assert len(channel.messages) == 2
    assert channel.messages[1]["tts"] is False


def test_process_recap_cycle_no_new_matches_skips_post_and_sync():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakePoroService()

    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_1", "EUW1_0"]}

    state = {_state_key("Alpha#NA1"): "EUW1_1"}
    upserts = []
    edit_calls = []

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    def db_upsert_daily_stats(cycle_key, riot_id, mode_records, performance_totals, primary_role=None):
        upserts.append((cycle_key, riot_id, mode_records, performance_totals))

    async def edit_last_report_message(**kwargs):
        edit_calls.append(kwargs)

    logs = []

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            poro_service=mood,
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

    assert channel.messages == []
    assert upserts == []
    assert mood.invalidated is False
    assert edit_calls == []
    assert any("No new matches to post" in row for row in logs)
    assert any("checked_players=1" in row for row in logs)


def test_process_recap_cycle_skips_remake_notifications_and_sync():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakePoroService()
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

    logs = []

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            poro_service=mood,
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_upsert_daily_stats=lambda *_args, **_kwargs: upserts.append(True),
            edit_last_report_message=edit_last_report_message,
            log=logs.append,
        )
    )

    assert channel.messages == []
    assert state[_state_key("Alpha#NA1")] == "EUW1_2"
    assert upserts == []
    assert edit_calls == []
    assert any("Match scan summary" in row for row in logs)
    assert any("skipped_remakes=1" in row for row in logs)


def test_process_recap_cycle_batches_multiple_matches_into_single_post():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakePoroService()
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
            {"solo_duo": {"wins": 1, "losses": 1}, "flex": {"wins": 0, "losses": 0}},
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
            poro_service=mood,
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
    assert channel.messages[0]["content"].count("-# ") == 2
    assert "\n\n---\n\n" in channel.messages[0]["content"]
    assert channel.messages[0]["tts"] is False
    assert edit_calls == [{"bypass_cache": True}]


def test_process_recap_cycle_formats_arena_3x6_by_placement():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakePoroService()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    alpha = _participant("puuid-a", win=False)
    alpha.update({
        "championName": "Sett",
        "placement": 1,
        "playerSubteamId": 2,
        "goldEarned": 18000,
        "totalDamageShieldedOnTeammates": 2345,
        "playerAugment1": 101,
        "playerAugment2": 202,
        "item0": 3157,
        "item1": 3089,
        "challenges": {
            "teamDamagePercentage": 0.456,
            "skillshotsHit": 13,
            "skillshotsDodged": 42,
        },
    })
    bravo = _participant("puuid-b", win=False)
    bravo.update({
        "championName": "Lux",
        "placement": 4,
        "playerSubteamId": 5,
        "goldEarned": 12000,
    })

    riot.puuid_by_riot_id = {"Bravo#NA1": "puuid-b", "Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_ARENA", "EUW1_0"], "puuid-b": ["EUW1_ARENA", "EUW1_0"]}
    riot.match_info_by_id = {
        "EUW1_ARENA": {
            "info": {
                "queueId": 1750,
                "gameDuration": 1400,
                "gameEndTimestamp": now_ms,
                "participants": [bravo, alpha],
            }
        }
    }
    riot.mode_records_by_riot_id = {
        "Alpha#NA1": (
            {"solo_duo": {"wins": 0, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
            {"cs_total": 0, "minutes_total": 23.3},
        ),
        "Bravo#NA1": (
            {"solo_duo": {"wins": 0, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
            {"cs_total": 0, "minutes_total": 23.3},
        ),
    }

    state = {_state_key("Alpha#NA1"): "EUW1_0", _state_key("Bravo#NA1"): "EUW1_0"}

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "src.discord_recap_worker.load_arena_display_names",
            lambda: {
                "augment_names": {"101": "Warmup Routine", "202": "Scoped Weapons"},
                "item_names": {"3157": "Zhonya's Hourglass", "3089": "Rabadon's Deathcap"},
            },
        )
        asyncio.run(
            process_recap_cycle(
                friends=["Bravo#NA1", "Alpha#NA1"],
                riot_client=riot,
                poro_service=mood,
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
    content = channel.messages[0]["content"]
    assert "Arena 3x6" in content
    assert "**Alpha**" in content
    assert "**Bravo**" in content
    assert content.index("**Alpha**") < content.index("**Bravo**")
    assert content.startswith("-# Arena 3x6 · **Placements** · ")
    assert "**Alpha** · **Sett** · Team 2 · **Place #1**\n" in content
    assert "**Place #4**" in content
    assert "Damage **21,000** (45.6% of team) · Taken **18,000**" in content
    assert "Healing **1,200** · Shielded **2,345**" in content
    assert "Skillshots hit **13** · Dodged **42**" in content
    assert "Augments · Warmup Routine, Scoped Weapons\nItems · Zhonya's Hourglass, Rabadon's Deathcap" in content
    assert "CS/min" not in content


def test_process_recap_cycle_does_not_advance_state_when_match_fetch_fails():
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakePoroService()

    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_2", "EUW1_1"]}
    riot.mode_records_by_riot_id = {
        "Alpha#NA1": (
            {"solo_duo": {"wins": 1, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
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
            poro_service=mood,
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


@pytest.mark.parametrize("tts_state, expected_tts", [("1", True), ("0", False)])
def test_process_recap_cycle_keeps_recap_and_streak_separate_with_tts_toggle(tts_state, expected_tts):
    channel = FakeChannel()
    riot = FakeRiotClient()
    mood = FakePoroService()
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
                "gameDuration": 1750,
                "gameEndTimestamp": now_ms - 200000,
                "participants": [_participant("puuid-a", win=True)],
            }
        },
        "EUW1_1": {
            "info": {
                "queueId": 420,
                "gameDuration": 1700,
                "gameEndTimestamp": now_ms - 400000,
                "participants": [_participant("puuid-a", win=True)],
            }
        },
    }
    riot.mode_records_by_riot_id = {
        "Alpha#NA1": (
            {"solo_duo": {"wins": 3, "losses": 0}, "flex": {"wins": 0, "losses": 0}},
            {"cs_total": 100, "minutes_total": 30.0},
        ),
    }

    state = {
        _state_key("Alpha#NA1"): "EUW1_1",
        "streak_tts_enabled": tts_state,
    }

    def db_get_state(key):
        return state.get(key)

    def db_set_state(key, value):
        state[key] = value

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            poro_service=mood,
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

    assert len(channel.messages) == 2
    recap_msg = channel.messages[0]
    streak_msg = channel.messages[1]

    assert "-# " in recap_msg["content"]
    assert recap_msg["content"].count("-# ") == 2
    assert "Momentum" not in recap_msg["content"]
    assert "Heater Alert" not in recap_msg["content"]
    assert recap_msg["tts"] is False

    assert "-# " not in streak_msg["content"]
    assert ("Momentum" in streak_msg["content"]) or ("Heater Alert" in streak_msg["content"])
    assert streak_msg["tts"] is expected_tts


def test_split_large_section():
    from src.discord_recap_worker import _split_large_section

    # Case 1: Multiple sections separated by double newlines
    # Each paragraph is 400 chars. Total length = 1200 + 4 (newlines) = 1204 chars.
    p1 = "a" * 400
    p2 = "b" * 400
    p3 = "c" * 400
    section = f"{p1}\n\n{p2}\n\n{p3}"

    # If max_len = 900, p1 + p2 fits (400 + 400 + 2 = 802 chars), but adding p3 exceeds it.
    # So it should split into [p1 + \n\n + p2, p3]
    chunks = _split_large_section(section, max_len=900)
    assert len(chunks) == 2
    assert chunks[0] == f"{p1}\n\n{p2}"
    assert chunks[1] == p3


def test_pack_sections_into_messages_graceful_split():
    from src.discord_recap_worker import _pack_sections_into_messages

    # Create a single massive section that exceeds max_len (2000 chars)
    # 6 paragraphs of 400 chars each. Total = 2400 + 10 = 2410 chars.
    p = ["x" * 400] * 6
    section = "\n\n".join(p)

    messages = _pack_sections_into_messages([section], separator="\n\n", max_len=2000)
    # It should split into 2 messages:
    # Msg 1: 4 paragraphs (400*4 + 6 = 1606 chars). Adding 5th would be 400*5 + 8 = 2008 chars (exceeds 2000).
    # Msg 2: 2 paragraphs (400*2 + 2 = 802 chars).
    assert len(messages) == 2
    assert messages[0] == "\n\n".join(p[:4])
    assert messages[1] == "\n\n".join(p[4:])


class MessageReturningChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def send(self, message=None, tts=False, *, view=None):
        await super().send(message, tts=tts, view=view)
        sent = ComponentMessage(len(self.sent) + 1, content=message, view=view)
        self.sent.append(sent)
        return sent

    async def fetch_message(self, message_id):
        return self.sent[message_id - 1]


def _run_single_match_recap(channel, state, *, win, queue_id=420, champion_icons=None, champion_id=103):
    riot = FakeRiotClient()
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000) - 60_000
    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["NA1_2", "NA1_1"]}
    riot.match_info_by_id = {
        "NA1_2": {
            "info": {
                "queueId": queue_id,
                "gameDuration": 1800,
                "gameEndTimestamp": end_ms,
                "participants": [dict(_participant("puuid-a", win=win), championId=champion_id)],
            }
        }
    }
    riot.mode_records_by_riot_id = {"Alpha#NA1": ({}, {})}
    state[_state_key("Alpha#NA1")] = "NA1_1"

    async def edit_last_report_message(**_kwargs):
        return None

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            poro_service=FakePoroService(),
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=state.get,
            db_set_state=state.__setitem__,
            db_upsert_daily_stats=lambda *_args, **_kwargs: None,
            edit_last_report_message=edit_last_report_message,
            champion_icons=champion_icons,
            log=lambda _msg: None,
        )
    )
    return end_ms / 1000


def _lp_event(*, won, delta, league_points, observed_at):
    return {
        "queue_type": "RANKED_SOLO_5X5", "won": won, "delta": delta, "tier": "GOLD",
        "rank": "II", "league_points": league_points, "observed_at": observed_at,
    }


def test_process_recap_cycle_includes_lp_when_rank_worker_saw_game_first():
    state = {
        lp_events_state_key("Alpha#NA1"): json.dumps(
            [_lp_event(won=True, delta=21, league_points=56, observed_at=time.time() - 10)]
        )
    }
    channel = MessageReturningChannel()

    _run_single_match_recap(channel, state, win=True)

    assert "**Ahri** Mid · Gold II · 35LP → **56LP\u00a0(+21)**\n" in channel.messages[0]["content"]
    assert json.loads(state[lp_events_state_key("Alpha#NA1")]) == []
    assert lp_pending_state_key("Alpha#NA1") not in state


def test_process_recap_cycle_tracks_recap_for_later_lp_edit():
    state = {}
    channel = MessageReturningChannel()

    end_ts = _run_single_match_recap(channel, state, win=False)

    assert "LP" not in channel.messages[0]["content"]
    pending = json.loads(state[lp_pending_state_key("Alpha#NA1")])
    assert [(p["match_id"], p["message_id"], p["won"]) for p in pending] == [("NA1_2", 1, False)]

    handled = asyncio.run(
        handle_lp_event(
            riot_id="Alpha#NA1",
            event=_lp_event(won=False, delta=-18, league_points=22, observed_at=end_ts + 120),
            recap_channel=channel,
            db_get_state=state.get,
            db_set_state=state.__setitem__,
            log=lambda _msg: None,
            now=time.time(),
        )
    )

    assert handled is True
    assert "**Ahri** Mid · Gold II · 40LP → **22LP\u00a0(-18)**\n" in channel.sent[0].display


def test_process_recap_cycle_skips_lp_for_non_ranked_queue():
    state = {}
    channel = MessageReturningChannel()

    _run_single_match_recap(channel, state, win=True, queue_id=400)

    assert len(channel.messages) == 1
    assert lp_pending_state_key("Alpha#NA1") not in state
    assert lp_events_state_key("Alpha#NA1") not in state


class FakeChampionIcons:
    def __init__(self, icon="<:Ahri:1>"):
        self.icon = icon
        self.calls = []

    async def get(self, champion_id, *, cached_only=False):
        self.calls.append((champion_id, cached_only))
        return self.icon


@pytest.mark.parametrize(
    "mode,expected",
    [
        (None, "· <:Ahri:1> Mid"),
        ("name", "· **Ahri** Mid"),
        ("icon", "· <:Ahri:1> Mid"),
        ("both", "· <:Ahri:1> **Ahri** Mid"),
    ],
)
def test_process_recap_cycle_champion_display_modes(mode, expected):
    state = {} if mode is None else {CHAMPION_DISPLAY_STATE_KEY: mode}
    channel = MessageReturningChannel()
    icons = FakeChampionIcons()

    _run_single_match_recap(channel, state, win=True, champion_icons=icons)

    assert expected in channel.messages[0]["content"]
    # Portraits are the default; name mode never looks them up.
    assert icons.calls == ([] if mode == "name" else [(103, False)])


def test_process_recap_cycle_portrait_modes_fall_back_to_name():
    state = {CHAMPION_DISPLAY_STATE_KEY: "icon"}
    channel = MessageReturningChannel()

    _run_single_match_recap(channel, state, win=False, champion_icons=FakeChampionIcons(icon=None))

    assert "· **Ahri** Mid" in channel.messages[0]["content"]


def test_process_recap_cycle_uses_cached_portraits_only_after_budget(monkeypatch):
    from src import discord_recap_worker

    monkeypatch.setattr(discord_recap_worker, "RECAP_LOOKUP_BUDGET_SECONDS", -1)
    state = {CHAMPION_DISPLAY_STATE_KEY: "both"}
    icons = FakeChampionIcons()

    _run_single_match_recap(MessageReturningChannel(), state, win=True, champion_icons=icons)

    assert icons.calls == [(103, True)]


def test_process_recap_cycle_lp_edit_still_matches_portrait_heading():
    state = {CHAMPION_DISPLAY_STATE_KEY: "both"}
    channel = MessageReturningChannel()

    end_ts = _run_single_match_recap(channel, state, win=False, champion_icons=FakeChampionIcons())
    handled = asyncio.run(
        handle_lp_event(
            riot_id="Alpha#NA1",
            event=_lp_event(won=False, delta=-18, league_points=22, observed_at=end_ts + 120),
            recap_channel=channel,
            db_get_state=state.get,
            db_set_state=state.__setitem__,
            log=lambda _msg: None,
            now=time.time(),
        )
    )

    assert handled is True
    assert "<:Ahri:1> **Ahri** Mid · Gold II" in channel.sent[0].display


def test_process_recap_cycle_sends_coloured_compact_card():
    channel = MessageReturningChannel()

    _run_single_match_recap(channel, {}, win=True)

    view = channel.messages[0]["view"]
    assert view is not None
    container = view.children[0]
    assert container.accent_colour == ACCENT_WIN
    text = channel.messages[0]["content"]
    assert text.startswith("-# Ranked Solo/Duo · **Win** · 30:00\n\n")  # game length, no date or clock
    # No portrait cache in this run: the default portrait mode falls back to the name.
    # Shared win: the green edge and header carry the result, so the player line has no mark.
    assert "\n\n**Alpha** · **Ahri** Mid\n**5/2/6** KDA" in text


def test_card_pending_lp_targets_the_player_component():
    state = {}
    channel = MessageReturningChannel()

    _run_single_match_recap(channel, state, win=False)

    [pending] = json.loads(state[lp_pending_state_key("Alpha#NA1")])
    assert isinstance(pending["component_id"], int)
    assert pending["message_id"] == 1


class RejectingCardChannel(MessageReturningChannel):
    async def send(self, message=None, tts=False, *, view=None):
        if view is not None:
            response = type("Response", (), {"status": 400, "reason": "Bad Request"})()
            raise discord.HTTPException(response, "Invalid Form Body")
        return await super().send(message, tts=tts)


def test_rejected_card_falls_back_to_plain_text_and_lp_still_edits():
    state = {}
    channel = RejectingCardChannel()

    end_ts = _run_single_match_recap(channel, state, win=False)

    assert len(channel.messages) == 1
    assert channel.messages[0]["view"] is None
    assert "-# " in channel.messages[0]["content"]
    assert state[_state_key("Alpha#NA1")] == "NA1_2"  # cursor still advances
    [pending] = json.loads(state[lp_pending_state_key("Alpha#NA1")])
    assert "component_id" not in pending

    handled = asyncio.run(
        handle_lp_event(
            riot_id="Alpha#NA1",
            event=_lp_event(won=False, delta=-18, league_points=22, observed_at=end_ts + 120),
            recap_channel=channel,
            db_get_state=state.get,
            db_set_state=state.__setitem__,
            log=lambda _msg: None,
            now=time.time(),
        )
    )
    assert handled is True
    assert "**Ahri** Mid · Gold II · 40LP → **22LP\u00a0(-18)**\n" in channel.sent[0].display


class MessageSnapshot:
    """What Discord returns from a fetch: a copy at that moment; edits go to the real message."""

    def __init__(self, original):
        self.id = original.id
        self.content = original.content
        self.components = list(original.components)
        self._original = original

    async def edit(self, **kwargs):
        return await self._original.edit(**kwargs)


class SlowFetchChannel(MessageReturningChannel):
    async def fetch_message(self, message_id):
        snapshot = MessageSnapshot(await super().fetch_message(message_id))
        await asyncio.sleep(0.01)  # let a concurrent edit interleave between fetch and edit
        return snapshot


def test_concurrent_lp_edits_on_one_card_both_survive():
    riot = FakeRiotClient()
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000) - 60_000
    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a", "Bravo#NA1": "puuid-b"}
    riot.recent_ids_by_puuid = {"puuid-a": ["NA1_2", "NA1_1"], "puuid-b": ["NA1_2", "NA1_1"]}
    riot.match_info_by_id = {
        "NA1_2": {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": end_ms,
                "participants": [_participant("puuid-a", win=False), _participant("puuid-b", win=False)],
            }
        }
    }
    riot.mode_records_by_riot_id = {"Alpha#NA1": ({}, {}), "Bravo#NA1": ({}, {})}
    state = {_state_key("Alpha#NA1"): "NA1_1", _state_key("Bravo#NA1"): "NA1_1"}
    channel = SlowFetchChannel()

    async def edit_last_report_message(**_kwargs):
        return None

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1", "Bravo#NA1"],
            riot_client=riot,
            poro_service=FakePoroService(),
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=state.get,
            db_set_state=state.__setitem__,
            db_upsert_daily_stats=lambda *_args, **_kwargs: None,
            edit_last_report_message=edit_last_report_message,
            log=lambda _msg: None,
        )
    )
    assert len(channel.sent) == 1
    observed = end_ms / 1000 + 120

    async def both():
        return await asyncio.gather(
            *(
                handle_lp_event(
                    riot_id=riot_id,
                    event=_lp_event(won=False, delta=delta, league_points=lp, observed_at=observed),
                    recap_channel=channel,
                    db_get_state=state.get,
                    db_set_state=state.__setitem__,
                    log=lambda _msg: None,
                    now=time.time(),
                )
                for riot_id, delta, lp in (("Alpha#NA1", -18, 22), ("Bravo#NA1", -20, 30))
            )
        )

    assert asyncio.run(both()) == [True, True]
    text = channel.sent[0].display
    assert "40LP → **22LP\u00a0(-18)**" in text
    assert "50LP → **30LP\u00a0(-20)**" in text



def _record_cycle(*, iter_rows, kills=12, upserted=None, logs=None):
    channel = FakeChannel()
    riot = FakeRiotClient()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    riot.puuid_by_riot_id = {"Alpha#NA1": "puuid-a"}
    riot.recent_ids_by_puuid = {"puuid-a": ["EUW1_2", "EUW1_1"]}
    participant = dict(
        _participant("puuid-a", win=True), kills=kills, teamId=100, challenges={"teamDamagePercentage": 0.2}
    )
    riot.match_info_by_id = {
        "EUW1_2": {
            "metadata": {"matchId": "EUW1_2"},
            "info": {"queueId": 420, "gameDuration": 1800, "gameEndTimestamp": now_ms, "participants": [participant]},
        }
    }
    riot.mode_records_by_riot_id = {"Alpha#NA1": ({}, {})}
    state = {_state_key("Alpha#NA1"): "EUW1_1"}

    async def edit_last_report_message(**kwargs):
        pass

    asyncio.run(
        process_recap_cycle(
            friends=["Alpha#NA1"],
            riot_client=riot,
            poro_service=FakePoroService(),
            match_recap_channel_id=123,
            channel=channel,
            db_enabled=True,
            db_get_state=state.get,
            db_set_state=state.__setitem__,
            db_upsert_daily_stats=lambda *args, **kwargs: None,
            edit_last_report_message=edit_last_report_message,
            db_iter_record_rows=iter_rows,
            db_upsert_player_match_stats=(upserted.extend if upserted is not None else None),
            log=(logs.append if logs is not None else (lambda _msg: None)),
        )
    )
    return channel


def _stored_history(queue_ids, *, before=None, exclude_match_ids=()):
    assert tuple(queue_ids) == (420, 440, 710)
    assert "EUW1_2" in exclude_match_ids and before is not None
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return iter([
        {"match_id": f"EUW1_OLD{i}", "riot_id": "Bravo#NA1", "queue_id": 420, "game_end_at": start.replace(day=1 + i % 28),
         "duration_seconds": 1800, "champion_name": "Zed", "win": True, "remake": False, "kills": 10, "deaths": 5,
         "assists": 50, "damage_to_champions": 90000, "damage_share": 0.45, "damage_taken": 90000, "gold_earned": 30000,
         "cs": 400, "vision_score": 150, "solo_kills": 9, "penta_kills": 0}
        for i in range(250)
    ])


def test_recap_shows_one_line_per_new_group_record_and_stores_the_game():
    upserted = []
    logs = []
    channel = _record_cycle(iter_rows=_stored_history, kills=12, upserted=upserted, logs=logs)
    content = channel.messages[0]["content"]
    assert "\n🏆 **New group record:** 12 kills (was 10, Bravo on Zed, Jan 2025)" in content
    # Deaths (2 vs 5) and the other categories didn't beat the stored records.
    assert content.count("New group record") == 1
    assert [row["match_id"] for row in upserted] == ["EUW1_2"]
    assert any("[records] Checked 1 ranked match(es) against 250 stored games: new_records=1 (kills Alpha#NA1 12" in line for line in logs)


def test_recap_ends_with_a_small_personal_best_line():
    def iter_rows(queue_ids, **kwargs):
        rows = list(_stored_history(queue_ids, **kwargs))
        bravo = [dict(stored, kills=20) for stored in rows]
        # Alpha's own history: 60 games with 10 kills on Lux, everything else higher than this game.
        alpha = [dict(stored, match_id=f"EUW1_A{i}", riot_id="Alpha#NA1", champion_name="Lux") for i, stored in enumerate(rows[:60])]
        return iter(sorted(bravo + alpha, key=lambda stored: stored["game_end_at"]))

    logs = []
    channel = _record_cycle(iter_rows=iter_rows, kills=12, logs=logs)
    content = channel.messages[0]["content"]
    assert content.endswith("\n-# 📈 Personal best: 12 kills (was 10 on Lux, Jan 2025)")
    assert "New group record" not in content
    assert any("new_records=0 personal_bests=1 (kills Alpha#NA1 12 (was 10))." in line for line in logs)


def test_recap_without_a_record_has_no_record_line():
    channel = _record_cycle(iter_rows=_stored_history, kills=10)
    assert "New group record" not in channel.messages[0]["content"]


def test_recap_still_posts_when_the_record_check_fails():
    logs = []

    def broken(*args, **kwargs):
        raise RuntimeError("db down")

    channel = _record_cycle(iter_rows=broken, logs=logs)
    assert "**Alpha** · **Ahri** Mid" in channel.messages[0]["content"]
    assert "New group record" not in channel.messages[0]["content"]
    assert any("[records] Record check skipped; recap posts without it: RuntimeError: db down" in line for line in logs)


def _card_shape(view):
    return [(type(item).__name__, getattr(item, "content", None)) for item in view.walk_children()]


def test_recap_card_is_unchanged_when_no_record_is_broken_or_the_check_fails():
    before = _record_cycle(iter_rows=None, kills=10).messages[0]

    def broken(*args, **kwargs):
        raise RuntimeError("db down")

    for iter_rows in (_stored_history, broken):
        after = _record_cycle(iter_rows=iter_rows, kills=10).messages[0]
        assert after["content"] == before["content"]
        assert _card_shape(after["view"]) == _card_shape(before["view"])
        assert after["view"].children[0].accent_colour == before["view"].children[0].accent_colour
