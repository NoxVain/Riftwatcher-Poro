"""Hall of Fame: group records and personal bests from ranked games.

Pure logic over `player_match_stats` rows (see src/match_stats.py); no Discord, Riot or
DB access. Only ranked games count (Solo/Duo, Flex and ranked 5v5), remakes excluded.

`RecordBook` reads rows in game order and keeps each player's best per category,
their ranked streaks and career totals. The same book drives `!records` (group card),
`!records Name` (personal bests) and the "New group record" lines in match recaps.
"""

from dataclasses import dataclass, field

import discord

RECORD_QUEUE_IDS = (420, 440, 710)
# Don't announce records until the table holds a real history (fresh installs, rebuilds).
MIN_GAMES_FOR_RECAP_RECORDS = 200
RECORDS_ACCENT = 0xF0B232

SECTION_CARRY = "Carry"
SECTION_FARM = "Farm and map"
SECTION_PAIN = "Pain"


def _int_text(value):
    return f"{int(value):,}"


def _duration_text(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass(frozen=True)
class RecordCategory:
    key: str
    label: str
    section: str
    # Row -> value, or None when the game doesn't qualify for this category.
    value: object
    text: object = _int_text
    # Shown on `!records Name`; "best" or "worst" section, or None to leave it out.
    personal: str | None = None

    def phrase(self, value):
        """"34 kills", "longest game 64:46": the record as it reads in a recap line."""
        if self.key == "longest_game":
            return f"{self.label} {self.text(value)}"
        return f"{self.text(value)} {self.label}"


def _minutes(row):
    return (row.get("duration_seconds") or 0) / 60


def _column(name, min_minutes=0):
    def value(row):
        raw = row.get(name)
        if raw is None or _minutes(row) < min_minutes:
            return None
        return raw

    return value


def _deathless_kills(row):
    return row.get("kills") if row.get("deaths") == 0 and row.get("kills") else None


def _cs_per_minute(row):
    minutes = _minutes(row)
    return row["cs"] / minutes if minutes >= 20 and row.get("cs") is not None else None


CATEGORIES = (
    RecordCategory("kills", "kills", SECTION_CARRY, _column("kills"), personal="best"),
    RecordCategory("deathless_kills", "kills without dying", SECTION_CARRY, _deathless_kills),
    RecordCategory("damage", "damage", SECTION_CARRY, _column("damage_to_champions"), personal="best"),
    RecordCategory(
        "damage_share", "of team damage", SECTION_CARRY, _column("damage_share", min_minutes=15),
        text=lambda v: f"{v * 100:.1f}%",
    ),
    RecordCategory("solo_kills", "solo kills", SECTION_CARRY, _column("solo_kills"), personal="best"),
    RecordCategory("cs", "CS", SECTION_FARM, _column("cs"), personal="best"),
    RecordCategory("cs_per_minute", "CS/min", SECTION_FARM, _cs_per_minute, text=lambda v: f"{v:.1f}"),
    RecordCategory("gold", "gold", SECTION_FARM, _column("gold_earned")),
    RecordCategory("vision", "vision score", SECTION_FARM, _column("vision_score"), personal="best"),
    RecordCategory("assists", "assists", SECTION_FARM, _column("assists"), personal="best"),
    RecordCategory("deaths", "deaths", SECTION_PAIN, _column("deaths"), personal="worst"),
    RecordCategory("damage_taken", "damage taken", SECTION_PAIN, _column("damage_taken")),
    RecordCategory(
        "longest_game", "longest game", SECTION_PAIN, _column("duration_seconds"), text=_duration_text
    ),
)
CATEGORIES_BY_KEY = {category.key: category for category in CATEGORIES}


@dataclass
class Holder:
    """One player's best (or the group record) in a category, or one streak."""

    value: float
    riot_id: str
    at: object
    champion: str | None = None
    match_id: str | None = None


@dataclass
class _Career:
    games: int = 0
    wins: int = 0
    seconds: int = 0
    pentakills: int = 0
    run_win: bool | None = None
    run_length: int = 0
    first_at: object = None


@dataclass
class RecordBook:
    # category key -> riot_id -> that player's best
    bests: dict = field(default_factory=dict)
    # "win" / "loss" -> riot_id -> longest ranked streak
    streaks: dict = field(default_factory=lambda: {"win": {}, "loss": {}})
    careers: dict = field(default_factory=dict)
    games: int = 0
    first_at: object = None

    def add(self, row):
        """Add one ranked, non-remake game; rows must come in game order per player."""
        if row.get("queue_id") not in RECORD_QUEUE_IDS or row.get("remake"):
            return
        riot_id = row["riot_id"]
        at = row["game_end_at"]
        self.games += 1
        if self.first_at is None or at < self.first_at:
            self.first_at = at
        for category in CATEGORIES:
            value = category.value(row)
            if value is None:
                continue
            per_player = self.bests.setdefault(category.key, {})
            current = per_player.get(riot_id)
            # Strictly better replaces, so a tie keeps the earlier game.
            if current is None or value > current.value:
                per_player[riot_id] = Holder(value, riot_id, at, row.get("champion_name"), row.get("match_id"))

        career = self.careers.setdefault(riot_id, _Career())
        won = bool(row.get("win"))
        career.games += 1
        career.wins += int(won)
        career.seconds += int(row.get("duration_seconds") or 0)
        career.pentakills += int(row.get("penta_kills") or 0)
        if career.first_at is None:
            career.first_at = at
        career.run_length = career.run_length + 1 if career.run_win == won else 1
        career.run_win = won
        kind = "win" if won else "loss"
        best_run = self.streaks[kind].get(riot_id)
        if best_run is None or career.run_length > best_run.value:
            self.streaks[kind][riot_id] = Holder(career.run_length, riot_id, at)

    def record(self, key):
        """Group record for a category: the best value, the earliest game on a tie."""
        holders = (self.streaks[key] if key in self.streaks else self.bests.get(key, {})).values()
        return min(holders, key=lambda h: (-h.value, h.at), default=None)

    def group_rank(self, key, riot_id):
        """1 + the number of players with a strictly better best, or None."""
        holders = self.streaks[key] if key in self.streaks else self.bests.get(key, {})
        mine = holders.get(riot_id)
        if mine is None:
            return None
        return 1 + sum(1 for holder in holders.values() if holder.value > mine.value)


def build_record_book(rows):
    book = RecordBook()
    for row in rows:
        book.add(row)
    return book


def load_record_book(iter_record_rows, *, before=None, exclude_match_ids=()):
    """Blocking (pages through the stats table); run via asyncio.to_thread."""
    return build_record_book(
        iter_record_rows(RECORD_QUEUE_IDS, before=before, exclude_match_ids=exclude_match_ids)
    )


@dataclass
class RecordBreak:
    category: RecordCategory
    riot_id: str
    value: float
    previous: Holder


def detect_record_breaks(book, matches):
    """Group records broken by new games, in order.

    `matches` is [(match_id, [row, ...])] oldest first; each row is a stats row with
    `riot_id`. Only a value strictly above the record counts; when several tracked
    players in one game pass it, the best of them takes the record. Each match is
    added to the book afterwards, so a later game must beat an earlier one.
    Returns {(match_id, riot_id): [RecordBreak, ...]}.
    """
    breaks = {}
    if book.games < MIN_GAMES_FOR_RECAP_RECORDS:
        for _match_id, rows in matches:
            for row in rows:
                book.add(row)
        return breaks
    for match_id, rows in matches:
        eligible = [row for row in rows if row.get("queue_id") in RECORD_QUEUE_IDS and not row.get("remake")]
        for category in CATEGORIES:
            scored = [(category.value(row), row) for row in eligible]
            scored = [(value, row) for value, row in scored if value is not None]
            if not scored:
                continue
            value, row = max(scored, key=lambda pair: pair[0])
            previous = book.record(category.key)
            if previous is not None and value > previous.value:
                breaks.setdefault((match_id, row["riot_id"]), []).append(
                    RecordBreak(category, row["riot_id"], value, previous)
                )
        for row in rows:
            book.add(row)
    return breaks


def _name(riot_id):
    return discord.utils.escape_markdown(riot_id.split("#", 1)[0])


def _month(at):
    return at.strftime("%b %Y")


def format_record_break(record_break):
    """One recap line per broken record."""
    category = record_break.category
    previous = record_break.previous
    champion = f" on {previous.champion}" if previous.champion else ""
    return (
        f"🏆 **New group record:** {category.phrase(record_break.value)} "
        f"(was {category.text(previous.value)}, {_name(previous.riot_id)}{champion}, {_month(previous.at)})"
    )


def _holder_line(category, holder, *, with_name=True, rank=None):
    parts = [f"**{category.text(holder.value)}** {category.label}"]
    if with_name:
        parts.append(_name(holder.riot_id))
    if holder.champion:
        parts.append(holder.champion)
    parts.append(_month(holder.at))
    if rank == 1:
        parts.append("group record")
    elif rank in (2, 3):
        parts.append(f"#{rank} in the group")
    return " · ".join(parts)


def _streak_line(holder, kind, *, with_name=True, rank=None):
    word = "wins" if kind == "win" else "losses"
    parts = [f"**{int(holder.value)}** ranked {word} in a row"]
    if with_name:
        parts.append(_name(holder.riot_id))
    parts.append(_month(holder.at))
    if rank == 1:
        parts.append("group record")
    elif rank in (2, 3):
        parts.append(f"#{rank} in the group")
    return " · ".join(parts)


def _hours(seconds):
    return f"{round(seconds / 3600):,}"


FOOTER = "-# Ranked Solo/Duo, Flex and 5v5 · remakes excluded"


def format_group_sections(book):
    """Text blocks for the Hall of Fame card; also joined for plain text."""
    if not book.games:
        return ["🏆 **Hall of Fame**", "No ranked games recorded yet."]
    players = len(book.careers)
    header = (
        f"🏆 **Hall of Fame**\n-# {book.games:,} ranked games · {players} players · since {_month(book.first_at)}"
    )
    sections = [header]
    for section in (SECTION_CARRY, SECTION_FARM, SECTION_PAIN):
        lines = []
        for category in CATEGORIES:
            holder = book.record(category.key) if category.section == section else None
            if holder is not None:
                lines.append(_holder_line(category, holder))
        if lines:
            sections.append(f"**{section}**\n" + "\n".join(lines))
    lines = []
    for kind in ("win", "loss"):
        holder = book.record(kind)
        if holder is not None:
            lines.append(_streak_line(holder, kind))
    total_pentas = sum(career.pentakills for career in book.careers.values())
    if total_pentas:
        name, career = max(book.careers.items(), key=lambda item: (item[1].pentakills, -item[1].games))
        lines.append(f"**{career.pentakills}** pentakills · {_name(name)} · group total {total_pentas}")
    name, career = max(book.careers.items(), key=lambda item: item[1].games)
    lines.append(f"**{career.games:,}** games, {_hours(career.seconds)} hours · {_name(name)}")
    sections.append("**Streaks and career**\n" + "\n".join(lines))
    sections.append(f"{FOOTER} · `!records Name` for personal bests")
    return sections


def format_personal_sections(book, riot_id):
    """Text blocks for one player's personal bests, with their place in the group."""
    name = _name(riot_id)
    career = book.careers.get(riot_id)
    if career is None:
        return [f"🏆 **{name}** · personal bests", "No ranked games recorded yet."]
    header = (
        f"🏆 **{name}** · personal bests\n"
        f"-# {career.games:,} ranked games · {career.wins:,} wins · since {_month(career.first_at)}"
    )
    best, worst = [], []
    for category in CATEGORIES:
        holder = book.bests.get(category.key, {}).get(riot_id)
        if category.personal is None or holder is None or not holder.value:
            continue
        line = _holder_line(category, holder, with_name=False, rank=book.group_rank(category.key, riot_id))
        (best if category.personal == "best" else worst).append(line)
    win_run = book.streaks["win"].get(riot_id)
    if win_run is not None:
        best.append(_streak_line(win_run, "win", with_name=False, rank=book.group_rank("win", riot_id)))
    if career.pentakills:
        best.append(f"**{career.pentakills}** pentakill{'s' if career.pentakills != 1 else ''}")
    loss_run = book.streaks["loss"].get(riot_id)
    if loss_run is not None:
        worst.append(_streak_line(loss_run, "loss", with_name=False, rank=book.group_rank("loss", riot_id)))
    sections = [header]
    if best:
        sections.append("**Best**\n" + "\n".join(best))
    if worst:
        sections.append("**Worst**\n" + "\n".join(worst))
    sections.append(f"{FOOTER} · `!records` for the group")
    return sections


def build_records_view(sections):
    """Components V2 card: one container, sections split by separators."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=RECORDS_ACCENT)
    for index, section in enumerate(sections):
        if index:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(section))
    view.add_item(container)
    return view
