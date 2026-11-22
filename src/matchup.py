"""`!matchup Name Champion [months] [role] [solo|flex]`: one player's games on one champion, and their
best and worst lane matchups.

Pure logic over `player_match_stats` rows. Champion names come from the stored rows too (match data
carries Riot's internal names such as `MonkeyKing`), so no Riot or Data Dragon request is needed.
"""

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field

import discord

from src.discord_text import player_name
from src.player_analysis import MAX_ANALYSIS_MONTHS, QUEUES, ROLE_ARGS
from src.analysis_metrics import ROLE_NAMES

MATCHUP_ACCENT = 0x378ADD
# A year by default: over 3 months even a main champion rarely meets one opponent 3 times.
DEFAULT_MATCHUP_MONTHS = 12
# Games on the champion before there is a card, and against one opponent before it is a matchup.
MIN_CARD_GAMES = 5
MIN_MATCHUP_GAMES = 3
MATCHUPS_SHOWN = 3
QUEUE_LABEL_BOTH = "Solo/Duo and Flex"
MONTHS_ERROR = "Choose a whole number of months from 1 to 12, like `!matchup Name Yorick 6`."
USAGE = (
    "Usage: `!matchup Name Champion [months] [role] [solo|flex]`, like `!matchup NoxVain Yorick top flex` "
    f"(last {DEFAULT_MATCHUP_MONTHS} months of Solo/Duo and Flex in every role unless you say otherwise; 1-12 months; "
    "role is top, jungle, mid, adc or support)."
)

# Riot's internal champion names that don't read as the champion's name once split into words.
DISPLAY_NAMES = {
    "Belveth": "Bel'Veth",
    "Chogath": "Cho'Gath",
    "DrMundo": "Dr. Mundo",
    "FiddleSticks": "Fiddlesticks",
    "Kaisa": "Kai'Sa",
    "Khazix": "Kha'Zix",
    "KogMaw": "Kog'Maw",
    "KSante": "K'Sante",
    "Leblanc": "LeBlanc",
    "MonkeyKing": "Wukong",
    "Nunu": "Nunu & Willump",
    "RekSai": "Rek'Sai",
    "Renata": "Renata Glasc",
    "Velkoz": "Vel'Koz",
}


def champion_display_name(internal_name):
    """`MonkeyKing` -> `Wukong`, `JarvanIV` -> `Jarvan IV`, `LeeSin` -> `Lee Sin`."""
    if internal_name in DISPLAY_NAMES:
        return DISPLAY_NAMES[internal_name]
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", internal_name)


def _key(text):
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def resolve_champion(query, champion_names):
    """Find a champion ID from what the player typed.

    `champion_names` is {champion_id: internal name}. Any case, spaces and apostrophes match
    (`kaisa`, `Kai'Sa`, `wukong`, `monkeyking`), and so does a prefix only one champion has
    (`cait`). Returns (champion_id, error_text); exactly one of them is None.
    """
    wanted = _key(query)
    if not wanted:
        return None, USAGE
    # Mode variants such as `Strawberry_Jinx` (Swarm) are never ranked picks.
    champions = {cid: name for cid, name in champion_names.items() if name and "_" not in name}
    keys = {}
    for cid, name in champions.items():
        for key in {_key(name), _key(champion_display_name(name))}:
            keys.setdefault(key, cid)
    if wanted in keys:
        return keys[wanted], None
    starting = {cid for key, cid in keys.items() if key.startswith(wanted)}
    if len(starting) == 1:
        return starting.pop(), None
    shown = query.strip().replace("`", "'")
    close = difflib.get_close_matches(wanted, keys, n=1, cutoff=0.75)
    if close:
        return None, f"`{shown}` isn't a champion. Did you mean {champion_display_name(champions[keys[close[0]]])}?"
    return None, f"`{shown}` isn't a champion."


def _parse_options(words):
    """Pop trailing month count, role and queue words, in any order. Queue None means both."""
    queue, months, role, seen = None, DEFAULT_MATCHUP_MONTHS, None, set()
    while words:
        token = words[-1].casefold()
        if token in QUEUES:
            kind = "queue"
        elif token in ROLE_ARGS:
            kind = "role"
        elif token.lstrip("+-").replace(".", "", 1).isdigit():
            kind = "months"
        else:
            break
        if kind in seen:
            raise ValueError("Use one month count, one role and one queue, like `!matchup Name Yorick 6 top flex`.")
        seen.add(kind)
        words.pop()
        if kind == "queue":
            queue = token
        elif kind == "role":
            role = ROLE_ARGS[token]
        elif not token.isdecimal() or not 1 <= int(token) <= MAX_ANALYSIS_MONTHS:
            raise ValueError(MONTHS_ERROR)
        else:
            months = int(token)
    return words, queue, months, role


def parse_matchup_args(raw, tracked=()):
    """`"Not a snake Lee Sin 6 jungle"` -> ("Not a snake", "Lee Sin", None, 6, "JUNGLE").

    The player comes first and the champion after; both can contain spaces. The longest start
    that is a tracked player's name (or full Riot ID) is the player. Raises ValueError with a
    user-facing message when something is missing or a month count is wrong.
    """
    words, queue, months, role = _parse_options(raw.split())
    if len(words) < 2:
        raise ValueError(USAGE)
    names = set()
    for riot_id, _puuid in tracked:
        names.add(riot_id.casefold())
        names.add(riot_id.split("#", 1)[0].casefold())
    for split in range(len(words) - 1, 0, -1):
        if " ".join(words[:split]).casefold() in names:
            return " ".join(words[:split]), " ".join(words[split:]), queue, months, role
    # No tracked name: take the first word, so the reply names who wasn't found.
    return words[0], " ".join(words[1:]), queue, months, role


@dataclass
class Matchup:
    opponent: str
    games: int
    wins: int
    gold_diff: float | None

    @property
    def win_rate(self):
        return self.wins / self.games


@dataclass
class MatchupReport:
    riot_id: str
    champion: str
    queue: str | None
    months: int
    role: str | None
    games: int = 0
    wins: int = 0
    kills: float = 0.0
    deaths: float = 0.0
    assists: float = 0.0
    cs_per_min: float | None = None
    damage_share: float | None = None
    kill_participation: float | None = None
    gold_diff: float | None = None
    # Lane opponents with MIN_MATCHUP_GAMES or more games, and how many opponents there were in all.
    best: list = field(default_factory=list)
    worst: list = field(default_factory=list)
    qualified_opponents: int = 0
    opponents: int = 0
    roles: dict = field(default_factory=dict)


def _mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def build_matchup_report(*, riot_id, champion_id, rows, champion_names, queue, months, role):
    """Stats for one player's games on one champion; `rows` are their ranked games in the window."""
    queue_ids = {QUEUES[queue]["queue_id"]} if queue else {spec["queue_id"] for spec in QUEUES.values()}
    games = [
        row for row in rows
        if row.get("champion_id") == champion_id and row.get("queue_id") in queue_ids
        and (role is None or row.get("team_position") == role)
    ]
    report = MatchupReport(
        riot_id=riot_id, champion=champion_display_name(champion_names.get(champion_id, str(champion_id))),
        queue=queue, months=months, role=role,
    )
    report.games = len(games)
    report.wins = sum(1 for row in games if row.get("win"))
    if not games:
        return report
    report.kills = sum(row.get("kills") or 0 for row in games) / len(games)
    report.deaths = sum(row.get("deaths") or 0 for row in games) / len(games)
    report.assists = sum(row.get("assists") or 0 for row in games) / len(games)
    minutes = sum((row.get("duration_seconds") or 0) for row in games if row.get("cs") is not None) / 60
    if minutes:
        report.cs_per_min = sum(row["cs"] for row in games if row.get("cs") is not None) / minutes
    report.damage_share = _mean(row.get("damage_share") for row in games)
    report.kill_participation = _mean(row.get("kill_participation") for row in games)
    report.gold_diff = _mean(row.get("opponent_gold_diff") for row in games)
    for row in games:
        if row.get("team_position"):
            report.roles[row["team_position"]] = report.roles.get(row["team_position"], 0) + 1

    by_opponent = defaultdict(list)
    for row in games:
        if row.get("opponent_champion_id") is not None:
            by_opponent[row["opponent_champion_id"]].append(row)
    report.opponents = len(by_opponent)
    matchups = [
        Matchup(
            opponent=champion_display_name(champion_names.get(opponent_id, f"Champion {opponent_id}")),
            games=len(opponent_rows),
            wins=sum(1 for row in opponent_rows if row.get("win")),
            gold_diff=_mean(row.get("opponent_gold_diff") for row in opponent_rows),
        )
        for opponent_id, opponent_rows in by_opponent.items()
        if len(opponent_rows) >= MIN_MATCHUP_GAMES
    ]
    report.qualified_opponents = len(matchups)
    # Best only wins at least half its games and worst only loses more than half, so a losing matchup is never
    # called best just because there are few. Ties go to more games, then the name, so the order is stable.
    winning = [m for m in matchups if m.win_rate >= 0.5]
    losing = [m for m in matchups if m.win_rate < 0.5]
    report.best = sorted(winning, key=lambda m: (-m.win_rate, -m.games, m.opponent))[:MATCHUPS_SHOWN]
    report.worst = sorted(losing, key=lambda m: (m.win_rate, -m.games, m.opponent))[:MATCHUPS_SHOWN]
    return report


def _scope(report):
    queue = QUEUES[report.queue]["label"] if report.queue else QUEUE_LABEL_BOTH
    months = "last month" if report.months == 1 else f"last {report.months} months"
    return f"{queue} · {months}"


def _role_text(report):
    if report.role:
        return ROLE_NAMES[report.role]
    if len(report.roles) == 1:
        return ROLE_NAMES.get(next(iter(report.roles)), "All roles")
    return "All roles"


def _gold(value):
    if value is None:
        return None
    sign = "−" if value < 0 else "+"
    return f"{sign}{abs(value) / 1000:.1f}k gold"


def _matchup_line(marker, matchup):
    parts = [f"{marker} {matchup.opponent}", f"**{matchup.wins}W-{matchup.games - matchup.wins}L** ({matchup.win_rate:.0%})"]
    gold = _gold(matchup.gold_diff)
    if gold:
        parts.append(gold)
    return " · ".join(parts)


def too_few_games_text(report):
    """The reply when there aren't enough games on the champion for a card."""
    where = f" in {ROLE_NAMES[report.role].lower()}" if report.role else ""
    queue = QUEUES[report.queue]["label"] if report.queue else QUEUE_LABEL_BOTH
    window = "the last month" if report.months == 1 else f"the last {report.months} months"
    name = player_name(report.riot_id)
    if not report.games:
        return f"{name} has no {queue} games as {report.champion}{where} in {window}."
    return (
        f"{name} has {report.games} {queue} game{'s' if report.games != 1 else ''} as {report.champion}{where} "
        f"in {window} ({report.wins}W-{report.games - report.wins}L); the card needs {MIN_CARD_GAMES}."
        + (" Try more months, like `!matchup Name Champion 12`." if report.months < MAX_ANALYSIS_MONTHS else "")
    )


def format_matchup_sections(report):
    header = (
        f"🎯 **{player_name(report.riot_id)} · {report.champion}** · {_role_text(report)}\n"
        f"-# {_scope(report)}"
    )
    kda = (report.kills + report.assists) / max(1.0, report.deaths)
    lines = [
        f"**{report.games}** games · **{report.wins}W-{report.games - report.wins}L** · "
        f"**{report.wins / report.games:.0%}** win rate",
        f"**{report.kills:.1f} / {report.deaths:.1f} / {report.assists:.1f}** KDA ({kda:.2f})"
        + (f" · **{report.cs_per_min:.1f}** CS/min" if report.cs_per_min is not None else ""),
    ]
    extras = []
    if report.damage_share is not None:
        extras.append(f"**{report.damage_share:.0%}** damage share")
    if report.kill_participation is not None:
        extras.append(f"**{report.kill_participation:.0%}** kill participation")
    gold = _gold(report.gold_diff)
    if gold:
        extras.append(f"**{gold.replace(' gold', '')}** gold vs lane opponent")
    if extras:
        lines.append(" · ".join(extras))
    sections = [header, "\n".join(lines)]
    if report.best or report.worst:
        matchups = []
        if report.best:
            matchups += ["**Best matchups**", *(_matchup_line("🟢", m) for m in report.best)]
        if report.worst:
            matchups += [*([""] if matchups else []), "**Worst matchups**", *(_matchup_line("🔴", m) for m in report.worst)]
        sections.append("\n".join(matchups))
    else:
        sections.append(f"Not enough games against one opponent yet ({MIN_MATCHUP_GAMES} needed).")
    sections.append(
        f"-# Lane opponent = enemy in the same role · matchups need {MIN_MATCHUP_GAMES}+ games "
        f"({report.qualified_opponents} of {report.opponents} opponents) · gold is the difference at game end"
    )
    return sections


def build_matchup_view(report):
    """Components V2 card: one container, sections split by separators."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=MATCHUP_ACCENT)
    for index, section in enumerate(format_matchup_sections(report)):
        if index:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(section))
    view.add_item(container)
    return view


def format_matchup_text(report):
    return "\n\n".join(format_matchup_sections(report))
