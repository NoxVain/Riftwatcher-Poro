"""`!wrapped Name`: one player's Poro Wrapped card.

Open from the group post (31 December, 18:00) until the end of January. Admins can run
it any time, and `!wrapped` alone shows them the group post, to preview the year so far.
Read-only: pages through the year's rows in `player_match_stats` (src/wrapped.py).
"""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.commands.permissions import has_admin_access
from src.constants import WRAPPED_COMMAND
from src.discord_text import format_user_error
from src.player_analysis import resolve_tracked_player
from src.wrapped import (
    WRAPPED_ACCENT,
    WRAPPED_PERSONAL_ACCENT,
    format_group_cards,
    format_personal_sections,
    load_wrapped,
    send_sections,
    wrapped_window,
)

PREVIEW_NOTE = "-# Preview for admins: the year so far. Everyone can use `!wrapped Name` from 31 Dec, 18:00."


def _now():
    return datetime.now(timezone.utc)


def _is_wrapped_command(content_lower):
    prefix = WRAPPED_COMMAND.casefold()
    return content_lower == prefix or content_lower.startswith(f"{prefix} ")


async def handle_wrapped_commands(ctx):
    if not _is_wrapped_command(ctx.content_lower):
        return False
    channel = ctx.message.channel
    if not ctx.db_enabled or ctx.db_iter_wrapped_rows is None:
        await channel.send("Wrapped needs the database, which isn't configured.")
        return True

    tz = ZoneInfo(ctx.report_timezone_name)
    now_utc = _now()
    year, post_at, closes_at = wrapped_window(now_utc, tz)
    is_open = post_at <= now_utc < closes_at
    is_admin = has_admin_access(ctx.message.author, ctx.admin_role_id)
    query = ctx.content[len(WRAPPED_COMMAND):].strip()
    if not is_open and not is_admin:
        await channel.send(
            f"Poro Wrapped {year} opens on 31 Dec at {post_at:%H:%M}. See you then."
            if now_utc < post_at else f"Poro Wrapped {year} has closed. See you next December."
        )
        return True
    if not query and not is_admin:
        await channel.send("Use `!wrapped Name` (or `Name#Tag`) to see a player's year.")
        return True

    riot_id = None
    try:
        if query:
            tracked = [(rid, puuid) for rid, puuid in await asyncio.to_thread(ctx.db_load_tracked_puuids) if puuid]
            match, error = resolve_tracked_player(query, tracked)
            if error:
                await channel.send(error)
                return True
            riot_id = match[0]
        wrapped = await asyncio.to_thread(
            load_wrapped, ctx.db_iter_wrapped_rows, ctx.db_load_all_ranked_states, year, tz, end=now_utc
        )
    except Exception as exc:
        await channel.send(format_user_error("Wrapped failed", exc, ctx.request_id_context.get()))
        ctx.log(f"[wrapped] Lookup failed for {query!r}: {type(exc).__name__}: {exc}")
        return True

    if riot_id:
        cards = [format_personal_sections(wrapped, riot_id)]
        accent, what = WRAPPED_PERSONAL_ACCENT, "personal Wrapped"
    else:
        cards = format_group_cards(wrapped)
        accent, what = WRAPPED_ACCENT, "group Wrapped"
    if not is_open:
        cards[0][0] += f"\n{PREVIEW_NOTE}"
    for sections in cards:
        await send_sections(channel, sections, accent=accent, log=ctx.log, what=what)
    ctx.log(
        f"[wrapped] Sent {'Wrapped ' + str(year) + ' for ' + riot_id if riot_id else 'group Wrapped ' + str(year)}"
        f"{' (admin preview)' if not is_open else ''}: games={len(wrapped.matches)} players={len(wrapped.players)}."
    )
    return True
