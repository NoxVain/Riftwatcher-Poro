import asyncio
import time

from src.report_logic import compute_role_baselines
from src import config as cfg
from src.runtime.memory import release_freed_memory


def _build_role_baselines(load_payloads, limit):
    """Runs in a worker thread: the loader streams payloads (DB I/O while iterating)."""
    match_count = 0

    def counted():
        nonlocal match_count
        for payload in load_payloads(limit):
            match_count += 1
            yield payload

    return compute_role_baselines(counted()), match_count


def _baselines_fresh(service):
    return (
        service._role_baselines is not None
        and (time.monotonic() - service._baselines_built_at) < service.BASELINE_TTL_SECONDS
    )


async def ensure_role_baselines(service):
    if service.db_load_match_payloads_for_baseline is None or _baselines_fresh(service):
        return
    lock = getattr(service, "_baselines_lock", None)
    if lock is None:
        # Created lazily for services built without one; no await between check and set.
        lock = service._baselines_lock = asyncio.Lock()
    async with lock:
        # Another caller may have finished a rebuild while this one waited (e.g. the
        # startup scoreboard and the refresh worker both asking at startup).
        if _baselines_fresh(service):
            return
        await _rebuild_role_baselines(service)


async def _rebuild_role_baselines(service):
    now = time.monotonic()
    try:
        service._role_baselines, match_count = await asyncio.to_thread(
            _build_role_baselines, service.db_load_match_payloads_for_baseline, cfg.BASELINE_MATCH_LIMIT
        )
        service._baselines_built_at = now
        total_samples = sum(
            len(v) for stats in service._role_baselines.values() for v in stats.values()
        )
        service.log(
            f"[poro] Role baselines built: roles={len(service._role_baselines)} "
            f"matches={match_count} samples={total_samples}"
        )
    except Exception as exc:
        service.log(f"[poro] Failed to build role baselines: {exc}")
    finally:
        await asyncio.to_thread(release_freed_memory)
