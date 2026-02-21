import asyncio

import requests


def backfill_offset_state_key(riot_id):
    return f"backfill_offset::{riot_id.casefold()}"


async def process_backfill_cycle(
    *,
    friends,
    riot_client,
    db_get_state,
    db_set_state,
    db_get_match_info,
    recent_ids_count,
    per_player_limit,
    log,
):
    total_backfilled = 0
    for riot_id in friends:
        try:
            puuid = await riot_client.fetch_puuid(riot_id, request_tier="backfill")
            state_key = backfill_offset_state_key(riot_id)
            raw_offset = await asyncio.to_thread(db_get_state, state_key)
            try:
                offset = max(0, int(raw_offset or 0))
            except ValueError:
                offset = 0

            page_ids = await riot_client.fetch_match_ids_page(
                puuid,
                start=offset,
                count=max(1, recent_ids_count),
                request_tier="backfill",
            )
            if not page_ids:
                if offset > 0:
                    await asyncio.to_thread(db_set_state, state_key, "0")
                continue

            backfilled_for_player = 0
            page_fully_processed = True
            ordered_ids = list(reversed(page_ids))
            for index, match_id in enumerate(ordered_ids):
                cached = await asyncio.to_thread(db_get_match_info, match_id)
                if cached is not None:
                    continue
                await riot_client.fetch_match_info(
                    match_id,
                    cache_in_memory=False,
                    request_tier="backfill",
                )
                backfilled_for_player += 1
                total_backfilled += 1
                if backfilled_for_player >= max(1, per_player_limit):
                    remaining_ids = ordered_ids[index + 1 :]
                    has_remaining_uncached = False
                    for remaining_match_id in remaining_ids:
                        remaining_cached = await asyncio.to_thread(db_get_match_info, remaining_match_id)
                        if remaining_cached is None:
                            has_remaining_uncached = True
                            break
                    page_fully_processed = not has_remaining_uncached
                    break

            if page_fully_processed:
                await asyncio.to_thread(db_set_state, state_key, str(offset + len(page_ids)))
            else:
                # Keep the same offset so the next cycle continues filling this page.
                await asyncio.to_thread(db_set_state, state_key, str(offset))

            if backfilled_for_player > 0:
                log(
                    f"[backfill] Cached {backfilled_for_player} older match(es) for {riot_id} "
                    f"(offset={offset}, page={len(page_ids)})."
                )
        except requests.RequestException as exc:
            log(f"[backfill] Failed for {riot_id}: {exc}")
        except Exception as exc:
            log(f"[backfill] Unexpected error for {riot_id}: {exc}")

    return total_backfilled
