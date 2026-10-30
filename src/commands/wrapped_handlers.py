"""`!wrapped Name`: one player's Poro Wrapped card.

Wrapped is a surprise: the command only exists once the group post is out (31 December,
18:00) and until the end of January. Outside that, `!wrapped` gets no reply at all, not
even a wrong-channel pointer, and that goes for admins too. It's left out of `!help`.
Read-only: pages through the year's rows in `player_match_stats` (src/wrapped.py).
"""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.constants import WRAPPED_COMMAND
from src.discord_text import format_user_error
from src.player_analysis import resolve_tracked_player
from src.wrapped import (
    WRAPPED_PERSONAL_ACCENT,
    WRAPPED_STATE_KEY,
    format_personal_sections,
    load_wrapped,
    send_sections,
    wrapped_window,
)


def _now():
    return datetime.now(timezone.utc)


def _is_wrapped_command(content_lower):
    prefix = WRAPPED_COMMAND.casefold()
    return content_lower == prefix or content_lower.startswith(f"{prefix} ")


async def _open_year(ctx):
    """The year whose Wrapped is open now (posted, and before February), or None."""
    tz = ZoneInfo(ctx.report_timezone_name)
    now_utc = _now()
    year, post_at, closes_at = wrapped_window(now_utc, tz)
    if not post_at <= now_utc < closes_at or ctx.db_get_state is None:
        return None
    try:
        posted = await asyncio.to_thread(ctx.db_get_state, WRAPPED_STATE_KEY)
    except Exception as exc:
        ctx.log(f"[wrapped] Could not read {WRAPPED_STATE_KEY}; treating Wrapped as closed: {type(exc).__name__}: {exc}")
        return None
    return year if posted == str(year) else None


async def ignore_closed_wrapped(ctx):
    """True (and silent) for `!wrapped` while Wrapped is closed, before channel routing can mention it."""
    if not _is_wrapped_command(ctx.content_lower) or await _open_year(ctx) is not None:
        return False
    ctx.log("[wrapped] Ignored !wrapped: Wrapped isn't open (not posted yet, or past January).")
    return True


async def handle_wrapped_commands(ctx):
    if not _is_wrapped_command(ctx.content_lower):
        return False
    year = await _open_year(ctx)
    if year is None:
        return True
    channel = ctx.message.channel
    if not ctx.db_enabled or ctx.db_iter_wrapped_rows is None:
        await channel.send("Wrapped needs the database, which isn't configured.")
        return True
    query = ctx.content[len(WRAPPED_COMMAND):].strip()
    if not query:
        await channel.send("Use `!wrapped Name` (or `Name#Tag`) to see a player's year.")
        return True

    try:
        tracked = [(rid, puuid) for rid, puuid in await asyncio.to_thread(ctx.db_load_tracked_puuids) if puuid]
        match, error = resolve_tracked_player(query, tracked)
        if error:
            await channel.send(error)
            return True
        riot_id = match[0]
        tz = ZoneInfo(ctx.report_timezone_name)
        wrapped = await asyncio.to_thread(
            load_wrapped, ctx.db_iter_wrapped_rows, ctx.db_load_all_ranked_states, year, tz
        )
    except Exception as exc:
        await channel.send(format_user_error("Wrapped failed", exc, ctx.request_id_context.get()))
        ctx.log(f"[wrapped] Lookup failed for {query!r}: {type(exc).__name__}: {exc}")
        return True

    await send_sections(
        channel, format_personal_sections(wrapped, riot_id), accent=WRAPPED_PERSONAL_ACCENT, log=ctx.log,
        what="personal Wrapped",
    )
    ctx.log(f"[wrapped] Sent Wrapped {year} for {riot_id}: games={len(wrapped.matches)} players={len(wrapped.players)}.")
    return True
