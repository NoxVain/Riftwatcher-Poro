"""Weekly title belts: awards, defended streaks, week windows, the card, and the posting loop."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from src.belts import (
    BELTS_ACCENT,
    BELTS_POST_DELAY,
    BELTS_STATE_KEY,
    apply_defended,
    build_belts_view,
    compute_week_belts,
    format_belts_sections,
    last_closed_week,
    post_weekly_belts_once,
    run_weekly_belts_loop,
)
from src.match_stats import extract_player_match_rows
from tests.discord_fakes import view_text

OSLO = ZoneInfo("Europe/Oslo")
START = datetime(2026, 10, 19, 6, tzinfo=OSLO)
END = START + timedelta(days=7)


def game(riot_id, *, queue=420, remake=False, minutes=30, damage=20000, vision=30, turrets=1, dragons=1, barons=0,
         heralds=0, saves=0, outnumbered=0, dodged=30, pings=10, dead_minutes=3):
    return {
        "riot_id": riot_id,
        "queue_id": queue,
        "remake": remake,
        "duration_seconds": minutes * 60,
        "damage_to_champions": damage,
        "vision_score": vision,
        "turret_takedowns": turrets,
        "dragon_takedowns": dragons,
        "baron_takedowns": barons,
        "herald_takedowns": heralds,
        "ally_saves": saves,
        "outnumbered_kills": outnumbered,
        "skillshots_dodged": dodged,
        "pings": pings,
        "time_dead_seconds": dead_minutes * 60,
    }


def games(riot_id, count=3, **kwargs):
    return [game(riot_id, **kwargs) for _ in range(count)]


def awards_by_key(week):
    return {award.belt.key: award for award in week.awards}


# --- Awards -----------------------------------------------------------------


def test_belts_need_three_ranked_games_and_skip_other_queues():
    rows = (
        games("Nox#EUW", 3, damage=30000)
        + games("Azim#EUW", 2, damage=90000)  # only two games
        + [game("Tam#EUW", queue=450, damage=90000) for _ in range(5)]  # ARAM
        + [game("Tam#EUW", remake=True, damage=90000) for _ in range(5)]
    )
    week = compute_week_belts(rows, START, END)
    assert week.games == 5
    assert week.players == 2
    assert awards_by_key(week)["damage_king"].holders == ["Nox#EUW"]
    assert awards_by_key(week)["damage_king"].text == "**1,000** damage/min"


def test_rates_are_per_minute_or_per_game_not_totals():
    rows = games("Grinder#EUW", 10, saves=1, vision=30) + games("Support#EUW", 3, saves=2, vision=45)
    belts = awards_by_key(compute_week_belts(rows, START, END))
    assert belts["bodyguard"].holders == ["Support#EUW"]
    assert belts["bodyguard"].text == "saved an ally from death **6** times"
    assert belts["vision_dad"].holders == ["Support#EUW"]
    assert belts["vision_dad"].text == "**1.50** vision/min"


def test_each_belt_text():
    rows = games("A#1", turrets=3, dragons=2, barons=1, heralds=1, outnumbered=2, dodged=60, pings=40, dead_minutes=6)
    belts = awards_by_key(compute_week_belts(rows, START, END))
    assert belts["objective_goblin"].text == "**7.0** objectives a game"
    assert belts["outnumbered"].text == "**6** kills while outnumbered"
    assert belts["matrix"].text == "**2.0** skillshots dodged/min"
    assert belts["backseat_gamer"].text == "**40** pings a game"
    assert belts["grey_screen"].text == "dead for **20%** of every game"


def test_ties_share_and_zero_or_missing_values_win_nothing():
    rows = games("B#1", damage=30000, saves=0, pings=None) + games("A#1", damage=30000, saves=0, pings=None)
    belts = awards_by_key(compute_week_belts(rows, START, END))
    assert belts["damage_king"].holders == ["A#1", "B#1"]
    assert "bodyguard" not in belts
    assert "backseat_gamer" not in belts


# --- Defended ---------------------------------------------------------------


def test_defended_streaks_continue_only_from_the_previous_week():
    week = compute_week_belts(games("Nox#EUW", pings=50) + games("Tam#EUW", pings=10), START, END)
    previous = {
        "week_start": (START - timedelta(days=7)).isoformat(),
        "holders": {"backseat_gamer": {"Nox#EUW": 2}, "damage_king": {"Tam#EUW": 4}},
    }
    holders = apply_defended(week, previous)
    assert holders["backseat_gamer"] == {"Nox#EUW": 3}
    # Tam held Damage King before, but Nox and Tam now share it with equal damage.
    assert holders["damage_king"] == {"Nox#EUW": 1, "Tam#EUW": 5}

    old = dict(previous, week_start=(START - timedelta(days=14)).isoformat())
    assert apply_defended(week, old)["backseat_gamer"] == {"Nox#EUW": 1}
    assert apply_defended(week, None)["backseat_gamer"] == {"Nox#EUW": 1}


# --- Week window ------------------------------------------------------------


def test_last_closed_week_uses_monday_day_start():
    # Monday 05:59 Oslo still belongs to the old week, so the closed week is the one before.
    before = datetime(2026, 10, 26, 5, 59, tzinfo=OSLO).astimezone(timezone.utc)
    assert last_closed_week(before, OSLO, 6) == (START - timedelta(days=7), START)
    after = datetime(2026, 10, 26, 6, 0, tzinfo=OSLO).astimezone(timezone.utc)
    assert last_closed_week(after, OSLO, 6) == (START, END)
    wednesday = datetime(2026, 10, 28, 12, tzinfo=OSLO).astimezone(timezone.utc)
    assert last_closed_week(wednesday, OSLO, 6) == (START, END)


def test_week_window_keeps_local_time_across_dst():
    # Summer time ended 25 Oct 2026: the week is 169 hours long but both ends are 06:00 local.
    start, end = last_closed_week(datetime(2026, 10, 27, tzinfo=timezone.utc), OSLO, 6)
    assert (start.hour, end.hour) == (6, 6)
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=169)


# --- Card -------------------------------------------------------------------


def test_card_sections_with_defended_and_shared_belts():
    rows = games("NoxVain#EUW", pings=50, damage=10000) + games("Tamarin#EUW", pings=20, damage=10000)
    week = compute_week_belts(rows, START, END)
    apply_defended(week, {
        "week_start": (START - timedelta(days=7)).isoformat(),
        "holders": {"backseat_gamer": {"NoxVain#EUW": 2}, "damage_king": {"Tamarin#EUW": 1}},
    })
    sections = format_belts_sections(week)
    text = "\n\n".join(sections)
    assert sections[0] == (
        "🎖️ **Weekly title belts** · 19–26 Oct\n-# 6 ranked games · 2 players · 3+ games to qualify"
    )
    assert "📣 **Backseat Gamer** · NoxVain · **50** pings a game · 🔁 defended ×2" in text
    assert "👑 **Damage King** · NoxVain and Tamarin (🔁 defended) · **333** damage/min" in text
    assert text.index("**Glory**") < text.index("**Shame**")
    assert sections[-1].startswith("-# Ranked Solo/Duo, Flex and 5v5")


def test_card_date_range_across_months_and_view():
    start = datetime(2026, 10, 26, 6, tzinfo=OSLO)
    week = compute_week_belts(games("A#1"), start, start + timedelta(days=7))
    sections = format_belts_sections(week)
    assert "· 26 Oct – 2 Nov\n" in sections[0]
    view = build_belts_view(sections)
    container = view.children[0]
    assert getattr(container.accent_colour, "value", container.accent_colour) == BELTS_ACCENT
    assert view_text(view) == "\n\n".join(sections)


# --- Stats extraction -------------------------------------------------------


def test_belt_columns_are_extracted_and_old_payloads_have_no_pings():
    participant = {
        "puuid": "p1", "teamId": 100, "win": True, "totalTimeSpentDead": 95, "allInPings": 2, "commandPings": 5,
        "challenges": {"riftHeraldTakedowns": 1, "epicMonsterSteals": 1, "saveAllyFromDeath": 3,
                       "outnumberedKills": 2, "skillshotsDodged": 40},
    }
    payload = {"metadata": {"matchId": "EUW1_1"},
               "info": {"queueId": 420, "gameDuration": 1800, "gameEndTimestamp": 1_760_000_000_000, "participants": [participant]}}
    row = extract_player_match_rows(payload, {"p1"})[0]
    assert {k: row[k] for k in ("herald_takedowns", "objective_steals", "ally_saves", "outnumbered_kills",
                                "skillshots_dodged", "pings", "time_dead_seconds")} == {
        "herald_takedowns": 1, "objective_steals": 1, "ally_saves": 3, "outnumbered_kills": 2,
        "skillshots_dodged": 40, "pings": 7, "time_dead_seconds": 95,
    }
    old = {"metadata": {"matchId": "EUW1_2"},
           "info": {"queueId": 420, "gameDuration": 1800, "gameEndTimestamp": 1_700_000_000_000,
                    "participants": [{"puuid": "p1", "teamId": 100, "win": False}]}}
    assert extract_player_match_rows(old, {"p1"})[0]["pings"] is None


# --- Posting ----------------------------------------------------------------


class FakeChannel:
    def __init__(self, reject_views=False):
        self.reject_views = reject_views
        self.sent = []

    async def send(self, content=None, *, view=None):
        if view is not None and self.reject_views:
            raise discord.HTTPException(type("Response", (), {"status": 400, "reason": "Bad"})(), "bad card")
        self.sent.append(view_text(view) if view is not None else content)


def run_once(*, state, rows=None, now=END + BELTS_POST_DELAY, channel=None, logs=None, missing_channel=False):
    channel = channel or FakeChannel()
    loads = []

    def load_rows(start, end):
        loads.append((start, end))
        return rows if rows is not None else games("NoxVain#EUW", pings=50)

    async def resolve(channel_id):
        assert channel_id == 555
        return None if missing_channel else channel

    result = asyncio.run(post_weekly_belts_once(
        db_load_belt_rows=load_rows,
        db_get_state=state.get,
        db_set_state=state.__setitem__,
        resolve_channel=resolve,
        channel_id=555,
        tz=OSLO,
        day_start_hour=6,
        now_utc=now.astimezone(timezone.utc),
        log=(logs.append if logs is not None else (lambda _msg: None)),
    ))
    return result, channel, loads


def previous_state():
    return json.dumps({"week_start": (START - timedelta(days=7)).isoformat(),
                       "holders": {"backseat_gamer": {"NoxVain#EUW": 1}}})


def test_first_run_stores_holders_without_posting():
    state = {}
    logs = []
    result, channel, loads = run_once(state=state, logs=logs)
    assert result == "baseline" and channel.sent == []
    assert loads == [(START, END)]
    stored = json.loads(state[BELTS_STATE_KEY])
    assert stored["week_start"] == START.isoformat()
    assert stored["holders"]["backseat_gamer"] == {"NoxVain#EUW": 1}
    assert "[belts] First run" in logs[0]


def test_posts_once_after_the_week_closes():
    state = {BELTS_STATE_KEY: previous_state()}
    logs = []
    result, channel, _ = run_once(state=state, logs=logs)
    assert result == "posted"
    assert channel.sent[0].startswith("🎖️ **Weekly title belts** · 19–26 Oct")
    assert "📣 **Backseat Gamer** · NoxVain · **50** pings a game · 🔁 defended" in channel.sent[0]
    assert json.loads(state[BELTS_STATE_KEY])["holders"]["backseat_gamer"] == {"NoxVain#EUW": 2}
    assert logs[-1].startswith("[belts] Posted weekly belts for 2026-10-19..2026-10-26 in channel 555: belts=")
    assert "defended=1" in logs[-1]

    again, channel, loads = run_once(state=state)
    assert again == "already_posted" and channel.sent == [] and loads == []


def test_waits_for_the_post_delay():
    state = {BELTS_STATE_KEY: previous_state()}
    result, channel, loads = run_once(state=state, now=END + BELTS_POST_DELAY - timedelta(minutes=1))
    assert result == "waiting" and channel.sent == [] and loads == []


def test_empty_week_is_marked_without_posting():
    state = {BELTS_STATE_KEY: previous_state()}
    result, channel, _ = run_once(state=state, rows=games("A#1", 2))
    assert result == "empty" and channel.sent == []
    assert json.loads(state[BELTS_STATE_KEY])["week_start"] == START.isoformat()


def test_missing_channel_retries_next_check():
    state = {BELTS_STATE_KEY: previous_state()}
    result, _, _ = run_once(state=state, missing_channel=True)
    assert result == "no_channel"
    assert json.loads(state[BELTS_STATE_KEY])["week_start"] != START.isoformat()


def test_rejected_card_falls_back_to_text():
    state = {BELTS_STATE_KEY: previous_state()}
    logs = []
    result, channel, _ = run_once(state=state, channel=FakeChannel(reject_views=True), logs=logs)
    assert result == "posted"
    assert channel.sent[0].startswith("🎖️ **Weekly title belts**")
    assert any("sending it as plain text" in line for line in logs)


def test_loop_never_raises():
    logs = []

    def broken(_key):
        raise RuntimeError("db down")

    async def resolve(_channel_id):
        return None

    asyncio.run(run_weekly_belts_loop(
        db_load_belt_rows=lambda start, end: [],
        db_get_state=broken,
        db_set_state=lambda key, value: None,
        resolve_channel=resolve,
        channel_id=1,
        tz=OSLO,
        day_start_hour=6,
        log=logs.append,
        startup_delay_seconds=0,
        max_passes=1,
    ))
    assert logs == ["[belts] Weekly belts check failed; retrying next check: RuntimeError: db down"]
