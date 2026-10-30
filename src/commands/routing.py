from src.constants import (
    ADD_COMMAND,
    ANALYZE_ALIAS,
    ANALYZE_COMMAND,
    BACKFILL_COMMAND,
    CHAMPION_DISPLAY_COMMAND,
    DEBUG_PLAYER_COMMAND,
    HEALTH_COMMAND,
    HELP_COMMAND,
    DAILY_COMMAND,
    PROFILE_COMMAND,
    RECORDS_ALIAS,
    RECORDS_COMMAND,
    REMOVE_COMMAND,
    RIOT_TEST_COMMAND,
    SCORE_COMMAND,
    STREAK_COMMAND,
    TEST_COMMAND,
    TTS_COMMAND,
    WEEK_COMMAND,
    WRAPPED_COMMAND,
)


def format_help_text(
    *, report_day_start_hour, daily_channel_id, weekly_channel_id, events_channel_id, match_recap_channel_id=None,
    commands_channel_id=None,
):
    daily_channel_ref = f"<#{daily_channel_id}>"
    weekly_channel_ref = f"<#{weekly_channel_id}>" if weekly_channel_id else daily_channel_ref
    commands_channel_ref = f"<#{commands_channel_id or events_channel_id}>"
    return (
        "**Riftwatcher Poro commands**\n"
        f"- `{DAILY_COMMAND}`: Refresh daily scoreboard (run in {daily_channel_ref}).\n"
        f"- `{WEEK_COMMAND}`: Refresh weekly scoreboard for Monday `{report_day_start_hour:02d}:00` -> next Monday "
        f"`{report_day_start_hour:02d}:00` (run/post in {weekly_channel_ref}).\n"
        f"- `{ADD_COMMAND} Name#Tag`: Add a tracked Riot ID (run in {commands_channel_ref}).\n"
        f"- `{REMOVE_COMMAND} Name#Tag`: Remove a tracked Riot ID (run in {commands_channel_ref}).\n"
        f"- `{DEBUG_PLAYER_COMMAND} Name#Tag`: Show queue/window debug details (run in {commands_channel_ref}).\n"
        f"- `{HEALTH_COMMAND}`: Show bot/DB/cache health (run in {commands_channel_ref}).\n"
        f"- `{BACKFILL_COMMAND} YYYY-MM-DD YYYY-MM-DD`: Rebuild historical daily stats from cached matches (run in {commands_channel_ref}).\n"
        f"- `{SCORE_COMMAND}`: Show how each player's Gamer Score is calculated today (run in {commands_channel_ref}).\n"
        f"- `{PROFILE_COMMAND} Name#Tag`: Show player profile summary (run in {commands_channel_ref}).\n"
        f"- `{ANALYZE_COMMAND} Name#Tag [months] [solo|flex]`: Strengths and tips from the last 3 months of ranked games (1-12 months); Solo/Duo unless `flex` (run in {commands_channel_ref}).\n"
        f"- `{RECORDS_COMMAND} [Name#Tag]`: Group records from ranked games, or one player's personal bests (run in {commands_channel_ref}).\n"
        f"- `{RIOT_TEST_COMMAND}`: Verify Riot API connectivity (run in {commands_channel_ref}).\n"
        f"- `{TEST_COMMAND}`: Verify Discord send permissions (run in {commands_channel_ref}).\n"
        f"- `{STREAK_COMMAND} Name#Tag`: Manually post current win/loss streak callout (run in {f'<#{match_recap_channel_id}>' if match_recap_channel_id else commands_channel_ref}).\n"
        f"- `{TTS_COMMAND} on|off|status`: Toggle streak alert TTS (run in {commands_channel_ref} or {f'<#{match_recap_channel_id}>' if match_recap_channel_id else commands_channel_ref}).\n"
        f"- `{CHAMPION_DISPLAY_COMMAND} name|icon|both|status`: Show recap champions as name, portrait, or both (run in {commands_channel_ref} or {f'<#{match_recap_channel_id}>' if match_recap_channel_id else commands_channel_ref}).\n"
        f"- `{HELP_COMMAND}`: Show this help (run in {commands_channel_ref})."
    )


def is_supported_command(content_lower):
    known_exact = {
        TEST_COMMAND.casefold(),
        RIOT_TEST_COMMAND.casefold(),
        DAILY_COMMAND.casefold(),
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
        CHAMPION_DISPLAY_COMMAND.casefold(),
        ANALYZE_COMMAND.casefold(),
        ANALYZE_ALIAS.casefold(),
        RECORDS_COMMAND.casefold(),
        RECORDS_ALIAS.casefold(),
        WRAPPED_COMMAND.casefold(),
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
        or content_lower.startswith(f"{CHAMPION_DISPLAY_COMMAND.casefold()} ")
        or content_lower.startswith(f"{ANALYZE_COMMAND.casefold()} ")
        or content_lower.startswith(f"{ANALYZE_ALIAS.casefold()} ")
        or content_lower.startswith(f"{RECORDS_COMMAND.casefold()} ")
        or content_lower.startswith(f"{RECORDS_ALIAS.casefold()} ")
        or content_lower.startswith(f"{WRAPPED_COMMAND.casefold()} ")
    )


def command_channel_id(
    content_lower, *, daily_channel_id, weekly_channel_id, events_channel_id, match_recap_channel_id=None,
    commands_channel_id=None,
):
    # Commands that used to run in the events channel run in the commands channel when it's set.
    commands_channel_id = commands_channel_id or events_channel_id
    if content_lower == DAILY_COMMAND.casefold():
        return daily_channel_id
    if content_lower == WEEK_COMMAND.casefold():
        return weekly_channel_id
    if (
        content_lower == STREAK_COMMAND.casefold()
        or content_lower.startswith(f"{STREAK_COMMAND.casefold()} ")
    ):
        return match_recap_channel_id or commands_channel_id
    if (
        content_lower == TTS_COMMAND.casefold()
        or content_lower.startswith(f"{TTS_COMMAND.casefold()} ")
        or content_lower == CHAMPION_DISPLAY_COMMAND.casefold()
        or content_lower.startswith(f"{CHAMPION_DISPLAY_COMMAND.casefold()} ")
    ):
        allowed = [commands_channel_id]
        if match_recap_channel_id and match_recap_channel_id != commands_channel_id:
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
            ANALYZE_COMMAND.casefold(),
            ANALYZE_ALIAS.casefold(),
            RECORDS_COMMAND.casefold(),
            RECORDS_ALIAS.casefold(),
            WRAPPED_COMMAND.casefold(),
        }
        or content_lower.startswith(f"{ADD_COMMAND.casefold()} ")
        or content_lower.startswith(f"{REMOVE_COMMAND.casefold()} ")
        or content_lower.startswith(f"{DEBUG_PLAYER_COMMAND.casefold()} ")
        or content_lower.startswith(f"{BACKFILL_COMMAND.casefold()} ")
        or content_lower.startswith(f"{PROFILE_COMMAND.casefold()} ")
        or content_lower.startswith(f"{TTS_COMMAND.casefold()} ")
        or content_lower.startswith(f"{ANALYZE_COMMAND.casefold()} ")
        or content_lower.startswith(f"{ANALYZE_ALIAS.casefold()} ")
        or content_lower.startswith(f"{RECORDS_COMMAND.casefold()} ")
        or content_lower.startswith(f"{RECORDS_ALIAS.casefold()} ")
        or content_lower.startswith(f"{WRAPPED_COMMAND.casefold()} ")
    ):
        return commands_channel_id
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
        commands_channel_id=getattr(ctx, "commands_channel_id", None),
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
