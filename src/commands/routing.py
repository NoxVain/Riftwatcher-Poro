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
    MATCHUP_COMMAND,
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
        f"- `{ANALYZE_COMMAND} Name#Tag [months] [solo|flex] [role]`: Strengths and tips from the last 3 months of ranked games (1-12 months); Solo/Duo unless `flex`; every role unless you add `top`, `jungle`, `mid`, `adc` or `support` (run in {commands_channel_ref}).\n"
        f"- `{MATCHUP_COMMAND} Name Champion [months] [role] [solo|flex]`: Your stats on one champion and your best and worst lane matchups, from the last 12 months of Solo/Duo and Flex (1-12 months; narrow with `top`, `jungle`, `mid`, `adc`, `support`, `solo` or `flex`) (run in {commands_channel_ref}).\n"
        f"- `{RECORDS_COMMAND} [Name#Tag]`: Group records from ranked games, or one player's personal records (run in {commands_channel_ref}).\n"
        f"- `{RIOT_TEST_COMMAND}`: Verify Riot API connectivity (run in {commands_channel_ref}).\n"
        f"- `{TEST_COMMAND}`: Verify Discord send permissions (run in {commands_channel_ref}).\n"
        f"- `{STREAK_COMMAND} Name#Tag`: Manually post current win/loss streak callout (run in {f'<#{match_recap_channel_id}>' if match_recap_channel_id else commands_channel_ref}).\n"
        f"- `{TTS_COMMAND} on|off|status`: Toggle streak alert TTS (run in {commands_channel_ref} or {f'<#{match_recap_channel_id}>' if match_recap_channel_id else commands_channel_ref}).\n"
        f"- `{CHAMPION_DISPLAY_COMMAND} name|icon|both|status`: Show recap champions as name, portrait, or both (run in {commands_channel_ref} or {f'<#{match_recap_channel_id}>' if match_recap_channel_id else commands_channel_ref}).\n"
        f"- `{HELP_COMMAND}`: Show this help (run in {commands_channel_ref})."
    )


# Commands that are only valid on their own, and commands that can also take an argument.
BARE_COMMANDS = (
    TEST_COMMAND,
    RIOT_TEST_COMMAND,
    DAILY_COMMAND,
    WEEK_COMMAND,
    HEALTH_COMMAND,
    HELP_COMMAND,
    SCORE_COMMAND,
)
ARGUMENT_COMMANDS = (
    ADD_COMMAND,
    REMOVE_COMMAND,
    DEBUG_PLAYER_COMMAND,
    BACKFILL_COMMAND,
    PROFILE_COMMAND,
    STREAK_COMMAND,
    TTS_COMMAND,
    CHAMPION_DISPLAY_COMMAND,
    ANALYZE_COMMAND,
    ANALYZE_ALIAS,
    RECORDS_COMMAND,
    RECORDS_ALIAS,
    WRAPPED_COMMAND,
    MATCHUP_COMMAND,
)


def _is_command(content_lower, command):
    """True for the command on its own or followed by a space and an argument."""
    prefix = command.casefold()
    return content_lower == prefix or content_lower.startswith(f"{prefix} ")


def is_supported_command(content_lower):
    if any(content_lower == command.casefold() for command in BARE_COMMANDS):
        return True
    return any(_is_command(content_lower, command) for command in ARGUMENT_COMMANDS)


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
    if _is_command(content_lower, STREAK_COMMAND):
        return match_recap_channel_id or commands_channel_id
    if _is_command(content_lower, TTS_COMMAND) or _is_command(content_lower, CHAMPION_DISPLAY_COMMAND):
        allowed = [commands_channel_id]
        if match_recap_channel_id and match_recap_channel_id != commands_channel_id:
            allowed.append(match_recap_channel_id)
        return tuple(allowed)
    # Every other command runs in the commands channel.
    if is_supported_command(content_lower):
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
