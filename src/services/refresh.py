import asyncio
from datetime import datetime, timezone

import requests

from src.report_logic import (
    accumulate_participant_performance,
    derive_primary_role,
    get_match_duration_seconds,
    get_mode_bucket,
    is_match_in_report_cycle,
    is_remake_match,
)


async def get_players_ordered_by_oldest_stats(service, cycle_key):
    players = list(service.friends)
    if not service.db_enabled:
        return players

    try:
        stored_rows = await asyncio.to_thread(service.db_load_latest_stats, cycle_key)
    except Exception as exc:
        service.log(f"[refresh] Could not load stats ordering from DB: {exc}")
        return players

    updated_at_by_riot_id = {}
    for row in stored_rows or []:
        riot_id = row["riot_id"]
        updated_at = row["updated_at"]
        updated_at_by_riot_id[str(riot_id).casefold()] = updated_at

    def sort_key(riot_id):
        updated_at = updated_at_by_riot_id.get(riot_id.casefold())
        if updated_at is None:
            return (0, datetime.min.replace(tzinfo=timezone.utc), riot_id.casefold())
        return (1, updated_at.astimezone(timezone.utc), riot_id.casefold())

    return sorted(players, key=sort_key)


async def refresh_daily_stats_once(service, progress_callback=None):
    if not service.db_enabled:
        return
    service.log("[refresh] Starting daily stats refresh.")
    service.riot_client.clear_match_cache()
    cycle_key = service.get_cycle_key()
    total_players = len(service.friends)
    processed_players = 0
    ordered_players = await get_players_ordered_by_oldest_stats(service, cycle_key)
    for riot_id in ordered_players:
        try:
            mode_records, performance_totals = await service.riot_client.get_today_mode_records(riot_id)
            primary_role = derive_primary_role(performance_totals)
            await asyncio.to_thread(
                service.db_upsert_daily_stats,
                cycle_key,
                riot_id,
                mode_records,
                performance_totals,
                primary_role,
            )
        except requests.RequestException as exc:
            service.log(f"[refresh] Failed for {riot_id}: {exc}")
        finally:
            processed_players += 1
            if progress_callback is not None:
                await progress_callback(processed_players, total_players, riot_id)
    service.invalidate_report_cache()
    service.log("[refresh] Daily stats refresh complete.")


async def refresh_recent_matches_snapshot(service, recent_count=20):
    if not service.db_enabled:
        return
    service.log(f"[refresh] Running on-demand recent refresh (count={recent_count})")
    cycle_key = service.get_cycle_key()
    changed_any = False
    candidates = []

    for riot_id in service.friends:
        try:
            puuid = await service.riot_client.fetch_puuid(riot_id)
            recent_ids = await service.riot_client.fetch_recent_match_ids(
                puuid,
                count=max(1, recent_count),
                riot_id=riot_id,
            )
            if not recent_ids:
                continue

            last_seen_match_id = await asyncio.to_thread(service.db_get_last_seen_match_id, riot_id)
            new_match_ids = service.get_new_match_ids(recent_ids, last_seen_match_id)
            if not new_match_ids:
                continue
            candidates.append((riot_id, puuid, recent_ids[0], new_match_ids))
        except requests.RequestException as exc:
            service.log(f"[refresh] On-demand refresh failed for {riot_id}: {exc}")

    for riot_id, puuid, latest_match_id, new_match_ids in candidates:
        try:
            row = await asyncio.to_thread(service.db_get_daily_stats_for_player, cycle_key, riot_id)
            if row is None:
                service.log(
                    f"[refresh] No baseline daily stats for {riot_id}; "
                    "running full player refresh fallback."
                )
                mode_records, performance_totals = await service.riot_client.get_today_mode_records(riot_id)
                primary_role = derive_primary_role(performance_totals)
                await asyncio.to_thread(
                    service.db_upsert_daily_stats,
                    cycle_key,
                    riot_id,
                    mode_records,
                    performance_totals,
                    primary_role,
                )
                await asyncio.to_thread(service.db_set_last_seen_match_id, riot_id, latest_match_id)
                changed_any = True
                continue

            mode_records = {
                "solo_duo": {"wins": int(row["solo_wins"]), "losses": int(row["solo_losses"])},
                "flex": {"wins": int(row["flex_wins"]), "losses": int(row["flex_losses"])},
            }
            performance_totals = {
                "cs_total": int(row["cs_total"] or 0),
                "minutes_total": float(row["minutes_total"] or 0.0),
                "objective_damage": int(row["objective_damage"] or 0),
                "player_damage": int(row["player_damage"] or 0),
                "healing": int(row["healing"] or 0),
                "damage_taken": int(row["damage_taken"] or 0),
                "kills": int(row["kills"] or 0),
                "assists": int(row.get("assists", 0) or 0),
                "deaths": int(row["deaths"] or 0),
                "vision_score": int(row["vision_score"] or 0),
                "gold_earned": int(row.get("gold_earned", 0) or 0),
                "wards_placed": int(row.get("wards_placed", 0) or 0),
                "wards_killed": int(row.get("wards_killed", 0) or 0),
                "turret_takedowns": int(row.get("turret_takedowns", 0) or 0),
                "dragon_takedowns": int(row.get("dragon_takedowns", 0) or 0),
                "baron_takedowns": int(row.get("baron_takedowns", 0) or 0),
                "double_kills": int(row.get("double_kills", 0) or 0),
                "triple_kills": int(row.get("triple_kills", 0) or 0),
                "quadra_kills": int(row.get("quadra_kills", 0) or 0),
                "penta_kills": int(row.get("penta_kills", 0) or 0),
                "kill_participation_num": int(row.get("kill_participation_num", 0) or 0),
                "kill_participation_den": int(row.get("kill_participation_den", 0) or 0),
            }
            player_changed = False
            for match_id in reversed(new_match_ids):
                match_info = await service.riot_client.fetch_match_info(match_id)
                if not is_match_in_report_cycle(
                    match_info,
                    service.report_timezone,
                    day_start_hour=service.report_day_start_hour,
                ):
                    continue
                if is_remake_match(match_info):
                    continue
                queue_id = match_info["info"].get("queueId", -1)
                bucket_name = get_mode_bucket(queue_id)
                if bucket_name is None:
                    continue
                participant = service.riot_client.get_participant(match_info, puuid)
                if participant is None:
                    continue
                result = participant.get("win")
                if result is True:
                    mode_records[bucket_name]["wins"] += 1
                    player_changed = True
                elif result is False:
                    mode_records[bucket_name]["losses"] += 1
                    player_changed = True
                duration_seconds = get_match_duration_seconds(match_info)
                accumulate_participant_performance(
                    performance_totals,
                    participant,
                    duration_seconds,
                    match_info=match_info,
                )

            if player_changed:
                await asyncio.to_thread(
                    service.db_upsert_daily_stats,
                    cycle_key,
                    riot_id,
                    mode_records,
                    performance_totals,
                )
                changed_any = True

            await asyncio.to_thread(service.db_set_last_seen_match_id, riot_id, latest_match_id)
        except requests.RequestException as exc:
            service.log(f"[refresh] On-demand refresh failed for {riot_id}: {exc}")
    if changed_any:
        service.invalidate_report_cache()
