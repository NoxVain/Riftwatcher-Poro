"""`!analyze`: one player's recent ranked games (3 months unless asked) turned into strengths and tips.

Pure logic over `player_match_stats` rows (see src/match_stats.py); no Discord, Riot or
DB access. Every finding is guarded: it needs a minimum number of games on each side
of a comparison and a clear gap, so small or noisy samples never become advice.
Candidates are ranked by gap size weighted by sample size, and the card shows at most
three strengths and three things to work on, each with its record.

Only the best duo partner is ever named; a weak duo is never called out in public.
"""

import calendar
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

import discord

QUEUES = {
    "solo": {"queue_id": 420, "label": "Solo/Duo"},
    "flex": {"queue_id": 440, "label": "Flex"},
}
DEFAULT_QUEUE = "solo"
DEFAULT_MONTHS = 3
MAX_ANALYSIS_MONTHS = 12
# Trends compare the most recent part of the window with the rest: 90 days, or half of a shorter window.
TREND_DAYS = 90

MIN_QUEUE_GAMES = 20
MIN_GROUP_GAMES = 10  # champions, off-roles
MIN_PARTNER_GAMES = 15
MIN_SPLIT_GAMES = 20  # each side of trend / tilt / session comparisons
MIN_BASELINE_GAMES = 50  # group games in a role before comparing against it

CHAMPION_GAP_PP = 8
ROLE_GAP_PP = 10
PARTNER_GAP_PP = 6
TREND_GAP_PP = 6
TILT_GAP_PP = 10
NO_TILT_MAX_GAP_PP = 4
SESSION_GAP_PP = 8
GAMES_BEFORE_LONG_SESSION = 3
NEXT_GAME_WINDOW = timedelta(minutes=70)
LANE_CS_DROP = 5
KDA_RATIO = 1.2
DEATHS_RATIO = 1.15
MAX_FINDINGS = 3

ANALYSIS_ACCENT = 0x378ADD

ROLE_NAMES = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
ROLE_IN_SENTENCE = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "ADC", "UTILITY": "support"}
ROLE_PLURALS = {"TOP": "top laners", "JUNGLE": "junglers", "MIDDLE": "mid laners", "BOTTOM": "ADCs", "UTILITY": "supports"}


@dataclass
class Finding:
    text: str
    score: float


@dataclass
class PlayerAnalysis:
    riot_id: str
    queue: str
    games: int
    wins: int
    other_queue_games: int
    other_queue_wins: int
    since: object = None
    months: int = DEFAULT_MONTHS
    strengths: list = field(default_factory=list)
    tips: list = field(default_factory=list)

    @property
    def enough_games(self):
        return self.games >= MIN_QUEUE_GAMES


MONTHS_ERROR = "Choose a whole number of months from 1 to 12, like `!analyze Name 6`."


def analysis_start(now, months=DEFAULT_MONTHS):
    """`months` calendar months before `now` (31 May minus 3 months is 28 Feb, not 3 Mar)."""
    if not 1 <= months <= MAX_ANALYSIS_MONTHS:
        raise ValueError(MONTHS_ERROR)
    year, month = divmod(now.year * 12 + now.month - 1 - months, 12)
    month += 1
    return now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))


def _parse_options(words):
    """Pop a trailing queue word and month count, in either order."""
    queue, months, seen = DEFAULT_QUEUE, DEFAULT_MONTHS, set()
    while words:
        token = words[-1].casefold()
        if token in QUEUES:
            kind = "queue"
        elif token.lstrip("+-").replace(".", "", 1).isdigit():
            kind = "months"
        else:
            break
        if kind in seen:
            raise ValueError("Use one month count and one queue, like `!analyze Name 6 flex`.")
        seen.add(kind)
        words.pop()
        if kind == "queue":
            queue = token
        elif not token.isdecimal() or not 1 <= int(token) <= MAX_ANALYSIS_MONTHS:
            raise ValueError(MONTHS_ERROR)
        else:
            months = int(token)
    return " ".join(words), queue, months


def parse_analyze_args(raw, tracked=()):
    """`"Not a snake 6 flex"` -> ("Not a snake", "flex", 6); months and queue are optional.

    A tracked player's name wins over options, so "Player 2" stays a name; raises
    ValueError with a user-facing message for a bad month count.
    """
    words = raw.split()
    for options in range(min(2, len(words) - 1) + 1):
        name = " ".join(words[:len(words) - options])
        wanted = name.casefold()
        if not any(wanted in (riot_id.casefold(), riot_id.split("#", 1)[0].casefold()) for riot_id, _puuid in tracked):
            continue
        rest, queue, months = _parse_options(words[len(words) - options:])
        if not rest:
            return name, queue, months
    return _parse_options(words)


def resolve_tracked_player(query, tracked):
    """Find a tracked (riot_id, puuid) by full Riot ID or, if unique, by name alone.

    Returns (match, error_text); exactly one of them is None.
    """
    query = query.strip().replace("`", "'")
    wanted = query.casefold()
    if not wanted:
        return None, "Give a tracked player's Riot ID, like `Name#Tag`."
    exact = [entry for entry in tracked if entry[0].casefold() == wanted]
    if exact:
        return exact[0], None
    if "#" not in wanted:
        by_name = [entry for entry in tracked if entry[0].split("#", 1)[0].casefold() == wanted]
        if len(by_name) == 1:
            return by_name[0], None
        if len(by_name) > 1:
            return None, f"Several tracked players are called `{query}`; add the `#Tag`."
    return None, f"`{query}` isn't a tracked player."


def _pct(wins, games):
    return round(100 * wins / games) if games else 0


def _record(games):
    wins = sum(1 for g in games if g["win"])
    return wins, len(games)


def _weight(*sizes):
    return math.sqrt(min(sizes))


def _kda(games):
    kills = sum(g["kills"] for g in games)
    deaths = sum(g["deaths"] for g in games)
    assists = sum(g["assists"] for g in games)
    return (kills + assists) / max(1, deaths)


def _per_ten_minutes(games, key):
    minutes = sum(g["duration_seconds"] for g in games) / 60
    return 10 * sum(g[key] or 0 for g in games) / minutes if minutes else None


def _avg(games, key):
    values = [g[key] for g in games if g[key] is not None]
    return sum(values) / len(values) if values else None


def _bold(text):
    return f"**{discord.utils.escape_markdown(str(text))}**"


def _champion_findings(games, overall_pct):
    strengths, tips = [], []
    by_champion = defaultdict(list)
    for g in games:
        by_champion[g["champion_name"] or "?"].append(g)
    good, bad = [], []
    for champion, champ_games in by_champion.items():
        wins, n = _record(champ_games)
        if n < MIN_GROUP_GAMES:
            continue
        pct = _pct(wins, n)
        if pct >= 55 and pct >= overall_pct + CHAMPION_GAP_PP:
            good.append(((pct - overall_pct) * _weight(n), champion, wins, n, pct))
        elif pct <= 45 and pct <= overall_pct - CHAMPION_GAP_PP:
            bad.append(((overall_pct - pct) * _weight(n), champion, wins, n, pct))
    for found, target, label in ((good, strengths, "best"), (bad, tips, "weakest")):
        top = sorted(found, reverse=True)[:2]
        if not top:
            continue
        parts = [f"{_bold(champion)} {wins}/{n} ({pct}%)" for _score, champion, wins, n, pct in top]
        noun = "picks" if len(top) > 1 else "pick"
        target.append(Finding(f"{' and '.join(parts)}: your {label} {noun}", sum(t[0] for t in top) / len(top)))
    return strengths, tips


def _main_role(games):
    by_role = defaultdict(list)
    for g in games:
        if g["team_position"]:
            by_role[g["team_position"]].append(g)
    if not by_role:
        return None, by_role
    main = max(by_role, key=lambda role: len(by_role[role]))
    return main, by_role


def _role_findings(games, role_baselines, now, trend_days=TREND_DAYS):
    strengths, tips = [], []
    main, by_role = _main_role(games)
    if main is None or len(by_role[main]) < MIN_SPLIT_GAMES:
        return strengths, tips
    main_games = by_role[main]
    main_wins, main_n = _record(main_games)
    main_pct = _pct(main_wins, main_n)

    weak_roles = []
    for role, role_games in by_role.items():
        wins, n = _record(role_games)
        if role != main and n >= MIN_GROUP_GAMES and _pct(wins, n) <= main_pct - ROLE_GAP_PP:
            weak_roles.append((n, role, wins, role_games))
    if weak_roles:
        # Worst win rate first; more games break ties.
        weak_roles.sort(key=lambda entry: (_pct(entry[2], entry[0]), -entry[0]))
        parts = [f"{ROLE_NAMES.get(role, role)} {wins}/{n}" for n, role, wins, _games in weak_roles[:2]]
        text = f"Stay on {ROLE_IN_SENTENCE.get(main, main)}: {main_pct}% in {main_n} games; {', '.join(parts)}"
        gold = _avg([g for _n, _role, _wins, role_games in weak_roles[:2] for g in role_games], "opponent_gold_diff")
        if gold is not None and gold <= -1000:
            text += f", ~{abs(gold) / 1000:.1f}k gold behind your lane opponent at game end"
        gap = main_pct - min(_pct(w, n) for n, _r, w, _g in weak_roles)
        tips.append(Finding(text, gap * _weight(sum(n for n, *_ in weak_roles))))

    base = (role_baselines or {}).get(main)
    if base and base.get("games", 0) >= MIN_BASELINE_GAMES and base.get("minutes"):
        group_kda = (base["kills"] + base["assists"]) / max(1, base["deaths"])
        my_kda = _kda(main_games)
        role_name = ROLE_NAMES.get(main, main)
        plural = ROLE_PLURALS.get(main, "players in that role")
        if my_kda >= group_kda * KDA_RATIO:
            strengths.append(Finding(
                f"{role_name} KDA {my_kda:.2f}, against {group_kda:.2f} for the group's {plural}",
                100 * (my_kda / group_kda - 1) * _weight(main_n) / 2,
            ))
        group_deaths = 10 * base["deaths"] / base["minutes"]
        my_deaths = _per_ten_minutes(main_games, "deaths")
        if my_deaths is not None and group_deaths and my_deaths >= group_deaths * DEATHS_RATIO:
            tips.append(Finding(
                f"You die more than the group's {plural}: {my_deaths:.1f} vs {group_deaths:.1f} deaths per 10 min",
                100 * (my_deaths / group_deaths - 1) * _weight(main_n) / 2,
            ))

    if main != "UTILITY":
        cutoff = now - timedelta(days=trend_days)
        recent = [g for g in main_games if g["game_end_at"] > cutoff and g["opponent_cs_diff"] is not None]
        older = [g for g in main_games if g["game_end_at"] <= cutoff and g["opponent_cs_diff"] is not None]
        if len(recent) >= MIN_SPLIT_GAMES and len(older) >= MIN_SPLIT_GAMES:
            recent_cs, older_cs = _avg(recent, "opponent_cs_diff"), _avg(older, "opponent_cs_diff")
            if recent_cs <= older_cs - LANE_CS_DROP:
                # opponent_cs_diff is measured at game end, not at the end of laning.
                tips.append(Finding(
                    f"CS gap slipped: {recent_cs:+.0f} CS vs your lane opponent at game end in the last "
                    f"{trend_days} days ({older_cs:+.0f} before)",
                    (older_cs - recent_cs) * _weight(len(recent), len(older)) / 2,
                ))
    return strengths, tips


def _partner_findings(games, overall_pct, names_by_puuid):
    by_partner = defaultdict(list)
    for g in games:
        for partner in g["tracked_teammates"] or ():
            by_partner[partner].append(g)
    best = None
    for partner, partner_games in by_partner.items():
        wins, n = _record(partner_games)
        pct = _pct(wins, n)
        if n >= MIN_PARTNER_GAMES and pct >= 55 and pct >= overall_pct + PARTNER_GAP_PP:
            score = (pct - overall_pct) * _weight(n)
            if best is None or score > best[0]:
                best = (score, partner, wins, n, pct)
    if best is None:
        return []
    score, partner, wins, n, pct = best
    name = names_by_puuid.get(partner, "a friend").split("#", 1)[0]
    return [Finding(f"Duo with {_bold(name)}: {wins}/{n} ({pct}%)", score)]


def _trend_findings(games, now, trend_days=TREND_DAYS):
    cutoff = now - timedelta(days=trend_days)
    recent = [g for g in games if g["game_end_at"] > cutoff]
    older = [g for g in games if g["game_end_at"] <= cutoff]
    if len(recent) < MIN_SPLIT_GAMES or len(older) < MIN_SPLIT_GAMES:
        return [], []
    recent_pct = _pct(*_record(recent))
    older_pct = _pct(*_record(older))
    weight = _weight(len(recent), len(older))
    if recent_pct >= older_pct + TREND_GAP_PP:
        return [Finding(f"Trending up: {recent_pct}% in the last {trend_days} days ({len(recent)}), {older_pct}% before",
                        (recent_pct - older_pct) * weight)], []
    if recent_pct <= older_pct - TREND_GAP_PP:
        text = f"Last {trend_days} days {recent_pct}% ({len(recent)}), {older_pct}% before"
        recent_kda, older_kda = _kda(recent), _kda(older)
        if recent_kda >= older_kda + 0.2:
            text += f", even though KDA rose {older_kda:.2f} → {recent_kda:.2f}"
        return [], [Finding(text, (older_pct - recent_pct) * weight)]
    return [], []


def _session_findings(games, tz, day_start_hour):
    """Tilt (the game right after a loss) and long sessions (game 4+ of a day)."""
    strengths, tips = [], []
    ordered = sorted(games, key=lambda g: g["game_end_at"])
    after_loss, after_win = [], []
    for previous, current in zip(ordered, ordered[1:]):
        if current["game_end_at"] - previous["game_end_at"] <= NEXT_GAME_WINDOW:
            (after_win if previous["win"] else after_loss).append(current)
    if len(after_loss) >= MIN_SPLIT_GAMES and len(after_win) >= MIN_SPLIT_GAMES:
        loss_pct, win_pct = _pct(*_record(after_loss)), _pct(*_record(after_win))
        weight = _weight(len(after_loss), len(after_win))
        if loss_pct <= win_pct - TILT_GAP_PP:
            tips.append(Finding(
                f"Take a break after a loss: {loss_pct}% in the next game, {win_pct}% after a win",
                (win_pct - loss_pct) * weight,
            ))
        elif abs(win_pct - loss_pct) <= NO_TILT_MAX_GAP_PP:
            strengths.append(Finding(f"No tilt: {loss_pct}% after a loss, {win_pct}% after a win", 2 * weight))

    by_day = defaultdict(list)
    for g in ordered:
        local = g["game_end_at"].astimezone(tz) - timedelta(hours=day_start_hour)
        by_day[local.date()].append(g)
    early = [g for day in by_day.values() for g in day[:GAMES_BEFORE_LONG_SESSION]]
    late = [g for day in by_day.values() for g in day[GAMES_BEFORE_LONG_SESSION:]]
    if len(early) >= MIN_SPLIT_GAMES and len(late) >= MIN_SPLIT_GAMES:
        early_pct, late_pct = _pct(*_record(early)), _pct(*_record(late))
        if late_pct <= early_pct - SESSION_GAP_PP:
            tips.append(Finding(
                f"Stop after {GAMES_BEFORE_LONG_SESSION} games a day: {late_pct}% from game "
                f"{GAMES_BEFORE_LONG_SESSION + 1} on, {early_pct}% before",
                (early_pct - late_pct) * _weight(len(early), len(late)),
            ))
    return strengths, tips


def _top(findings):
    return [f.text for f in sorted(findings, key=lambda f: f.score, reverse=True)[:MAX_FINDINGS]]


def analyze_player(
    *, riot_id, games, queue, role_baselines, names_by_puuid, now, tz, day_start_hour, months=DEFAULT_MONTHS,
):
    """Build the analysis for one queue from the player's ranked rows (both queues, no remakes).

    Only games in the last `months` count; trends split that window at 90 days, or in
    half when the window is shorter than 180 days.
    """
    since = analysis_start(now, months)
    games = [g for g in games if since < g["game_end_at"] <= now]
    trend_days = min(TREND_DAYS, (now - since).days // 2)
    queue_id = QUEUES[queue]["queue_id"]
    queue_games = [g for g in games if g["queue_id"] == queue_id]
    other_games = [g for g in games if g["queue_id"] != queue_id]
    wins, n = _record(queue_games)
    other_wins, other_n = _record(other_games)
    analysis = PlayerAnalysis(
        riot_id=riot_id,
        queue=queue,
        games=n,
        wins=wins,
        other_queue_games=other_n,
        other_queue_wins=other_wins,
        since=min((g["game_end_at"] for g in queue_games), default=None),
        months=months,
    )
    if not analysis.enough_games:
        return analysis
    overall_pct = _pct(wins, n)
    strengths, tips = [], []
    for found_strengths, found_tips in (
        _champion_findings(queue_games, overall_pct),
        _role_findings(queue_games, role_baselines, now, trend_days),
        _trend_findings(queue_games, now, trend_days),
        _session_findings(queue_games, tz, day_start_hour),
    ):
        strengths.extend(found_strengths)
        tips.extend(found_tips)
    strengths.extend(_partner_findings(queue_games, overall_pct, names_by_puuid))
    analysis.strengths = _top(strengths)
    analysis.tips = _top(tips)
    return analysis


def _queue_record(label, wins, games):
    if not games:
        return f"no {label} games"
    return f"{label} {wins}W-{games - wins}L ({_pct(wins, games)}%)"


def format_analysis_sections(analysis):
    """Text blocks for the card (header, strengths, tips, footer); also joined for plain text."""
    label = QUEUES[analysis.queue]["label"]
    other = "flex" if analysis.queue == "solo" else "solo"
    other_label = QUEUES[other]["label"]
    name = discord.utils.escape_markdown(analysis.riot_id)
    records = [_queue_record(label, analysis.wins, analysis.games)]
    if analysis.other_queue_games:
        records.append(_queue_record(other_label, analysis.other_queue_wins, analysis.other_queue_games))
    period = f"last {analysis.months} month{'s' if analysis.months != 1 else ''}"
    header = f"**{name}** · {label} analysis\n-# {period.capitalize()} · {' · '.join(records)}"
    command_name = discord.utils.escape_markdown(analysis.riot_id.split("#", 1)[0])
    # Keep a non-default month count in suggested commands.
    months_arg = f" {analysis.months}" if analysis.months != DEFAULT_MONTHS else ""
    if not analysis.enough_games:
        body = f"Not enough {label} games to analyze yet: {analysis.games} in the {period}, {MIN_QUEUE_GAMES} needed."
        if analysis.other_queue_games >= MIN_QUEUE_GAMES:
            body += f" Try `!analyze {command_name}{months_arg} {other}`."
        if analysis.months < MAX_ANALYSIS_MONTHS:
            body += f" Or look further back: `!analyze {command_name} {MAX_ANALYSIS_MONTHS}`."
        return [header, body]
    strengths = "\n".join(f"✅ {line}" for line in analysis.strengths) or "Nothing stands out yet."
    tips = "\n".join(f"⚠️ {line}" for line in analysis.tips) or "No clear weak spots in these games."
    since = analysis.since.strftime("%d %b %Y").lstrip("0") if analysis.since else "?"
    footer = (
        f"-# {label} games since {since} · tips need {MIN_GROUP_GAMES}+ games · "
        f"`!analyze {command_name}{months_arg} {other}` for {other_label} · add 1-12 for months"
    )
    return [header, f"**Strengths**\n{strengths}", f"**Work on**\n{tips}", footer]


def format_analysis_text(analysis):
    return "\n\n".join(format_analysis_sections(analysis))


def build_analysis_view(analysis):
    """Components V2 card: one container, sections split by separators."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=ANALYSIS_ACCENT)
    sections = format_analysis_sections(analysis)
    for index, section in enumerate(sections):
        if index:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(section))
    view.add_item(container)
    return view
