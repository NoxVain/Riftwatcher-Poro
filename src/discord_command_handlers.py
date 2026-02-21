import asyncio
from datetime import timedelta

import discord
import requests

from src.constants import (
    ADD_COMMAND,
    DEBUG_PLAYER_COMMAND,
    HEALTH_COMMAND,
    HELP_COMMAND,
    MOOD_COMMAND,
    RIOT_TEST_COMMAND,
    TEST_COMMAND,
    WEEK_COMMAND,
)


def format_help_text(*, report_day_start_hour, daily_channel_id, weekly_channel_id):
    daily_channel_ref = f"<#{daily_channel_id}>"
    weekly_channel_ref = f"<#{weekly_channel_id}>" if weekly_channel_id else daily_channel_ref
    return (
        "**MoodBot commands**\n"
        f"- `{MOOD_COMMAND}`: Refresh daily scoreboard (games since `{report_day_start_hour:02d}:00`).\n"
        f"- `{WEEK_COMMAND}`: Refresh weekly scoreboard for Monday `{report_day_start_hour:02d}:00` -> next Monday "
        f"`{report_day_start_hour:02d}:00` (posted in {weekly_channel_ref}).\n"
        f"- `{ADD_COMMAND} Name#Tag`: Add a tracked Riot ID.\n"
        f"- `{DEBUG_PLAYER_COMMAND} Name#Tag`: Show queue/window debug details.\n"
        f"- `{HEALTH_COMMAND}`: Show bot/DB/cache health.\n"
        f"- `{RIOT_TEST_COMMAND}`: Verify Riot API connectivity.\n"
        f"- `{TEST_COMMAND}`: Verify Discord send permissions.\n"
        f"- `{HELP_COMMAND}`: Show this help.\n"
        f"Use commands in {daily_channel_ref}."
    )


def is_supported_command(content_lower):
    known_exact = {
        TEST_COMMAND.casefold(),
        RIOT_TEST_COMMAND.casefold(),
        MOOD_COMMAND.casefold(),
        WEEK_COMMAND.casefold(),
        ADD_COMMAND.casefold(),
        DEBUG_PLAYER_COMMAND.casefold(),
        HEALTH_COMMAND.casefold(),
        HELP_COMMAND.casefold(),
    }
    if content_lower in known_exact:
        return True
    return (
        content_lower.startswith(f"{ADD_COMMAND.casefold()} ")
        or content_lower.startswith(f"{DEBUG_PLAYER_COMMAND.casefold()} ")
    )


async def handle_incoming_message(
    *,
    message,
    channel_id,
    friends,
    riot_client,
    mood_service,
    report_timezone_name,
    report_day_start_hour,
    db_enabled,
    start_monotonic,
    mood_request_lock,
    request_id_context,
    create_request_id,
    get_or_create_report_message,
    remember_report_message,
    normalize_riot_id,
    db_upsert_player,
    log,
    get_or_create_weekly_report_message=None,
    remember_weekly_report_message=None,
    weekly_report_channel_id=None,
    resolve_channel=None,
):
    content = message.content.strip()
    content_lower = content.casefold()
    weekly_channel_id = weekly_report_channel_id or channel_id

    if message.channel.id != channel_id:
        if content.startswith("!") and is_supported_command(content_lower):
            await message.channel.send(
                f"Use MoodBot commands in <#{channel_id}>. Run `{HELP_COMMAND}` there for usage."
            )
        return

    if content_lower == HELP_COMMAND.casefold():
        await message.channel.send(
            format_help_text(
                report_day_start_hour=report_day_start_hour,
                daily_channel_id=channel_id,
                weekly_channel_id=weekly_channel_id,
            )
        )
        return

    if content_lower == TEST_COMMAND.casefold():
        await message.channel.send("API test: MoodBot is online and can send messages.")
        log(f"[test] Sent API test message in channel {channel_id}.")
        return

    if content_lower == RIOT_TEST_COMMAND.casefold():
        if not friends:
            await message.channel.send(
                f"Riot API test skipped: no tracked players in database. Add one with `{ADD_COMMAND} Name#Tag`."
            )
            return
        try:
            riot_id, puuid, match_count = await riot_client.run_riot_connectivity_test(friends[0])
            await message.channel.send(
                f"Riot API test OK for `{riot_id}`. Retrieved puuid and {match_count} matches."
            )
            log(f"[test] Riot API test succeeded for {riot_id} ({puuid[:8]}...).")
        except (KeyError, requests.RequestException) as exc:
            await message.channel.send(f"Riot API test failed: {exc}")
            log(f"[test] Riot API test failed: {exc}")
        return

    if content_lower == HEALTH_COMMAND.casefold():
        stats = await mood_service.run_health_check(start_monotonic)
        uptime = str(timedelta(seconds=stats["uptime_seconds"]))
        top_backfill_offsets = ", ".join(stats.get("top_backfill_offsets", [])) or "none"
        await message.channel.send(
            (
                f"Health OK\n"
                f"- Uptime: `{uptime}`\n"
                f"- Tracked players: `{stats['tracked_players']}`\n"
                f"- DB: `{'ok' if stats['db_ok'] else 'down'}`\n"
                f"- Match cache entries: `{stats['match_cache_entries']}`\n"
                f"- Report cache active: `{'yes' if stats['request_cache_active'] else 'no'}`\n"
                f"- Backfill cursors active: `{stats.get('players_with_backfill_offset', 0)}/{stats['tracked_players']}`\n"
                f"- Backfill max offset: `{stats.get('max_backfill_offset', 0)}`\n"
                f"- Backfill top offsets: `{top_backfill_offsets}`"
            )
        )
        log("[health] Sent health status message.")
        return

    if content_lower == DEBUG_PLAYER_COMMAND.casefold():
        await message.channel.send(f"Usage: `{DEBUG_PLAYER_COMMAND} Name#Tag`")
        return

    if content_lower.startswith(f"{DEBUG_PLAYER_COMMAND.casefold()} "):
        raw_riot_id = content[len(DEBUG_PLAYER_COMMAND):].strip()
        status_message = await message.channel.send(f"\u23F3 Building debug report for `{raw_riot_id}`...")
        try:
            report_text = await riot_client.build_debug_player_report(
                raw_riot_id,
                report_timezone_name=report_timezone_name,
                normalize_riot_id=normalize_riot_id,
            )
            if len(report_text) > 1900:
                report_text = report_text[:1900] + "\n...truncated..."
            await status_message.edit(content=f"```text\n{report_text}\n```")
        except ValueError as exc:
            await status_message.edit(content=f"Debug report failed: {exc}")
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Debug report failed: {exc}")
            log(f"[debug] Debug report failed: {exc}")
        return

    if content_lower == MOOD_COMMAND.casefold():
        request_id = create_request_id("mood")
        token = request_id_context.set(request_id)
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log(f"[mood] Could not delete command message {message.id}: {exc}")

        try:
            if mood_request_lock.locked():
                await message.channel.send("\u23F3 Another report is already in progress. Please wait.")
                return

            async with mood_request_lock:
                loading_text = (
                    f"\u23F3 Gathering match results since {report_day_start_hour:02d}:00 from Riot..."
                )
                status_message = await get_or_create_report_message(message.channel, loading_text)
                if status_message.content != loading_text:
                    await status_message.edit(content=loading_text)
                try:
                    if db_enabled:
                        snapshot_text = await mood_service.build_today_win_rate_report()
                        refresh_note = "_Refreshing latest matches..._"
                        snapshot_with_note = f"{snapshot_text}\n\n{refresh_note}"
                        if len(snapshot_with_note) > 2000:
                            snapshot_with_note = snapshot_text
                        await status_message.edit(content=snapshot_with_note)
                        displayed_text = snapshot_with_note
                        remember_report_message(status_message)
                        log(f"[mood] Sent stored snapshot report in channel {channel_id}.")

                        await mood_service.refresh_recent_matches_snapshot(recent_count=20)
                        refreshed_text = await mood_service.build_today_win_rate_report()
                        if refreshed_text != displayed_text:
                            await status_message.edit(content=refreshed_text)
                            log(f"[mood] Updated report after quick refresh in channel {channel_id}.")
                        else:
                            log("[mood] Quick refresh produced no visible report change.")
                    else:
                        async def progress(done, total, last_name):
                            await status_message.edit(
                                content=(
                                    f"\u23F3 Gathering match results since {report_day_start_hour:02d}:00 "
                                    f"from Riot... ({done}/{total}) `{last_name}`"
                                )
                            )

                        report_text = await mood_service.build_today_win_rate_report(progress_callback=progress)
                        await status_message.edit(content=report_text)
                        remember_report_message(status_message)
                        log(
                            f"[mood] Sent cycle win rate report (since {report_day_start_hour:02d}:00) "
                            f"in channel {channel_id}."
                        )
                except (KeyError, requests.RequestException) as exc:
                    await status_message.edit(content=f"Mood report failed: {exc}")
                    log(f"[mood] Mood report failed: {exc}")
                except Exception as exc:
                    await status_message.edit(content=f"Mood report failed unexpectedly: {exc}")
                    log(f"[mood] Unexpected mood report failure: {exc}")
        finally:
            request_id_context.reset(token)
        return

    if content_lower == WEEK_COMMAND.casefold():
        request_id = create_request_id("week")
        token = request_id_context.set(request_id)
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log(f"[week] Could not delete command message {message.id}: {exc}")

        try:
            if mood_request_lock.locked():
                await message.channel.send("\u23F3 Another report is already in progress. Please wait.")
                return

            async with mood_request_lock:
                if get_or_create_weekly_report_message is None:
                    await message.channel.send("Weekly report is not configured.")
                    return
                target_channel = message.channel
                if weekly_report_channel_id is not None and resolve_channel is not None:
                    resolved_channel = await resolve_channel(weekly_report_channel_id)
                    if resolved_channel is None:
                        await message.channel.send("Weekly report failed: could not access weekly report channel.")
                        return
                    target_channel = resolved_channel
                loading_text = (
                    "\u23F3 Building weekly report "
                    f"(Monday {report_day_start_hour:02d}:00 -> next Monday {report_day_start_hour:02d}:00) "
                    "from stored stats..."
                )
                status_message = await get_or_create_weekly_report_message(target_channel, loading_text)
                if status_message.content != loading_text:
                    await status_message.edit(content=loading_text)
                report_text = await mood_service.build_weekly_win_rate_report(bypass_cache=True)
                await status_message.edit(content=report_text)
                if remember_weekly_report_message is not None:
                    remember_weekly_report_message(status_message)
                target_channel_id = getattr(target_channel, "id", weekly_report_channel_id)
                log(f"[week] Sent weekly report in channel {target_channel_id}.")
        except Exception as exc:
            await message.channel.send(f"Weekly report failed: {exc}")
            log(f"[week] Weekly report failed: {exc}")
        finally:
            request_id_context.reset(token)
        return

    if content_lower == ADD_COMMAND.casefold():
        await message.channel.send(f"Usage: `{ADD_COMMAND} Name#Tag`")
        return

    if content_lower.startswith(f"{ADD_COMMAND.casefold()} "):
        raw_riot_id = content[len(ADD_COMMAND):].strip()
        try:
            riot_id = normalize_riot_id(raw_riot_id)
        except ValueError as exc:
            await message.channel.send(f"Add failed: {exc}")
            return

        if any(existing.casefold() == riot_id.casefold() for existing in friends):
            await message.channel.send(f"`{riot_id}` is already tracked.")
            return

        status_message = await message.channel.send(f"\u23F3 Validating `{riot_id}` with Riot API...")
        try:
            await riot_client.fetch_puuid(riot_id)
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Add failed for `{riot_id}`: {exc}")
            return

        friends.append(riot_id)
        try:
            await asyncio.to_thread(db_upsert_player, riot_id, None)
        except Exception as exc:
            await status_message.edit(content=f"Added `{riot_id}`, but failed to persist to postgres: {exc}")
            return

        await status_message.edit(
            content=(
                f"\u2705 Added `{riot_id}` and saved to postgres. "
                f"Total tracked players: {len(friends)}"
            )
        )
        mood_service.invalidate_report_cache()
        log(f"[add] Added player {riot_id}.")
