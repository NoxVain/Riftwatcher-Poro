import asyncio
import json

from src.recap_lp import (
    LP_MATCH_WINDOW_SECONDS,
    add_lp_to_content,
    build_lp_event,
    event_matches_game,
    format_lp_summary,
    handle_lp_event,
    load_state_list,
    lp_events_state_key,
    lp_pending_state_key,
    rank_points,
    register_pending_recap,
    retry_stored_lp_events,
    take_lp_event_for_game,
)


HEADING = "✅ **Alpha** · **Ahri** Mid"
RECAP = f"\U0001F3AE **New Match Recap**\n\n{HEADING}\n   stats line"


def _prev(tier, division, lp, wins, losses):
    return {"tier": tier, "rank_division": division, "league_points": lp, "wins": wins, "losses": losses}


def _cur(tier, division, lp, wins, losses, queue="RANKED_SOLO_5x5"):
    return {"queueType": queue, "tier": tier, "rank": division, "leaguePoints": lp, "wins": wins, "losses": losses}


def _event(*, won=True, delta=21, observed_at=1000.0, queue_type="RANKED_SOLO_5X5"):
    return {
        "queue_type": queue_type,
        "won": won,
        "delta": delta,
        "tier": "GOLD",
        "rank": "II",
        "league_points": 56,
        "observed_at": observed_at,
    }


class FakeMessage:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content
        self.edits = []

    async def edit(self, *, content):
        self.content = content
        self.edits.append(content)
        return self


class FakeRecapChannel:
    def __init__(self, messages=()):
        self.by_id = {message.id: message for message in messages}

    async def fetch_message(self, message_id):
        return self.by_id[message_id]


def _state_store():
    state = {}
    return state, state.get, state.__setitem__


def test_rank_points_handles_divisions_and_apex_ladder():
    assert rank_points("GOLD", "II", 56) - rank_points("GOLD", "III", 90) == 66
    assert rank_points("MASTER", "I", 0) - rank_points("DIAMOND", "I", 80) == 20
    assert rank_points("GRANDMASTER", "I", 400) == rank_points("MASTER", "I", 400)
    assert rank_points("GOLD", "V", 10) is None
    assert rank_points("UNRANKED", "I", 10) is None


def test_build_lp_event_win_with_promotion():
    event = build_lp_event(_prev("GOLD", "I", 90, 10, 8), _cur("PLATINUM", "IV", 8, 11, 8), 500.0)
    assert event["won"] is True
    assert event["delta"] == 18
    assert event["queue_type"] == "RANKED_SOLO_5X5"
    assert event["observed_at"] == 500.0


def test_build_lp_event_loss_allows_zero_for_demotion_protection():
    event = build_lp_event(_prev("SILVER", "IV", 0, 3, 3), _cur("SILVER", "IV", 0, 3, 4), 1.0)
    assert event["won"] is False
    assert event["delta"] == 0


def test_build_lp_event_rejects_unattributable_changes():
    prev = _prev("GOLD", "II", 50, 10, 10)
    # Dodge/decay: LP moved without a game.
    assert build_lp_event(prev, _cur("GOLD", "II", 45, 10, 10), 1.0) is None
    # Two games between polls.
    assert build_lp_event(prev, _cur("GOLD", "II", 90, 12, 10), 1.0) is None
    # Season reset.
    assert build_lp_event(prev, _cur("GOLD", "II", 0, 0, 0), 1.0) is None
    # Win with no LP gain, loss with LP gain.
    assert build_lp_event(prev, _cur("GOLD", "II", 50, 11, 10), 1.0) is None
    assert build_lp_event(prev, _cur("GOLD", "II", 60, 10, 11), 1.0) is None
    # Not a tracked ranked queue, missing counters, no baseline.
    assert build_lp_event(prev, _cur("GOLD", "II", 70, 11, 10, queue="CHERRY"), 1.0) is None
    assert build_lp_event(prev, {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 70}, 1.0) is None
    assert build_lp_event(None, _cur("GOLD", "II", 70, 11, 10), 1.0) is None


def test_format_lp_summary():
    assert format_lp_summary(_event()) == "Gold II · 35LP → **56LP\u00a0(+21)**"
    assert format_lp_summary(_event(won=False, delta=-18)) == "Gold II · 74LP → **56LP\u00a0(-18)**"
    assert format_lp_summary(_event(won=False, delta=0)) == "Gold II · 56LP → **56LP\u00a0(0)**"
    apex = dict(_event(), tier="MASTER", rank="I", league_points=120)
    assert format_lp_summary(apex) == "Master · 99LP → **120LP\u00a0(+21)**"


def test_format_lp_summary_rank_transitions():
    promotion = build_lp_event(_prev("GOLD", "I", 90, 10, 8), _cur("PLATINUM", "IV", 8, 11, 8), 500)
    assert format_lp_summary(promotion) == "Gold I 90LP → Platinum IV **8LP\u00a0(+18)**"
    demotion = build_lp_event(_prev("GOLD", "II", 5, 10, 8), _cur("GOLD", "III", 75, 10, 9), 500)
    assert format_lp_summary(demotion) == "Gold II 5LP → Gold III **75LP\u00a0(-30)**"


def test_format_lp_summary_old_events_at_division_boundaries():
    assert format_lp_summary(dict(_event(delta=18), league_points=8)) == "Gold II · **8LP\u00a0(+18)**"
    assert format_lp_summary(dict(_event(won=False, delta=-30), league_points=75)) == "Gold II · **75LP\u00a0(-30)**"


def test_add_lp_to_content_only_edits_a_unique_heading():
    updated = add_lp_to_content(RECAP, HEADING, "Gold II · 35LP → 56LP (+21)")
    assert f"{HEADING} · Gold II · 35LP → 56LP (+21)\n   stats line" in updated
    # Already edited: the bare heading line is gone, so it is not edited twice.
    assert add_lp_to_content(updated, HEADING, "x") is None
    assert add_lp_to_content(RECAP + "\n\n" + RECAP, HEADING, "x") is None
    assert add_lp_to_content(RECAP + "y" * 2000, HEADING, "x") is None


def test_event_matches_game_window():
    event = _event(observed_at=10_000.0)
    assert event_matches_game(event, queue_type="RANKED_SOLO_5X5", won=True, end_ts=9_700)
    assert event_matches_game(event, queue_type="RANKED_SOLO_5X5", won=True, end_ts=10_030)
    assert not event_matches_game(event, queue_type="RANKED_SOLO_5X5", won=False, end_ts=9_700)
    assert not event_matches_game(event, queue_type="RANKED_FLEX_SR", won=True, end_ts=9_700)
    assert not event_matches_game(event, queue_type="RANKED_SOLO_5X5", won=True, end_ts=10_000 - LP_MATCH_WINDOW_SECONDS - 1)
    assert not event_matches_game(event, queue_type="RANKED_SOLO_5X5", won=True, end_ts=10_200)


def test_load_state_list_tolerates_bad_json():
    assert load_state_list(None) == []
    assert load_state_list("not json") == []
    assert load_state_list('{"a": 1}') == []
    assert load_state_list('[{"a": 1}, 3]') == [{"a": 1}]


def test_event_first_is_stored_then_taken_by_recap():
    state, get, put = _state_store()
    handled = asyncio.run(
        handle_lp_event(
            riot_id="Alpha#EUW", event=_event(observed_at=1000.0), recap_channel=None,
            db_get_state=get, db_set_state=put, log=lambda _m: None, now=1000.0,
        )
    )
    assert handled is False
    assert len(json.loads(state[lp_events_state_key("Alpha#EUW")])) == 1

    taken = asyncio.run(
        take_lp_event_for_game(
            db_get_state=get, db_set_state=put, riot_id="alpha#euw",
            queue_type="RANKED_SOLO_5X5", won=True, end_ts=900, now=1010.0,
        )
    )
    assert taken["delta"] == 21
    assert json.loads(state[lp_events_state_key("Alpha#EUW")]) == []


def test_recap_first_is_edited_when_event_arrives():
    state, get, put = _state_store()
    message = FakeMessage(55, RECAP)
    pending = {"match_id": "EUW1_9", "queue_type": "RANKED_SOLO_5X5", "won": True, "end_ts": 900, "heading": HEADING, "message_id": 55}
    assert asyncio.run(register_pending_recap(db_get_state=get, db_set_state=put, riot_id="Alpha#EUW", pending=pending, now=950.0)) is None

    logs = []
    handled = asyncio.run(
        handle_lp_event(
            riot_id="Alpha#EUW", event=_event(observed_at=1000.0), recap_channel=FakeRecapChannel([message]),
            db_get_state=get, db_set_state=put, log=logs.append, now=1000.0,
        )
    )
    assert handled is True
    assert "35LP → **56LP\u00a0(+21)**" in message.content
    assert json.loads(state[lp_pending_state_key("Alpha#EUW")]) == []
    assert lp_events_state_key("Alpha#EUW") not in state


def test_register_pending_returns_event_that_arrived_during_send():
    state, get, put = _state_store()
    put(lp_events_state_key("Alpha#EUW"), json.dumps([_event(observed_at=1000.0)]))
    pending = {"match_id": "EUW1_9", "queue_type": "RANKED_SOLO_5X5", "won": True, "end_ts": 900, "heading": HEADING, "message_id": 55}
    event = asyncio.run(register_pending_recap(db_get_state=get, db_set_state=put, riot_id="Alpha#EUW", pending=pending, now=1001.0))
    assert event["delta"] == 21
    assert lp_pending_state_key("Alpha#EUW") not in state


def test_stale_pending_and_events_are_pruned():
    state, get, put = _state_store()
    put(lp_pending_state_key("Alpha#EUW"), json.dumps([{"match_id": "old", "queue_type": "RANKED_SOLO_5X5", "won": True, "end_ts": 0, "heading": HEADING, "message_id": 1}]))
    now = 10 * LP_MATCH_WINDOW_SECONDS
    handled = asyncio.run(
        handle_lp_event(
            riot_id="Alpha#EUW", event=_event(observed_at=now), recap_channel=FakeRecapChannel(),
            db_get_state=get, db_set_state=put, log=lambda _m: None, now=now,
        )
    )
    assert handled is False
    assert len(json.loads(state[lp_events_state_key("Alpha#EUW")])) == 1


def test_missing_recap_channel_keeps_pending_and_retry_edits_later():
    state, get, put = _state_store()
    message = FakeMessage(55, RECAP)
    pending = {"match_id": "EUW1_9", "queue_type": "RANKED_SOLO_5X5", "won": True, "end_ts": 900, "heading": HEADING, "message_id": 55}
    put(lp_pending_state_key("Alpha#EUW"), json.dumps([pending]))

    handled = asyncio.run(
        handle_lp_event(
            riot_id="Alpha#EUW", event=_event(observed_at=1000.0), recap_channel=None,
            db_get_state=get, db_set_state=put, log=lambda _m: None, now=1000.0,
        )
    )
    assert handled is False
    assert len(json.loads(state[lp_pending_state_key("Alpha#EUW")])) == 1
    assert len(json.loads(state[lp_events_state_key("Alpha#EUW")])) == 1
    assert asyncio.run(
        retry_stored_lp_events(
            riot_id="Alpha#EUW", recap_channel=None, db_get_state=get, db_set_state=put,
            log=lambda _m: None, now=1100.0,
        )
    ) == 0

    edited = asyncio.run(
        retry_stored_lp_events(
            riot_id="Alpha#EUW", recap_channel=FakeRecapChannel([message]), db_get_state=get, db_set_state=put,
            log=lambda _m: None, now=1300.0,
        )
    )
    assert edited == 1
    assert "35LP → **56LP\u00a0(+21)**" in message.content
    assert json.loads(state[lp_pending_state_key("Alpha#EUW")]) == []
    assert json.loads(state[lp_events_state_key("Alpha#EUW")]) == []


def test_retry_leaves_unmatched_events_for_the_recap_worker():
    state, get, put = _state_store()
    put(lp_events_state_key("Alpha#EUW"), json.dumps([_event(observed_at=1000.0)]))
    edited = asyncio.run(
        retry_stored_lp_events(
            riot_id="Alpha#EUW", recap_channel=FakeRecapChannel(), db_get_state=get, db_set_state=put,
            log=lambda _m: None, now=1010.0,
        )
    )
    assert edited == 0
    assert len(json.loads(state[lp_events_state_key("Alpha#EUW")])) == 1
    assert lp_pending_state_key("Alpha#EUW") not in state


def test_edit_failure_is_logged_not_raised():
    state, get, put = _state_store()
    pending = {"match_id": "EUW1_9", "queue_type": "RANKED_SOLO_5X5", "won": True, "end_ts": 900, "heading": HEADING, "message_id": 404}
    put(lp_pending_state_key("Alpha#EUW"), json.dumps([pending]))
    logs = []
    handled = asyncio.run(
        handle_lp_event(
            riot_id="Alpha#EUW", event=_event(observed_at=1000.0), recap_channel=FakeRecapChannel(),
            db_get_state=get, db_set_state=put, log=logs.append, now=1000.0,
        )
    )
    assert handled is False
    assert any("Could not add LP" in line for line in logs)
