import asyncio
import random
import time

import discord

from src.champion_icons import ChampionIcons
from src.discord_backfill_worker import process_backfill_cycle
from src.discord_esports_worker import process_esports_cycle
from src.discord_patch_worker import get_last_checked_unix, process_patch_notes_cycle
from src.discord_rank_worker import process_rank_cycle
from src.discord_recap_worker import process_recap_cycle
from src.discord_text import create_request_id, report_signature
from src.patch_notes_logic import seconds_until_next_check


def _ensure_worker_stat_entry(worker_stats, worker_name):
    entry = worker_stats.setdefault(worker_name, {})
    entry.setdefault("cycles", 0)
    entry.setdefault("errors", 0)
    entry.setdefault("runs", 0)
    entry.setdefault("elapsed_ms_last", 0)
    entry.setdefault("elapsed_ms_avg", 0)
    entry.setdefault("elapsed_ms_max", 0)
    entry.setdefault("elapsed_ms_total", 0)
    entry.setdefault("last_success_at", 0.0)
    return entry


def _record_worker_cycle(worker_stats, worker_name, *, had_error, elapsed_ms, ended_at_mono):
    entry = _ensure_worker_stat_entry(worker_stats, worker_name)
    entry["runs"] += 1
    if had_error:
        entry["errors"] += 1
    else:
        entry["cycles"] += 1
    entry["elapsed_ms_last"] = int(elapsed_ms)
    entry["elapsed_ms_total"] += int(elapsed_ms)
    entry["elapsed_ms_max"] = max(int(entry["elapsed_ms_max"]), int(elapsed_ms))
    entry["elapsed_ms_avg"] = int(entry["elapsed_ms_total"] / max(1, entry["runs"]))
    if not had_error:
        entry["last_success_at"] = float(ended_at_mono)


async def evaluate_rank_changes_and_notify(
    *,
    resolve_channel,
    events_channel_id,
    friends,
    riot_client,
    db_load_ranked_state,
    db_upsert_ranked_state,
    db_delete_ranked_state_queue,
    log,
    match_recap_channel_id=None,
    db_get_state=None,
    db_set_state=None,
    db_insert_lp_change=None,
):
    channel = await resolve_channel(events_channel_id)
    if channel is None:
        return
    # Recap channel is only needed to edit LP into posted recaps.
    recap_channel = await resolve_channel(match_recap_channel_id) if match_recap_channel_id else None
    await process_rank_cycle(
        friends=friends,
        channel=channel,
        riot_client=riot_client,
        db_load_ranked_state=db_load_ranked_state,
        db_upsert_ranked_state=db_upsert_ranked_state,
        db_delete_ranked_state_queue=db_delete_ranked_state_queue,
        log=log,
        recap_channel=recap_channel,
        db_get_state=db_get_state,
        db_set_state=db_set_state,
        db_insert_lp_change=db_insert_lp_change,
    )


async def background_rank_notifier(
    *,
    db_enabled,
    daily_refresh_seconds,
    client,
    request_id_context,
    worker_stats,
    evaluate_rank_changes_and_notify_fn,
    log,
):
    if not db_enabled:
        return
    sleep_seconds = max(30, daily_refresh_seconds)
    initial_jitter = random.uniform(0.0, min(30.0, sleep_seconds / 2))
    if initial_jitter > 0:
        log(f"[rank] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)
    while not client.is_closed():
        cycle_start = time.monotonic()
        token = request_id_context.set(create_request_id("rank"))
        had_error = False
        try:
            await evaluate_rank_changes_and_notify_fn()
        except Exception as exc:
            had_error = True
            log(f"[rank] Unexpected background error: {exc}")
        finally:
            request_id_context.reset(token)
        cycle_end = time.monotonic()
        elapsed = int((cycle_end - cycle_start) * 1000)
        _record_worker_cycle(worker_stats, "rank", had_error=had_error, elapsed_ms=elapsed, ended_at_mono=cycle_end)
        log(f"[rank] Cycle complete elapsed={elapsed}ms next_sleep={sleep_seconds}s")
        await asyncio.sleep(sleep_seconds)


async def background_match_recap_notifier(
    *,
    client,
    match_recap_poll_seconds,
    request_id_context,
    friends,
    riot_client,
    poro_service,
    match_recap_channel_id,
    db_enabled,
    db_get_state,
    db_set_state,
    db_upsert_daily_stats,
    edit_last_report_message,
    edit_last_weekly_report_message,
    resolve_channel,
    worker_stats,
    log,
):
    sleep_seconds = max(30, match_recap_poll_seconds)
    # Lives as long as the worker so portrait lookups stay cached between cycles.
    champion_icons = ChampionIcons(client, log)
    initial_jitter = random.uniform(0.0, min(30.0, sleep_seconds / 2))
    if initial_jitter > 0:
        log(f"[recap] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)
    while not client.is_closed():
        cycle_start = time.monotonic()
        token = request_id_context.set(create_request_id("recap"))
        had_error = False
        try:
            channel = await resolve_channel(match_recap_channel_id)
            if channel is None:
                await asyncio.sleep(max(30, match_recap_poll_seconds))
                continue

            await process_recap_cycle(
                friends=friends,
                riot_client=riot_client,
                poro_service=poro_service,
                match_recap_channel_id=match_recap_channel_id,
                channel=channel,
                champion_icons=champion_icons,
                db_enabled=db_enabled,
                db_get_state=db_get_state,
                db_set_state=db_set_state,
                db_upsert_daily_stats=db_upsert_daily_stats,
                edit_last_report_message=edit_last_report_message,
                edit_last_weekly_report_message=edit_last_weekly_report_message,
                log=log,
            )
        except Exception as exc:
            had_error = True
            log(f"[recap] Unexpected error: {exc}")
        finally:
            request_id_context.reset(token)
        cycle_end = time.monotonic()
        elapsed = int((cycle_end - cycle_start) * 1000)
        _record_worker_cycle(worker_stats, "recap", had_error=had_error, elapsed_ms=elapsed, ended_at_mono=cycle_end)
        log(f"[recap] Cycle complete elapsed={elapsed}ms next_sleep={sleep_seconds}s")
        await asyncio.sleep(sleep_seconds)


async def background_esports_notifier(
    *,
    client,
    esports_poll_seconds,
    request_id_context,
    resolve_channel,
    news_channel_id,
    esports_client,
    league_slugs,
    report_timezone,
    report_day_start_hour,
    spoiler_tags,
    db_get_state,
    db_set_state,
    worker_stats,
    log,
):
    sleep_seconds = max(30, esports_poll_seconds)
    initial_jitter = random.uniform(0.0, min(15.0, sleep_seconds / 2))
    if initial_jitter > 0:
        log(f"[esports] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)
    while not client.is_closed():
        cycle_start = time.monotonic()
        token = request_id_context.set(create_request_id("esports"))
        had_error = False
        try:
            channel = await resolve_channel(news_channel_id)
            if channel is None:
                had_error = True
                log(f"[esports] News channel {news_channel_id} unavailable; skipping cycle.")
            else:
                await process_esports_cycle(
                    esports_client=esports_client,
                    league_slugs=league_slugs,
                    channel=channel,
                    report_timezone=report_timezone,
                    report_day_start_hour=report_day_start_hour,
                    spoiler_tags=spoiler_tags,
                    db_get_state=db_get_state,
                    db_set_state=db_set_state,
                    log=log,
                )
        except Exception as exc:
            had_error = True
            log(f"[esports] Unexpected error: {exc}")
        finally:
            request_id_context.reset(token)
        cycle_end = time.monotonic()
        elapsed = int((cycle_end - cycle_start) * 1000)
        _record_worker_cycle(worker_stats, "esports", had_error=had_error, elapsed_ms=elapsed, ended_at_mono=cycle_end)
        log(f"[esports] Cycle complete elapsed={elapsed}ms next_sleep={sleep_seconds}s")
        await asyncio.sleep(sleep_seconds)


async def background_patch_notes_notifier(
    *,
    client,
    poll_seconds,
    retry_seconds,
    request_id_context,
    resolve_channel,
    events_channel_id,
    patch_client,
    db_get_state,
    db_set_state,
    worker_stats,
    log,
):
    initial_jitter = random.uniform(0.0, 60.0)
    if initial_jitter > 0:
        log(f"[patch] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)

    # Resume the schedule from the persisted last check so restarts/redeploys don't re-fetch.
    try:
        last_checked = await get_last_checked_unix(db_get_state)
    except Exception as exc:
        last_checked = 0
        log(f"[patch] Could not read last check time: {exc}")
    wait_seconds = seconds_until_next_check(last_checked, now_unix=int(time.time()), interval_seconds=poll_seconds)
    if wait_seconds > 0:
        log(f"[patch] Last check was recent; next check in {wait_seconds}s.")
        await asyncio.sleep(wait_seconds)

    while not client.is_closed():
        cycle_start = time.monotonic()
        token = request_id_context.set(create_request_id("patch"))
        had_error = False
        try:
            channel = await resolve_channel(events_channel_id)
            if channel is None:
                had_error = True
                log(f"[patch] Events channel {events_channel_id} unavailable; skipping cycle.")
            else:
                await process_patch_notes_cycle(
                    patch_client=patch_client,
                    channel=channel,
                    db_get_state=db_get_state,
                    db_set_state=db_set_state,
                    log=log,
                )
        except Exception as exc:
            had_error = True
            log(f"[patch] Unexpected error: {exc}")
        finally:
            request_id_context.reset(token)
        cycle_end = time.monotonic()
        elapsed = int((cycle_end - cycle_start) * 1000)
        _record_worker_cycle(worker_stats, "patch", had_error=had_error, elapsed_ms=elapsed, ended_at_mono=cycle_end)
        # Always sleep a full interval (shorter retry after errors) so a failure can never hot-loop.
        sleep_seconds = retry_seconds if had_error else poll_seconds
        log(f"[patch] Cycle complete elapsed={elapsed}ms next_sleep={sleep_seconds}s")
        await asyncio.sleep(sleep_seconds)


async def background_match_cache_backfiller(
    *,
    db_enabled,
    daily_refresh_seconds,
    client,
    request_id_context,
    friends,
    riot_client,
    db_get_state,
    db_set_state,
    db_get_match_info,
    db_load_backfill_offsets,
    worker_stats,
    log,
):
    if not db_enabled:
        return

    backfill_recent_ids_count = 100
    backfill_per_player_limit = 3
    backfill_interval_seconds = max(120, daily_refresh_seconds * 2)
    initial_jitter = random.uniform(0.0, min(60.0, backfill_interval_seconds / 2))
    if initial_jitter > 0:
        log(f"[backfill] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)

    while not client.is_closed():
        cycle_start = time.monotonic()
        token = request_id_context.set(create_request_id("backfill"))
        had_error = False
        try:
            total_backfilled = await process_backfill_cycle(
                friends=friends,
                riot_client=riot_client,
                db_get_state=db_get_state,
                db_set_state=db_set_state,
                db_get_match_info=db_get_match_info,
                recent_ids_count=backfill_recent_ids_count,
                per_player_limit=backfill_per_player_limit,
                log=log,
            )
            offsets = await asyncio.to_thread(db_load_backfill_offsets)
            active_offsets = sum(1 for riot_id in friends if int(offsets.get(riot_id.casefold(), 0) or 0) > 0)
            max_offset = max((int(offsets.get(riot_id.casefold(), 0) or 0) for riot_id in friends), default=0)
            log(
                f"[backfill] Cycle summary: cached={total_backfilled}, "
                f"active_offsets={active_offsets}/{len(friends)}, max_offset={max_offset}."
            )
        except Exception as exc:
            had_error = True
            log(f"[backfill] Unexpected background error: {exc}")
        finally:
            request_id_context.reset(token)
        cycle_end = time.monotonic()
        elapsed = int((cycle_end - cycle_start) * 1000)
        _record_worker_cycle(worker_stats, "backfill", had_error=had_error, elapsed_ms=elapsed, ended_at_mono=cycle_end)
        log(f"[backfill] Cycle complete elapsed={elapsed}ms next_sleep={backfill_interval_seconds}s")
        await asyncio.sleep(backfill_interval_seconds)


async def background_daily_refresher(
    *,
    db_enabled,
    daily_refresh_seconds,
    client,
    request_id_context,
    poro_service,
    resolve_channel,
    daily_report_channel_id,
    get_or_create_report_message,
    edit_last_weekly_report_message,
    db_cleanup_old_match_cache,
    match_cache_retention_days,
    db_set_last_report_message,
    report_state,
    runtime_state,
    worker_stats,
    log,
):
    if not db_enabled:
        return
    sleep_seconds = max(30, daily_refresh_seconds)
    initial_jitter = random.uniform(0.0, min(30.0, sleep_seconds / 2))
    if initial_jitter > 0:
        log(f"[refresh] Startup jitter sleep={initial_jitter:.1f}s")
        await asyncio.sleep(initial_jitter)
    last_snapshot_push_at = 0.0
    last_snapshot_signature = None
    snapshot_push_interval = 120.0
    changed_push_min_interval = 30.0
    while not client.is_closed():
        cycle_start = time.monotonic()
        token = request_id_context.set(create_request_id("bg"))
        had_error = False
        try:
            async def push_snapshot_update(force=False):
                nonlocal last_snapshot_push_at, last_snapshot_signature
                now_mono = time.monotonic()

                try:
                    snapshot_text = await poro_service.build_today_win_rate_report(
                        prefer_snapshot=True,
                        bypass_cache=True,
                    )
                    interval_elapsed = (now_mono - last_snapshot_push_at) >= snapshot_push_interval
                    signature = report_signature(snapshot_text)
                    changed = signature != last_snapshot_signature
                    changed_interval_elapsed = (now_mono - last_snapshot_push_at) >= changed_push_min_interval
                    should_push = force or interval_elapsed or (changed and changed_interval_elapsed)
                    if not should_push:
                        return

                    channel = await resolve_channel(daily_report_channel_id)
                    if channel is None:
                        return
                    message = await get_or_create_report_message(channel, snapshot_text)
                    if message.content != snapshot_text:
                        await message.edit(content=snapshot_text)
                        log(
                            f"[refresh] Updated last report message {message.id} in channel {channel.id} "
                            f"(force={force})."
                        )
                    else:
                        log(f"[refresh] Snapshot unchanged in Discord for message {message.id}.")

                    last_snapshot_signature = signature
                    last_snapshot_push_at = now_mono
                except (discord.NotFound, discord.Forbidden) as exc:
                    log(f"[refresh] Could not edit last report message: {exc}")
                    report_state["channel_id"] = None
                    report_state["message_id"] = None
                    if db_enabled:
                        await asyncio.to_thread(db_set_last_report_message, 0, 0)
                except discord.HTTPException as exc:
                    log(f"[refresh] Discord API error while editing last report message: {exc}")

            async def on_player_refreshed(_processed, _total, _riot_id):
                await push_snapshot_update(force=False)

            await poro_service.refresh_daily_stats_once(progress_callback=on_player_refreshed)
            await push_snapshot_update(force=True)
            await edit_last_weekly_report_message(bypass_cache=True)
            now_mono = time.monotonic()
            last_cleanup_at = runtime_state.get("last_cache_cleanup_at", 0.0)
            if (now_mono - last_cleanup_at) >= max(3600, daily_refresh_seconds):
                deleted = await asyncio.to_thread(db_cleanup_old_match_cache, match_cache_retention_days)
                runtime_state["last_cache_cleanup_at"] = now_mono
                log(
                    f"[refresh] Match cache cleanup complete: deleted={deleted}, "
                    f"retention_days={match_cache_retention_days}"
                )
        except Exception as exc:
            had_error = True
            log(f"[refresh] Unexpected error: {exc}")
        finally:
            request_id_context.reset(token)
        cycle_end = time.monotonic()
        elapsed = int((cycle_end - cycle_start) * 1000)
        _record_worker_cycle(worker_stats, "refresh", had_error=had_error, elapsed_ms=elapsed, ended_at_mono=cycle_end)
        log(f"[refresh] Cycle complete elapsed={elapsed}ms next_sleep={sleep_seconds}s")
        await asyncio.sleep(sleep_seconds)
