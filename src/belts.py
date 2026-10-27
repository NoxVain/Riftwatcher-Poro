"""Weekly title belts: posted once when a report week closes, in the events channel.

Pure logic over one week of `player_match_stats` rows (ranked Solo/Duo, Flex and 5v5,
no remakes), plus a background loop that posts the card. Each belt goes to the best
rate (per minute or per game) among players with at least MIN_BELT_GAMES games; ties
share the belt. Holders are stored in `bot_state`, so a belt held again the next week
is marked as defended. No Riot requests.
"""

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

import discord

BELT_QUEUE_IDS = (420, 440, 710)
MIN_BELT_GAMES = 3
BELTS_STATE_KEY = "weekly_belts_state"
# Wait after the week closes so the last games are recapped and synced into the stats table.
BELTS_POST_DELAY = timedelta(minutes=20)
BELTS_CHECK_SECONDS = 600
# First check after startup: lets a stats rebuild (STATS_VERSION bump) finish first.
BELTS_STARTUP_DELAY_SECONDS = 900
BELTS_ACCENT = 0xF0B232

GLORY = "Glory"
SHAME = "Shame"


def _per_minute(column):
    def rate(totals):
        value, _games, seconds = totals[column]
        return value / (seconds / 60) if seconds else None

    return rate


def _per_game(column):
    def rate(totals):
        value, games, _seconds = totals[column]
        return value / games if games else None

    return rate


def _dead_share(totals):
    value, _games, seconds = totals["time_dead_seconds"]
    return value / seconds if seconds else None


@dataclass(frozen=True)
class Belt:
    key: str
    emoji: str
    name: str
    section: str
    rate: object
    # (totals, rate) -> the belt's number as it reads on the card
    text: object


BELTS = (
    Belt("damage_king", "👑", "Damage King", GLORY, _per_minute("damage_to_champions"),
         lambda t, r: f"**{r:,.0f}** damage/min"),
    Belt("vision_dad", "👁️", "Vision Dad", GLORY, _per_minute("vision_score"),
         lambda t, r: f"**{r:.2f}** vision/min"),
    Belt("objective_goblin", "🏰", "Objective Goblin", GLORY, _per_game("objectives"),
         lambda t, r: f"**{r:.1f}** objectives a game"),
    Belt("bodyguard", "🛡️", "Bodyguard", GLORY, _per_game("ally_saves"),
         lambda t, r: f"saved an ally from death **{t['ally_saves'][0]:,.0f}** times"),
    Belt("outnumbered", "⚔️", "Outnumbered, not outplayed", GLORY, _per_game("outnumbered_kills"),
         lambda t, r: f"**{t['outnumbered_kills'][0]:,.0f}** kills while outnumbered"),
    Belt("matrix", "🕺", "Matrix", GLORY, _per_minute("skillshots_dodged"),
         lambda t, r: f"**{r:.1f}** skillshots dodged/min"),
    Belt("backseat_gamer", "📣", "Backseat Gamer", SHAME, _per_game("pings"),
         lambda t, r: f"**{r:.0f}** pings a game"),
    Belt("grey_screen", "🪦", "Grey Screen Enjoyer", SHAME, _dead_share,
         lambda t, r: f"dead for **{r * 100:.0f}%** of every game"),
)

_SUMMED_COLUMNS = (
    "damage_to_champions",
    "vision_score",
    "ally_saves",
    "outnumbered_kills",
    "skillshots_dodged",
    "pings",
    "time_dead_seconds",
)


def _objectives(row):
    if row.get("turret_takedowns") is None:
        return None
    return sum(
        row.get(column) or 0
        for column in ("turret_takedowns", "dragon_takedowns", "baron_takedowns", "herald_takedowns")
    )


def _player_totals(rows):
    """riot_id -> {"games": n, column: [sum, games with a value, seconds of those games]}."""
    totals = {}
    for row in rows:
        if row.get("queue_id") not in BELT_QUEUE_IDS or row.get("remake"):
            continue
        player = totals.setdefault(row["riot_id"], defaultdict(lambda: [0, 0, 0]))
        player["games"][0] += 1
        seconds = int(row.get("duration_seconds") or 0)
        values = {column: row.get(column) for column in _SUMMED_COLUMNS}
        values["objectives"] = _objectives(row)
        for column, value in values.items():
            if value is None:
                continue
            player[column][0] += value
            player[column][1] += 1
            player[column][2] += seconds
    return totals


@dataclass
class Award:
    belt: Belt
    holders: list
    text: str
    # riot_id -> consecutive weeks held, this week included
    streaks: dict = field(default_factory=dict)


@dataclass
class WeekBelts:
    start: datetime
    end: datetime
    games: int
    players: int
    awards: list


def compute_week_belts(rows, start, end):
    totals = _player_totals(rows)
    qualified = {riot_id: t for riot_id, t in totals.items() if t["games"][0] >= MIN_BELT_GAMES}
    awards = []
    for belt in BELTS:
        rates = {riot_id: belt.rate(t) for riot_id, t in qualified.items()}
        rates = {riot_id: rate for riot_id, rate in rates.items() if rate is not None and rate > 0}
        if not rates:
            continue
        best = max(rates.values())
        holders = sorted((riot_id for riot_id, rate in rates.items() if rate == best), key=str.casefold)
        awards.append(Award(belt, holders, belt.text(qualified[holders[0]], best)))
    return WeekBelts(
        start=start,
        end=end,
        games=sum(t["games"][0] for t in totals.values()),
        players=len(totals),
        awards=awards,
    )


def parse_belts_state(raw):
    """{"week_start": iso, "holders": {belt_key: {riot_id: weeks}}}, or None."""
    try:
        state = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("holders"), dict):
        return None
    return state


def apply_defended(week, previous_state):
    """Fill each award's streaks and return the holders to store for this week.

    A streak continues only from the week right before this one; a missed week,
    a lost belt or a belt nobody won resets it.
    """
    previous = {}
    if previous_state and previous_state.get("week_start") == (week.start - timedelta(days=7)).isoformat():
        previous = previous_state.get("holders") or {}
    holders = {}
    for award in week.awards:
        before = previous.get(award.belt.key) or {}
        award.streaks = {riot_id: int(before.get(riot_id, 0)) + 1 for riot_id in award.holders}
        holders[award.belt.key] = award.streaks
    return holders


def last_closed_week(now_utc, tz, day_start_hour):
    """(start, end) of the newest report week (Monday `day_start_hour` to Monday) that has ended."""
    report_day = (now_utc.astimezone(tz) - timedelta(hours=day_start_hour)).date()
    this_monday = report_day - timedelta(days=report_day.weekday())
    end = datetime.combine(this_monday, time(day_start_hour), tzinfo=tz)
    start = datetime.combine(this_monday - timedelta(days=7), time(day_start_hour), tzinfo=tz)
    return start, end


def _name(riot_id):
    return discord.utils.escape_markdown(riot_id.split("#", 1)[0])


def _defended(streak):
    if streak < 2:
        return ""
    return "🔁 defended" if streak == 2 else f"🔁 defended ×{streak - 1}"


def _award_line(award):
    parts = [f"{award.belt.emoji} **{award.belt.name}**"]
    if len(award.holders) == 1:
        parts.append(_name(award.holders[0]))
        parts.append(award.text)
        defended = _defended(award.streaks.get(award.holders[0], 1))
        if defended:
            parts.append(defended)
    else:
        names = []
        for riot_id in award.holders:
            defended = _defended(award.streaks.get(riot_id, 1))
            names.append(f"{_name(riot_id)} ({defended})" if defended else _name(riot_id))
        parts.append(" and ".join(names))
        parts.append(award.text)
    return " · ".join(parts)


def _date_range(start, end):
    last_day = end.date()
    if start.month == last_day.month:
        return f"{start.day}–{last_day.day} {last_day:%b}"
    return f"{start.day} {start:%b} – {last_day.day} {last_day:%b}"


def format_belts_sections(week):
    header = (
        f"🎖️ **Weekly title belts** · {_date_range(week.start, week.end)}\n"
        f"-# {week.games:,} ranked games · {week.players} players · {MIN_BELT_GAMES}+ games to qualify"
    )
    sections = [header]
    for section in (GLORY, SHAME):
        lines = [_award_line(award) for award in week.awards if award.belt.section == section]
        if lines:
            sections.append(f"**{section}**\n" + "\n".join(lines))
    sections.append(
        "-# Ranked Solo/Duo, Flex and 5v5 · the best rate wins, ties share the belt · 🔁 held last week too"
    )
    return sections


def build_belts_view(sections):
    """Components V2 card: one container, sections split by separators."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=BELTS_ACCENT)
    for index, section in enumerate(sections):
        if index:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(section))
    view.add_item(container)
    return view


async def post_weekly_belts_once(
    *, db_load_belt_rows, db_get_state, db_set_state, resolve_channel, channel_id, tz, day_start_hour, now_utc, log
):
    """One check: post the last closed week's belts if they're due and not posted yet.

    The first run only stores that week's holders (no post), so a deploy mid-week
    doesn't post a late card. Returns what happened, for logs and tests.
    """
    start, end = last_closed_week(now_utc, tz, day_start_hour)
    state = parse_belts_state(await asyncio.to_thread(db_get_state, BELTS_STATE_KEY))
    if state is not None and state.get("week_start") == start.isoformat():
        return "already_posted"
    if now_utc < end + BELTS_POST_DELAY:
        return "waiting"
    rows = await asyncio.to_thread(db_load_belt_rows, start, end)
    week = compute_week_belts(rows, start, end)
    holders = apply_defended(week, state)
    new_state = json.dumps({"week_start": start.isoformat(), "holders": holders})
    week_label = f"{start.date()}..{end.date()}"
    if state is None:
        await asyncio.to_thread(db_set_state, BELTS_STATE_KEY, new_state)
        log(f"[belts] First run: stored holders for week {week_label} without posting; belts post from next week.")
        return "baseline"
    if not week.awards:
        await asyncio.to_thread(db_set_state, BELTS_STATE_KEY, new_state)
        log(f"[belts] No belts for week {week_label}: nobody played {MIN_BELT_GAMES} ranked games.")
        return "empty"
    channel = await resolve_channel(channel_id)
    if channel is None:
        log(f"[belts] Channel {channel_id} not available; retrying next check.")
        return "no_channel"
    sections = format_belts_sections(week)
    # Mark the week first: a failed send must not repost the card every check.
    await asyncio.to_thread(db_set_state, BELTS_STATE_KEY, new_state)
    try:
        await channel.send(view=build_belts_view(sections))
    except discord.HTTPException as exc:
        log(f"[belts] Discord rejected the belts card (HTTP {getattr(exc, 'status', '?')}); sending it as plain text.")
        await channel.send("\n\n".join(sections))
    defended = sum(1 for award in week.awards for streak in award.streaks.values() if streak >= 2)
    log(
        f"[belts] Posted weekly belts for {week_label} in channel {channel_id}: belts={len(week.awards)} "
        f"defended={defended} games={week.games} players={week.players}."
    )
    return "posted"


async def run_weekly_belts_loop(
    *,
    db_load_belt_rows,
    db_get_state,
    db_set_state,
    resolve_channel,
    channel_id,
    tz,
    day_start_hour,
    log,
    startup_delay_seconds=BELTS_STARTUP_DELAY_SECONDS,
    interval_seconds=BELTS_CHECK_SECONDS,
    max_passes=None,
    now=lambda: datetime.now(timezone.utc),
):
    """Background task: never raises, so a failure can't affect the workers."""
    await asyncio.sleep(startup_delay_seconds)
    passes = 0
    while max_passes is None or passes < max_passes:
        passes += 1
        try:
            await post_weekly_belts_once(
                db_load_belt_rows=db_load_belt_rows,
                db_get_state=db_get_state,
                db_set_state=db_set_state,
                resolve_channel=resolve_channel,
                channel_id=channel_id,
                tz=tz,
                day_start_hour=day_start_hour,
                now_utc=now(),
                log=log,
            )
        except Exception as exc:
            log(f"[belts] Weekly belts check failed; retrying next check: {type(exc).__name__}: {exc}")
        if max_passes is None or passes < max_passes:
            await asyncio.sleep(interval_seconds)
