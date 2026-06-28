import asyncio
import time

from src.report_logic import compute_role_baselines
from src import config as cfg


async def ensure_role_baselines(service):
    if service.db_load_match_payloads_for_baseline is None:
        return
    now = time.monotonic()
    if service._role_baselines is not None and (now - service._baselines_built_at) < service.BASELINE_TTL_SECONDS:
        return
    try:
        match_payloads = await asyncio.to_thread(service.db_load_match_payloads_for_baseline, cfg.BASELINE_MATCH_LIMIT)
        service._role_baselines = compute_role_baselines(match_payloads)
        service._baselines_built_at = now
        total_samples = sum(
            len(v) for stats in service._role_baselines.values() for v in stats.values()
        )
        service.log(
            f"[poro] Role baselines built: roles={len(service._role_baselines)} "
            f"matches={len(match_payloads)} samples={total_samples}"
        )
    except Exception as exc:
        service.log(f"[poro] Failed to build role baselines: {exc}")
