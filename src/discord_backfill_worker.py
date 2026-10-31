import asyncio
import time
from dataclasses import dataclass

import requests

# Once a pass reaches the end of a player's history with everything cached, the player
# rests this long. New games are cached by the recap worker as they're played, so the
# next pass only matters for gaps (the bot down for 20+ of the player's games).
BACKFILL_REVISIT_SECONDS = 7 * 24 * 3600


def backfill_offset_state_key(riot_id):
    return f"backfill_offset::{riot_id.casefold()}"


def backfill_done_state_key(riot_id):
    return f"backfill_done_at::{riot_id.casefold()}"


@dataclass
class BackfillCycleResult:
    cached: int = 0
    # Players skipped because their last pass finished less than BACKFILL_REVISIT_SECONDS ago.
    resting: int = 0


def _parse_timestamp(raw):
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


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
    now=None,
):
    now = time.time() if now is None else now
    result = BackfillCycleResult()
    for riot_id in friends:
        try:
            done_at = _parse_timestamp(await asyncio.to_thread(db_get_state, backfill_done_state_key(riot_id)))
            if done_at is not None and now - done_at < BACKFILL_REVISIT_SECONDS:
                result.resting += 1
                continue

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
                riot_id=riot_id,
            )
            if not page_ids:
                # End of the history Riot serves: everything is cached, so rest instead of starting over.
                await asyncio.to_thread(db_set_state, state_key, "0")
                await asyncio.to_thread(db_set_state, backfill_done_state_key(riot_id), str(int(now)))
                log(
                    f"[backfill] History fully cached for {riot_id} ({offset} matches); "
                    f"next pass in {BACKFILL_REVISIT_SECONDS // 86400} days."
                )
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
                result.cached += 1
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

    return result
