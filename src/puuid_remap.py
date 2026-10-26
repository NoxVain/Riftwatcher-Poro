"""Rewrite old PUUIDs in cached match payloads to the tracked players' current ones.

Riot encrypts PUUIDs per API key project, so after the key changed (Feb 2026) every
payload cached before then names tracked players by a PUUID we no longer use, and
lookups by PUUID (`get_participant`, recap attribution, analysis) miss those games.

The fix maps each tracked player's old PUUID to their current one by Riot ID:
participants whose Riot ID matches a tracked player but whose PUUID differs give the
old PUUID. The rewrite then replaces that PUUID everywhere in the cache, including
games played under an earlier Riot ID. Runs once per set of tracked players, in the
background after startup; a new player or a future key change triggers it again.
"""

import asyncio
import hashlib
import time
from collections import Counter, defaultdict

from src.runtime.memory import release_freed_memory

REMAP_STATE_KEY = "puuid_remap_fingerprint"
# Let startup work (scoreboards, first baselines) finish before scanning the cache.
REMAP_START_DELAY_SECONDS = 300


def participant_riot_key(participant):
    name = participant.get("riotIdGameName")
    tag = participant.get("riotIdTagline")
    if not name or not tag:
        return None
    return f"{name}#{tag}".casefold()


def tracked_fingerprint(tracked):
    """Stable hash of {riot_key: current_puuid}; changes when players or PUUIDs change."""
    lines = "\n".join(f"{key}={puuid}" for key, puuid in sorted(tracked.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def collect_puuid_observations(payloads, tracked):
    """Count (riot_key, puuid) pairs for participants whose Riot ID is tracked."""
    seen = Counter()
    for payload in payloads:
        try:
            participants = payload["info"]["participants"]
        except (KeyError, TypeError):
            continue
        for participant in participants:
            key = participant_riot_key(participant)
            puuid = participant.get("puuid")
            if key in tracked and puuid:
                seen[(key, puuid)] += 1
    return seen


def build_puuid_remap(observations, tracked, log=None):
    """Return {old_puuid: current_puuid}, skipping anything ambiguous.

    Skipped: a PUUID that is some tracked player's current one, a player seen with
    more than one other PUUID (someone else may have held the Riot ID), and an old
    PUUID seen under two tracked Riot IDs.
    """
    current_puuids = set(tracked.values())
    old_by_player = defaultdict(set)
    players_by_old = defaultdict(set)
    for key, puuid in observations:
        if puuid == tracked[key] or puuid in current_puuids:
            continue
        old_by_player[key].add(puuid)
        players_by_old[puuid].add(key)

    remap = {}
    for key, olds in sorted(old_by_player.items()):
        if len(olds) > 1:
            if log:
                log(f"[puuid] Skipping {key}: seen with {len(olds)} different old PUUIDs.")
            continue
        old = next(iter(olds))
        if len(players_by_old[old]) > 1:
            if log:
                log(f"[puuid] Skipping {key}: its old PUUID also appears under another tracked Riot ID.")
            continue
        remap[old] = tracked[key]
    return remap


def remap_cached_puuids(*, load_tracked, load_payloads, rewrite_puuids, get_state, set_state, log):
    """Blocking (pages through the whole cache); run via asyncio.to_thread.

    Returns a summary dict, or None when this set of tracked players was already done.
    The fingerprint is stored only after a successful rewrite, so a failed run is
    retried on the next startup (the rewrite is idempotent).
    """
    tracked = {riot_id.casefold(): puuid for riot_id, puuid in load_tracked() if puuid}
    if not tracked:
        return None
    fingerprint = tracked_fingerprint(tracked)
    if get_state(REMAP_STATE_KEY) == fingerprint:
        return None

    started = time.monotonic()
    match_count = 0

    def counted():
        nonlocal match_count
        for payload in load_payloads(0):
            match_count += 1
            yield payload

    observations = collect_puuid_observations(counted(), tracked)
    remap = build_puuid_remap(observations, tracked, log)
    rewritten = rewrite_puuids(remap) if remap else 0
    set_state(REMAP_STATE_KEY, fingerprint)
    return {
        "matches": match_count,
        "players": len(remap),
        "rewritten": rewritten,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


async def run_puuid_remap_once(
    *,
    db_load_tracked_puuids,
    db_load_match_payloads,
    db_rewrite_cached_puuids,
    db_get_state,
    db_set_state,
    log,
    start_delay_seconds=REMAP_START_DELAY_SECONDS,
):
    """Background task: never raises, so a failure can't affect the workers."""
    if start_delay_seconds > 0:
        await asyncio.sleep(start_delay_seconds)
    try:
        summary = await asyncio.to_thread(
            remap_cached_puuids,
            load_tracked=db_load_tracked_puuids,
            load_payloads=db_load_match_payloads,
            rewrite_puuids=db_rewrite_cached_puuids,
            get_state=db_get_state,
            set_state=db_set_state,
            log=log,
        )
    except Exception as exc:
        log(f"[puuid] PUUID remap failed; will retry on next startup: {exc}")
        return None
    finally:
        await asyncio.to_thread(release_freed_memory)
    if summary is not None:
        log(
            f"[puuid] Remapped old PUUIDs: players={summary['players']} "
            f"rewritten={summary['rewritten']} scanned={summary['matches']} "
            f"elapsed={summary['elapsed_ms']}ms"
        )
    return summary
