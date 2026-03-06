from src.constants import (
    ADD_COMMAND,
    BACKFILL_COMMAND,
    DEBUG_PLAYER_COMMAND,
    HEALTH_COMMAND,
    HELP_COMMAND,
    MOOD_COMMAND,
    PROFILE_COMMAND,
    REMOVE_COMMAND,
    RIOT_TEST_COMMAND,
    SCORE_COMMAND,
    STREAK_COMMAND,
    TEST_COMMAND,
    TTS_COMMAND,
    WEEK_COMMAND,
)


def format_help_text(*, report_day_start_hour, daily_channel_id, weekly_channel_id, events_channel_id, match_recap_channel_id=None):
    daily_channel_ref = f"<#{daily_channel_id}>"
    weekly_channel_ref = f"<#{weekly_channel_id}>" if weekly_channel_id else daily_channel_ref
    events_channel_ref = f"<#{events_channel_id}>"
    return (
        "**MoodBot commands**\n"
        f"- `{MOOD_COMMAND}`: Refresh daily scoreboard (run in {daily_channel_ref}).\n"
        f"- `{WEEK_COMMAND}`: Refresh weekly scoreboard for Monday `{report_day_start_hour:02d}:00` -> next Monday "
        f"`{report_day_start_hour:02d}:00` (run/post in {weekly_channel_ref}).\n"
        f"- `{ADD_COMMAND} Name#Tag`: Add a tracked Riot ID (run in {events_channel_ref}).\n"
        f"- `{REMOVE_COMMAND} Name#Tag`: Remove a tracked Riot ID (run in {events_channel_ref}).\n"
        f"- `{DEBUG_PLAYER_COMMAND} Name#Tag`: Show queue/window debug details (run in {events_channel_ref}).\n"
        f"- `{HEALTH_COMMAND}`: Show bot/DB/cache health (run in {events_channel_ref}).\n"
        f"- `{BACKFILL_COMMAND} YYYY-MM-DD YYYY-MM-DD`: Rebuild historical daily stats from cached matches (run in {events_channel_ref}).\n"
        f"- `{SCORE_COMMAND}`: Show how each player's Gamer Score is calculated today (run in {events_channel_ref}).\n"
        f"- `{PROFILE_COMMAND} Name#Tag`: Show player profile summary (run in {events_channel_ref}).\n"
        f"- `{RIOT_TEST_COMMAND}`: Verify Riot API connectivity (run in {events_channel_ref}).\n"
        f"- `{TEST_COMMAND}`: Verify Discord send permissions (run in {events_channel_ref}).\n"
        f"- `{STREAK_COMMAND} Name#Tag`: Manually post current win/loss streak callout (run in {f'<#{match_recap_channel_id}>' if match_recap_channel_id else events_channel_ref}).\n"
        f"- `{TTS_COMMAND} on|off|status`: Toggle streak alert TTS (run in {events_channel_ref} or {f'<#{match_recap_channel_id}>' if match_recap_channel_id else events_channel_ref}).\n"
        f"- `{HELP_COMMAND}`: Show this help (run in {events_channel_ref})."
    )


def is_supported_command(content_lower):
    known_exact = {
        TEST_COMMAND.casefold(),
        RIOT_TEST_COMMAND.casefold(),
        MOOD_COMMAND.casefold(),
        WEEK_COMMAND.casefold(),
        STREAK_COMMAND.casefold(),
        ADD_COMMAND.casefold(),
        REMOVE_COMMAND.casefold(),
        DEBUG_PLAYER_COMMAND.casefold(),
        HEALTH_COMMAND.casefold(),
        BACKFILL_COMMAND.casefold(),
        HELP_COMMAND.casefold(),
        SCORE_COMMAND.casefold(),
        PROFILE_COMMAND.casefold(),
        TTS_COMMAND.casefold(),
    }
    if content_lower in known_exact:
        return True
    return (
        content_lower.startswith(f"{ADD_COMMAND.casefold()} ")
        or content_lower.startswith(f"{REMOVE_COMMAND.casefold()} ")
        or content_lower.startswith(f"{DEBUG_PLAYER_COMMAND.casefold()} ")
        or content_lower.startswith(f"{BACKFILL_COMMAND.casefold()} ")
        or content_lower.startswith(f"{PROFILE_COMMAND.casefold()} ")
        or content_lower.startswith(f"{STREAK_COMMAND.casefold()} ")
        or content_lower.startswith(f"{TTS_COMMAND.casefold()} ")
    )


def command_channel_id(content_lower, *, daily_channel_id, weekly_channel_id, events_channel_id, match_recap_channel_id=None):
    if content_lower == MOOD_COMMAND.casefold():
        return daily_channel_id
    if content_lower == WEEK_COMMAND.casefold():
        return weekly_channel_id
    if (
        content_lower == STREAK_COMMAND.casefold()
        or content_lower.startswith(f"{STREAK_COMMAND.casefold()} ")
    ):
        return match_recap_channel_id or events_channel_id
    if (
        content_lower == TTS_COMMAND.casefold()
        or content_lower.startswith(f"{TTS_COMMAND.casefold()} ")
    ):
        allowed = [events_channel_id]
        if match_recap_channel_id and match_recap_channel_id != events_channel_id:
            allowed.append(match_recap_channel_id)
        return tuple(allowed)

    if (
        content_lower in {
            TEST_COMMAND.casefold(),
            RIOT_TEST_COMMAND.casefold(),
            ADD_COMMAND.casefold(),
            REMOVE_COMMAND.casefold(),
            DEBUG_PLAYER_COMMAND.casefold(),
            HEALTH_COMMAND.casefold(),
            BACKFILL_COMMAND.casefold(),
            HELP_COMMAND.casefold(),
            SCORE_COMMAND.casefold(),
            PROFILE_COMMAND.casefold(),
            TTS_COMMAND.casefold(),
        }
        or content_lower.startswith(f"{ADD_COMMAND.casefold()} ")
        or content_lower.startswith(f"{REMOVE_COMMAND.casefold()} ")
        or content_lower.startswith(f"{DEBUG_PLAYER_COMMAND.casefold()} ")
        or content_lower.startswith(f"{BACKFILL_COMMAND.casefold()} ")
        or content_lower.startswith(f"{PROFILE_COMMAND.casefold()} ")
        or content_lower.startswith(f"{TTS_COMMAND.casefold()} ")
    ):
        return events_channel_id
    return None


async def enforce_command_channel(ctx):
    content = ctx.content
    content_lower = ctx.content_lower
    if not content.startswith("!") or not is_supported_command(content_lower):
        return False

    expected_channel_id = command_channel_id(
        content_lower,
        daily_channel_id=ctx.daily_channel_id,
        weekly_channel_id=ctx.weekly_channel_id,
        events_channel_id=ctx.events_channel_id,
        match_recap_channel_id=ctx.match_recap_channel_id,
    )
    if expected_channel_id is None:
        return False

    if isinstance(expected_channel_id, tuple):
        if ctx.message.channel.id in expected_channel_id:
            return False
        channel_refs = " or ".join(f"<#{cid}>" for cid in expected_channel_id)
        await ctx.message.channel.send(f"Use `{content.split(' ', 1)[0]}` in {channel_refs}.")
        return True

    if ctx.message.channel.id == expected_channel_id:
        return False
    await ctx.message.channel.send(f"Use `{content.split(' ', 1)[0]}` in <#{expected_channel_id}>.")
    return True
