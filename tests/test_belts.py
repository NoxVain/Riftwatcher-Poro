"""Weekly title belts: awards, defended streaks, week windows, the card, and the posting loop."""

import asyncio
import itertools
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
MATCH_IDS = itertools.count(1)


def game(riot_id, *, match_id=None, queue=420, remake=False, minutes=30, damage=20000, gold=12000, vision=30, turrets=1,
         dragons=1, barons=0, heralds=0, saves=0, outnumbered=0, dodged=30, pings=10, dead_minutes=3):
    return {
        "match_id": match_id or f"EUW1_{next(MATCH_IDS)}",
        "riot_id": riot_id,
        "queue_id": queue,
        "remake": remake,
        "duration_seconds": minutes * 60,
        "damage_to_champions": damage,
        "gold_earned": gold,
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


def test_games_played_together_count_once():
    # Two duo games and one solo game: 3 games, 5 player-games.
    rows = (
        [game("Nox#EUW", match_id="EUW1_A"), game("Tam#EUW", match_id="EUW1_A")]
        + [game("Nox#EUW", match_id="EUW1_B"), game("Tam#EUW", match_id="EUW1_B")]
        + [game("Nox#EUW", match_id="EUW1_C"), game("Nox#EUW", match_id="EUW1_D", remake=True)]
    )
    week = compute_week_belts(rows, START, END)
    assert (week.games, week.player_games, week.players) == (3, 5, 2)
    assert "-# 3 ranked games · 2 players" in format_belts_sections(week)[0]


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
    assert belts["gold_magnet"].text == "**400** gold/min"


def test_ties_share_and_zero_or_missing_values_win_nothing():
    rows = games("B#1", damage=30000, saves=0, pings=None) + games("A#1", damage=30000, saves=0, pings=None)
    belts = awards_by_key(compute_week_belts(rows, START, END))
    assert belts["damage_king"].holders == ["A#1", "B#1"]
    assert "bodyguard" not in belts
    assert "backseat_gamer" not in belts


def lp(delta, games=5, reliable=True):
    return {"lp_delta": delta, "games": games, "reliable": reliable}


def test_lp_belts_go_to_the_biggest_net_gain_and_loss_and_share_ties():
    rows = games("A#1") + games("B#1") + games("C#1") + games("D#1")
    lp_totals = {"A#1": lp(125, 7), "B#1": lp(125), "C#1": lp(-80, 6), "D#1": lp(-20, 3)}
    week = compute_week_belts(rows, START, END, lp_totals=lp_totals)
    belts = awards_by_key(week)
    assert belts["lp_gained"].holders == ["A#1", "B#1"]
    assert belts["lp_gained"].text == "gained **125 LP**"
    assert belts["lp_lost"].holders == ["C#1"]
    assert belts["lp_lost"].text == "lost **80 LP**"
    state = apply_defended(week, {
        "week_start": (START - timedelta(days=7)).isoformat(),
        "holders": {"lp_gained": {"A#1": 2}, "lp_lost": {"D#1": 1}},
    })
    assert state["lp_gained"] == {"A#1": 3, "B#1": 1}
    assert state["lp_lost"] == {"C#1": 1}


def test_lp_belts_need_three_observed_games_and_a_sum_that_can_be_trusted():
    rows = (games("Rookie#1", 2) + games("Few#1") + games("Reset#1") + games("Unknown#1") + games("Zero#1")
            + games("NoHistory#1"))
    lp_totals = {
        "Rookie#1": lp(999),                    # only 2 ranked games
        "Few#1": lp(-999, games=2),             # only 2 games seen by the rank worker
        "Reset#1": lp(500, reliable=False),     # a win/loss counter went down
        "Unknown#1": lp(None, reliable=False),  # no LP change stored
        "Zero#1": lp(0),
        "Removed#1": lp(1000),                  # no games in the belt rows
    }
    belts = awards_by_key(compute_week_belts(rows, START, END, lp_totals=lp_totals))
    assert "lp_gained" not in belts and "lp_lost" not in belts
    assert "damage_king" in belts


def test_a_week_with_only_gains_has_no_lp_donor_and_the_other_way_round():
    for delta, expected, absent in ((20, "lp_gained", "lp_lost"), (-20, "lp_lost", "lp_gained")):
        belts = awards_by_key(compute_week_belts(games("A#1"), START, END, lp_totals={"A#1": lp(delta)}))
        assert expected in belts and absent not in belts


def test_gold_magnet_uses_total_gold_divided_by_total_minutes():
    # A's average of per-game rates is higher (567), but B earns more per minute played.
    rows = [game("A#1", gold=9000, minutes=10), game("A#1", gold=12000, minutes=50),
            game("A#1", gold=9000, minutes=30)] + games("B#1", gold=12000)
    rows += games("Rookie#1", 2, gold=60000)
    belt = awards_by_key(compute_week_belts(rows, START, END))["gold_magnet"]
    assert belt.holders == ["B#1"] and belt.text == "**400** gold/min"


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


def test_lp_belts_sit_first_and_last_and_add_a_footer_line():
    rows = games("NoxVain#EUW") + games("Tamarin#EUW")
    week = compute_week_belts(rows, START, END, lp_totals={"NoxVain#EUW": lp(41), "Tamarin#EUW": lp(-103)})
    apply_defended(week, None)
    sections = format_belts_sections(week)
    glory = next(s for s in sections if s.startswith("**Glory**"))
    shame = next(s for s in sections if s.startswith("**Shame**"))
    assert glory.splitlines()[1] == "🪜 **LP Climber** · NoxVain · gained **41 LP**"
    assert shame.splitlines()[-1] == "📉 **LP Donor** · Tamarin · lost **103 LP**"
    assert sections[-1].endswith("\n-# LP is the net change in Solo/Duo and Flex together")
    # No LP belt on the card, no LP footer.
    assert "LP" not in "\n".join(format_belts_sections(compute_week_belts(rows, START, END)))


def test_vision_daddy_keeps_the_vision_dad_key_so_streaks_continue():
    # Renamed on the card only; the stored holders still use "vision_dad".
    rows = games("Support#EUW", vision=45)
    week = compute_week_belts(rows, START, END)
    apply_defended(week, {
        "week_start": (START - timedelta(days=7)).isoformat(),
        "holders": {"vision_dad": {"Support#EUW": 1}},
    })
    text = "\n\n".join(format_belts_sections(week))
    assert "👁️ **Vision Daddy** · Support · **1.50** vision/min · 🔁 defended" in text
    assert "Vision Dad " not in text


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


def run_once(*, state, rows=None, now=END + BELTS_POST_DELAY, channel=None, logs=None, missing_channel=False,
             lp_totals=None, lp_error=None):
    channel = channel or FakeChannel()
    loads = []

    def load_rows(start, end):
        loads.append((start, end))
        return rows if rows is not None else games("NoxVain#EUW", pings=50)

    def load_lp_totals(start, end, queue_types):
        assert (start, end) == (START, END)
        assert queue_types == ("RANKED_SOLO_5X5", "RANKED_FLEX_SR")
        if lp_error is not None:
            raise lp_error
        return lp_totals or {}

    async def resolve(channel_id):
        assert channel_id == 555
        return None if missing_channel else channel

    result = asyncio.run(post_weekly_belts_once(
        db_load_belt_rows=load_rows,
        db_load_belt_lp_totals=load_lp_totals,
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


def test_lp_belts_post_and_are_stored_and_logged():
    state = {BELTS_STATE_KEY: previous_state()}
    logs = []
    lp_totals = {"NoxVain#EUW": lp(140, 8), "Tamarin#EUW": lp(-60, reliable=False)}
    result, channel, _ = run_once(state=state, logs=logs, lp_totals=lp_totals)
    assert result == "posted"
    assert "🪜 **LP Climber** · NoxVain · gained **140 LP**" in channel.sent[0]
    assert json.loads(state[BELTS_STATE_KEY])["holders"]["lp_gained"] == {"NoxVain#EUW": 1}
    # One of the two players has an LP sum that can be used.
    assert logs[-1].endswith("lp_players=1.")


def test_failed_lp_read_still_posts_the_other_belts():
    state = {BELTS_STATE_KEY: previous_state()}
    logs = []
    result, channel, _ = run_once(state=state, logs=logs, lp_error=RuntimeError("db down"))
    assert result == "posted"
    assert "Backseat Gamer" in channel.sent[0] and "LP Climber" not in channel.sent[0]
    assert logs[0] == "[belts] Could not read LP history; posting without the LP belts: RuntimeError: db down"
    assert logs[-1].endswith("lp_players=0.")


def test_loop_never_raises():
    logs = []

    def broken(_key):
        raise RuntimeError("db down")

    async def resolve(_channel_id):
        return None

    asyncio.run(run_weekly_belts_loop(
        db_load_belt_rows=lambda start, end: [],
        db_load_belt_lp_totals=lambda start, end, queue_types: {},
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
