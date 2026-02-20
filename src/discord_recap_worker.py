import asyncio
from datetime import datetime

import requests

from src.discord_text import (
    format_match_duration,
    format_recap_player_line,
    format_recap_queue_name,
    format_streak_callout,
    match_recap_state_key,
    streak_callout_state_key,
)
from src.report_logic import get_match_end_unix_seconds, get_mode_bucket


async def get_ranked_streak_info(riot_client, puuid, recent_ids, max_matches=8):
    streak_result = None
    streak_count = 0
    for match_id in recent_ids[:max(1, max_matches)]:
        match_info = await riot_client.fetch_match_info(match_id)
        queue_id = int(match_info.get("info", {}).get("queueId", -1))
        if get_mode_bucket(queue_id) is None:
            continue
        participant = riot_client.get_participant(match_info, puuid)
        if participant is None:
            continue
        won = bool(participant.get("win"))
        if streak_result is None:
            streak_result = won
            streak_count = 1
            continue
        if won == streak_result:
            streak_count += 1
            continue
        break
    if streak_result is None:
        return 0, None
    return streak_count, streak_result


async def process_recap_cycle(
    *,
    friends,
    riot_client,
    mood_service,
    report_timezone,
    match_recap_channel_id,
    channel,
    db_enabled,
    db_get_state,
    db_set_state,
    db_upsert_daily_stats,
    edit_last_report_message,
    log,
):
    puuid_by_riot_id = {}
    recent_ids_by_riot_id = {}
    matches_to_report = set()
    for riot_id in friends:
        try:
            puuid = await riot_client.fetch_puuid(riot_id)
            puuid_by_riot_id[riot_id] = puuid
            recent_ids = await riot_client.fetch_recent_match_ids(puuid, count=20)
            recent_ids_by_riot_id[riot_id] = recent_ids
            if not recent_ids:
                continue

            state_key = match_recap_state_key(riot_id)
            last_announced = await asyncio.to_thread(db_get_state, state_key)
            latest_match_id = recent_ids[0]
            if not last_announced:
                await asyncio.to_thread(db_set_state, state_key, latest_match_id)
                continue

            new_match_ids = mood_service.get_new_match_ids(recent_ids, last_announced)
            if new_match_ids:
                matches_to_report.update(new_match_ids)
            await asyncio.to_thread(db_set_state, state_key, latest_match_id)
        except requests.RequestException as exc:
            log(f"[recap] Failed while checking {riot_id}: {exc}")

    if not matches_to_report:
        return

    puuid_to_riot_id = {puuid: riot_id for riot_id, puuid in puuid_by_riot_id.items()}
    match_entries = []
    for match_id in matches_to_report:
        try:
            match_info = await riot_client.fetch_match_info(match_id)
        except requests.RequestException as exc:
            log(f"[recap] Failed fetching match {match_id}: {exc}")
            continue

        participants = match_info.get("info", {}).get("participants", [])
        tracked_participants = []
        for participant in participants:
            riot_id = puuid_to_riot_id.get(participant.get("puuid"))
            if riot_id:
                tracked_participants.append((riot_id, participant))
        if not tracked_participants:
            continue

        end_ts = get_match_end_unix_seconds(match_info)
        queue_id = int(match_info.get("info", {}).get("queueId", -1))
        duration_seconds = int(match_info.get("info", {}).get("gameDuration", 0) or 0)
        if duration_seconds > 10_000:
            duration_seconds = int(duration_seconds / 1000)
        match_entries.append((end_ts, match_id, queue_id, duration_seconds, tracked_participants))

    match_entries.sort(key=lambda row: row[0])
    for end_ts, match_id, queue_id, duration_seconds, tracked_participants in match_entries:
        queue_name = format_recap_queue_name(queue_id)
        end_local = datetime.fromtimestamp(end_ts, tz=report_timezone)
        duration_label = format_match_duration(duration_seconds)
        lines = [
            "\U0001F3AE **New Match Recap**",
            f"`{queue_name}` - \U0001F552 `{end_local:%d.%m.%Y %H:%M}` - \u23F1\uFE0F `{duration_label}`",
            "",
        ]
        for riot_id, participant in sorted(tracked_participants, key=lambda row: row[0].casefold()):
            lines.append(format_recap_player_line(riot_id, participant, duration_seconds))
        message = "\n".join(lines)
        if len(message) > 2000:
            message = message[:1950] + "\n..."
        await channel.send(message)
        log(f"[recap] Posted new match recap for {match_id} in channel {match_recap_channel_id}.")

    affected_riot_ids = set()
    for _end_ts, _match_id, _queue_id, _duration_seconds, tracked_participants in match_entries:
        for riot_id, _participant in tracked_participants:
            affected_riot_ids.add(riot_id)

    for riot_id in sorted(affected_riot_ids, key=str.casefold):
        puuid = puuid_by_riot_id.get(riot_id)
        recent_ids = recent_ids_by_riot_id.get(riot_id, [])
        if puuid is None or not recent_ids:
            continue
        try:
            streak_count, is_win_streak = await get_ranked_streak_info(riot_client, puuid, recent_ids)
            state_key = streak_callout_state_key(riot_id)
            if streak_count < 3 or is_win_streak is None:
                await asyncio.to_thread(db_set_state, state_key, "none")
                continue

            streak_token = f"{'W' if is_win_streak else 'L'}:{streak_count}"
            last_token = await asyncio.to_thread(db_get_state, state_key)
            if last_token == streak_token:
                continue
            await channel.send(format_streak_callout(riot_id, streak_count, is_win_streak))
            await asyncio.to_thread(db_set_state, state_key, streak_token)
            log(f"[recap] Posted streak callout for {riot_id} ({streak_token}).")
        except requests.RequestException as exc:
            log(f"[recap] Could not build streak callout for {riot_id}: {exc}")
        except Exception as exc:
            log(f"[recap] Streak callout skipped for {riot_id}: {exc}")

    if not db_enabled:
        return

    try:
        cycle_key = mood_service.get_cycle_key()

        for riot_id in sorted(affected_riot_ids, key=str.casefold):
            mode_records, performance_totals = await riot_client.get_today_mode_records(riot_id)
            await asyncio.to_thread(
                db_upsert_daily_stats,
                cycle_key,
                riot_id,
                mode_records,
                performance_totals,
            )

        mood_service.invalidate_report_cache()
        await edit_last_report_message(bypass_cache=True)
        log(
            f"[recap] Synced daily report after posting new match recap(s). "
            f"affected_players={len(affected_riot_ids)}"
        )
    except Exception as exc:
        log(f"[recap] Failed to sync daily report after recap: {exc}")
