import asyncio
import time

import requests

from src.rank_logic import format_rank_change_message, normalize_queue_type
from src.recap_lp import build_lp_event, build_lp_history_row, handle_lp_event, retry_stored_lp_events


async def process_rank_cycle(
    *,
    friends,
    channel,
    riot_client,
    db_load_ranked_state,
    db_upsert_ranked_state,
    db_delete_ranked_state_queue,
    log,
    recap_channel=None,
    db_get_state=None,
    db_set_state=None,
    db_insert_lp_change=None,
):
    lp_enabled = db_get_state is not None and db_set_state is not None
    for riot_id in friends:
        try:
            previous_rows = await asyncio.to_thread(db_load_ranked_state, riot_id)
            previous_by_queue = {}
            for row in previous_rows:
                queue_type = normalize_queue_type(row["queue_type"]) or row["queue_type"]
                previous_by_queue[queue_type] = {
                    "tier": row["tier"],
                    "rank_division": row["rank_division"],
                    "league_points": int(row["league_points"] or 0),
                    "wins": int(row["wins"] or 0),
                    "losses": int(row["losses"] or 0),
                    "hot_streak": bool(row["hot_streak"]),
                    "veteran": bool(row["veteran"]),
                    "fresh_blood": bool(row["fresh_blood"]),
                    "inactive": bool(row["inactive"]),
                }

            current_entries_raw = await riot_client.fetch_ranked_entries(riot_id)
            current_by_queue = {}
            for entry in current_entries_raw:
                queue_type = normalize_queue_type(entry.get("queueType"))
                if queue_type is None:
                    continue
                current_by_queue[queue_type] = entry

            # First observation for this player: persist baseline without notifications.
            if not previous_by_queue:
                for queue_type, entry in current_by_queue.items():
                    await asyncio.to_thread(db_upsert_ranked_state, riot_id, queue_type, entry)
                continue

            for queue_type in sorted(set(previous_by_queue.keys()) | set(current_by_queue.keys())):
                previous_entry = previous_by_queue.get(queue_type)
                current_entry = current_by_queue.get(queue_type)
                message = format_rank_change_message(riot_id, queue_type, previous_entry, current_entry)
                if message:
                    await channel.send(message)
                    log(f"[rank] Rank change posted for {riot_id} ({queue_type}).")

            if lp_enabled:
                await _record_lp_events(
                    riot_id, previous_by_queue, current_by_queue,
                    recap_channel=recap_channel, db_get_state=db_get_state, db_set_state=db_set_state, log=log,
                )
            if db_insert_lp_change is not None:
                await _record_lp_history(riot_id, previous_by_queue, current_by_queue, db_insert_lp_change, log)

            for queue_type, entry in current_by_queue.items():
                await asyncio.to_thread(db_upsert_ranked_state, riot_id, queue_type, entry)
            for queue_type in previous_by_queue.keys():
                if queue_type not in current_by_queue:
                    await asyncio.to_thread(db_delete_ranked_state_queue, riot_id, queue_type)
        except requests.RequestException as exc:
            log(f"[rank] Failed rank-check for {riot_id}: {exc}")
        except Exception as exc:
            log(f"[rank] Unexpected rank-check error for {riot_id}: {exc}")


async def _record_lp_events(riot_id, previous_by_queue, current_by_queue, *, recap_channel, db_get_state, db_set_state, log):
    now = time.time()
    try:
        await retry_stored_lp_events(
            riot_id=riot_id,
            recap_channel=recap_channel,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            log=log,
            now=now,
        )
    except Exception as exc:
        log(f"[rank] Could not retry stored LP changes for {riot_id}: {exc}")
    for queue_type, current_entry in current_by_queue.items():
        event = build_lp_event(previous_by_queue.get(queue_type), current_entry, now)
        if event is None:
            continue
        try:
            await handle_lp_event(
                riot_id=riot_id,
                event=event,
                recap_channel=recap_channel,
                db_get_state=db_get_state,
                db_set_state=db_set_state,
                log=log,
                now=now,
            )
        except Exception as exc:
            # LP is cosmetic; never let it block rank-state persistence.
            log(f"[rank] Could not record LP change for {riot_id} ({queue_type}): {exc}")


async def _record_lp_history(riot_id, previous_by_queue, current_by_queue, db_insert_lp_change, log):
    for queue_type, current_entry in current_by_queue.items():
        row = build_lp_history_row(riot_id, queue_type, previous_by_queue.get(queue_type), current_entry)
        if row is None:
            continue
        try:
            await asyncio.to_thread(db_insert_lp_change, row)
        except Exception as exc:
            # History is for analysis only; never let it block rank alerts or state.
            log(f"[rank] Could not store LP history for {riot_id} ({queue_type}): {exc}")
