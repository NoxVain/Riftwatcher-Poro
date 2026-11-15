import discord
import requests

from src.constants import DAILY_COMMAND, DISCORD_MESSAGE_LIMIT, WEEK_COMMAND
from src.discord_text import format_user_error


async def handle_report_commands(ctx):
    content_lower = ctx.content_lower

    if content_lower == DAILY_COMMAND.casefold():
        request_id = ctx.create_request_id("poro")
        token = ctx.request_id_context.set(request_id)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            ctx.log(f"[poro] Could not delete command message {ctx.message.id}: {exc}")

        try:
            if ctx.daily_request_lock.locked():
                await ctx.message.channel.send("\u23F3 Another report is already in progress. Please wait.")
                return True

            async with ctx.daily_request_lock:
                loading_text = (
                    f"\u23F3 Gathering match results since {ctx.report_day_start_hour:02d}:00 from Riot..."
                )
                status_message = await ctx.get_or_create_report_message(ctx.message.channel, loading_text)
                if status_message.content != loading_text:
                    await status_message.edit(content=loading_text)
                try:
                    if ctx.db_enabled:
                        snapshot_text = await ctx.poro_service.build_today_win_rate_report()
                        refresh_note = "_Refreshing latest matches..._"
                        snapshot_with_note = f"{snapshot_text}\n\n{refresh_note}"
                        if len(snapshot_with_note) > DISCORD_MESSAGE_LIMIT:
                            snapshot_with_note = snapshot_text
                        await status_message.edit(content=snapshot_with_note)
                        displayed_text = snapshot_with_note
                        ctx.remember_report_message(status_message)
                        ctx.log(f"[poro] Sent stored snapshot report in channel {ctx.daily_channel_id}.")

                        await ctx.poro_service.refresh_recent_matches_snapshot(recent_count=20)
                        refreshed_text = await ctx.poro_service.build_today_win_rate_report()
                        if refreshed_text != displayed_text:
                            await status_message.edit(content=refreshed_text)
                            ctx.log(f"[poro] Updated report after quick refresh in channel {ctx.daily_channel_id}.")
                        else:
                            ctx.log("[poro] Quick refresh produced no visible report change.")
                    else:
                        async def progress(done, total, last_name):
                            await status_message.edit(
                                content=(
                                    f"\u23F3 Gathering match results since {ctx.report_day_start_hour:02d}:00 "
                                    f"from Riot... ({done}/{total}) `{last_name}`"
                                )
                            )

                        report_text = await ctx.poro_service.build_today_win_rate_report(progress_callback=progress)
                        await status_message.edit(content=report_text)
                        ctx.remember_report_message(status_message)
                        ctx.log(
                            f"[poro] Sent cycle win rate report (since {ctx.report_day_start_hour:02d}:00) "
                            f"in channel {ctx.daily_channel_id}."
                        )
                except (KeyError, requests.RequestException) as exc:
                    await status_message.edit(content=format_user_error("Daily report failed", exc, request_id))
                    ctx.log(f"[poro] Daily report failed: {type(exc).__name__}: {exc}")
                except Exception as exc:
                    await status_message.edit(
                        content=format_user_error("Daily report failed unexpectedly", exc, request_id)
                    )
                    ctx.log(f"[poro] Unexpected daily report failure: {type(exc).__name__}: {exc}")
        finally:
            ctx.request_id_context.reset(token)
        return True

    if content_lower == WEEK_COMMAND.casefold():
        request_id = ctx.create_request_id("week")
        token = ctx.request_id_context.set(request_id)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            ctx.log(f"[week] Could not delete command message {ctx.message.id}: {exc}")

        try:
            if ctx.daily_request_lock.locked():
                await ctx.message.channel.send("\u23F3 Another report is already in progress. Please wait.")
                return True

            async with ctx.daily_request_lock:
                if ctx.get_or_create_weekly_report_message is None:
                    await ctx.message.channel.send("Weekly report is not configured.")
                    return True
                target_channel = ctx.message.channel
                if ctx.weekly_report_channel_id is not None and ctx.resolve_channel is not None:
                    resolved_channel = await ctx.resolve_channel(ctx.weekly_report_channel_id)
                    if resolved_channel is None:
                        await ctx.message.channel.send("Weekly report failed: could not access weekly report channel.")
                        return True
                    target_channel = resolved_channel
                loading_text = (
                    "\u23F3 Building weekly report "
                    f"(Monday {ctx.report_day_start_hour:02d}:00 -> next Monday {ctx.report_day_start_hour:02d}:00) "
                    "from stored stats..."
                )
                status_message = await ctx.get_or_create_weekly_report_message(target_channel, loading_text)
                if status_message.content != loading_text:
                    await status_message.edit(content=loading_text)
                report_text = await ctx.poro_service.build_weekly_win_rate_report(bypass_cache=True)
                await status_message.edit(content=report_text)
                if ctx.remember_weekly_report_message is not None:
                    ctx.remember_weekly_report_message(status_message)
                target_channel_id = getattr(target_channel, "id", ctx.weekly_report_channel_id)
                ctx.log(f"[week] Sent weekly report in channel {target_channel_id}.")
        except Exception as exc:
            await ctx.message.channel.send(format_user_error("Weekly report failed", exc, request_id))
            ctx.log(f"[week] Weekly report failed: {type(exc).__name__}: {exc}")
        finally:
            ctx.request_id_context.reset(token)
        return True

    return False
