import asyncio
from datetime import datetime, timedelta

import requests

from src.constants import (
    ADD_COMMAND,
    BACKFILL_COMMAND,
    HEALTH_COMMAND,
    HELP_COMMAND,
    RIOT_TEST_COMMAND,
    SCORE_COMMAND,
    TEST_COMMAND,
    TTS_COMMAND,
)
from src.discord_text import parse_streak_tts_enabled, streak_tts_enabled_state_key


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
            )
        )
        return True

    if content_lower == TEST_COMMAND.casefold():
        await ctx.message.channel.send("API test: MoodBot is online and can send messages.")
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
            await ctx.message.channel.send(f"Riot API test failed: {exc}")
            ctx.log(f"[test] Riot API test failed: {exc}")
        return True

    if content_lower == HEALTH_COMMAND.casefold():
        stats = await ctx.mood_service.run_health_check(ctx.start_monotonic, worker_stats=ctx.worker_stats)
        uptime = str(timedelta(seconds=stats["uptime_seconds"]))
        top_backfill_offsets = ", ".join(stats.get("top_backfill_offsets", [])) or "none"
        wstats = stats.get("worker_stats") or {}
        workers_line = ""
        if wstats:
            parts = []
            for key, value in wstats.items():
                segment = f"{key}: {value['cycles']}ok/{value['errors']}err"
                if "elapsed_ms_last" in value:
                    segment += (
                        f" ({value.get('elapsed_ms_last', 0)}ms last/"
                        f"{value.get('elapsed_ms_avg', 0)}ms avg/"
                        f"{value.get('elapsed_ms_max', 0)}ms max)"
                    )
                parts.append(segment)
            workers_line = f"\n- Workers: `{' | '.join(parts)}`"
        baseline_age_seconds = stats.get("baseline_age_seconds")
        if baseline_age_seconds is None:
            baseline_line = "\n- Gamer Score baselines: `not yet built`"
        else:
            age = str(timedelta(seconds=baseline_age_seconds))
            baseline_line = (
                f"\n- Gamer Score baselines: "
                f"`{stats['baseline_roles']} roles, {stats['baseline_samples']} samples (built {age} ago)`"
            )
        await ctx.message.channel.send(
            (
                "Health OK\n"
                f"- Uptime: `{uptime}`\n"
                f"- Tracked players: `{stats['tracked_players']}`\n"
                f"- DB: `{'ok' if stats['db_ok'] else 'down'}`\n"
                f"- Match cache entries: `{stats['match_cache_entries']}`\n"
                f"- Report cache active: `{'yes' if stats['request_cache_active'] else 'no'}`\n"
                f"- Backfill cursors active: `{stats.get('players_with_backfill_offset', 0)}/{stats['tracked_players']}`\n"
                f"- Backfill max offset: `{stats.get('max_backfill_offset', 0)}`\n"
                f"- Backfill top offsets: `{top_backfill_offsets}`"
                f"{baseline_line}"
                f"{workers_line}"
            )
        )
        ctx.log("[health] Sent health status message.")
        return True

    if content_lower == SCORE_COMMAND.casefold():
        report = await ctx.mood_service.build_score_breakdown_report()
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
            result = await ctx.mood_service.backfill_daily_stats_from_cache(start_day, end_day, max_payloads=0)
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
            await status_message.edit(content=f"Backfill failed: {exc}")
            ctx.log(f"[backfill] Failed cache backfill: {exc}")
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

    return False
