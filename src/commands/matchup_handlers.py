"""`!matchup Name Champion [months] [role] [solo|flex]`: one player's games on one champion and their lane matchups.

Read-only: one indexed query on `player_match_stats` for the player and one small query for
champion names, so it needs no Riot requests.
"""

import asyncio
from datetime import datetime, timezone

import discord

from src.constants import MATCHUP_COMMAND
from src.discord_text import format_user_error, split_message_text
from src.matchup import (
    MIN_MATCHUP_GAMES,
    USAGE,
    build_matchup_report,
    build_matchup_view,
    format_matchup_text,
    parse_matchup_args,
    resolve_champion,
    too_few_games_text,
)
from src.player_analysis import ROLE_ARG_NAMES, analysis_start, resolve_tracked_player


def _now():
    return datetime.now(timezone.utc)


async def handle_matchup_commands(ctx):
    prefix = MATCHUP_COMMAND.casefold()
    if ctx.content_lower != prefix and not ctx.content_lower.startswith(f"{prefix} "):
        return False
    channel = ctx.message.channel
    raw = ctx.content[len(MATCHUP_COMMAND):].strip()
    if not raw:
        await channel.send(USAGE)
        return True
    if ctx.db_load_matchup_rows is None or ctx.db_load_champion_names is None:
        await channel.send("Matchups need the database, which isn't configured.")
        return True

    try:
        tracked = [(riot_id, puuid) for riot_id, puuid in await asyncio.to_thread(ctx.db_load_tracked_puuids) if puuid]
        try:
            name, champion_query, queue, months, role = parse_matchup_args(raw, tracked)
        except ValueError as exc:
            await channel.send(str(exc))
            return True
        match, error = resolve_tracked_player(name, tracked)
        if error:
            await channel.send(error)
            return True
        champion_names = await asyncio.to_thread(ctx.db_load_champion_names)
        champion_id, error = resolve_champion(champion_query, champion_names)
        if error:
            await channel.send(error)
            return True
        riot_id, puuid = match
        now = _now()
        rows = await asyncio.to_thread(ctx.db_load_matchup_rows, puuid, analysis_start(now, months), until=now)
        report = build_matchup_report(
            riot_id=riot_id, champion_id=champion_id, rows=rows, champion_names=champion_names,
            queue=queue, months=months, role=role,
        )
    except Exception as exc:
        await channel.send(format_user_error("Matchup lookup failed", exc, ctx.request_id_context.get()))
        ctx.log(f"[matchup] Lookup failed for {raw!r}: {type(exc).__name__}: {exc}")
        return True

    scope = f"queue={queue or 'both'} months={months} role={ROLE_ARG_NAMES.get(role, 'all')}"
    if report.games < MIN_MATCHUP_GAMES:
        await channel.send(too_few_games_text(report))
        ctx.log(f"[matchup] Too few games for {riot_id} on {report.champion}: games={report.games} {scope}.")
        return True
    try:
        await channel.send(view=build_matchup_view(report))
    except discord.HTTPException as exc:
        ctx.log(f"[matchup] Discord rejected the matchup card (HTTP {getattr(exc, 'status', '?')}); sending it as plain text.")
        for chunk in split_message_text(format_matchup_text(report)):
            await channel.send(chunk)
    ctx.log(
        f"[matchup] Sent {report.champion} for {riot_id}: games={report.games} {scope} "
        f"matchups={report.qualified_opponents}/{report.opponents}."
    )
    return True
