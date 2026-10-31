"""!analyze: guarded rule-based findings, the compact card, and the command handler."""

import asyncio
import contextvars
from datetime import datetime, timedelta, timezone

import discord
import pytest

from src.discord_command_handlers import handle_incoming_message
from src.player_analysis import (
    ANALYSIS_ACCENT,
    analysis_start,
    analyze_player,
    build_analysis_view,
    format_analysis_sections,
    format_analysis_text,
    parse_analyze_args,
    resolve_tracked_player,
)
from tests.discord_fakes import view_text

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
UTC = timezone.utc


def game(days_ago, win, *, champion="Lux", role="UTILITY", queue=420, kills=2, deaths=3, assists=8,
         minutes=30, gold_diff=0, cs_diff=0, teammates=(), hour=12):
    end = (NOW - timedelta(days=days_ago)).replace(hour=hour)
    return {
        "queue_id": queue,
        "game_end_at": end,
        "duration_seconds": minutes * 60,
        "champion_name": champion,
        "team_position": role,
        "win": win,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "opponent_gold_diff": gold_diff,
        "opponent_cs_diff": cs_diff,
        "tracked_teammates": list(teammates),
    }


def games_with(wins, losses, **kwargs):
    """`wins + losses` games spread one per day (oldest first), alternating results evenly."""
    total = wins + losses
    results = [i * wins // total != (i + 1) * wins // total for i in range(total)]
    start = kwargs.pop("start_days_ago", 300)
    return [game(start - i, result, **kwargs) for i, result in enumerate(results)]


def analyze(games, *, queue="solo", baselines=None, names=None, months=12):
    # The rule tests spread games over a year; the 3-month default has its own tests.
    return analyze_player(
        riot_id="NoxVain#EUW",
        games=games,
        queue=queue,
        role_baselines=baselines or {},
        names_by_puuid=names or {},
        now=NOW,
        tz=UTC,
        day_start_hour=6,
        months=months,
    )


def test_parse_analyze_args_takes_optional_months_and_queue():
    assert parse_analyze_args("Not a snake flex") == ("Not a snake", "flex", 3)
    assert parse_analyze_args("NoxVain#EUW SOLO") == ("NoxVain#EUW", "solo", 3)
    assert parse_analyze_args("NoxVain") == ("NoxVain", "solo", 3)
    assert parse_analyze_args("NoxVain 6 flex") == ("NoxVain", "flex", 6)
    assert parse_analyze_args("NoxVain flex 12") == ("NoxVain", "flex", 12)


def test_parse_analyze_args_keeps_tracked_names_that_end_in_a_number():
    tracked = [("Player 2#EUW", "p2")]
    assert parse_analyze_args("Player 2", tracked) == ("Player 2", "solo", 3)
    assert parse_analyze_args("player 2 6", tracked) == ("player 2", "solo", 6)
    assert parse_analyze_args("Player 2#EUW flex", tracked) == ("Player 2#EUW", "flex", 3)


@pytest.mark.parametrize("raw", ["NoxVain 0", "NoxVain 13", "NoxVain 2.5", "NoxVain -1", "NoxVain 3 6"])
def test_parse_analyze_args_rejects_bad_month_counts(raw):
    with pytest.raises(ValueError):
        parse_analyze_args(raw)


def test_analysis_start_counts_calendar_months_and_clamps_short_months():
    may_31 = datetime(2026, 5, 31, 12, tzinfo=UTC)
    assert analysis_start(may_31, 3) == datetime(2026, 2, 28, 12, tzinfo=UTC)
    assert analysis_start(datetime(2026, 1, 15, tzinfo=UTC), 1) == datetime(2025, 12, 15, tzinfo=UTC)
    assert analysis_start(NOW, 12) == datetime(2025, 10, 27, 12, tzinfo=UTC)
    with pytest.raises(ValueError):
        analysis_start(NOW, 13)


def test_default_window_is_three_months_and_trends_split_it_in_half():
    # 25 games 80-56 days ago (mostly won), 25 in the last 40 days (mostly lost), one game outside the window.
    older = [game(80 - i, i % 5 != 0) for i in range(25)]
    recent = [game(40 - i, i % 3 == 0) for i in range(25)]
    analysis = analyze_player(
        riot_id="NoxVain#EUW", games=older + recent + [game(200, True)], queue="solo", role_baselines={},
        names_by_puuid={}, now=NOW, tz=UTC, day_start_hour=6,
    )
    assert analysis.months == 3 and analysis.games == 50
    assert any(tip.startswith("Last 46 days 36% (25), 80% before") for tip in analysis.tips)
    header = format_analysis_sections(analysis)[0]
    assert header.split("\n")[1] == "-# Last 3 months · Solo/Duo 29W-21L (58%)"
    assert header.split("\n")[2].startswith("-# Last 20 games: 7W-13L (35%)")


TRACKED = [("NoxVain#EUW", "nox"), ("Rayke#EUW", "rayke"), ("Rayke#NA1", "rayke-na")]


@pytest.mark.parametrize("query, expected", [
    ("noxvain#euw", ("NoxVain#EUW", "nox")),
    ("NOXVAIN", ("NoxVain#EUW", "nox")),
    ("Rayke#NA1", ("Rayke#NA1", "rayke-na")),
])
def test_resolve_tracked_player_by_riot_id_or_unique_name(query, expected):
    assert resolve_tracked_player(query, TRACKED) == (expected, None)


def test_resolve_tracked_player_errors():
    assert resolve_tracked_player("Rayke", TRACKED)[1] == "Several tracked players are called `Rayke`; add the `#Tag`."
    assert resolve_tracked_player("Some`one", TRACKED)[1] == "`Some'one` isn't a tracked player."
    assert resolve_tracked_player("  ", TRACKED)[0] is None


def test_too_few_games_gives_no_findings_and_points_to_other_queue():
    games = games_with(5, 5) + games_with(12, 13, queue=440)

    analysis = analyze(games)
    sections = format_analysis_sections(analysis)

    assert not analysis.enough_games and analysis.strengths == [] and analysis.tips == []
    assert len(sections) == 3
    assert "Not enough Solo/Duo games to analyze yet: 10 in the last 12 months, 20 needed." in sections[-1]
    assert "`!analyze NoxVain 12 flex`" in sections[-1]
    # Already the longest window, so no hint to look further back.
    assert "further back" not in sections[-1]
    short = format_analysis_sections(analyze(games, months=3))[1]
    assert short.endswith("Or look further back: `!analyze NoxVain 12`.")


def test_best_and_weakest_champions_need_ten_games_and_a_clear_gap():
    games = (
        games_with(14, 6, champion="Zyra", start_days_ago=300)
        + games_with(3, 9, champion="Lux", start_days_ago=270)
        + games_with(10, 10, champion="Seraphine", start_days_ago=250)
        + games_with(5, 0, champion="Rell", start_days_ago=220)  # too few games to judge
    )

    analysis = analyze(games)

    assert "**Zyra** 14/20 (70%): your best pick" in analysis.strengths
    assert "**Lux** 3/12 (25%): your weakest pick" in analysis.tips
    assert not any("Seraphine" in line or "Rell" in line for line in analysis.strengths + analysis.tips)


def test_off_roles_tip_names_records_and_lane_gold():
    games = (
        games_with(30, 20, role="UTILITY", champion="A", start_days_ago=300)
        + games_with(4, 8, role="JUNGLE", champion="B", gold_diff=-3000, start_days_ago=240)
        + games_with(5, 7, role="TOP", champion="C", gold_diff=-2000, start_days_ago=220)
    )

    analysis = analyze(games)

    assert "Stay on support: 60% in 50 games; Jungle 4/12, Top 5/12, ~2.5k gold behind your lane opponent at game end" in analysis.tips


def test_trend_down_mentions_rising_kda_and_trend_up_is_a_strength():
    older = games_with(30, 20, champion="A", start_days_ago=300, deaths=4)
    recent_down = games_with(8, 17, champion="B", start_days_ago=80, deaths=2)
    recent_up = games_with(20, 5, champion="B", start_days_ago=80)

    down = analyze(older + recent_down)
    up = analyze(older + recent_up)

    assert "Last 90 days 32% (25), 60% before, even though KDA rose 2.50 → 5.00" in down.tips
    assert "Trending up: 80% in the last 90 days (25), 60% before" in up.strengths


def test_tilt_tip_and_no_tilt_strength():
    tilted = []
    steady = []
    pairs = [(True, True), (True, False), (False, True), (False, False)]
    for day in range(40):
        # Loss then a quick follow-up game that is mostly lost; win then mostly won.
        tilted += [game(300 - day, False, hour=12), game(300 - day, day % 4 == 0, hour=13)]
        tilted += [game(300 - day, True, hour=18), game(300 - day, True, hour=19)]
        # The second game's result doesn't depend on the first one's.
        first, second = pairs[day % 4]
        steady += [game(300 - day, first, hour=12), game(300 - day, second, hour=13)]

    assert any(line.startswith("Take a break after a loss: 25% in the next game") for line in analyze(tilted).tips)
    assert any(line.startswith("No tilt:") for line in analyze(steady).strengths)


def test_long_sessions_tip():
    games = []
    for day in range(25):
        games += [game(300 - day, True, hour=10), game(300 - day, True, hour=12), game(300 - day, day % 2 == 0, hour=14)]
        games += [game(300 - day, False, hour=16), game(300 - day, day % 5 == 0, hour=18)]

    tips = analyze(games).tips

    assert "Stop after 3 games a day: 10% from game 4 on, 84% before" in tips


def test_only_the_best_duo_partner_is_named():
    games = (
        games_with(20, 5, teammates=["snake"], champion="A", start_days_ago=300)
        + games_with(5, 20, teammates=["gizhmo"], champion="A", start_days_ago=260)
        + games_with(10, 10, champion="A", start_days_ago=220)
    )
    names = {"snake": "Not a snake#EUW", "gizhmo": "gizhmo#EUW"}

    analysis = analyze(games, names=names)
    text = format_analysis_text(analysis)

    assert "Duo with **Not a snake**: 20/25 (80%)" in analysis.strengths
    assert "gizhmo" not in text


def test_role_kda_and_deaths_compare_against_group_baseline():
    baselines = {"UTILITY": {"games": 400, "kills": 800, "deaths": 1200, "assists": 2800, "minutes": 12000}}
    strong = games_with(15, 15, kills=3, deaths=2, assists=12)
    feeding = games_with(15, 15, kills=1, deaths=6, assists=8)
    small_group = {"UTILITY": {**baselines["UTILITY"], "games": 30}}

    assert "Support KDA 7.50, against 3.00 for the group's supports" in analyze(strong, baselines=baselines).strengths
    assert "You die more than the group's supports: 2.0 vs 1.0 deaths per 10 min" in analyze(feeding, baselines=baselines).tips
    assert analyze(strong, baselines=small_group).strengths == []


def test_laning_slip_only_for_laners():
    adc = games_with(25, 25, role="BOTTOM", cs_diff=8, start_days_ago=300) + games_with(12, 13, role="BOTTOM", cs_diff=-6, start_days_ago=80)
    support = [dict(g, team_position="UTILITY") for g in adc]

    assert "CS gap slipped: -6 CS vs your lane opponent at game end in the last 90 days (+8 before)" in analyze(adc).tips
    assert not any("Laning" in line for line in analyze(support).tips)


def test_at_most_three_findings_strongest_first():
    games = []
    for index, (wins, losses) in enumerate([(10, 0), (9, 1), (8, 2), (8, 2)]):
        games += games_with(wins, losses, champion=f"Good{index}", start_days_ago=300 - 20 * index)
    games += games_with(10, 30, champion="Filler", start_days_ago=200)

    analysis = analyze(games)

    assert len(analysis.strengths) <= 3 and len(analysis.tips) <= 3
    assert analysis.strengths[0].startswith("**Good0** 10/10 (100%) and **Good1** 9/10 (90%)")


def test_sections_header_footer_and_escaping():
    games = games_with(12, 10) + games_with(30, 25, queue=440)

    analysis = analyze_player(
        riot_id="xX_snake_Xx#EUW", games=games, queue="solo", role_baselines={}, names_by_puuid={},
        now=NOW, tz=UTC, day_start_hour=6, months=12,
    )
    header, profile, strengths, tips, footer = format_analysis_sections(analysis)

    assert header == (
        "**xX\\_snake\\_Xx#EUW** · Solo/Duo analysis\n"
        "-# Last 12 months · Solo/Duo 12W-10L (55%) · Flex 30W-25L (55%)\n"
        # These test rows carry no CS or kill participation, so both read N/A.
        "-# Last 20 games: 11W-9L (55%) · N/A CS/min · KDA 3.33 · KP N/A"
    )
    assert strengths == "**Strengths**\nNothing stands out yet."
    assert tips == "**Work on**\nNo clear weak spots in these games."
    assert footer.startswith("-# Solo/Duo games since 31 Dec 2025 · tips need 10+ games · ")
    assert footer.splitlines()[0].endswith("`!analyze xX\\_snake\\_Xx 12 flex` for Flex · add 1-12 for months")


def test_header_without_other_queue_games():
    analysis = analyze(games_with(12, 10), queue="flex")

    assert format_analysis_sections(analysis)[0].endswith("-# Last 12 months · no Flex games · Solo/Duo 12W-10L (55%)")


def test_build_analysis_view_is_one_accented_container():
    analysis = analyze(games_with(14, 6, champion="Zyra") + games_with(3, 9, champion="Lux", start_days_ago=200))

    view = build_analysis_view(analysis)

    assert len(view.children) == 1
    container = view.children[0]
    assert getattr(container.accent_colour, "value", container.accent_colour) == ANALYSIS_ACCENT
    assert view_text(view) == format_analysis_text(analysis)
    assert sum(1 for item in container.walk_children() if isinstance(item, discord.ui.Separator)) == 4


def test_playstyle_and_findings_use_only_selected_queue_and_period_with_topic_variety():
    from tests.test_analysis_metrics import match

    # Use the local fixed clock, not the helper module's different date.
    games = [dict(match(i), game_end_at=NOW - timedelta(hours=40 - i),
                  cs=180 if i < 20 else 240, lane_cs_10=60 if i < 20 else 75,
                  vision_score=30 if i < 20 else 9, deaths=3 if i < 20 else 6,
                  kill_participation=0.4 if i < 20 else 0.7,
                  tracked_teammates=[], opponent_gold_diff=0, opponent_cs_diff=0) for i in range(40)]
    games += [dict(games[0], queue_id=440, cs=900),
              dict(games[0], game_end_at=NOW - timedelta(days=400), cs=900),
              dict(games[0], game_end_at=NOW + timedelta(days=1), cs=900)]
    analysis = analyze(games, months=3)
    assert analysis.games == 40 and "40/40 games" in analysis.profile
    assert analysis.recent.startswith("Last 20 games: 10W-10L (50%) · 8.0 CS/min")
    assert len(analysis.strengths) == 2  # one farming insight, not both CS metrics
    assert any("kill participation" in text for text in analysis.strengths)
    assert len(analysis.tips) == 2 and all("Review" in text for text in analysis.tips)
    assert len(format_analysis_text(analysis)) < 4000
    sections = format_analysis_sections(analysis)
    # Last 20 games is a line in the header; the profile is the only new section.
    assert "\n-# Last 20 games: " in sections[0]
    assert sections[1].startswith("**How you play")
    assert sections[2].startswith("**Strengths**")
    assert "Patterns describe" not in sections[-1]


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


def default_rows():
    return games_with(14, 6, champion="Zyra") + games_with(3, 9, champion="Lux", start_days_ago=200)


def run_command(content, *, channel=None, db_enabled=True, rows=None, calls=None, fail=False, logs=None):
    import src.commands.analysis_handlers as analysis_handlers

    channel = channel or FakeChannel()
    calls = calls if calls is not None else []
    real_now, analysis_handlers._now = analysis_handlers._now, lambda: NOW

    def load_rows(puuid, since, *, until=None):
        calls.append(("rows", puuid, since, until))
        if fail:
            raise RuntimeError("connection to server at 10.0.0.1 failed")
        return rows if rows is not None else default_rows()

    def load_totals(queue_id, since, *, until=None):
        calls.append(("totals", queue_id, since, until))
        return {}

    asyncio.run(handle_incoming_message(
        message=FakeMessage(content, channel),
        channel_id=777,
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
        db_load_tracked_puuids=lambda: [("NoxVain#EUW", "nox"), ("Pending#EUW", None)],
        db_load_player_analysis_rows=load_rows,
        db_load_role_group_totals=load_totals,
    ))
    analysis_handlers._now = real_now
    return channel, calls


def test_analyze_command_sends_card_for_tracked_player():
    logs = []
    channel, calls = run_command("!analyze noxvain flex", logs=logs)

    # Both reads cover the same window: 3 months back, to the same captured end time.
    three_months = datetime(2026, 7, 27, 12, tzinfo=UTC)
    assert calls == [("rows", "nox", three_months, NOW), ("totals", 440, three_months, NOW)]
    assert len(channel.sent) == 1 and channel.sent[0].startswith("**NoxVain#EUW** · Flex analysis")
    assert logs[-1] == "[analyze] Sent flex analysis for NoxVain#EUW: months=3 games=0 strengths=0 tips=0 profile=False."


def test_analyze_command_takes_a_month_count_and_rejects_bad_ones():
    _channel, calls = run_command("!analyze NoxVain 12")
    assert calls[0][2] == datetime(2025, 10, 27, 12, tzinfo=UTC)
    channel, calls = run_command("!analyze NoxVain 13")
    assert channel.sent == ["Choose a whole number of months from 1 to 12, like `!analyze Name 6`."] and calls == []


def test_analyse_alias_and_default_queue():
    channel, calls = run_command("!analyse NoxVain#EUW 12")

    assert any(call[:2] == ("totals", 420) for call in calls)
    assert "Solo/Duo analysis" in channel.sent[0] and "**Zyra** 14/20 (70%): your best pick" in channel.sent[0]
    assert "**Lux** 3/12 (25%): your weakest pick" in channel.sent[0]


def test_analyze_falls_back_to_plain_text_when_card_is_rejected():
    logs = []
    channel, _calls = run_command("!analyze NoxVain 12", channel=FakeChannel(reject_views=True), logs=logs)

    assert channel.sent == [format_analysis_text(analyze(default_rows()))]
    assert "[analyze] Discord rejected the analysis card (HTTP 400); sending it as plain text." in logs


@pytest.mark.parametrize("content, expected", [
    ("!analyze", "Usage: `!analyze Name#Tag [months] [solo|flex]` (last 3 months of Solo/Duo unless you say otherwise; 1-12 months)."),
    ("!analyze Pending", "`Pending` isn't a tracked player."),
    ("!analyze Nobody#EUW solo", "`Nobody#EUW` isn't a tracked player."),
])
def test_analyze_usage_and_unknown_players(content, expected):
    channel, calls = run_command(content)

    assert channel.sent == [expected] and calls == []


def test_analyze_without_database_and_on_db_errors():
    no_db, _ = run_command("!analyze NoxVain", db_enabled=False)
    failed, _ = run_command("!analyze NoxVain", fail=True)

    assert no_db.sent == ["Player analysis needs the database, which isn't configured."]
    assert failed.sent[0].startswith("Analysis failed: ")
    assert "10.0.0.1" not in failed.sent[0] and "(ref `cmd-1`)" in failed.sent[0]


def test_analyze_in_wrong_channel_points_to_events_channel():
    channel, calls = run_command("!analyze NoxVain", channel=FakeChannel(channel_id=5))

    assert channel.sent == ["Use `!analyze` in <#999>."] and calls == []


def test_analyze_routing_and_help():
    from src.commands.routing import command_channel_id, format_help_text, is_supported_command

    for content in ("!analyze", "!analyze noxvain flex", "!analyse", "!analyse noxvain"):
        assert is_supported_command(content)
        assert command_channel_id(content, daily_channel_id=1, weekly_channel_id=2, events_channel_id=3) == 3
    help_text = format_help_text(report_day_start_hour=6, daily_channel_id=1, weekly_channel_id=2, events_channel_id=3)
    assert "`!analyze Name#Tag [months] [solo|flex]`" in help_text


def test_db_analysis_queries_map_rows(monkeypatch):
    from src.db import match_stats as db_match_stats

    queries = []

    def fake_execute(query, params=None, fetch=False, fetchone=False):
        queries.append((query, params))
        if query.startswith("SELECT queue_id"):
            return [tuple(range(len(db_match_stats.ANALYSIS_ROW_COLUMNS)))]
        return [("UTILITY", 10, 20, 30, 80, 300.0)]

    monkeypatch.setattr(db_match_stats, "db_execute", fake_execute)

    rows = db_match_stats.db_load_player_analysis_rows("nox", NOW)
    totals = db_match_stats.db_load_role_group_totals(440, NOW)
    db_match_stats.db_load_player_analysis_rows("nox", NOW, until=NOW + timedelta(days=1))
    db_match_stats.db_load_role_group_totals(440, NOW, until=NOW + timedelta(days=1))

    assert rows == [dict(zip(db_match_stats.ANALYSIS_ROW_COLUMNS, range(len(db_match_stats.ANALYSIS_ROW_COLUMNS))))]
    assert queries[0][1] == ("nox", [420, 440], NOW) and "NOT remake" in queries[0][0]
    assert totals == {"UTILITY": {"games": 10, "kills": 20, "deaths": 30, "assists": 80, "minutes": 300.0}}
    assert queries[1][1] == (440, NOW) and "GROUP BY team_position" in queries[1][0]
    assert queries[2][1] == ("nox", [420, 440], NOW, NOW + timedelta(days=1)) and "game_end_at <= %s" in queries[2][0]
    assert queries[3][1] == (440, NOW, NOW + timedelta(days=1)) and "game_end_at <= %s" in queries[3][0]
