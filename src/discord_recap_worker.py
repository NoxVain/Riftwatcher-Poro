import asyncio
from datetime import datetime

import requests

from src.arena_static import load_arena_display_names
from src.discord_text import (
    ARENA_QUEUE_IDS,
    format_match_duration,
    get_arena_placement,
    get_arena_subteam_id,
    format_recap_player_line,
    format_recap_queue_name,
    format_streak_callout,
    parse_streak_tts_enabled,
    match_recap_state_key,
    streak_callout_state_key,
    streak_tts_enabled_state_key,
)
from src.report_logic import derive_primary_role, get_match_duration_seconds, get_match_end_unix_seconds, get_mode_bucket, is_remake_match


async def get_ranked_streak_info(riot_client, puuid, recent_ids, max_matches=20):
    streak_result = None
    streak_count = 0
    for match_id in recent_ids[:max(1, max_matches)]:
        match_info = await riot_client.fetch_match_info(match_id)
        if is_remake_match(match_info):
            continue
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


def _pack_sections_into_messages(sections, separator, max_len=2000):
    messages = []
    current = []
    for section in sections:
        candidate = separator.join(current + [section]) if current else section
        if len(candidate) <= max_len:
            current.append(section)
        elif current:
            messages.append(separator.join(current))
            current = [section]
        else:
            messages.append(section[:max_len - 50] + "\n...")
    if current:
        messages.append(separator.join(current))
    return messages


def _recap_participant_sort_key(queue_id, row):
    riot_id, participant = row
    if queue_id in ARENA_QUEUE_IDS:
        placement = get_arena_placement(participant)
        subteam_id = get_arena_subteam_id(participant)
        safe_placement = placement if placement is not None else 99
        safe_subteam_id = subteam_id if isinstance(subteam_id, int) else 99
        return (safe_placement, safe_subteam_id, riot_id.casefold())
    return (riot_id.casefold(),)


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
    edit_last_weekly_report_message=None,
    log,
):
    recap_split_spacing_seconds = 3.0
    recap_section_separator = "\n\n---\n\n"
    puuid_by_riot_id = {}
    recent_ids_by_riot_id = {}
    matches_to_report = set()
    pending_latest_match_id_by_riot = {}
    pending_new_match_ids_by_riot = {}
    for riot_id in friends:
        try:
            puuid = await riot_client.fetch_puuid(riot_id)
            puuid_by_riot_id[riot_id] = puuid
            recent_ids = await riot_client.fetch_recent_match_ids(puuid, count=20, riot_id=riot_id)
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
                pending_latest_match_id_by_riot[riot_id] = latest_match_id
                pending_new_match_ids_by_riot[riot_id] = set(new_match_ids)
        except requests.RequestException as exc:
            log(f"[recap] Failed while checking {riot_id}: {exc}")

    if not matches_to_report:
        return

    puuid_to_riot_id = {puuid: riot_id for riot_id, puuid in puuid_by_riot_id.items()}
    match_entries = []
    failed_match_ids = set()
    for match_id in matches_to_report:
        try:
            match_info = await riot_client.fetch_match_info(match_id)
        except requests.RequestException as exc:
            failed_match_ids.add(match_id)
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
        if is_remake_match(match_info):
            continue
        duration_seconds = get_match_duration_seconds(match_info)
        match_entries.append((end_ts, match_id, queue_id, duration_seconds, tracked_participants))

    match_entries.sort(key=lambda row: row[0])
    recap_sections = []
    arena_display_names = None
    for end_ts, match_id, queue_id, duration_seconds, tracked_participants in match_entries:
        queue_name = format_recap_queue_name(queue_id)
        end_local = datetime.fromtimestamp(end_ts, tz=report_timezone)
        duration_label = format_match_duration(duration_seconds)
        lines = [
            "\U0001F3AE **New Match Recap**",
            f"`{queue_name}` - \U0001F552 `{end_local:%d.%m.%Y %H:%M}` - \u23F1\uFE0F `{duration_label}`",
            "",
        ]
        if queue_id in ARENA_QUEUE_IDS and arena_display_names is None:
            arena_display_names = await asyncio.to_thread(load_arena_display_names)
        augment_names = (arena_display_names or {}).get("augment_names", {})
        item_names = (arena_display_names or {}).get("item_names", {})
        ordered_participants = sorted(tracked_participants, key=lambda row: _recap_participant_sort_key(queue_id, row))
        for index, (riot_id, participant) in enumerate(ordered_participants):
            lines.append(
                format_recap_player_line(
                    riot_id,
                    participant,
                    duration_seconds,
                    queue_id=queue_id,
                    augment_names=augment_names,
                    item_names=item_names,
                )
            )
            if index < len(ordered_participants) - 1:
                lines.append("")
        recap_sections.append("\n".join(lines))
        log(f"[recap] Prepared new match recap for {match_id} in channel {match_recap_channel_id}.")

    recap_messages = _pack_sections_into_messages(recap_sections, recap_section_separator)

    for riot_id, latest_match_id in pending_latest_match_id_by_riot.items():
        failed_for_player = False
        for match_id in pending_new_match_ids_by_riot.get(riot_id, set()):
            if match_id in failed_match_ids:
                failed_for_player = True
                break
        if failed_for_player:
            log(
                f"[recap] Keeping previous recap state for {riot_id}; "
                "will retry due to fetch failures in this cycle."
            )
            continue
        state_key = match_recap_state_key(riot_id)
        await asyncio.to_thread(db_set_state, state_key, latest_match_id)

    affected_riot_ids = set()
    for _end_ts, _match_id, _queue_id, _duration_seconds, tracked_participants in match_entries:
        for riot_id, _participant in tracked_participants:
            affected_riot_ids.add(riot_id)
    if not affected_riot_ids:
        return

    streak_sections = []
    pending_streak_state_updates = []
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
            streak_sections.append(format_streak_callout(riot_id, streak_count, is_win_streak))
            pending_streak_state_updates.append((state_key, streak_token, riot_id))
        except requests.RequestException as exc:
            log(f"[recap] Could not build streak callout for {riot_id}: {exc}")
        except Exception as exc:
            log(f"[recap] Streak callout skipped for {riot_id}: {exc}")

    for index, recap_message in enumerate(recap_messages):
        await channel.send(recap_message)
        if index < len(recap_messages) - 1:
            await asyncio.sleep(recap_split_spacing_seconds)
    if recap_messages:
        log(
            f"[recap] Posted recap batch in channel {match_recap_channel_id}: "
            f"matches={len(recap_sections)} messages={len(recap_messages)}."
        )

    streak_tts_enabled = True
    try:
        raw_tts_state = await asyncio.to_thread(db_get_state, streak_tts_enabled_state_key())
        streak_tts_enabled = parse_streak_tts_enabled(raw_tts_state, default=True)
    except Exception as exc:
        log(f"[recap] Could not read streak TTS setting: {exc}")

    for streak_message in streak_sections:
        await channel.send(streak_message, tts=streak_tts_enabled)
    if streak_sections:
        log(
            f"[recap] Posted streak callout batch in channel {match_recap_channel_id}: "
            f"streaks={len(streak_sections)} tts={str(streak_tts_enabled).lower()}."
        )
    for state_key, streak_token, riot_id in pending_streak_state_updates:
        await asyncio.to_thread(db_set_state, state_key, streak_token)
        log(f"[recap] Posted streak callout for {riot_id} ({streak_token}).")

    if not db_enabled:
        return

    try:
        cycle_key = mood_service.get_cycle_key()

        for riot_id in sorted(affected_riot_ids, key=str.casefold):
            mode_records, performance_totals = await riot_client.get_today_mode_records(riot_id)
            primary_role = derive_primary_role(performance_totals)
            await asyncio.to_thread(
                db_upsert_daily_stats,
                cycle_key,
                riot_id,
                mode_records,
                performance_totals,
                primary_role,
            )

        mood_service.invalidate_report_cache()
        await edit_last_report_message(bypass_cache=True)
        if edit_last_weekly_report_message is not None:
            await edit_last_weekly_report_message(bypass_cache=True)
        log(
            f"[recap] Synced daily report after posting new match recap(s). "
            f"affected_players={len(affected_riot_ids)}"
        )
    except Exception as exc:
        log(f"[recap] Failed to sync daily report after recap: {exc}")
