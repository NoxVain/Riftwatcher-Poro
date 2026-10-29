"""!records: group records, personal bests, recap record lines, and the command handler."""

import asyncio
import contextvars
from datetime import datetime, timedelta, timezone

import discord

from src.discord_command_handlers import handle_incoming_message
from src.match_stats import extract_player_match_rows
from src.records import (
    CATEGORIES_BY_KEY,
    MIN_GAMES_FOR_PERSONAL_BESTS,
    MIN_GAMES_FOR_RECAP_RECORDS,
    RECORDS_ACCENT,
    RecordBreak,
    build_record_book,
    build_records_view,
    detect_record_breaks,
    format_group_sections,
    format_personal_sections,
    format_record_break,
    load_record_book,
)
from tests.discord_fakes import view_text

START = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
_counter = iter(range(1, 1_000_000))


def row(riot_id="Nox#EUW", *, days=0, queue=420, win=True, kills=5, deaths=3, assists=8, minutes=30,
        champion="Lux", damage=20000, share=0.25, taken=20000, gold=12000, cs=200, vision=30,
        solo_kills=1, pentas=0, spree=3, camps=None, buffs=None, dragons=None, barons=None, steals=None,
        remake=False):
    n = next(_counter)
    return {
        "match_id": f"EUW1_{n}",
        "riot_id": riot_id,
        "queue_id": queue,
        "game_end_at": START + timedelta(days=days, minutes=n),
        "duration_seconds": minutes * 60,
        "champion_name": champion,
        "win": win,
        "remake": remake,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "damage_to_champions": damage,
        "damage_share": share,
        "damage_taken": taken,
        "gold_earned": gold,
        "cs": cs,
        "vision_score": vision,
        "solo_kills": solo_kills,
        "penta_kills": pentas,
        "killing_spree": spree,
        "enemy_jungle_camps": camps,
        "buffs_stolen": buffs,
        "dragon_takedowns": dragons,
        "baron_takedowns": barons,
        "objective_steals": steals,
    }


def history(count=MIN_GAMES_FOR_RECAP_RECORDS, riot_id="Filler#EUW", **overrides):
    return [row(riot_id, days=i % 300, **overrides) for i in range(count)]


# --- RecordBook -------------------------------------------------------------


def test_only_ranked_non_remake_games_count():
    book = build_record_book([
        row(kills=10),
        row(queue=440, kills=11),
        row(queue=710, kills=12),
        row(queue=450, kills=40),  # ARAM
        row(queue=400, kills=30),  # normal draft
        row(kills=35, remake=True),
    ])
    assert book.games == 3
    assert book.record("kills").value == 12


def test_tie_keeps_the_earlier_game():
    first = row("Alpha#EUW", days=1, kills=20, champion="Akshan")
    book = build_record_book([first, row("Bravo#EUW", days=5, kills=20)])
    holder = book.record("kills")
    assert (holder.riot_id, holder.champion) == ("Alpha#EUW", "Akshan")


def test_category_eligibility():
    book = build_record_book([
        row(spree=9),
        row(spree=None),  # stats row from before the column existed
        row(minutes=14, share=0.6, cs=400),  # too short for share and CS/min
        row(minutes=25, share=0.4, cs=250),
    ])
    assert book.record("kill_streak").value == 9
    assert book.record("damage_share").value == 0.4
    assert book.record("cs_per_minute").value == 10.0
    assert book.record("cs").value == 400


def test_ranked_streaks_span_queues_and_skip_other_games():
    rows = [
        row("Nox#EUW", days=0, win=True),
        row("Nox#EUW", days=1, queue=440, win=True),
        row("Nox#EUW", days=2, queue=450, win=False),  # ARAM doesn't break the streak
        row("Nox#EUW", days=3, win=True),
        row("Nox#EUW", days=4, win=False),
        row("Nox#EUW", days=5, win=False),
        row("Tam#EUW", days=0, win=True),
        row("Tam#EUW", days=1, win=True),
    ]
    book = build_record_book(rows)
    assert book.record("win").value == 3
    assert book.record("win").riot_id == "Nox#EUW"
    assert book.record("loss").value == 2
    assert book.group_rank("win", "Tam#EUW") == 2


def test_group_rank_counts_strictly_better_players():
    book = build_record_book([row("A#1", kills=20), row("B#1", kills=20), row("C#1", kills=10)])
    assert book.group_rank("kills", "A#1") == 1
    assert book.group_rank("kills", "B#1") == 1
    assert book.group_rank("kills", "C#1") == 3
    assert book.group_rank("kills", "Missing#1") is None


def test_load_record_book_asks_for_ranked_queues():
    calls = []

    def iter_rows(queue_ids, *, before=None, exclude_match_ids=()):
        calls.append((tuple(queue_ids), before, tuple(exclude_match_ids)))
        return iter([row(kills=7)])

    book = load_record_book(iter_rows, before=START, exclude_match_ids=["EUW1_9"])
    assert calls == [((420, 440, 710), START, ("EUW1_9",))]
    assert book.record("kills").value == 7


# --- Recap record breaks ----------------------------------------------------


def test_no_record_lines_before_the_history_is_big_enough():
    book = build_record_book(history(MIN_GAMES_FOR_RECAP_RECORDS - 1))
    new = row("Nox#EUW", days=400, kills=50)
    assert detect_record_breaks(book, [(new["match_id"], [new])]) == {}
    # The game still counts for later comparisons.
    assert book.record("kills").value == 50


def test_record_break_needs_a_strictly_higher_value_and_lists_each_category():
    book = build_record_book(history(kills=10, vision=30, deaths=3) + [row("Tam#EUW", days=310, vision=155, champion="Rakan")])
    tie = row("Nox#EUW", days=400, vision=155)
    big = row("Fresh#EUW", days=401, kills=11, vision=173, champion="Pyke")
    breaks = detect_record_breaks(book, [(tie["match_id"], [tie]), (big["match_id"], [big])])
    assert (tie["match_id"], "Nox#EUW") not in breaks
    found = breaks[(big["match_id"], "Fresh#EUW")]
    assert [b.category.key for b in found] == ["kills", "vision"]
    assert found[1].previous.riot_id == "Tam#EUW"


def test_best_player_in_a_game_takes_the_record_and_later_games_must_beat_it():
    book = build_record_book(history(kills=10))
    a = row("A#1", days=400, kills=14)
    b = dict(row("B#1", days=400, kills=12), match_id=a["match_id"], game_end_at=a["game_end_at"])
    later = row("C#1", days=401, kills=13)
    breaks = detect_record_breaks(book, [(a["match_id"], [b, a]), (later["match_id"], [later])])
    assert list(breaks) == [(a["match_id"], "A#1")]
    assert book.record("kills").value == 14


def test_personal_bests_need_a_history_and_a_strictly_better_value():
    rookie = history(MIN_GAMES_FOR_PERSONAL_BESTS - 1, riot_id="Rookie#EUW", kills=5, vision=20)
    book = build_record_book(history(kills=30, vision=200) + history(riot_id="Nox#EUW", kills=8, vision=40) + rookie)
    tie = row("Nox#EUW", days=400, kills=8, vision=40)
    better = row("Nox#EUW", days=401, kills=9, vision=40)
    rookie_game = row("Rookie#EUW", days=401, kills=6, vision=25)
    breaks = detect_record_breaks(
        book, [(game["match_id"], [game]) for game in (tie, better, rookie_game)]
    )
    assert list(breaks) == [(better["match_id"], "Nox#EUW")]
    found = breaks[(better["match_id"], "Nox#EUW")]
    assert [(b.category.key, b.value, b.previous.value, b.personal) for b in found] == [("kills", 9, 8, True)]


def test_personal_bests_skip_worst_categories_and_group_records_come_first():
    book = build_record_book(history(kills=10, vision=30, deaths=3) + history(riot_id="Nox#EUW", kills=5, vision=20, deaths=2))
    game = row("Nox#EUW", days=400, kills=11, vision=25, deaths=9, minutes=40)
    found = detect_record_breaks(book, [(game["match_id"], [game])])[(game["match_id"], "Nox#EUW")]
    # kills: group record (no personal line too); vision: personal best; deaths and
    # longest game are "worst" categories, so no personal line (deaths 9 is a group record).
    assert [(b.category.key, b.personal) for b in found] == [
        ("kills", False), ("deaths", False), ("longest_game", False), ("vision", True)
    ]


def test_personal_best_line():
    book = build_record_book([row("NoxVain#EUW", vision=144, champion="Rell", days=40)])
    line = format_record_break(
        RecordBreak(CATEGORIES_BY_KEY["vision"], "NoxVain#EUW", 155, book.bests["vision"]["NoxVain#EUW"], personal=True)
    )
    assert line == "-# 📈 Personal best: 155 vision score (was 144 on Rell, Feb 2025)"


def test_record_break_line():
    book = build_record_book([row("Tamarin#EUW", vision=155, champion="Rakan", days=305)])
    line = format_record_break(RecordBreak(CATEGORIES_BY_KEY["vision"], "Freshwhite#euw", 173, book.record("vision")))
    assert line == "🏆 **New group record:** 173 vision score (was 155, Tamarin on Rakan, Nov 2025)"
    longest = format_record_break(
        RecordBreak(CATEGORIES_BY_KEY["longest_game"], "F#1", 3886, book.record("longest_game"))
    )
    assert longest.startswith("🏆 **New group record:** longest game 64:46 (was 30:00, Tamarin on Rakan,")
    share = format_record_break(RecordBreak(CATEGORIES_BY_KEY["damage_share"], "F#1", 0.4896, book.record("damage_share")))
    assert "49.0% of team damage (was 25.0%" in share


# --- Cards ------------------------------------------------------------------


def test_group_card_sections():
    rows = [
        row("Freshwhite#euw", days=10, kills=34, champion="Caitlyn", pentas=2),
        row("JonastCvuHU#UNC", days=20, deaths=23, champion="Briar", win=False),
        row("Not a snake#EUW", days=30, pentas=1, deaths=0),
        row("Not a snake#EUW", days=31, deaths=0),
    ]
    # Freshwhite and JonastCvuHU in the same match: one game in the header.
    rows[1]["match_id"] = rows[0]["match_id"]
    sections = format_group_sections(build_record_book(rows))
    text = "\n\n".join(sections)
    assert sections[0] == "🏆 **Hall of Fame**\n-# 3 ranked games · 3 players · since Jan 2025"
    assert "**Carry**\n**34** kills · Freshwhite · Caitlyn · Jan 2025" in text
    assert "**23** deaths · JonastCvuHU · Briar · Jan 2025" in text
    assert "**3** pentakills" not in text
    assert "**2** pentakills · Freshwhite · group total 3" in text
    assert "**2** deathless games · Not a snake · group total 2" in text
    assert sections[-1].endswith("`!records Name` for personal bests")


def test_group_card_jungle_section():
    rows = [
        row("ÇÛΜÌŃΜÝÄŠŠ#EUW", days=1, camps=80, buffs=2, dragons=6, steals=3, champion="Vi"),
        row("ÇÛΜÌŃΜÝÄŠŠ#EUW", days=2, steals=2),
        row("JonastCvuHU#UNC", days=3, buffs=15, champion="Karthus", steals=1),
        row("Gizhmo#EUW", days=4, barons=1, champion="Kaisa"),
    ]
    sections = format_group_sections(build_record_book(rows))
    jungle = next(section for section in sections if section.startswith("**Jungle**"))
    assert jungle == (
        "**Jungle**\n"
        "**80** enemy jungle camps · ÇÛΜÌŃΜÝÄŠŠ · Vi · Jan 2025\n"
        "**15** buffs stolen · JonastCvuHU · Karthus · Jan 2025\n"
        "**6** dragons · ÇÛΜÌŃΜÝÄŠŠ · Vi · Jan 2025\n"
        "**1** baron · Gizhmo · Kaisa · Jan 2025\n"
        "**5** objective steals (career) · ÇÛΜÌŃΜÝÄŠŠ · group total 6"
    )
    # Jungle sits between Farm and map and Pain; it stays off personal cards.
    order = [section.split("\n", 1)[0] for section in sections]
    assert order.index("**Farm and map**") < order.index("**Jungle**") < order.index("**Pain**")
    personal = "\n\n".join(format_personal_sections(build_record_book(rows), "JonastCvuHU#UNC"))
    assert "buffs stolen" not in personal


def test_teammates_tied_in_one_game_resolve_by_name():
    nox = row("NoxVain#EUW", barons=4, champion="Amumu")
    giz = dict(row("gizhmo#EUW", barons=4, champion="Kaisa"), match_id=nox["match_id"], game_end_at=nox["game_end_at"])
    for rows in ([nox, giz], [giz, nox]):
        assert build_record_book(rows).record("barons").riot_id == "gizhmo#EUW"


def test_group_card_with_no_games():
    assert format_group_sections(build_record_book([])) == ["🏆 **Hall of Fame**", "No ranked games recorded yet."]


def test_personal_card_shows_place_in_group_and_hides_empty_bests():
    rows = [
        row("Tamarin#EUW", days=1, vision=160),
        row("NoxVain#EUW", days=2, vision=155, kills=23, champion="Rakan"),
        row("NoxVain#EUW", days=3, win=False, deaths=19, solo_kills=0),
    ]
    text = "\n\n".join(format_personal_sections(build_record_book(rows), "NoxVain#EUW"))
    assert "🏆 **NoxVain** · personal bests\n-# 2 ranked games · 1 wins · since Jan 2025" in text
    assert "**155** vision score · Rakan · Jan 2025 · #2 in the group" in text
    assert "**23** kills · Rakan · Jan 2025 · group record" in text
    assert "**Worst**\n**19** deaths" in text
    assert "pentakill" not in text
    assert "deathless" not in text


def test_personal_card_lists_kill_streak_cs_per_minute_longest_game_and_deathless_games():
    rows = [
        row("NoxVain#EUW", days=1, spree=11, deaths=0, cs=246, minutes=30, champion="Ahri"),
        row("NoxVain#EUW", days=2, minutes=52, win=False, champion="Rakan"),
        row("Tamarin#EUW", days=3, deaths=0),
        row("Tamarin#EUW", days=4, deaths=0),
    ]
    sections = format_personal_sections(build_record_book(rows), "NoxVain#EUW")
    best = next(section for section in sections if section.startswith("**Best**"))
    worst = next(section for section in sections if section.startswith("**Worst**"))
    assert "**11** kills in a row · Ahri · Jan 2025 · group record" in best
    assert best.endswith("\n**1** deathless game · #2 in the group")
    assert "**8.2** CS/min · Ahri · Jan 2025 · group record" in best
    assert "**52:00** longest game · Rakan · Jan 2025 · group record" in worst


def test_personal_card_for_player_without_games():
    sections = format_personal_sections(build_record_book([]), "New#EUW")
    assert sections == ["🏆 **New** · personal bests", "No ranked games recorded yet."]


def test_records_view_is_one_container():
    view = build_records_view(["a", "b"])
    container = view.children[0]
    assert getattr(container.accent_colour, "value", container.accent_colour) == RECORDS_ACCENT
    assert view_text(view) == "a\n\nb"


# --- Stats extraction -------------------------------------------------------


def test_pentakills_are_extracted():
    payload = {
        "metadata": {"matchId": "EUW1_1"},
        "info": {
            "queueId": 420,
            "gameDuration": 1800,
            "gameEndTimestamp": 1_760_000_000_000,
            "participants": [{"puuid": "p1", "teamId": 100, "pentaKills": 2, "win": True}],
        },
    }
    assert extract_player_match_rows(payload, {"p1"})[0]["penta_kills"] == 2


# --- Command ----------------------------------------------------------------


class FakeChannel:
    def __init__(self, channel_id=999, reject_views=False):
        self.id = channel_id
        self.reject_views = reject_views
        self.sent = []

    async def send(self, content=None, *, view=None, tts=False):
        if view is not None and self.reject_views:
            raise discord.HTTPException(type("Response", (), {"status": 400, "reason": "Bad"})(), "bad card")
        self.sent.append(view_text(view) if view is not None else content)


class FakeAuthor:
    bot = False
    id = 1
    guild_permissions = None
    roles = []


class FakeMessage:
    def __init__(self, content, channel):
        self.content = content
        self.channel = channel
        self.author = FakeAuthor()


def run_command(content, *, channel=None, db_enabled=True, rows=None, fail=False, logs=None):
    channel = channel or FakeChannel()

    def iter_rows(queue_ids, *, before=None, exclude_match_ids=()):
        if fail:
            raise RuntimeError("connection to server at 10.0.0.1 failed")
        return iter(rows if rows is not None else [row("NoxVain#EUW", kills=23), row("Tamarin#EUW", kills=30)])

    asyncio.run(handle_incoming_message(
        message=FakeMessage(content, channel),
        channel_id=channel.id,
        friends=[],
        riot_client=None,
        poro_service=None,
        report_timezone_name="UTC",
        report_day_start_hour=6,
        db_enabled=db_enabled,
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
        db_load_tracked_puuids=lambda: [("NoxVain#EUW", "nox"), ("Tamarin#EUW", "tam"), ("Pending#EUW", None)],
        db_iter_record_rows=iter_rows,
    ))
    return channel


def test_records_command_sends_group_card():
    logs = []
    channel = run_command("!records", logs=logs)
    assert channel.sent[0].startswith("🏆 **Hall of Fame**")
    assert "**30** kills · Tamarin" in channel.sent[0]
    assert logs[-1] == "[records] Sent group records: games=2 matches=2."


def test_records_command_sends_personal_bests():
    channel = run_command("!RECORDS noxvain")
    assert channel.sent[0].startswith("🏆 **NoxVain** · personal bests")
    assert "**23** kills · Lux" in channel.sent[0]
    assert "#2 in the group" in channel.sent[0]


def test_records_command_unknown_player():
    channel = run_command("!records Nobody")
    assert len(channel.sent) == 1 and "Nobody" in channel.sent[0]
    assert "Hall of Fame" not in channel.sent[0]


def test_records_command_needs_database():
    channel = run_command("!records", db_enabled=False)
    assert channel.sent == ["Records need the database, which isn't configured."]


def test_records_command_hides_error_details():
    logs = []
    channel = run_command("!records", fail=True, logs=logs)
    assert "10.0.0.1" not in channel.sent[0]
    assert "Records failed" in channel.sent[0]
    assert any("[records] Lookup failed" in line for line in logs)


def test_records_command_falls_back_to_text():
    logs = []
    channel = run_command("!records", channel=FakeChannel(reject_views=True), logs=logs)
    assert channel.sent[0].startswith("🏆 **Hall of Fame**")
    assert any("sending it as plain text" in line for line in logs)


def test_records_command_only_in_events_channel():
    channel = run_command("!records", channel=FakeChannel(channel_id=5))
    assert channel.sent == ["Use `!records` in <#999>."]


def test_a_value_of_one_reads_singular():
    book = build_record_book([row("Gizhmo#EUW", kills=1, solo_kills=1, assists=1, champion="Caitlyn")])
    text = "\n\n".join(format_personal_sections(book, "Gizhmo#EUW"))
    assert "**1** kill · Caitlyn" in text
    assert "**1** solo kill · Caitlyn" in text
    assert "**1** assist · Caitlyn" in text
    assert CATEGORIES_BY_KEY["kill_streak"].phrase(22) == "22 kills in a row"
