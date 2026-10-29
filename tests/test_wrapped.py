"""Poro Wrapped: window, year accumulation, group cards, personal card, posting task, command."""

import asyncio
import contextvars
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

import src.commands.wrapped_handlers as wrapped_handlers
from src.discord_command_handlers import handle_incoming_message
from src.wrapped import (
    MESSAGE_CHAR_BUDGET,
    MIN_AWARD_GAMES,
    MIN_DUO_GAMES,
    PLAIN_MESSAGE_LIMIT,
    WRAPPED_STATE_KEY,
    build_wrapped,
    build_wrapped_views,
    compute_awards,
    format_group_cards,
    format_personal_sections,
    load_wrapped,
    post_wrapped_once,
    run_wrapped_loop,
    text_chunks,
    wrapped_period,
    wrapped_window,
)
from tests.discord_fakes import view_text

OSLO = ZoneInfo("Europe/Oslo")
YEAR = 2026
_counter = iter(range(1, 1_000_000))


def game(riot_id, *, day=10, hour=20, queue=420, win=True, champion="Lux", kills=5, deaths=3, assists=8, minutes=30,
         position="UTILITY", pings=5, dead=120, mates=(), placement=None, match_id=None, damage=20000, vision=30):
    n = next(_counter)
    end = datetime(YEAR, 3, 1, hour, tzinfo=OSLO) + timedelta(days=day, minutes=minutes)
    return {
        "match_id": match_id or f"EUW1_{n}",
        "puuid": riot_id.split("#")[0].lower(),
        "riot_id": riot_id,
        "queue_id": queue,
        "game_end_at": end.astimezone(timezone.utc),
        "duration_seconds": minutes * 60,
        "champion_name": champion,
        "team_position": position,
        "win": win,
        "remake": False,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "damage_to_champions": damage,
        "damage_share": 0.25,
        "damage_taken": 20000,
        "gold_earned": 12000,
        "cs": 200,
        "vision_score": vision,
        "solo_kills": 1,
        "penta_kills": 0,
        "killing_spree": 3,
        "pings": pings,
        "time_dead_seconds": dead,
        "tracked_teammates": [mate.split("#")[0].lower() for mate in mates],
        "arena_placement": placement,
    }


def year_of(rows, ranks=None):
    return build_wrapped(sorted(rows, key=lambda r: r["game_end_at"]), YEAR, OSLO, ranks)


# --- Window -------------------------------------------------------------------


def test_window_is_this_year_until_january_then_last_year():
    year, post_at, closes_at = wrapped_window(datetime(2026, 10, 30, tzinfo=timezone.utc), OSLO)
    assert (year, post_at, closes_at) == (2026, datetime(2026, 12, 31, 18, tzinfo=OSLO), datetime(2027, 2, 1, tzinfo=OSLO))
    assert wrapped_window(datetime(2027, 1, 20, tzinfo=timezone.utc), OSLO)[0] == 2026
    assert wrapped_window(datetime(2027, 2, 2, tzinfo=timezone.utc), OSLO)[0] == 2027
    assert wrapped_period(2026, OSLO) == (datetime(2026, 1, 1, tzinfo=OSLO), datetime(2026, 12, 31, 18, tzinfo=OSLO))


def test_load_wrapped_reads_the_year_up_to_now():
    calls = []

    def iter_rows(start, end):
        calls.append((start, end))
        return iter([game("Nox#EUW")])

    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    wrapped = load_wrapped(iter_rows, lambda: {"Nox#EUW": []}, YEAR, OSLO, end=now)
    assert calls == [(datetime(2026, 1, 1, tzinfo=OSLO), now)]
    assert wrapped.players["Nox#EUW"].games == 1


# --- Year ---------------------------------------------------------------------


def test_modes_unique_matches_and_night_games():
    shared = "EUW1_SHARED"
    wrapped = year_of([
        game("Nox#EUW", match_id=shared, mates=["Tam#EUW"]),
        game("Tam#EUW", match_id=shared, mates=["Nox#EUW"]),
        game("Nox#EUW", queue=450, hour=1),  # ARAM, started after midnight
        game("Nox#EUW", queue=1750, placement=1),
        game("Nox#EUW", queue=400),
    ])
    nox = wrapped.players["Nox#EUW"]
    assert set(nox.modes) == {"ranked", "aram", "arena", "other"}
    assert len(wrapped.matches) == 4
    assert nox.night_games == 1
    assert nox.modes["arena"].firsts == 1
    assert list(wrapped.duos.values()) == [[1, 1]]
    # Only ranked games feed the year's records.
    assert wrapped.book.games == 2


def _awards_year():
    rows = []
    # 20 ranked + 20 ARAM games, a new champion every game.
    for i in range(MIN_AWARD_GAMES * 2):
        rows.append(game("Grind#EUW", day=i % 200, queue=450 if i % 2 else 420, pings=40, champion=f"C{i}"))
    for i in range(MIN_DUO_GAMES):
        won = i < 4
        rows.append(game("Fizz#EUW", day=i, champion="Fizz", win=won, mates=["Pair#EUW"], match_id=f"EUW1_P{i}"))
        rows.append(game("Pair#EUW", day=i, champion="Ahri", win=won, mates=["Fizz#EUW"], match_id=f"EUW1_P{i}",
                         hour=2, dead=4000))
    for i in range(MIN_AWARD_GAMES + 5):
        rows.append(game("Pair#EUW", day=100, win=False, mates=["Duo#EUW"], match_id=f"EUW1_D{i}") if i < 3 else
                    game("Pair#EUW", day=101 + i, win=True, mates=["Duo#EUW"], match_id=f"EUW1_D{i}"))
        rows.append(game("Duo#EUW", day=100 if i < 3 else 101 + i, win=i >= 3, mates=["Pair#EUW"], match_id=f"EUW1_D{i}",
                         champion=f"D{i}"))
    return year_of(rows)


def test_awards_pick_holders_and_mark_all_mode_awards():
    awards = {award.title: award for award in compute_awards(_awards_year())}
    assert awards["The Grinder"].holder == "Pair#EUW" and awards["The Grinder"].all_modes
    assert awards["Backseat Gamer of the Year"].holder == "Grind#EUW"
    assert awards["Backseat Gamer of the Year"].text == "800 pings, 40 a game"
    assert awards["Backseat Gamer of the Year"].banter
    assert awards["Grey Screen Enjoyer"].holder == "Pair#EUW"
    assert awards["Night Owl"].holder == "Pair#EUW" and awards["Night Owl"].text.startswith("25 games")
    assert awards["One-Trick Pony"].holder == "Fizz#EUW"
    assert awards["Champion Ocean"].holder == "Grind#EUW"
    assert awards["Dream Duo"].holder == "Duo#EUW + Pair#EUW"
    assert awards["Cursed Duo"].holder == "Fizz#EUW + Pair#EUW" and awards["Cursed Duo"].text == "4-21 together"
    assert awards["Worst Day"].holder == "Pair#EUW" and "losses on" in awards["Worst Day"].text


def test_players_under_the_minimum_win_no_awards():
    rows = [game("Few#EUW", pings=999) for _ in range(MIN_AWARD_GAMES - 1)]
    assert compute_awards(year_of(rows)) == []


# --- Group post ---------------------------------------------------------------


def test_group_cards_have_ranked_main_event_then_aram_and_arena():
    rows = [game("Nox#EUW", day=i) for i in range(3)]
    rows += [game("Nox#EUW", queue=450, champion="Annie", kills=27), game("Tam#EUW", queue=1750, placement=1, damage=99000)]
    ranks = {"Nox#EUW": [{"queue_type": "RANKED_SOLO_5X5", "tier": "GOLD", "rank_division": "II"}],
             "Tam#EUW": [{"queue_type": "RANKED_FLEX_SR", "tier": "EMERALD", "rank_division": "I"}]}
    cards = format_group_cards(year_of(rows, ranks))
    text = "\n\n".join("\n\n".join(card) for card in cards)
    assert cards[0][0].startswith("🎁 **Poro Wrapped 2026**")
    assert cards[0][1].startswith("**5** games") and "\n**3** ranked · **1** ARAM · **1** Arena\n" in cards[0][1]
    assert "📜 **Ranked records of the year**" in text
    assert "❄️ **ARAM**\n-# 1 game on the Howling Abyss" in text
    assert "Most kills: **27** · Nox · Annie" in text
    assert "⚔️ **Arena**" in text and "Most 1st places: **Tam** · 1" in text
    # Tam ranks higher but played no ranked game this year, so only Nox is listed.
    assert cards[-1][1] == "**Gold II solo** · Nox"
    assert cards[-1][-1].endswith("open until 31 Jan")


def test_group_cards_skip_modes_nobody_played():
    cards = format_group_cards(year_of([game("Nox#EUW")]))
    text = "\n".join("\n".join(card) for card in cards)
    assert "**1** game · " in cards[0][1] and "\n**1** ranked\n" in cards[0][1]
    assert "Most played in ranked: **Lux**, 1 game" in cards[0][1]
    assert "ARAM" not in text and "Arena" not in text


# --- Personal card ------------------------------------------------------------


def test_personal_card_sections_and_title():
    champions = ["Zyra", "Lux", "Nami", "Janna", "Sona"]
    rows = [game("Nox#EUW", day=i, champion=champions[i % 5], vision=155 if i == 3 else 30,
                 mates=["Tam#EUW"] if i < 12 else []) for i in range(MIN_AWARD_GAMES)]
    rows += [game("Tam#EUW", day=i, mates=["Nox#EUW"], match_id=rows[i]["match_id"]) for i in range(12)]
    rows += [game("Nox#EUW", queue=450, kills=11, champion="Mordekaiser"),
             game("Nox#EUW", queue=1750, placement=3, damage=77525, champion="Leona")]
    ranks = {"Nox#EUW": [{"queue_type": "RANKED_FLEX_SR", "tier": "GOLD", "rank_division": "IV"},
                         {"queue_type": "RANKED_SOLO_5X5", "tier": "BRONZE", "rank_division": "I"}]}
    sections = format_personal_sections(year_of(rows, ranks), "Nox#EUW")
    text = "\n\n".join(sections)
    assert sections[0] == "🎁 **Nox** · Wrapped 2026\n-# Support main · Bronze I solo · Gold IV flex"
    assert sections[1].startswith(f"**{MIN_AWARD_GAMES + 2}** games")
    assert "**Ranked**\n**20** games · **100%** wins" in text
    assert "Most games with **Tam** (12) · best with **Tam** (100%)" in text
    # The outright best comes before values Nox shares with Tam.
    highs = next(section for section in sections if section.startswith("**Highs**")).split("\n")
    assert highs[1] == "155 vision score on Janna · best in the group this year"
    assert len(highs) == 4
    assert "❄️ **ARAM**\n**1** game · **100%** wins" in text
    assert "Best game: **11** kills on Mordekaiser" in text
    assert "⚔️ **Arena**\n**1** game · **100%** wins · **0** 1st places · avg **3.0**" in text
    assert "Best game: **77,525** damage on Leona" in text
    assert sections[-1] == "Your title: **The Support Who Survived**\n-# *100 pings all year. The quiet type. We respect it.*"


def test_personal_card_leaves_out_weak_highs_and_empty_modes():
    rows = [game("Top#EUW", vision=500, kills=40)] + [game("Low#EUW", vision=10, kills=1)]
    rows += [game(f"P{i}#EUW", vision=100 + i, kills=10 + i) for i in range(6)]
    text = "\n\n".join(format_personal_sections(year_of(rows), "Low#EUW"))
    assert "**Highs**" not in text
    assert "ARAM" not in text and "Arena" not in text
    assert "hours spent dead" not in text


def test_arena_main_gets_a_mode_label_and_title():
    rows = [game("Glad#EUW", queue=1750, placement=2, day=i, hour=20) for i in range(5)] + [game("Glad#EUW", queue=420)]
    sections = format_personal_sections(year_of(rows), "Glad#EUW")
    assert sections[0].endswith("-# Arena main")
    assert sections[-1].startswith("Your title: **The Gladiator**")


def test_personal_card_for_player_without_games():
    assert format_personal_sections(year_of([]), "New#EUW") == ["🎁 **New** · Wrapped 2026", "No games recorded in 2026 yet."]


# --- Cards and text -----------------------------------------------------------


def test_long_cards_split_into_several_messages():
    sections = ["x" * 1500] * 5
    views = build_wrapped_views(sections)
    assert len(views) == 3
    assert all(sum(len(t) for t in view_text(view).split("\n\n")) <= MESSAGE_CHAR_BUDGET for view in views)


def test_plain_text_chunks_stay_under_the_message_limit():
    chunks = text_chunks(["a" * 1200, "b" * 1200, "c" * 300])
    assert [len(chunk) for chunk in chunks] == [1200, 1502]
    assert all(len(chunk) <= PLAIN_MESSAGE_LIMIT for chunk in text_chunks(["z" * 5000]))


# --- Posting task -------------------------------------------------------------


class FakeChannel:
    def __init__(self, channel_id=999, reject_views=False):
        self.id = channel_id
        self.reject_views = reject_views
        self.sent = []

    async def send(self, content=None, *, view=None, tts=False):
        if view is not None and self.reject_views:
            raise discord.HTTPException(type("Response", (), {"status": 400, "reason": "Bad"})(), "bad card")
        self.sent.append(view_text(view) if view is not None else content)


def post(now, *, state=None, channel=None, logs=None):
    state = {} if state is None else state
    channel = channel if channel is not None else FakeChannel()

    async def resolve(_channel_id):
        return channel if channel is not False else None

    result = asyncio.run(post_wrapped_once(
        db_iter_wrapped_rows=lambda start, end: iter([game("Nox#EUW", day=i) for i in range(3)]),
        db_load_all_ranked_states=lambda: {},
        db_get_state=state.get,
        db_set_state=state.__setitem__,
        resolve_channel=resolve,
        channel_id=999,
        tz=OSLO,
        now_utc=now,
        log=(logs.append if logs is not None else (lambda _msg: None)),
    ))
    return result, state, channel


POST_TIME = datetime(2026, 12, 31, 18, tzinfo=OSLO).astimezone(timezone.utc)


def test_waits_until_new_years_eve():
    result, state, channel = post(POST_TIME - timedelta(minutes=1))
    assert result == "waiting" and state == {} and channel.sent == []


def test_posts_the_group_cards_once_and_marks_the_year_first():
    logs = []
    result, state, channel = post(POST_TIME + timedelta(minutes=5), logs=logs)
    assert result == "posted"
    assert state == {WRAPPED_STATE_KEY: "2026"}
    assert channel.sent[0].startswith("🎁 **Poro Wrapped 2026**")
    assert any(line.startswith("[wrapped] Posted Wrapped 2026 in channel 999: cards=") for line in logs)
    again, _state, channel2 = post(POST_TIME + timedelta(minutes=15), state=state)
    assert again == "already_posted" and channel2.sent == []


def test_a_late_start_still_posts_in_january_but_not_after():
    assert post(datetime(2027, 1, 3, tzinfo=timezone.utc))[0] == "posted"
    assert post(datetime(2027, 2, 1, 12, tzinfo=timezone.utc))[0] == "waiting"


def test_missing_channel_retries_without_marking():
    logs = []
    result, state, _channel = post(POST_TIME + timedelta(minutes=5), channel=False, logs=logs)
    assert result == "no_channel" and state == {}
    assert logs == ["[wrapped] Channel 999 not available; retrying next check."]


def test_rejected_cards_fall_back_to_plain_text():
    logs = []
    _result, _state, channel = post(POST_TIME + timedelta(minutes=5), channel=FakeChannel(reject_views=True), logs=logs)
    assert channel.sent and channel.sent[0].startswith("🎁 **Poro Wrapped 2026**")
    assert any("Discord rejected the group Wrapped card" in line for line in logs)


def test_loop_logs_failures_and_keeps_going():
    logs = []

    async def broken(_channel_id):
        raise RuntimeError("discord down")

    asyncio.run(run_wrapped_loop(
        db_iter_wrapped_rows=lambda start, end: iter([]),
        db_load_all_ranked_states=dict,
        db_get_state=lambda _key: None,
        db_set_state=lambda _key, _value: None,
        resolve_channel=broken,
        channel_id=999,
        tz=OSLO,
        log=logs.append,
        startup_delay_seconds=0,
        interval_seconds=0,
        max_passes=2,
        now=lambda: POST_TIME + timedelta(minutes=1),
    ))
    assert logs == ["[wrapped] Wrapped check failed; retrying next check: RuntimeError: discord down"] * 2


# --- Command ------------------------------------------------------------------


class FakeAuthor:
    bot = False
    id = 1
    roles = []

    def __init__(self, admin=False):
        self.guild_permissions = type("Perms", (), {"administrator": admin, "manage_guild": False})()


class FakeMessage:
    def __init__(self, content, channel, admin=False):
        self.content = content
        self.channel = channel
        self.author = FakeAuthor(admin)


def run_command(content, monkeypatch, *, now, admin=False, logs=None):
    channel = FakeChannel()
    monkeypatch.setattr(wrapped_handlers, "_now", lambda: now)
    rows = [game("NoxVain#EUW", day=i) for i in range(3)] + [game("Tamarin#EUW")]
    asyncio.run(handle_incoming_message(
        message=FakeMessage(content, channel, admin),
        channel_id=channel.id,
        friends=[],
        riot_client=None,
        poro_service=None,
        report_timezone_name="Europe/Oslo",
        report_day_start_hour=6,
        db_enabled=True,
        start_monotonic=0.0,
        daily_request_lock=asyncio.Lock(),
        request_id_context=contextvars.ContextVar("request_id", default=None),
        create_request_id=lambda _prefix: "cmd-1",
        get_or_create_report_message=None,
        remember_report_message=None,
        normalize_riot_id=lambda riot_id: riot_id,
        db_upsert_player=None,
        log=(logs.append if logs is not None else (lambda _msg: None)),
        events_channel_id=999,
        db_load_tracked_puuids=lambda: [("NoxVain#EUW", "noxvain"), ("Tamarin#EUW", "tamarin")],
        db_iter_wrapped_rows=lambda start, end: iter(rows),
        db_load_all_ranked_states=dict,
    ))
    return channel


OPEN = datetime(2027, 1, 5, 12, tzinfo=timezone.utc)
BEFORE = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def test_command_shows_a_players_card_while_open(monkeypatch):
    logs = []
    channel = run_command("!wrapped noxvain", monkeypatch, now=OPEN, logs=logs)
    assert channel.sent[0].startswith("🎁 **NoxVain** · Wrapped 2026")
    assert "Preview" not in channel.sent[0]
    assert logs[-1] == "[wrapped] Sent Wrapped 2026 for NoxVain#EUW: games=4 players=2."


def test_command_is_closed_before_new_years_eve_for_players(monkeypatch):
    channel = run_command("!wrapped NoxVain", monkeypatch, now=BEFORE)
    assert channel.sent == ["Poro Wrapped 2026 opens on 31 Dec at 18:00. See you then."]


def test_command_is_closed_after_january(monkeypatch):
    # In February the next Wrapped (2027) is the one coming up.
    channel = run_command("!wrapped NoxVain", monkeypatch, now=datetime(2027, 2, 3, tzinfo=timezone.utc))
    assert channel.sent == ["Poro Wrapped 2027 opens on 31 Dec at 18:00. See you then."]


def test_admins_can_preview_a_player_or_the_group_post(monkeypatch):
    logs = []
    personal = run_command("!wrapped NoxVain", monkeypatch, now=BEFORE, admin=True, logs=logs)
    assert "-# Preview for admins: the year so far." in personal.sent[0]
    assert logs[-1].endswith("(admin preview): games=4 players=2.")
    group = run_command("!wrapped", monkeypatch, now=BEFORE, admin=True)
    assert group.sent[0].startswith("🎁 **Poro Wrapped 2026**") and "Preview for admins" in group.sent[0]
    assert len(group.sent) >= 3


def test_command_without_a_name_explains_usage_and_unknown_names_fail(monkeypatch):
    assert run_command("!wrapped", monkeypatch, now=OPEN).sent == [
        "Use `!wrapped Name` (or `Name#Tag`) to see a player's year."
    ]
    assert run_command("!wrapped Nobody", monkeypatch, now=OPEN).sent == ["`Nobody` isn't a tracked player."]
