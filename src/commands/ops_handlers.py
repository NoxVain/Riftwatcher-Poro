import asyncio
from datetime import datetime

import requests

from src.champion_icons import CHAMPION_DISPLAY_MODES, CHAMPION_DISPLAY_STATE_KEY, parse_champion_display_mode
from src.constants import (
    ADD_COMMAND,
    BACKFILL_COMMAND,
    CHAMPION_DISPLAY_COMMAND,
    HEALTH_COMMAND,
    HELP_COMMAND,
    RIOT_TEST_COMMAND,
    SCORE_COMMAND,
    TEST_COMMAND,
    TTS_COMMAND,
)
from src.discord_text import (
    format_health_report,
    format_user_error,
    parse_streak_tts_enabled,
    streak_tts_enabled_state_key,
)

CHAMPION_DISPLAY_LABELS = {"name": "name only", "icon": "portrait only", "both": "portrait and name"}
CHAMPION_DISPLAY_USAGE = f"Usage: `{CHAMPION_DISPLAY_COMMAND} name|icon|both|status`"


async def handle_ops_commands(ctx):
    content = ctx.content
    content_lower = ctx.content_lower

    if content_lower == HELP_COMMAND.casefold():
        from src.commands.routing import format_help_text

        await ctx.message.channel.send(
            format_help_text(
                report_day_start_hour=ctx.report_day_start_hour,
                daily_channel_id=ctx.daily_channel_id,
                weekly_channel_id=ctx.weekly_channel_id,
                events_channel_id=ctx.events_channel_id,
                match_recap_channel_id=ctx.match_recap_channel_id,
                commands_channel_id=ctx.commands_channel_id,
            )
        )
        return True

    if content_lower == TEST_COMMAND.casefold():
        await ctx.message.channel.send("API test: Riftwatcher Poro is online and can send messages.")
        ctx.log(f"[test] Sent API test message in channel {ctx.message.channel.id}.")
        return True

    if content_lower == RIOT_TEST_COMMAND.casefold():
        if not ctx.friends:
            await ctx.message.channel.send(
                f"Riot API test skipped: no tracked players in database. Add one with `{ADD_COMMAND} Name#Tag`."
            )
            return True
        try:
            riot_id, puuid, match_count = await ctx.riot_client.run_riot_connectivity_test(ctx.friends[0])
            await ctx.message.channel.send(
                f"Riot API test OK for `{riot_id}`. Retrieved puuid and {match_count} matches."
            )
            ctx.log(f"[test] Riot API test succeeded for {riot_id} ({puuid[:8]}...).")
        except (KeyError, requests.RequestException) as exc:
            await ctx.message.channel.send(
                format_user_error("Riot API test failed", exc, ctx.request_id_context.get())
            )
            ctx.log(f"[test] Riot API test failed: {type(exc).__name__}: {exc}")
        return True

    if content_lower == HEALTH_COMMAND.casefold():
        stats = await ctx.poro_service.run_health_check(ctx.start_monotonic, worker_stats=ctx.worker_stats)
        await ctx.message.channel.send(format_health_report(stats))
        ctx.log("[health] Sent health status message.")
        return True

    if content_lower == SCORE_COMMAND.casefold():
        report = await ctx.poro_service.build_score_breakdown_report()
        await ctx.message.channel.send(report)
        ctx.log("[score] Sent score breakdown.")
        return True

    if content_lower == BACKFILL_COMMAND.casefold():
        await ctx.message.channel.send(f"Usage: `{BACKFILL_COMMAND} YYYY-MM-DD YYYY-MM-DD`")
        return True

    if content_lower.startswith(f"{BACKFILL_COMMAND.casefold()} "):
        if not ctx.db_enabled:
            await ctx.message.channel.send("Backfill requires postgres-backed mode.")
            return True

        parts = content.split()
        if len(parts) != 3:
            await ctx.message.channel.send(f"Usage: `{BACKFILL_COMMAND} YYYY-MM-DD YYYY-MM-DD`")
            return True

        try:
            start_day = datetime.strptime(parts[1], "%Y-%m-%d").date()
            end_day = datetime.strptime(parts[2], "%Y-%m-%d").date()
        except ValueError:
            await ctx.message.channel.send(
                f"Backfill failed: invalid date format. Use `{BACKFILL_COMMAND} YYYY-MM-DD YYYY-MM-DD`."
            )
            return True

        if end_day < start_day:
            await ctx.message.channel.send("Backfill failed: end date must be on or after start date.")
            return True

        day_span = (end_day - start_day).days + 1
        if day_span > 366:
            await ctx.message.channel.send(
                f"Backfill failed: range too large ({day_span} days). Max supported is 366 days per run."
            )
            return True

        status_message = await ctx.message.channel.send(
            f"\u23F3 Backfilling cached match stats for `{start_day.isoformat()}` -> `{end_day.isoformat()}`..."
        )
        try:
            result = await ctx.poro_service.backfill_daily_stats_from_cache(start_day, end_day, max_payloads=0)
            if result.get("error"):
                await status_message.edit(content=f"Backfill failed: {result['error']}")
                return True
            await status_message.edit(
                content=(
                    f"\u2705 Backfill complete for `{start_day.isoformat()}` -> `{end_day.isoformat()}`.\n"
                    f"- Cache payloads scanned: `{result.get('scanned_payloads', 0)}`\n"
                    f"- Matches matched: `{result.get('matched_matches', 0)}`\n"
                    f"- Player entries updated: `{result.get('upserts', 0)}`\n"
                    f"- Days written: `{result.get('days_written', 0)}`\n"
                    f"- Tracked players missing PUUID: `{result.get('players_without_puuid', 0)}`"
                )
            )
            ctx.log(
                f"[backfill] Completed cache backfill {start_day.isoformat()}..{end_day.isoformat()} "
                f"upserts={result.get('upserts', 0)} scanned={result.get('scanned_payloads', 0)}"
            )
        except Exception as exc:
            await status_message.edit(
                content=format_user_error("Backfill failed", exc, ctx.request_id_context.get())
            )
            ctx.log(f"[backfill] Failed cache backfill: {type(exc).__name__}: {exc}")
        return True

    if content_lower == TTS_COMMAND.casefold():
        await ctx.message.channel.send(f"Usage: `{TTS_COMMAND} on|off|status`")
        return True

    if content_lower.startswith(f"{TTS_COMMAND.casefold()} "):
        arg = content[len(TTS_COMMAND):].strip().lower()
        state_key = streak_tts_enabled_state_key()
        if arg == "status":
            raw_value = None
            if ctx.db_get_state is not None:
                raw_value = await asyncio.to_thread(ctx.db_get_state, state_key)
            enabled = parse_streak_tts_enabled(raw_value, default=True)
            await ctx.message.channel.send(
                f"Streak alert TTS is currently `{'ON' if enabled else 'OFF'}`."
            )
            return True
        if arg not in {"on", "off"}:
            await ctx.message.channel.send(f"Usage: `{TTS_COMMAND} on|off|status`")
            return True

        enabled = arg == "on"
        if ctx.db_set_state is not None:
            await asyncio.to_thread(ctx.db_set_state, state_key, "1" if enabled else "0")
        await ctx.message.channel.send(
            f"Streak alert TTS is now `{'ON' if enabled else 'OFF'}`."
        )
        ctx.log(f"[tts] Streak alert TTS set to {'ON' if enabled else 'OFF'}.")
        return True

    if content_lower == CHAMPION_DISPLAY_COMMAND.casefold():
        await ctx.message.channel.send(CHAMPION_DISPLAY_USAGE)
        return True

    if content_lower.startswith(f"{CHAMPION_DISPLAY_COMMAND.casefold()} "):
        arg = content[len(CHAMPION_DISPLAY_COMMAND):].strip().lower()
        if arg == "status":
            raw_value = None
            if ctx.db_get_state is not None:
                raw_value = await asyncio.to_thread(ctx.db_get_state, CHAMPION_DISPLAY_STATE_KEY)
            mode = parse_champion_display_mode(raw_value)
            await ctx.message.channel.send(f"Recap champions are shown as: `{CHAMPION_DISPLAY_LABELS[mode]}`.")
            return True
        if arg not in CHAMPION_DISPLAY_MODES:
            await ctx.message.channel.send(CHAMPION_DISPLAY_USAGE)
            return True
        if not ctx.db_enabled or ctx.db_set_state is None:
            await ctx.message.channel.send("Champion display settings require postgres-backed mode.")
            return True

        await asyncio.to_thread(ctx.db_set_state, CHAMPION_DISPLAY_STATE_KEY, arg)
        note = "" if arg == "name" else " Missing portraits fall back to the champion name."
        await ctx.message.channel.send(
            f"New recaps will show champions as: `{CHAMPION_DISPLAY_LABELS[arg]}`.{note}"
        )
        ctx.log(f"[champdisplay] Champion display set to {arg}.")
        return True

    return False
