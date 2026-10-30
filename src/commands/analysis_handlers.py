"""`!analyze Name#Tag [months] [solo|flex]`: strengths and tips from recent ranked games (3 months by default).

Read-only: one indexed query on `player_match_stats` plus one per-role aggregate, so it
needs no Riot requests and answers in milliseconds.
"""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord

from src.constants import ANALYZE_ALIAS, ANALYZE_COMMAND
from src.discord_text import format_user_error, split_message_text
from src.player_analysis import (
    DEFAULT_MONTHS,
    QUEUES,
    analysis_start,
    analyze_player,
    build_analysis_view,
    format_analysis_text,
    parse_analyze_args,
    resolve_tracked_player,
)


def _now():
    return datetime.now(timezone.utc)


def _matched_command(content_lower):
    for command in (ANALYZE_COMMAND, ANALYZE_ALIAS):
        prefix = command.casefold()
        if content_lower == prefix or content_lower.startswith(f"{prefix} "):
            return command
    return None


async def handle_analysis_commands(ctx):
    command = _matched_command(ctx.content_lower)
    if command is None:
        return False
    channel = ctx.message.channel
    raw = ctx.content[len(command):].strip()
    if not raw:
        await channel.send(
            f"Usage: `{ANALYZE_COMMAND} Name#Tag [months] [solo|flex]` "
            f"(last {DEFAULT_MONTHS} months of Solo/Duo unless you say otherwise; 1-12 months)."
        )
        return True
    if not ctx.db_enabled or ctx.db_load_player_analysis_rows is None:
        await channel.send("Player analysis needs the database, which isn't configured.")
        return True

    query = raw
    try:
        tracked = [(riot_id, puuid) for riot_id, puuid in await asyncio.to_thread(ctx.db_load_tracked_puuids) if puuid]
        try:
            query, queue, months = parse_analyze_args(raw, tracked)
        except ValueError as exc:
            await channel.send(str(exc))
            return True
        match, error = resolve_tracked_player(query, tracked)
        if error:
            await channel.send(error)
            return True
        riot_id, puuid = match
        # One end time for both reads, so a game stored in between can't skew one side.
        now = _now()
        since = analysis_start(now, months)
        games = await asyncio.to_thread(ctx.db_load_player_analysis_rows, puuid, since, until=now)
        baselines = await asyncio.to_thread(
            ctx.db_load_role_group_totals, QUEUES[queue]["queue_id"], since, until=now
        )
        # Pure but not free: keep it off the event loop.
        analysis = await asyncio.to_thread(
            analyze_player,
            riot_id=riot_id,
            games=games,
            queue=queue,
            role_baselines=baselines,
            names_by_puuid={puuid: name for name, puuid in tracked},
            now=now,
            tz=ZoneInfo(ctx.report_timezone_name),
            day_start_hour=ctx.report_day_start_hour,
            months=months,
        )
    except Exception as exc:
        await channel.send(format_user_error("Analysis failed", exc, ctx.request_id_context.get()))
        ctx.log(f"[analyze] Analysis failed for {query!r}: {type(exc).__name__}: {exc}")
        return True
    try:
        await channel.send(view=build_analysis_view(analysis))
    except discord.HTTPException as exc:
        ctx.log(f"[analyze] Discord rejected the analysis card (HTTP {getattr(exc, 'status', '?')}); sending it as plain text.")
        for chunk in split_message_text(format_analysis_text(analysis)):
            await channel.send(chunk)
    ctx.log(
        f"[analyze] Sent {queue} analysis for {riot_id}: months={months} games={analysis.games} "
        f"strengths={len(analysis.strengths)} tips={len(analysis.tips)}."
    )
    return True
