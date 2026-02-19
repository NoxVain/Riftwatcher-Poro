import asyncio

import requests


async def process_backfill_cycle(
    *,
    friends,
    riot_client,
    mood_service,
    db_get_last_seen_match_id,
    db_get_match_info,
    recent_ids_count,
    per_player_limit,
    log,
):
    total_backfilled = 0
    for riot_id in friends:
        try:
            puuid = await riot_client.fetch_puuid(riot_id, request_tier="backfill")
            recent_ids = await riot_client.fetch_recent_match_ids(
                puuid,
                count=max(1, recent_ids_count),
                request_tier="backfill",
            )
            if not recent_ids:
                continue

            last_seen_match_id = await asyncio.to_thread(db_get_last_seen_match_id, riot_id)
            new_match_ids = mood_service.get_new_match_ids(recent_ids, last_seen_match_id)
            if new_match_ids:
                continue

            backfilled_for_player = 0
            for match_id in reversed(recent_ids):
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
                    break

            if backfilled_for_player > 0:
                log(f"[backfill] Cached {backfilled_for_player} older match(es) for {riot_id}.")
        except requests.RequestException as exc:
            log(f"[backfill] Failed for {riot_id}: {exc}")
        except Exception as exc:
            log(f"[backfill] Unexpected error for {riot_id}: {exc}")

    return total_backfilled
