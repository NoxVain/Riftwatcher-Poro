import asyncio
import time

from src.report_logic import compute_role_baselines
from src import config as cfg
from src.runtime.memory import release_freed_memory

# Scoreboards never wait on a rebuild once baselines exist. Before the first build,
# callers wait at most this long, then score without performance data (pure Wilson).
FIRST_BUILD_WAIT_SECONDS = 30
# A rebuild that takes longer is abandoned: previous baselines stay in use.
REBUILD_TIMEOUT_SECONDS = 120
# After a failed or timed-out rebuild, don't start another before this.
REBUILD_RETRY_SECONDS = 300


def _build_role_baselines(load_payloads, limit, deadline):
    """Runs in a worker thread: the loader pages payloads (DB I/O while iterating).

    Stops between payloads once `deadline` (time.monotonic) passes, so an abandoned
    rebuild doesn't keep a thread busy.
    """
    match_count = 0

    def counted():
        nonlocal match_count
        for payload in load_payloads(limit):
            if time.monotonic() > deadline:
                raise TimeoutError("role baseline rebuild exceeded its time limit")
            match_count += 1
            yield payload

    return compute_role_baselines(counted()), match_count


def _baselines_fresh(service):
    return (
        service._role_baselines is not None
        and (time.monotonic() - service._baselines_built_at) < service.BASELINE_TTL_SECONDS
    )


async def ensure_role_baselines(service):
    """Start a rebuild when baselines are missing or stale; never block on one for long.

    One rebuild runs at a time (a background task shared by all callers). While it
    runs, callers keep using the previous baselines; only the very first build is
    waited for, up to FIRST_BUILD_WAIT_SECONDS.
    """
    if service.db_load_match_payloads_for_baseline is None or _baselines_fresh(service):
        return
    task = getattr(service, "_baselines_task", None)
    if task is None or task.done():
        if time.monotonic() < getattr(service, "_baselines_retry_at", 0.0):
            return
        # No await between the check and the assignment, so concurrent callers share it.
        task = service._baselines_task = asyncio.create_task(_rebuild_role_baselines(service))
    if service._role_baselines is not None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=FIRST_BUILD_WAIT_SECONDS)
    except TimeoutError:
        service.log("[poro] Role baselines not ready yet; scoring without them for now.")


async def _rebuild_role_baselines(service):
    started = time.monotonic()
    service.log("[poro] Building role baselines...")
    try:
        service._role_baselines, match_count = await asyncio.wait_for(
            asyncio.to_thread(
                _build_role_baselines,
                service.db_load_match_payloads_for_baseline,
                cfg.BASELINE_MATCH_LIMIT,
                started + REBUILD_TIMEOUT_SECONDS,
            ),
            # Grace period: the thread checks the deadline itself between payloads.
            timeout=REBUILD_TIMEOUT_SECONDS + 15,
        )
        service._baselines_built_at = started
        total_samples = sum(
            len(v) for stats in service._role_baselines.values() for v in stats.values()
        )
        service.log(
            f"[poro] Role baselines built: roles={len(service._role_baselines)} "
            f"matches={match_count} samples={total_samples} "
            f"elapsed={int((time.monotonic() - started) * 1000)}ms"
        )
    except TimeoutError:
        service._baselines_retry_at = time.monotonic() + REBUILD_RETRY_SECONDS
        service.log(
            f"[poro] Role baseline rebuild timed out after {REBUILD_TIMEOUT_SECONDS}s; "
            f"keeping previous baselines, retrying in {REBUILD_RETRY_SECONDS}s."
        )
    except Exception as exc:
        service._baselines_retry_at = time.monotonic() + REBUILD_RETRY_SECONDS
        service.log(f"[poro] Failed to build role baselines: {exc}")
    finally:
        await asyncio.to_thread(release_freed_memory)
