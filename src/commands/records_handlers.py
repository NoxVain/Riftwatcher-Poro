"""`!records [Name#Tag]`: the group's Hall of Fame, or one player's personal bests.

Read-only: pages through ranked rows in `player_match_stats` (src/records.py), so it
needs no Riot requests.
"""

import asyncio

import discord

from src.constants import RECORDS_COMMAND
from src.discord_text import format_user_error
from src.player_analysis import resolve_tracked_player
from src.records import build_records_view, format_group_sections, format_personal_sections, load_record_book


def _is_records_command(content_lower):
    prefix = RECORDS_COMMAND.casefold()
    return content_lower == prefix or content_lower.startswith(f"{prefix} ")


async def handle_records_commands(ctx):
    if not _is_records_command(ctx.content_lower):
        return False
    channel = ctx.message.channel
    if not ctx.db_enabled or ctx.db_iter_record_rows is None:
        await channel.send("Records need the database, which isn't configured.")
        return True

    query = ctx.content[len(RECORDS_COMMAND):].strip()
    riot_id = None
    try:
        if query:
            tracked = [(rid, puuid) for rid, puuid in await asyncio.to_thread(ctx.db_load_tracked_puuids) if puuid]
            match, error = resolve_tracked_player(query, tracked)
            if error:
                await channel.send(error)
                return True
            riot_id = match[0]
        book = await asyncio.to_thread(load_record_book, ctx.db_iter_record_rows)
    except Exception as exc:
        await channel.send(format_user_error("Records failed", exc, ctx.request_id_context.get()))
        ctx.log(f"[records] Lookup failed for {query!r}: {type(exc).__name__}: {exc}")
        return True

    sections = format_personal_sections(book, riot_id) if riot_id else format_group_sections(book)
    try:
        await channel.send(view=build_records_view(sections))
    except discord.HTTPException as exc:
        ctx.log(f"[records] Discord rejected the records card (HTTP {getattr(exc, 'status', '?')}); sending it as plain text.")
        await channel.send("\n\n".join(sections))
    ctx.log(f"[records] Sent {'personal bests for ' + riot_id if riot_id else 'group records'}: games={book.games}.")
    return True
