"""`!matchup`: argument parsing, champion names, the stats and matchups, the card, and the command."""

import asyncio
import contextvars
from datetime import datetime, timezone

import discord
import pytest

from src.discord_command_handlers import handle_incoming_message
from src.matchup import (
    MIN_MATCHUP_GAMES,
    USAGE,
    build_matchup_report,
    champion_display_name,
    format_matchup_sections,
    parse_matchup_args,
    resolve_champion,
)
from tests.discord_fakes import view_text

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
TRACKED = [("NoxVain#EUW", "nox"), ("Not a snake#EUW", "snake"), ("Pending#EUW", None)]
YORICK, FIZZ, DARIUS, GAREN, TEEMO, SION, WUKONG, KAISA = 83, 105, 122, 86, 17, 14, 62, 145
NAMES = {
    YORICK: "Yorick", FIZZ: "Fizz", DARIUS: "Darius", GAREN: "Garen", TEEMO: "Teemo", SION: "Sion",
    WUKONG: "MonkeyKing", KAISA: "Kaisa", 64: "LeeSin", 59: "JarvanIV", 777: "Strawberry_Jinx",
}


# --- Arguments ------------------------------------------------------------------------


def test_parse_takes_the_player_first_then_the_champion_and_options_in_any_order():
    assert parse_matchup_args("NoxVain Yorick", TRACKED) == ("NoxVain", "Yorick", None, 3, None)
    assert parse_matchup_args("NoxVain Yorick top flex", TRACKED) == ("NoxVain", "Yorick", "flex", 3, "TOP")
    assert parse_matchup_args("noxvain yorick flex 6 top", TRACKED) == ("noxvain", "yorick", "flex", 6, "TOP")
    # Names and champions with spaces.
    assert parse_matchup_args("Not a snake Lee Sin 12 jungle", TRACKED) == ("Not a snake", "Lee Sin", None, 12, "JUNGLE")
    assert parse_matchup_args("NoxVain#EUW Jarvan IV solo", TRACKED) == ("NoxVain#EUW", "Jarvan IV", "solo", 3, None)


def test_parse_without_a_tracked_name_takes_the_first_word_so_the_reply_can_say_who():
    assert parse_matchup_args("Nobody Yorick", TRACKED) == ("Nobody", "Yorick", None, 3, None)


@pytest.mark.parametrize("raw, message", [
    ("NoxVain", USAGE),
    ("NoxVain top flex", USAGE),
    ("NoxVain Yorick 13", "Choose a whole number of months from 1 to 12, like `!matchup Name Yorick 6`."),
    ("NoxVain Yorick 2.5", "Choose a whole number of months from 1 to 12, like `!matchup Name Yorick 6`."),
    ("NoxVain Yorick top mid", "Use one month count, one role and one queue, like `!matchup Name Yorick 6 top flex`."),
])
def test_parse_errors(raw, message):
    with pytest.raises(ValueError) as error:
        parse_matchup_args(raw, TRACKED)
    assert str(error.value) == message


# --- Champion names --------------------------------------------------------------------


def test_display_names_read_like_the_client():
    assert champion_display_name("MonkeyKing") == "Wukong"
    assert champion_display_name("Kaisa") == "Kai'Sa"
    assert champion_display_name("LeeSin") == "Lee Sin"
    assert champion_display_name("JarvanIV") == "Jarvan IV"
    assert champion_display_name("Yorick") == "Yorick"


@pytest.mark.parametrize("query, expected", [
    ("Yorick", YORICK), ("yorick", YORICK), ("YORICK", YORICK),
    ("Kai'Sa", KAISA), ("kaisa", KAISA), ("kai sa", KAISA),
    ("Wukong", WUKONG), ("monkeyking", WUKONG),
    ("lee sin", 64), ("leesin", 64), ("jarvan iv", 59),
    ("yor", YORICK),  # a prefix only one champion has
])
def test_resolve_champion_accepts_any_case_spacing_and_apostrophes(query, expected):
    assert resolve_champion(query, NAMES) == (expected, None)


def test_resolve_champion_suggests_a_close_name_and_ignores_mode_variants():
    assert resolve_champion("Yorik", NAMES) == (None, "`Yorik` isn't a champion. Did you mean Yorick?")
    assert resolve_champion("Strawberry_Jinx", NAMES) == (None, "`Strawberry_Jinx` isn't a champion.")
    # "s" starts both Sion and Strawberry_Jinx, but only Sion is a ranked pick.
    assert resolve_champion("si", NAMES) == (SION, None)
    assert resolve_champion("zzz", NAMES) == (None, "`zzz` isn't a champion.")


# --- The report ------------------------------------------------------------------------


def game(*, champion=YORICK, opponent=DARIUS, win=True, queue=440, role="TOP", kills=5, deaths=4, assists=6,
         cs=180, minutes=30, damage_share=0.25, kp=0.5, gold=500):
    return {
        "queue_id": queue, "champion_id": champion, "team_position": role, "win": win, "kills": kills,
        "deaths": deaths, "assists": assists, "cs": cs, "duration_seconds": minutes * 60,
        "damage_share": damage_share, "kill_participation": kp, "opponent_champion_id": opponent,
        "opponent_gold_diff": gold,
    }


def games(count, wins, **kwargs):
    return [game(win=index < wins, **kwargs) for index in range(count)]


def report_for(rows, *, queue=None, role=None, months=3):
    return build_matchup_report(
        riot_id="NoxVain#EUW", champion_id=YORICK, rows=rows, champion_names=NAMES, queue=queue, months=months, role=role,
    )


def sample_rows():
    return (
        games(6, 5, opponent=GAREN, gold=2000)       # 83%
        + games(5, 4, opponent=TEEMO, gold=1000)     # 80%
        + games(10, 3, opponent=DARIUS, gold=-1500)  # 30%
        + games(5, 1, opponent=SION, gold=-500)      # 20%
        + games(4, 0, opponent=FIZZ)                 # under 5 games: never best or worst
        + games(3, 3, champion=FIZZ, opponent=GAREN)  # another champion
        + games(2, 2, queue=420, opponent=GAREN)     # Solo/Duo
        + games(2, 0, queue=450, opponent=GAREN)     # ARAM is never counted
    )


def test_report_counts_one_champion_in_both_ranked_queues_by_default():
    report = report_for(sample_rows())
    # 30 Flex + 2 Solo/Duo games on Yorick; Fizz and ARAM games are left out.
    assert report.games == 32 and report.wins == 15
    assert report.champion == "Yorick"
    assert (report.kills, report.deaths, report.assists) == (5.0, 4.0, 6.0)
    assert report.cs_per_min == pytest.approx(6.0)
    assert report.damage_share == pytest.approx(0.25) and report.kill_participation == pytest.approx(0.5)


def test_best_and_worst_matchups_need_five_games_and_sort_by_win_rate():
    report = report_for(sample_rows())
    # Garen 7 games (5 Flex wins of 6, plus 2 Solo/Duo wins) = 7W-1L.
    assert [(m.opponent, m.wins, m.games) for m in report.best] == [("Garen", 7, 8), ("Teemo", 4, 5), ("Darius", 3, 10)]
    assert [(m.opponent, m.wins, m.games) for m in report.worst] == [("Sion", 1, 5)]
    assert (report.qualified_opponents, report.opponents) == (4, 5)


def test_queue_and_role_narrow_the_games():
    assert report_for(sample_rows(), queue="solo").games == 2
    assert report_for(sample_rows(), queue="flex").games == 30
    rows = sample_rows() + games(5, 5, role="MIDDLE", opponent=TEEMO)
    assert report_for(rows, role="MIDDLE").games == 5
    assert report_for(rows).games == 37


def test_worst_never_repeats_a_best_matchup():
    report = report_for(games(5, 5, opponent=GAREN) + games(5, 0, opponent=DARIUS))
    assert [m.opponent for m in report.best] == ["Garen", "Darius"]
    assert report.worst == []


def test_card_sections():
    report = report_for(sample_rows(), role="TOP")
    sections = format_matchup_sections(report)
    assert sections[0] == "🎯 **NoxVain · Yorick** · Top\n-# Solo/Duo and Flex · last 3 months"
    assert sections[1].splitlines() == [
        "**32** games · **15W-17L** · **47%** win rate",
        "**5.0 / 4.0 / 6.0** KDA (2.75) · **6.0** CS/min",
        "**25%** damage share · **50%** kill participation · **+0.1k** gold vs lane opponent",
    ]
    assert sections[2].splitlines()[:2] == ["**Best matchups**", "🟢 Garen · **7W-1L** (88%) · +1.6k gold"]
    assert "🔴 Sion · **1W-4L** (20%) · −0.5k gold" in sections[2]
    assert sections[3] == (
        "-# Lane opponent = enemy in the same role · matchups need 5+ games (4 of 5 opponents) "
        "· gold is the difference at game end"
    )


def test_card_without_any_five_game_matchup_says_so():
    sections = format_matchup_sections(report_for(games(6, 3, opponent=None)))
    assert sections[2] == f"Not enough games against one opponent yet ({MIN_MATCHUP_GAMES} needed)."


# --- The command -----------------------------------------------------------------------


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


def run_command(content, *, rows=None, fail=False, channel=None, with_loader=True):
    import src.commands.matchup_handlers as matchup_handlers

    channel = channel or FakeChannel()
    calls, logs = [], []
    real_now, matchup_handlers._now = matchup_handlers._now, lambda: NOW

    def load_rows(puuid, since, *, until):
        calls.append((puuid, since, until))
        if fail:
            raise RuntimeError("connection to server at 10.0.0.1 failed")
        return sample_rows() if rows is None else rows

    try:
        asyncio.run(handle_incoming_message(
            message=FakeMessage(content, channel),
            channel_id=777,
            friends=[],
            riot_client=None,
            poro_service=None,
            report_timezone_name="UTC",
            report_day_start_hour=6,
            start_monotonic=0.0,
            daily_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "cmd-1",
            get_or_create_report_message=None,
            remember_report_message=None,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=None,
            log=logs.append,
            events_channel_id=999,
            db_load_tracked_puuids=lambda: TRACKED,
            db_load_matchup_rows=load_rows if with_loader else None,
            db_load_champion_names=lambda: NAMES,
        ))
    finally:
        matchup_handlers._now = real_now
    return channel, calls, logs


def test_command_sends_the_card_for_the_last_three_months():
    channel, calls, logs = run_command("!matchup NoxVain yorick top")
    assert len(channel.sent) == 1 and channel.sent[0].startswith("🎯 **NoxVain · Yorick** · Top")
    (puuid, since, until), = calls
    assert puuid == "nox" and until == NOW and since == datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    assert logs[-1] == "[matchup] Sent Yorick for NoxVain#EUW: games=32 queue=both months=3 role=top matchups=4/5."


def test_command_months_option_reaches_back_further():
    _channel, calls, _logs = run_command("!matchup NoxVain Yorick 12 flex")
    assert calls[0][1] == datetime(2025, 11, 22, 12, 0, tzinfo=timezone.utc)


def test_command_replies_with_usage_unknown_player_and_unknown_champion():
    assert run_command("!matchup")[0].sent == [USAGE]
    assert run_command("!matchup Nobody Yorick")[0].sent == ["`Nobody` isn't a tracked player."]
    assert run_command("!matchup NoxVain Yorik")[0].sent == ["`Yorik` isn't a champion. Did you mean Yorick?"]
    assert run_command("!matchup NoxVain Yorick 0")[0].sent == [
        "Choose a whole number of months from 1 to 12, like `!matchup Name Yorick 6`."
    ]


def test_command_with_too_few_games_sends_a_short_reply_instead_of_a_card():
    channel, _calls, logs = run_command("!matchup NoxVain Yorick", rows=games(3, 2))
    assert channel.sent == [
        "NoxVain has 3 Solo/Duo and Flex games as Yorick in the last 3 months (2W-1L); the card needs 5. "
        "Try more months, like `!matchup Name Champion 12`."
    ]
    assert logs[-1] == "[matchup] Too few games for NoxVain#EUW on Yorick: games=3 queue=both months=3 role=all."
    assert run_command("!matchup NoxVain Yorick mid", rows=[])[0].sent == [
        "NoxVain has no Solo/Duo and Flex games as Yorick in mid in the last 3 months."
    ]


def test_command_hides_database_errors_and_falls_back_to_text_for_a_rejected_card():
    channel, _calls, logs = run_command("!matchup NoxVain Yorick", fail=True)
    assert len(channel.sent) == 1 and "10.0.0.1" not in channel.sent[0]
    assert logs[-1].startswith("[matchup] Lookup failed for 'NoxVain Yorick': RuntimeError")

    channel, _calls, logs = run_command("!matchup NoxVain Yorick", channel=FakeChannel(reject_views=True))
    assert channel.sent[0].startswith("🎯 **NoxVain · Yorick**")
    assert any("sending it as plain text" in line for line in logs)


def test_command_without_the_loader_says_the_database_is_needed():
    channel, _calls, _logs = run_command("!matchup NoxVain Yorick", with_loader=False)
    assert channel.sent == ["Matchups need the database, which isn't configured."]
