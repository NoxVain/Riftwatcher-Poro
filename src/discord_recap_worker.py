import asyncio
import time

import discord
import requests

from src.arena_static import load_arena_display_names
from src.champion_icons import (
    CHAMPION_DISPLAY_STATE_KEY,
    DEFAULT_CHAMPION_DISPLAY_MODE,
    RECAP_LOOKUP_BUDGET_SECONDS,
    format_champion_display,
    parse_champion_display_mode,
)
from src.discord_text import (
    ARENA_QUEUE_IDS,
    get_arena_placement,
    get_arena_subteam_id,
    format_streak_callout,
    parse_streak_tts_enabled,
    match_recap_state_key,
    streak_callout_state_key,
    streak_tts_enabled_state_key,
)
from src.recap_lp import (
    QUEUE_TYPE_BY_QUEUE_ID,
    LP_EDIT_LOCK,
    edit_recap_with_lp,
    restore_lp_event,
    format_lp_summary,
    register_pending_recap,
    take_lp_event_for_game,
)
from src.recap_cards import (
    CardMatch, CardPlayer, add_card_lp, build_card_messages, card_result,
    format_card_player, no_confirmed_result, safe_text,
)
from src.report_logic import derive_primary_role, get_match_duration_seconds, get_match_end_unix_seconds, get_mode_bucket, is_remake_match


async def _attach_stored_lp(
    player_line, *, riot_id, match_id, queue_type, won, end_ts, db_get_state, db_set_state, lp_waiting, taken_events, log
):
    heading = player_line.split("\n", 1)[0]
    try:
        event = await take_lp_event_for_game(
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            riot_id=riot_id,
            queue_type=queue_type,
            won=won,
            end_ts=end_ts,
            now=time.time(),
        )
    except Exception as exc:
        log(f"[recap] Could not read LP state for {riot_id}: {exc}")
        return player_line
    if event is not None:
        taken_events.append((riot_id, event))
        return add_card_lp(player_line, format_lp_summary(event))
    # The rank worker hasn't seen this game yet; it edits the posted recap when it does.
    lp_waiting.append(
        {
            "riot_id": riot_id,
            "pending": {
                "match_id": match_id,
                "queue_type": queue_type,
                "won": won,
                "end_ts": end_ts,
                "heading": heading,
            },
        }
    )
    return player_line


async def _register_lp_waiting(lp_waiting, message, channel, *, db_get_state, db_set_state, log):
    if message is None:
        return
    for waiting in lp_waiting:
        riot_id = waiting["riot_id"]
        pending = waiting["pending"]
        pending = dict(pending, message_id=message.id)
        event = None
        try:
            event = await register_pending_recap(
                db_get_state=db_get_state,
                db_set_state=db_set_state,
                riot_id=riot_id,
                pending=pending,
                now=time.time(),
            )
            if event is not None:
                async with LP_EDIT_LOCK:
                    latest = await channel.fetch_message(message.id)
                    edited = await edit_recap_with_lp(
                        latest, pending["heading"], format_lp_summary(event), component_id=pending["component_id"]
                    )
                if edited is None:
                    raise ValueError("LP component no longer matches the recap")
        except Exception as exc:
            if event is not None:
                await restore_lp_event(riot_id=riot_id, event=event, pending=pending,
                                       db_get_state=db_get_state, db_set_state=db_set_state)
            log(f"[recap] Could not track LP for {riot_id} ({pending['match_id']}): {exc}")


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
        if no_confirmed_result(match_info):
            break
        participant = riot_client.get_participant(match_info, puuid)
        if participant is None:
            continue
        if type(participant.get("win")) is not bool:
            break
        won = participant["win"]
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


def _split_large_section(section, max_len=2000):
    chunks = []
    lines = section.split("\n\n")
    current_chunk = []
    current_len = 0
    for line in lines:
        if len(line) > max_len:
            sublines = line.split("\n")
            for subline in sublines:
                if len(subline) > max_len:
                    sub_chunks = [subline[i:i+max_len] for i in range(0, len(subline), max_len)]
                    for sc in sub_chunks:
                        if current_chunk:
                            chunks.append("\n".join(current_chunk))
                            current_chunk = []
                            current_len = 0
                        chunks.append(sc)
                else:
                    if current_len + len(subline) + 1 > max_len:
                        if current_chunk:
                            chunks.append("\n".join(current_chunk))
                        current_chunk = [subline]
                        current_len = len(subline)
                    else:
                        current_chunk.append(subline)
                        current_len += len(subline) + 1
            continue

        if current_len + len(line) + 2 > max_len:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += len(line) + (2 if len(current_chunk) > 1 else 0)
            
    if current_chunk:
        chunks.append("\n\n".join(current_chunk))
    return chunks


def _pack_sections_into_messages(sections, separator, max_len=2000):
    messages = []
    current = []
    for section in sections:
        candidate = separator.join(current + [section]) if current else section
        if len(candidate) <= max_len:
            current.append(section)
        elif current:
            messages.append(separator.join(current))
            if len(section) > max_len:
                section_chunks = _split_large_section(section, max_len)
                current = [section_chunks[-1]]
                for chunk in section_chunks[:-1]:
                    messages.append(chunk)
            else:
                current = [section]
        else:
            section_chunks = _split_large_section(section, max_len)
            current = [section_chunks[-1]]
            for chunk in section_chunks[:-1]:
                messages.append(chunk)
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
    poro_service,
    report_timezone,
    match_recap_channel_id,
    channel,
    db_enabled,
    db_get_state,
    db_set_state,
    db_upsert_daily_stats,
    edit_last_report_message,
    edit_last_weekly_report_message=None,
    champion_icons=None,
    log,
):
    recap_split_spacing_seconds = 3.0
    puuid_by_riot_id = {}
    recent_ids_by_riot_id = {}
    matches_to_report = set()
    pending_latest_match_id_by_riot = {}
    pending_new_match_ids_by_riot = {}
    checked_players = 0
    players_with_recent_ids = 0
    initialized_players = 0
    players_with_new_ids = 0
    for riot_id in friends:
        try:
            checked_players += 1
            puuid = await riot_client.fetch_puuid(riot_id)
            puuid_by_riot_id[riot_id] = puuid
            recent_ids = await riot_client.fetch_recent_match_ids(puuid, count=20, riot_id=riot_id)
            recent_ids_by_riot_id[riot_id] = recent_ids
            if not recent_ids:
                continue

            players_with_recent_ids += 1
            state_key = match_recap_state_key(riot_id)
            last_announced = await asyncio.to_thread(db_get_state, state_key)
            latest_match_id = recent_ids[0]
            if not last_announced:
                await asyncio.to_thread(db_set_state, state_key, latest_match_id)
                initialized_players += 1
                continue

            new_match_ids = poro_service.get_new_match_ids(recent_ids, last_announced)
            if new_match_ids:
                matches_to_report.update(new_match_ids)
                pending_latest_match_id_by_riot[riot_id] = latest_match_id
                pending_new_match_ids_by_riot[riot_id] = set(new_match_ids)
                players_with_new_ids += 1
        except requests.RequestException as exc:
            log(f"[recap] Failed while checking {riot_id}: {exc}")

    if not matches_to_report:
        log(
            "[recap] No new matches to post: "
            f"checked_players={checked_players} players_with_recent_ids={players_with_recent_ids} "
            f"initialized_players={initialized_players} players_with_new_ids={players_with_new_ids}."
        )
        return

    puuid_to_riot_id = {puuid: riot_id for riot_id, puuid in puuid_by_riot_id.items()}
    match_entries = []
    match_results = {}
    failed_match_ids = set()
    skipped_no_tracked_participants = 0
    skipped_remakes = 0
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
            skipped_no_tracked_participants += 1
            continue

        end_ts = get_match_end_unix_seconds(match_info)
        queue_id = int(match_info.get("info", {}).get("queueId", -1))
        if is_remake_match(match_info):
            skipped_remakes += 1
            continue
        duration_seconds = get_match_duration_seconds(match_info)
        match_entries.append((end_ts, match_id, queue_id, duration_seconds, tracked_participants))
        match_results[match_id] = card_result(match_info, tracked_participants)

    log(
        "[recap] Match scan summary: "
        f"candidate_matches={len(matches_to_report)} prepared_matches={len(match_entries)} "
        f"failed_fetches={len(failed_match_ids)} skipped_no_tracked={skipped_no_tracked_participants} "
        f"skipped_remakes={skipped_remakes}."
    )

    match_entries.sort(key=lambda row: row[0])
    champion_display_mode = "name" if champion_icons is None else DEFAULT_CHAMPION_DISPLAY_MODE
    if champion_icons is not None:
        try:
            champion_display_mode = parse_champion_display_mode(
                await asyncio.to_thread(db_get_state, CHAMPION_DISPLAY_STATE_KEY)
            )
        except Exception as exc:
            log(f"[recap] Could not read champion display setting: {exc}")
    icon_lookup_deadline = time.monotonic() + RECAP_LOOKUP_BUDGET_SECONDS
    lp_waiting = []
    taken_events = []
    card_matches = []
    arena_display_names = None
    for end_ts, match_id, queue_id, duration_seconds, tracked_participants in match_entries:
        result = match_results[match_id]
        card_players = []
        if queue_id in ARENA_QUEUE_IDS and arena_display_names is None:
            arena_display_names = await asyncio.to_thread(load_arena_display_names)
        augment_names = (arena_display_names or {}).get("augment_names", {})
        item_names = (arena_display_names or {}).get("item_names", {})
        ordered_participants = sorted(tracked_participants, key=lambda row: _recap_participant_sort_key(queue_id, row))
        for riot_id, participant in ordered_participants:
            champion_icon = None
            if champion_display_mode != "name":
                champion_icon = await champion_icons.get(
                    participant.get("championId"),
                    cached_only=time.monotonic() >= icon_lookup_deadline,
                )
            player_line = format_card_player(
                riot_id,
                participant,
                duration_seconds,
                queue_id=queue_id,
                augment_names=augment_names,
                item_names=item_names,
                champion_display=format_champion_display(
                    safe_text(participant.get("championName", "Unknown")), champion_display_mode, champion_icon
                ),
                mixed=result == "Mixed results",
            )
            pending_count = len(lp_waiting)
            lp_queue_type = QUEUE_TYPE_BY_QUEUE_ID.get(queue_id)
            if lp_queue_type is not None and db_enabled and result not in {"Draw", "No result"}:
                player_line = await _attach_stored_lp(
                    player_line,
                    riot_id=riot_id,
                    match_id=match_id,
                    queue_type=lp_queue_type,
                    won=bool(participant.get("win")),
                    end_ts=end_ts,
                    db_get_state=db_get_state,
                    db_set_state=db_set_state,
                    lp_waiting=lp_waiting,
                    taken_events=taken_events,
                    log=log,
                )
            pending = lp_waiting[-1]["pending"] if len(lp_waiting) > pending_count else None
            card_players.append(CardPlayer(riot_id, player_line, pending))
        card_matches.append(CardMatch(queue_id, duration_seconds, result, card_players))
        log(f"[recap] Prepared new match recap for {match_id} in channel {match_recap_channel_id}.")

    recap_messages = build_card_messages(card_matches)

    pending_cursor_updates = []
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
        pending_cursor_updates.append((state_key, latest_match_id))

    affected_riot_ids = set()
    for _end_ts, _match_id, _queue_id, _duration_seconds, tracked_participants in match_entries:
        for riot_id, _participant in tracked_participants:
            affected_riot_ids.add(riot_id)
    if not affected_riot_ids:
        for state_key, latest_match_id in pending_cursor_updates:
            await asyncio.to_thread(db_set_state, state_key, latest_match_id)
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

    try:
        for index, recap_message in enumerate(recap_messages):
            sent = await channel.send(view=recap_message.view, allowed_mentions=discord.AllowedMentions.none())
            if recap_message.waiting:
                await _register_lp_waiting(
                    recap_message.waiting, sent, channel,
                    db_get_state=db_get_state, db_set_state=db_set_state, log=log,
                )
            if index < len(recap_messages) - 1:
                await asyncio.sleep(recap_split_spacing_seconds)
    except Exception:
        for riot_id, event in taken_events:
            await restore_lp_event(riot_id=riot_id, event=event, db_get_state=db_get_state, db_set_state=db_set_state)
        raise
    for state_key, latest_match_id in pending_cursor_updates:
        await asyncio.to_thread(db_set_state, state_key, latest_match_id)
    if recap_messages:
        log(
            f"[recap] Posted recap batch in channel {match_recap_channel_id}: "
            f"matches={len(card_matches)} messages={len(recap_messages)}."
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
        cycle_key = poro_service.get_cycle_key()

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

        poro_service.invalidate_report_cache()
        await edit_last_report_message(bypass_cache=True)
        if edit_last_weekly_report_message is not None:
            await edit_last_weekly_report_message(bypass_cache=True)
        log(
            f"[recap] Synced daily report after posting new match recap(s). "
            f"affected_players={len(affected_riot_ids)}"
        )
    except Exception as exc:
        log(f"[recap] Failed to sync daily report after recap: {exc}")
