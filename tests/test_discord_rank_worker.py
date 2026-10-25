import asyncio
import json

import requests

from src.discord_rank_worker import process_rank_cycle
from src.recap_lp import lp_pending_state_key


def _row(queue_type, tier, division, lp=0):
    return {
        "queue_type": queue_type,
        "tier": tier,
        "rank_division": division,
        "league_points": lp,
        "wins": 0,
        "losses": 0,
        "hot_streak": False,
        "veteran": False,
        "fresh_blood": False,
        "inactive": False,
        "updated_at": None,
    }


class FakeChannel:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FakeRiotClient:
    def __init__(self):
        self.by_player = {}

    async def fetch_ranked_entries(self, riot_id):
        value = self.by_player[riot_id]
        if isinstance(value, Exception):
            raise value
        return value


def test_process_rank_cycle_baseline_persists_without_notifications():
    channel = FakeChannel()
    riot = FakeRiotClient()
    riot.by_player["Alpha#EUW"] = [
        {"queueType": "RANKED_SOLO_5x5", "tier": "SILVER", "rank": "II", "leaguePoints": 55},
        {"queueType": "RANKED_FLEX_SR", "tier": "GOLD", "rank": "IV", "leaguePoints": 10},
    ]
    state = {}
    upserts = []

    def db_load_ranked_state(riot_id):
        return state.get(riot_id, [])

    def db_upsert_ranked_state(riot_id, queue_type, entry):
        upserts.append((riot_id, queue_type, entry["tier"], entry["rank"]))

    asyncio.run(
        process_rank_cycle(
            friends=["Alpha#EUW"],
            channel=channel,
            riot_client=riot,
            db_load_ranked_state=db_load_ranked_state,
            db_upsert_ranked_state=db_upsert_ranked_state,
            db_delete_ranked_state_queue=lambda _riot_id, _queue_type: None,
            log=lambda _msg: None,
        )
    )

    assert channel.messages == []
    assert len(upserts) == 2


def test_process_rank_cycle_sends_rank_up_and_rank_down_once_each():
    channel = FakeChannel()
    riot = FakeRiotClient()
    riot.by_player["Alpha#EUW"] = [
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "IV", "leaguePoints": 12},
        {"queueType": "RANKED_FLEX_SR", "tier": "SILVER", "rank": "I", "leaguePoints": 80},
    ]
    state = {
        "Alpha#EUW": [
            _row("RANKED_SOLO_5x5", "SILVER", "I"),
            _row("RANKED_FLEX_SR", "GOLD", "IV"),
        ]
    }

    asyncio.run(
        process_rank_cycle(
            friends=["Alpha#EUW"],
            channel=channel,
            riot_client=riot,
            db_load_ranked_state=lambda riot_id: state.get(riot_id, []),
            db_upsert_ranked_state=lambda _riot_id, _queue_type, _entry: None,
            db_delete_ranked_state_queue=lambda _riot_id, _queue_type: None,
            log=lambda _msg: None,
        )
    )

    assert len(channel.messages) == 2
    assert any("Rank Up" in msg for msg in channel.messages)
    assert any("Rank Down" in msg for msg in channel.messages)


def test_process_rank_cycle_continues_after_one_player_failure():
    channel = FakeChannel()
    riot = FakeRiotClient()
    riot.by_player["Bad#EUW"] = requests.RequestException("boom")
    riot.by_player["Good#EUW"] = [
        {"queueType": "RANKED_SOLO_5x5", "tier": "SILVER", "rank": "II", "leaguePoints": 20},
    ]
    upserts = []
    logs = []

    asyncio.run(
        process_rank_cycle(
            friends=["Bad#EUW", "Good#EUW"],
            channel=channel,
            riot_client=riot,
            db_load_ranked_state=lambda _riot_id: [],
            db_upsert_ranked_state=lambda riot_id, queue_type, _entry: upserts.append((riot_id, queue_type)),
            db_delete_ranked_state_queue=lambda _riot_id, _queue_type: None,
            log=logs.append,
        )
    )

    assert ("Good#EUW", "RANKED_SOLO_5X5") in upserts
    assert any("Failed rank-check for Bad#EUW" in msg for msg in logs)


class FakeRecapMessage:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content

    async def edit(self, *, content):
        self.content = content
        return self


class FakeRecapChannel:
    def __init__(self, message):
        self.message = message

    async def fetch_message(self, message_id):
        assert message_id == self.message.id
        return self.message


def _ranked_row(lp, wins, losses):
    return dict(_row("RANKED_SOLO_5x5", "GOLD", "II", lp), wins=wins, losses=losses)


def test_process_rank_cycle_edits_lp_into_pending_recap(monkeypatch):
    from src import discord_rank_worker

    heading = "❌ **Alpha** · **Ahri** Mid"
    recap = FakeRecapMessage(77, f"\U0001F3AE **New Match Recap**\n\n{heading}\n   stats")
    now = 1_000_000.0
    monkeypatch.setattr(discord_rank_worker.time, "time", lambda: now)
    bot_state = {
        lp_pending_state_key("Alpha#EUW"): json.dumps(
            [{"match_id": "EUW1_5", "queue_type": "RANKED_SOLO_5X5", "won": False,
              "end_ts": now - 240, "heading": heading, "message_id": 77}]
        )
    }
    riot = FakeRiotClient()
    riot.by_player["Alpha#EUW"] = [
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 22, "wins": 10, "losses": 10},
    ]
    upserts = []

    asyncio.run(
        process_rank_cycle(
            friends=["Alpha#EUW"],
            channel=FakeChannel(),
            riot_client=riot,
            db_load_ranked_state=lambda _riot_id: [_ranked_row(40, 10, 9)],
            db_upsert_ranked_state=lambda riot_id, queue_type, _entry: upserts.append((riot_id, queue_type)),
            db_delete_ranked_state_queue=lambda _riot_id, _queue_type: None,
            log=lambda _msg: None,
            recap_channel=FakeRecapChannel(recap),
            db_get_state=bot_state.get,
            db_set_state=bot_state.__setitem__,
        )
    )

    assert f"{heading} · Gold II · 40LP → **22LP\u00a0(-18)**\n" in recap.content
    assert json.loads(bot_state[lp_pending_state_key("Alpha#EUW")]) == []
    assert upserts == [("Alpha#EUW", "RANKED_SOLO_5X5")]


def test_process_rank_cycle_retries_lp_edit_after_recap_channel_was_missing(monkeypatch):
    from src import discord_rank_worker

    heading = "✅ **Alpha** · **Ahri** Mid"
    recap = FakeRecapMessage(77, f"\U0001F3AE **New Match Recap**\n\n{heading}\n   stats")
    now = 1_000_000.0
    monkeypatch.setattr(discord_rank_worker.time, "time", lambda: now)
    bot_state = {
        lp_pending_state_key("Alpha#EUW"): json.dumps(
            [{"match_id": "EUW1_5", "queue_type": "RANKED_SOLO_5X5", "won": True,
              "end_ts": now - 240, "heading": heading, "message_id": 77}]
        )
    }
    stored_rows = {"Alpha#EUW": [_ranked_row(40, 10, 9)]}

    def db_upsert_ranked_state(riot_id, _queue_type, entry):
        stored_rows[riot_id] = [_ranked_row(entry["leaguePoints"], entry["wins"], entry["losses"])]

    riot = FakeRiotClient()
    riot.by_player["Alpha#EUW"] = [
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 61, "wins": 11, "losses": 9},
    ]

    def run_cycle(recap_channel):
        asyncio.run(
            process_rank_cycle(
                friends=["Alpha#EUW"],
                channel=FakeChannel(),
                riot_client=riot,
                db_load_ranked_state=lambda riot_id: stored_rows[riot_id],
                db_upsert_ranked_state=db_upsert_ranked_state,
                db_delete_ranked_state_queue=lambda _riot_id, _queue_type: None,
                log=lambda _msg: None,
                recap_channel=recap_channel,
                db_get_state=bot_state.get,
                db_set_state=bot_state.__setitem__,
            )
        )

    # Recap channel unavailable: the game is detected but the recap can't be edited yet.
    run_cycle(None)
    assert "LP" not in recap.content
    assert len(json.loads(bot_state[lp_pending_state_key("Alpha#EUW")])) == 1

    # Next poll: counters are unchanged, but the stored change is paired with the recap.
    now += 300
    run_cycle(FakeRecapChannel(recap))
    assert f"{heading} · Gold II · 40LP → **61LP\u00a0(+21)**\n" in recap.content
    assert json.loads(bot_state[lp_pending_state_key("Alpha#EUW")]) == []


def test_process_rank_cycle_lp_failure_does_not_block_rank_state():
    riot = FakeRiotClient()
    riot.by_player["Alpha#EUW"] = [
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 60, "wins": 11, "losses": 9},
    ]
    upserts = []
    logs = []

    def broken_get_state(_key):
        raise RuntimeError("db down")

    asyncio.run(
        process_rank_cycle(
            friends=["Alpha#EUW"],
            channel=FakeChannel(),
            riot_client=riot,
            db_load_ranked_state=lambda _riot_id: [_ranked_row(40, 10, 9)],
            db_upsert_ranked_state=lambda riot_id, queue_type, _entry: upserts.append((riot_id, queue_type)),
            db_delete_ranked_state_queue=lambda _riot_id, _queue_type: None,
            log=logs.append,
            recap_channel=None,
            db_get_state=broken_get_state,
            db_set_state=lambda _key, _value: None,
        )
    )

    assert upserts == [("Alpha#EUW", "RANKED_SOLO_5X5")]
    assert any("Could not record LP change" in line for line in logs)
