"""Poro Wrapped: the group's year in review, posted once, and `!wrapped Name` per player.

Pure logic over `player_match_stats` rows (all queues, remakes excluded) streamed in
game order, plus each player's current rank; no Discord, Riot or DB access except the
posting task at the bottom. Ranked games are the main event (awards, records, the
Ranked section of `!wrapped`); ARAM and Arena get their own sections.

The group post goes to `EVENTS_CHANNEL_ID` on 31 December at 18:00 (`REPORT_TIMEZONE`)
and covers 1 January until then. `!wrapped` opens at the same moment and closes at the
end of January; admins can run it any time to preview the year so far.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import discord

from src.discord_text import ARENA_QUEUE_IDS
from src.rank_logic import rank_level
from src.records import CATEGORIES, RECORD_QUEUE_IDS, RecordBook

WRAPPED_STATE_KEY = "poro_wrapped_posted_year"
WRAPPED_ACCENT = 0xF0B232
WRAPPED_PERSONAL_ACCENT = 0x7F77DD
ARAM_QUEUE_IDS = frozenset({450, 720, 2400})
POST_MONTH, POST_DAY, POST_HOUR = 12, 31, 18
# !wrapped stays open through January.
CLOSE_MONTH = 2
MIN_AWARD_GAMES = 20
MIN_DUO_GAMES = 25
MIN_MODE_RATE_GAMES = 10
# Only a top-5 place in the group makes a personal "Highs" line.
MAX_HIGH_RANK = 5
NIGHT_END_HOUR = 5
WRAPPED_CHECK_SECONDS = 600
WRAPPED_STARTUP_DELAY_SECONDS = 900
# Discord allows 4,000 characters of text per message; leave room for separators.
MESSAGE_CHAR_BUDGET = 3_800

RANKED = "ranked"
ARAM = "aram"
ARENA = "arena"
OTHER = "other"

ROLE_LABELS = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
ROLE_TITLES = {
    "TOP": "The Island Dweller",
    "JUNGLE": "The Camp Counselor",
    "MIDDLE": "The Mid Diff",
    "BOTTOM": "The ADC Who Was Promised Peel",
    "UTILITY": "The Support Who Survived",
}
QUEUE_RANK_LABELS = {"RANKED_SOLO_5X5": "solo", "RANKED_FLEX_SR": "flex"}


def mode_of(queue_id):
    if queue_id in RECORD_QUEUE_IDS:
        return RANKED
    if queue_id in ARAM_QUEUE_IDS:
        return ARAM
    if queue_id in ARENA_QUEUE_IDS:
        return ARENA
    return OTHER


# --- Window -------------------------------------------------------------------


def wrapped_window(now_utc, tz):
    """(year, post_at, closes_at) for the Wrapped that is next or currently open.

    In January it's last year's Wrapped (open until February); otherwise this year's,
    posted on 31 December.
    """
    local = now_utc.astimezone(tz)
    year = local.year - 1 if local.month < CLOSE_MONTH else local.year
    post_at = datetime(year, POST_MONTH, POST_DAY, POST_HOUR, tzinfo=tz)
    closes_at = datetime(year + 1, CLOSE_MONTH, 1, tzinfo=tz)
    return year, post_at, closes_at


def wrapped_period(year, tz):
    """[start, end) of the games one Wrapped covers: 1 January until the post."""
    return datetime(year, 1, 1, tzinfo=tz), datetime(year, POST_MONTH, POST_DAY, POST_HOUR, tzinfo=tz)


# --- Accumulation -------------------------------------------------------------


@dataclass
class _ModeTotals:
    games: int = 0
    wins: int = 0
    seconds: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    # champion -> [games, wins]
    champions: dict = field(default_factory=dict)
    firsts: int = 0
    placement_sum: int = 0
    placement_games: int = 0
    best_kills: tuple | None = None
    best_damage: tuple | None = None

    def add(self, row):
        won = bool(row.get("win"))
        self.games += 1
        self.wins += int(won)
        self.seconds += int(row.get("duration_seconds") or 0)
        self.kills += int(row.get("kills") or 0)
        self.deaths += int(row.get("deaths") or 0)
        self.assists += int(row.get("assists") or 0)
        champion = row.get("champion_name") or "Unknown"
        entry = self.champions.setdefault(champion, [0, 0])
        entry[0] += 1
        entry[1] += int(won)
        placement = row.get("arena_placement")
        if placement:
            self.placement_sum += placement
            self.placement_games += 1
            self.firsts += int(placement == 1)
        for attr, column in (("best_kills", "kills"), ("best_damage", "damage_to_champions")):
            value = row.get(column) or 0
            current = getattr(self, attr)
            if value and (current is None or value > current[0]):
                setattr(self, attr, (value, champion, row.get("game_end_at")))

    def top_champions(self, count=3):
        return sorted(self.champions.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))[:count]


@dataclass
class _PlayerYear:
    riot_id: str
    modes: dict = field(default_factory=dict)
    roles: Counter = field(default_factory=Counter)
    days: Counter = field(default_factory=Counter)
    ranked_loss_days: Counter = field(default_factory=Counter)
    night_games: int = 0
    pings: int = 0
    dead_seconds: int = 0

    def mode(self, name):
        return self.modes.setdefault(name, _ModeTotals())

    @property
    def games(self):
        return sum(totals.games for totals in self.modes.values())

    @property
    def wins(self):
        return sum(totals.wins for totals in self.modes.values())

    @property
    def seconds(self):
        return sum(totals.seconds for totals in self.modes.values())

    @property
    def champions(self):
        return {champ for totals in self.modes.values() for champ in totals.champions}

    @property
    def ranked(self):
        return self.modes.get(RANKED) or _ModeTotals()


@dataclass
class WrappedYear:
    year: int
    tz: object
    players: dict = field(default_factory=dict)
    # match_id -> (mode, duration) for unique-match counts
    matches: dict = field(default_factory=dict)
    # sorted (puuid, puuid) -> [ranked games together, wins]
    duos: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    book: RecordBook = field(default_factory=RecordBook)
    # riot_id -> [rank entries]
    ranks: dict = field(default_factory=dict)

    def add(self, row):
        riot_id = row["riot_id"]
        self.names[row.get("puuid")] = riot_id
        player = self.players.setdefault(riot_id, _PlayerYear(riot_id))
        mode = mode_of(row.get("queue_id"))
        player.mode(mode).add(row)
        end = row["game_end_at"]
        duration = int(row.get("duration_seconds") or 0)
        local_end = end.astimezone(self.tz)
        player.days[local_end.date()] += 1
        if (end - timedelta(seconds=duration)).astimezone(self.tz).hour < NIGHT_END_HOUR:
            player.night_games += 1
        self.matches.setdefault(row.get("match_id"), (mode, duration))
        if mode != RANKED:
            return
        self.book.add(row)
        if row.get("team_position"):
            player.roles[row["team_position"]] += 1
        if not row.get("win"):
            player.ranked_loss_days[local_end.date()] += 1
        player.pings += int(row.get("pings") or 0)
        player.dead_seconds += int(row.get("time_dead_seconds") or 0)
        mine = row.get("puuid")
        for mate in row.get("tracked_teammates") or []:
            if mine and mate and mine < mate:
                entry = self.duos.setdefault((mine, mate), [0, 0])
                entry[0] += 1
                entry[1] += int(bool(row.get("win")))

    def eligible(self, *, ranked=False):
        return [
            player for player in self.players.values()
            if (player.ranked.games if ranked else player.games) >= MIN_AWARD_GAMES
        ]

    def duo_list(self):
        """[(riot_id_a, riot_id_b, games, wins)] with enough games, names resolved."""
        found = []
        for (a, b), (games, wins) in self.duos.items():
            if games >= MIN_DUO_GAMES and a in self.names and b in self.names:
                found.append((self.names[a], self.names[b], games, wins))
        return found


def build_wrapped(rows, year, tz, ranks=None):
    wrapped = WrappedYear(year=year, tz=tz, ranks=dict(ranks or {}))
    for row in rows:
        wrapped.add(row)
    return wrapped


def load_wrapped(db_iter_wrapped_rows, db_load_all_ranked_states, year, tz, *, end=None):
    """Blocking (pages through the year's rows); run via asyncio.to_thread."""
    start, period_end = wrapped_period(year, tz)
    end = min(end, period_end) if end is not None else period_end
    return build_wrapped(db_iter_wrapped_rows(start, end), year, tz, db_load_all_ranked_states())


# --- Text helpers -------------------------------------------------------------


def _name(riot_id):
    return discord.utils.escape_markdown(riot_id.split("#", 1)[0])


def _pct(wins, games):
    return f"{round(100 * wins / games)}%" if games else "0%"


def _hours(seconds):
    return round(seconds / 3600)


def _bold_count(count, word):
    return f"**{count:,}** {word if count == 1 else word + 's'}"


def _day(value):
    return f"{value.day} {value.strftime('%b')}"


def _plural(count, word, plural=None):
    return f"{count:,} {word if count == 1 else (plural or word + 's')}"


def _champ_line(totals, count=4):
    return " · ".join(f"{champ} {games} ({wins}W)" for champ, (games, wins) in totals.top_champions(count))


def _best_rank(entries):
    """(label, level) of a player's best queue, like "Emerald III solo"."""
    best = None
    for entry in entries or []:
        level = rank_level(entry)
        if level is None:
            continue
        if best is None or level > best[1]:
            best = (entry, level)
    return best


def _rank_text(entry):
    tier = str(entry.get("tier", "")).title()
    division = entry.get("rank_division") or ""
    queue = QUEUE_RANK_LABELS.get(str(entry.get("queue_type", "")).upper(), "")
    return " ".join(part for part in (tier, division, queue) if part)


def _ranks_text(entries):
    ordered = sorted(
        (entry for entry in entries or [] if rank_level(entry) is not None),
        key=lambda entry: 0 if str(entry.get("queue_type", "")).upper() == "RANKED_SOLO_5X5" else 1,
    )
    return " · ".join(_rank_text(entry) for entry in ordered)


# --- Group post ---------------------------------------------------------------


@dataclass
class Award:
    title: str
    holder: str
    text: str
    banter: str | None = None
    all_modes: bool = False


def _max_award(players, key):
    """Highest positive key; ties go to more games, then name order."""
    scored = sorted(
        ((key(player), player) for player in players),
        key=lambda pair: (-pair[0], -pair[1].games, pair[1].riot_id.casefold()),
    )
    return next(((value, player) for value, player in scored if value), None)


def compute_awards(wrapped):
    awards = []
    everyone = wrapped.eligible()
    ranked = wrapped.eligible(ranked=True)

    best = _max_award(everyone, lambda p: p.games)
    if best:
        value, player = best
        hours = _hours(player.seconds)
        days = round(hours / 24)
        awards.append(Award(
            "The Grinder", player.riot_id, f"{value:,} games, {hours:,} hours",
            f"{days} full days in the Rift. Grass is still out there, untouched." if days >= 5 else None,
            all_modes=True,
        ))
    best = _max_award(ranked, lambda p: p.pings)
    if best:
        value, player = best
        per_game = round(value / player.ranked.games)
        awards.append(Award(
            "Backseat Gamer of the Year", player.riot_id, f"{value:,} pings, {per_game} a game",
            "The enemy jungler isn't missing. He left because of the pings." if per_game >= 15 else None,
        ))
    best = _max_award(ranked, lambda p: p.dead_seconds)
    if best:
        value, player = best
        hours = _hours(value)
        awards.append(Award(
            "Grey Screen Enjoyer", player.riot_id, f"{hours:,} hours spent dead",
            f"That's {value / 86400:.1f} days of watching your teammates play." if hours >= 24 else None,
        ))
    best = _max_award(everyone, lambda p: p.night_games)
    if best:
        value, player = best
        awards.append(Award(
            "Night Owl", player.riot_id, f"{_plural(value, 'game')} started between midnight and {NIGHT_END_HOUR}",
            "The poros were asleep. You were not." if value >= 20 else None,
            all_modes=True,
        ))

    def top_share(player):
        top = player.ranked.top_champions(1)
        return top[0][1][0] / player.ranked.games if top else 0

    best = _max_award(ranked, top_share)
    if best:
        _share, player = best
        champ, (games, _wins) = player.ranked.top_champions(1)[0]
        awards.append(Award(
            "One-Trick Pony", player.riot_id, f"{champ} in {games} of {player.ranked.games} ranked games",
            f"{champ} has asked for some time apart." if games / player.ranked.games >= 0.25 else None,
        ))
    best = _max_award(everyone, lambda p: len(p.champions))
    if best:
        value, player = best
        awards.append(Award(
            "Champion Ocean", player.riot_id, f"{value} different champions",
            "Main role: yes." if value >= 80 else None,
            all_modes=True,
        ))
    duos = wrapped.duo_list()
    if duos:
        a, b, games, wins = max(duos, key=lambda d: (d[3] / d[2], d[2]))
        awards.append(Award(
            "Dream Duo", f"{a} + {b}", f"{games} ranked games, {_pct(wins, games)}",
            "Marriage material." if wins / games >= 0.55 else None,
        ))
        cursed = min(duos, key=lambda d: (d[3] / d[2], -d[2]))
        if cursed[:2] != (a, b) and cursed[3] / cursed[2] < 0.5:
            a, b, games, wins = cursed
            awards.append(Award(
                "Cursed Duo", f"{a} + {b}", f"{wins}-{games - wins} together",
                "For everyone's sake: queue separately." if wins / games < 0.4 else None,
            ))
    worst = None
    for player in ranked:
        if player.ranked_loss_days:
            day, losses = max(player.ranked_loss_days.items(), key=lambda item: (item[1], -item[0].toordinal()))
            if worst is None or losses > worst[0]:
                worst = (losses, day, player)
    if worst:
        losses, day, player = worst
        awards.append(Award(
            "Worst Day", player.riot_id, f"{losses} ranked losses on {_day(day)}",
            "Some days you're the poro. Some days you're the snowball." if losses >= 8 else None,
        ))
    return awards


def _award_block(award):
    holder = " + ".join(f"**{_name(part)}**" for part in award.holder.split(" + "))
    lines = [f"**{award.title}**", f"{holder} · {award.text}" + (" · all modes" if award.all_modes else "")]
    if award.banter:
        lines.append(f"-# *{award.banter}*")
    return "\n".join(lines)


def _mode_matches(wrapped, mode):
    return [duration for match_mode, duration in wrapped.matches.values() if match_mode == mode]


def _year_record_lines(wrapped):
    book = wrapped.book
    lines = []
    for key in ("kills", "kill_streak", "damage", "vision", "deaths", "longest_game"):
        category = next(c for c in CATEGORIES if c.key == key)
        holder = book.record(key)
        if holder is not None:
            champion = f" · {holder.champion}" if holder.champion else ""
            lines.append(
                f"**{category.text(holder.value)}** {category.label_for(holder.value)} · {_name(holder.riot_id)}"
                f"{champion} · {holder.at.astimezone(wrapped.tz).strftime('%b')}"
            )
    for kind, word in (("win", "wins"), ("loss", "losses")):
        holder = book.record(kind)
        if holder is not None:
            lines.append(f"**{int(holder.value)}** ranked {word} in a row · {_name(holder.riot_id)}")
    return lines


def _aram_section(wrapped):
    players = [p for p in wrapped.players.values() if ARAM in p.modes]
    if not players:
        return None
    games = len(_mode_matches(wrapped, ARAM))
    lines = [f"❄️ **ARAM**\n-# {_plural(games, 'game')} on the Howling Abyss"]
    top = max(players, key=lambda p: (p.modes[ARAM].games, p.riot_id.casefold()))
    lines.append(f"Most games: **{_name(top.riot_id)}** · {top.modes[ARAM].games}")
    rated = [p for p in players if p.modes[ARAM].games >= MIN_MODE_RATE_GAMES]
    if rated:
        best = max(rated, key=lambda p: (p.modes[ARAM].wins / p.modes[ARAM].games, p.modes[ARAM].games))
        lines.append(f"Best win rate: **{_name(best.riot_id)}** · {_pct(best.modes[ARAM].wins, best.modes[ARAM].games)}")
    champs = Counter()
    for player in players:
        for champ, (count, _wins) in player.modes[ARAM].champions.items():
            champs[champ] += count
    champ, count = sorted(champs.items(), key=lambda item: (-item[1], item[0]))[0]
    if count >= 3:
        lines.append(f"Most picked: **{champ}** · {count} games")
    kills = max(players, key=lambda p: (p.modes[ARAM].best_kills or (0,))[0])
    if kills.modes[ARAM].best_kills:
        value, champion, _at = kills.modes[ARAM].best_kills
        lines.append(f"Most kills: **{value}** · {_name(kills.riot_id)} · {champion}")
    if top.modes[ARAM].games >= 0.5 * sum(p.modes[ARAM].games for p in players) and len(players) > 1:
        lines.append(f"-# *Howling Abyss, population: {_name(top.riot_id)}.*")
    return "\n".join(lines)


def _arena_section(wrapped):
    players = [p for p in wrapped.players.values() if ARENA in p.modes]
    if not players:
        return None
    games = len(_mode_matches(wrapped, ARENA))
    lines = [f"⚔️ **Arena**\n-# {_plural(games, 'game')} in the Arena"]
    top = max(players, key=lambda p: (p.modes[ARENA].firsts, p.modes[ARENA].games))
    if top.modes[ARENA].firsts:
        lines.append(f"Most 1st places: **{_name(top.riot_id)}** · {top.modes[ARENA].firsts}")
    placed = [p for p in players if p.modes[ARENA].placement_games >= MIN_MODE_RATE_GAMES]
    if placed:
        best = min(placed, key=lambda p: (p.modes[ARENA].placement_sum / p.modes[ARENA].placement_games, -p.modes[ARENA].games))
        average = best.modes[ARENA].placement_sum / best.modes[ARENA].placement_games
        lines.append(f"Best average placement: **{_name(best.riot_id)}** · {average:.1f}")
    most = max(players, key=lambda p: (p.modes[ARENA].games, p.riot_id.casefold()))
    lines.append(f"Most games: **{_name(most.riot_id)}** · {most.modes[ARENA].games}")
    damage = max(players, key=lambda p: (p.modes[ARENA].best_damage or (0,))[0])
    if damage.modes[ARENA].best_damage:
        value, champion, _at = damage.modes[ARENA].best_damage
        lines.append(f"Most damage in a game: **{value:,}** · {_name(damage.riot_id)} · {champion}")
    if top.modes[ARENA].firsts >= 20:
        lines.append(f"-# *{top.modes[ARENA].firsts} crowns. The Arena has a new landlord.*")
    return "\n".join(lines)


def format_group_cards(wrapped, commands_channel_id=None):
    """The group post: a list of cards, each a list of text sections."""
    match_seconds = sum(duration for _mode, duration in wrapped.matches.values())
    hours = _hours(match_seconds)
    days = round(hours / 24)
    ranked_games = len(_mode_matches(wrapped, RANKED))
    champs = Counter()
    for player in wrapped.players.values():
        for champ, (count, _wins) in player.ranked.champions.items():
            champs[champ] += count
    mode_counts = [
        f"**{count:,}** {label}"
        for count, label in (
            (ranked_games, "ranked"),
            (len(_mode_matches(wrapped, ARAM)), "ARAM"),
            (len(_mode_matches(wrapped, ARENA)), "Arena"),
            (len(_mode_matches(wrapped, OTHER)), "other"),
        )
        if count
    ]
    headline = [
        f"🎁 **Poro Wrapped {wrapped.year}**\n-# 1 Jan to 31 Dec · all modes · {len(wrapped.players)} players",
        f"{_bold_count(len(wrapped.matches), 'game')} · **{hours:,}** hours together ({days} days)\n" + " · ".join(mode_counts),
    ]
    if champs:
        champ, count = sorted(champs.items(), key=lambda item: (-item[1], item[0]))[0]
        headline[-1] += f"\nMost played in ranked: **{champ}**, {_plural(count, 'game')}"
    if days >= 7:
        headline.append(f"-# *{days} days of League. The poros would like a word about your sleep schedule.*")
    cards = [headline]

    awards = compute_awards(wrapped)
    if awards:
        cards.append(["🏆 **The Poro Awards**\n-# ranked games unless noted · 20+ games to qualify"]
                     + [_award_block(award) for award in awards])
    record_lines = _year_record_lines(wrapped)
    if record_lines:
        cards.append(["📜 **Ranked records of the year**", "\n".join(record_lines)])
    mode_sections = [section for section in (_aram_section(wrapped), _arena_section(wrapped)) if section]
    if mode_sections:
        cards.append(mode_sections)

    standings = []
    for player in sorted(wrapped.players.values(), key=lambda p: p.riot_id.casefold()):
        best = _best_rank(wrapped.ranks.get(player.riot_id))
        if best is not None and player.ranked.games:
            standings.append((best[1], player.riot_id, _rank_text(best[0])))
    final = ["🏁 **Where everyone ended up**"]
    if standings:
        standings.sort(key=lambda item: (-item[0][0], -item[0][1], item[1].casefold()))
        final.append("\n".join(f"**{text}** · {_name(riot_id)}" for _level, riot_id, text in standings))
    where = f"<#{commands_channel_id}>" if commands_channel_id else "this channel"
    final.append(f"-# Your own year: `!wrapped Name` in {where} · open until 31 Jan")
    cards.append(final)
    return cards


# --- Personal card ------------------------------------------------------------


def _main_label(player):
    """"Support main" from ranked roles, or "Arena main" when another mode is most of the year."""
    if player.ranked.games >= MIN_MODE_RATE_GAMES and player.roles:
        role = player.roles.most_common(1)[0][0]
        return f"{ROLE_LABELS.get(role, role.title())} main"
    for mode, label in ((ARENA, "Arena"), (ARAM, "ARAM")):
        if mode in player.modes and player.modes[mode].games >= player.games / 2:
            return f"{label} main"
    return None


def _title(player):
    ranked = player.ranked
    if player.games and player.night_games / player.games >= 0.15:
        return "The Night Owl"
    top = ranked.top_champions(1)
    if ranked.games >= MIN_AWARD_GAMES and top and top[0][1][0] / ranked.games >= 0.25:
        return f"The {top[0][0]} Specialist"
    if len(player.champions) >= 80:
        return "The Champion Collector"
    if ARENA in player.modes and player.modes[ARENA].games >= player.games / 2:
        return "The Gladiator"
    if ARAM in player.modes and player.modes[ARAM].games >= player.games / 2:
        return "The Bridge Troll"
    if player.roles:
        return ROLE_TITLES.get(player.roles.most_common(1)[0][0], "The Poro Enthusiast")
    return "The Poro Enthusiast"


def _ping_banter(player):
    if not player.ranked.games:
        return None
    per_game = player.pings / player.ranked.games
    if per_game >= 25:
        return f"{player.pings:,} pings. Your teammates have filed a noise complaint."
    if per_game < 8:
        return f"{player.pings:,} pings all year. The quiet type. We respect it."
    return None


def _year_highs(wrapped, riot_id, count=3):
    """The player's best ranked games of the year, by their place in the group.

    Outright bests come first; a value three or more players share isn't a high, and
    a handful of ranked games isn't enough to have any.
    """
    book = wrapped.book
    player = wrapped.players.get(riot_id)
    if player is None or player.ranked.games < MIN_MODE_RATE_GAMES:
        return []
    scored = []
    for index, category in enumerate(CATEGORIES):
        holders = book.bests.get(category.key, {})
        holder = holders.get(riot_id)
        if holder is None or not holder.value or category.personal != "best":
            continue
        rank = book.group_rank(category.key, riot_id)
        shared = sum(1 for other in holders.values() if other.value == holder.value)
        if rank <= MAX_HIGH_RANK and shared < 3:
            scored.append((rank, shared, index, category, holder))
    scored.sort(key=lambda item: item[:3])
    lines = []
    for rank, _shared, _index, category, holder in scored[:count]:
        champion = f" on {holder.champion}" if holder.champion else ""
        place = "best in the group this year" if rank == 1 else f"#{rank} in the group this year"
        lines.append(f"{category.text(holder.value)} {category.label_for(holder.value)}{champion} · {place}")
    return lines


def format_personal_sections(wrapped, riot_id):
    name = _name(riot_id)
    player = wrapped.players.get(riot_id)
    if player is None:
        return [f"🎁 **{name}** · Wrapped {wrapped.year}", f"No games recorded in {wrapped.year} yet."]
    subtitle = []
    main = _main_label(player)
    if main:
        subtitle.append(main)
    ranks = _ranks_text(wrapped.ranks.get(riot_id))
    if ranks:
        subtitle.append(ranks)
    header = f"🎁 **{name}** · Wrapped {wrapped.year}"
    if subtitle:
        header += "\n-# " + " · ".join(subtitle)
    busiest_day, busiest = max(player.days.items(), key=lambda item: (item[1], -item[0].toordinal()))
    overview = (
        f"{_bold_count(player.games, 'game')} · **{_hours(player.seconds):,}** hours · **{_pct(player.wins, player.games)}** wins\n"
        f"{_bold_count(len(player.champions), 'champion')} · busiest day {_bold_count(busiest, 'game')} ({_day(busiest_day)})"
    )
    sections = [header, overview]

    ranked = player.ranked
    if ranked.games:
        lines = [
            "**Ranked**",
            f"{_bold_count(ranked.games, 'game')} · **{_pct(ranked.wins, ranked.games)}** wins · "
            f"**{ranked.kills:,} / {ranked.deaths:,} / {ranked.assists:,}** KDA",
            _champ_line(ranked),
        ]
        partners = []
        for (a, b), (games, wins) in wrapped.duos.items():
            if player.riot_id in (wrapped.names.get(a), wrapped.names.get(b)):
                mate = wrapped.names.get(b if wrapped.names.get(a) == player.riot_id else a)
                if mate:
                    partners.append((mate, games, wins))
        if partners:
            most = max(partners, key=lambda p: (p[1], p[2]))
            text = f"Most games with **{_name(most[0])}** ({most[1]})"
            rated = [p for p in partners if p[1] >= MIN_MODE_RATE_GAMES]
            if rated:
                best = max(rated, key=lambda p: (p[2] / p[1], p[1]))
                text += f" · best with **{_name(best[0])}** ({_pct(best[2], best[1])})"
            lines.append(text)
        sections.append("\n".join(lines))

        highs = _year_highs(wrapped, riot_id)
        if highs:
            sections.append("**Highs**\n" + "\n".join(highs))
        lows = []
        loss_run = wrapped.book.streaks["loss"].get(riot_id)
        if loss_run is not None and loss_run.value >= 3:
            lows.append(f"{int(loss_run.value)} ranked losses in a row ({loss_run.at.astimezone(wrapped.tz).strftime('%b')})")
        if player.ranked_loss_days:
            day, losses = max(player.ranked_loss_days.items(), key=lambda item: (item[1], -item[0].toordinal()))
            lows.append(f"{losses} ranked losses on {_day(day)}")
        if _hours(player.dead_seconds) >= 1:
            lows.append(f"{_hours(player.dead_seconds):,} hours spent dead in ranked")
        if lows:
            sections.append("**Lows**\n" + "\n".join(lows))

    for mode, label in ((ARAM, "❄️ **ARAM**"), (ARENA, "⚔️ **Arena**")):
        totals = player.modes.get(mode)
        if not totals:
            continue
        line = f"{_bold_count(totals.games, 'game')} · **{_pct(totals.wins, totals.games)}** wins"
        if mode == ARENA and totals.placement_games:
            line += f" · **{totals.firsts}** 1st places · avg **{totals.placement_sum / totals.placement_games:.1f}**"
        lines = [label, line, _champ_line(totals, 3)]
        best = totals.best_damage if mode == ARENA else totals.best_kills
        if best:
            value, champion, _at = best
            what = "damage" if mode == ARENA else "kills"
            lines.append(f"Best game: **{value:,}** {what} on {champion}")
        sections.append("\n".join(lines))

    footer = [f"Your title: **{_title(player)}**"]
    banter = _ping_banter(player)
    if banter:
        footer.append(f"-# *{banter}*")
    sections.append("\n".join(footer))
    return sections


# --- Cards --------------------------------------------------------------------


def build_wrapped_views(sections, accent=WRAPPED_ACCENT):
    """Components V2 cards: sections split by separators, a new message when one gets long."""
    views, current, size = [], [], 0
    for section in sections:
        if current and size + len(section) > MESSAGE_CHAR_BUDGET:
            views.append(current)
            current, size = [], 0
        current.append(section)
        size += len(section)
    if current:
        views.append(current)
    built = []
    for chunk in views:
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=accent)
        for index, section in enumerate(chunk):
            if index:
                container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(discord.ui.TextDisplay(section))
        view.add_item(container)
        built.append(view)
    return built


PLAIN_MESSAGE_LIMIT = 2_000


def text_chunks(sections, limit=PLAIN_MESSAGE_LIMIT):
    """Join sections into plain messages under Discord's limit, splitting between sections."""
    chunks, current = [], ""
    for section in sections:
        section = section[:limit]
        joined = f"{current}\n\n{section}" if current else section
        if len(joined) > limit:
            chunks.append(current)
            joined = section
        current = joined
    if current:
        chunks.append(current)
    return chunks


async def send_sections(channel, sections, *, accent, log, what):
    """Send as cards; a rejected card goes out as plain text instead."""
    for view in build_wrapped_views(sections, accent):
        try:
            await channel.send(view=view)
        except discord.HTTPException as exc:
            log(f"[wrapped] Discord rejected the {what} card (HTTP {getattr(exc, 'status', '?')}); sending it as plain text.")
            texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
            for chunk in text_chunks(texts):
                await channel.send(chunk)


# --- Posting task -------------------------------------------------------------


async def post_wrapped_once(
    *, db_iter_wrapped_rows, db_load_all_ranked_states, db_get_state, db_set_state, resolve_channel, channel_id, tz,
    now_utc, log, commands_channel_id=None,
):
    """One check: post the group Wrapped once its moment has come, once per year."""
    year, post_at, closes_at = wrapped_window(now_utc, tz)
    if now_utc < post_at:
        return "waiting"
    if now_utc >= closes_at:
        return "closed"
    if await asyncio.to_thread(db_get_state, WRAPPED_STATE_KEY) == str(year):
        return "already_posted"
    channel = await resolve_channel(channel_id)
    if channel is None:
        log(f"[wrapped] Channel {channel_id} not available; retrying next check.")
        return "no_channel"
    wrapped = await asyncio.to_thread(load_wrapped, db_iter_wrapped_rows, db_load_all_ranked_states, year, tz)
    cards = format_group_cards(wrapped, commands_channel_id if commands_channel_id != channel_id else None)
    # Mark the year first: a failed send must not repost the whole thing every check.
    await asyncio.to_thread(db_set_state, WRAPPED_STATE_KEY, str(year))
    for card in cards:
        await send_sections(channel, card, accent=WRAPPED_ACCENT, log=log, what="group Wrapped")
    log(
        f"[wrapped] Posted Wrapped {year} in channel {channel_id}: cards={len(cards)} games={len(wrapped.matches)} "
        f"players={len(wrapped.players)}."
    )
    return "posted"


async def run_wrapped_loop(
    *,
    db_iter_wrapped_rows,
    db_load_all_ranked_states,
    db_get_state,
    db_set_state,
    resolve_channel,
    channel_id,
    tz,
    log,
    commands_channel_id=None,
    startup_delay_seconds=WRAPPED_STARTUP_DELAY_SECONDS,
    interval_seconds=WRAPPED_CHECK_SECONDS,
    max_passes=None,
    now=lambda: datetime.now(timezone.utc),
):
    """Background task: never raises, so a failure can't affect the workers."""
    await asyncio.sleep(startup_delay_seconds)
    passes = 0
    while max_passes is None or passes < max_passes:
        passes += 1
        try:
            await post_wrapped_once(
                db_iter_wrapped_rows=db_iter_wrapped_rows,
                db_load_all_ranked_states=db_load_all_ranked_states,
                db_get_state=db_get_state,
                db_set_state=db_set_state,
                resolve_channel=resolve_channel,
                channel_id=channel_id,
                tz=tz,
                now_utc=now(),
                log=log,
                commands_channel_id=commands_channel_id,
            )
        except Exception as exc:
            log(f"[wrapped] Wrapped check failed; retrying next check: {type(exc).__name__}: {exc}")
        if max_passes is None or passes < max_passes:
            await asyncio.sleep(interval_seconds)
