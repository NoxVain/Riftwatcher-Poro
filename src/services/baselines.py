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


async def ensure_role_baselines(service):
    if service.db_load_match_payloads_for_baseline is None:
        return
    now = time.monotonic()
    if service._role_baselines is not None and (now - service._baselines_built_at) < service.BASELINE_TTL_SECONDS:
        return
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
