"""Paged match payloads for baselines/backfill, non-blocking baseline rebuilds, and memory release."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from src.db import cache as db_cache
from src.runtime import memory
from src.services import baselines


class FakeDb:
    """Stands in for db_execute: an ordered match cache keyed by match_id."""

    def __init__(self, payloads, *, missing=(), fail=False):
        self.ids = [match_id for match_id, _payload in payloads]
        self.payloads = dict(payloads)
        self.missing = set(missing)  # deleted between the ID query and the page query
        self.fail = fail
        self.queries = []

    def execute(self, query, params=None, fetch=False, fetchone=False):
        self.queries.append((query, params))
        if self.fail:
            raise RuntimeError("db down")
        if query.startswith("SELECT match_id FROM match_info_cache ORDER BY updated_at DESC"):
            limit = params[0] if params else len(self.ids)
            return [(match_id,) for match_id in self.ids[:limit]]
        if query.startswith("SELECT match_id, payload FROM match_info_cache WHERE match_id = ANY"):
            # Postgres returns ANY() rows in no particular order.
            return [
                (match_id, self.payloads[match_id])
                for match_id in reversed(params[0])
                if match_id not in self.missing
            ]
        raise AssertionError(f"unexpected query: {query}")


def _cache(count):
    return [(f"EUW1_{i}", json.dumps({"metadata": {"matchId": f"EUW1_{i}"}})) for i in range(count)]


def _match_ids(payloads):
    return [payload["metadata"]["matchId"] for payload in payloads]


def test_loader_pages_by_primary_key_in_newest_first_order(monkeypatch):
    fake = FakeDb(_cache(5) + [("EUW1_bad", "not json"), ("EUW1_empty", "")])
    monkeypatch.setattr(db_cache, "db_execute", fake.execute)

    payloads = list(db_cache.db_load_match_payloads_for_baseline(5000, batch_size=2))

    assert _match_ids(payloads) == ["EUW1_0", "EUW1_1", "EUW1_2", "EUW1_3", "EUW1_4"]
    id_query, id_params = fake.queries[0]
    assert id_query.endswith("LIMIT %s;") and id_params == (5000,)
    pages = [params[0] for _query, params in fake.queries[1:]]
    assert pages == [["EUW1_0", "EUW1_1"], ["EUW1_2", "EUW1_3"], ["EUW1_4", "EUW1_bad"], ["EUW1_empty"]]
    # No server-side cursor or long transaction: every query is a plain db_execute call.
    assert not any("FETCH" in query or "DECLARE" in query for query, _params in fake.queries)


def test_loader_without_limit_reads_everything(monkeypatch):
    fake = FakeDb(_cache(3))
    monkeypatch.setattr(db_cache, "db_execute", fake.execute)

    assert len(list(db_cache.db_load_match_payloads_for_baseline(0))) == 3
    id_query, id_params = fake.queries[0]
    assert "LIMIT" not in id_query and id_params == ()


def test_loader_skips_rows_deleted_between_queries(monkeypatch):
    fake = FakeDb(_cache(3), missing={"EUW1_1"})
    monkeypatch.setattr(db_cache, "db_execute", fake.execute)

    assert _match_ids(db_cache.db_load_match_payloads_for_baseline(10)) == ["EUW1_0", "EUW1_2"]


def test_loader_is_lazy_and_reads_one_page_at_a_time(monkeypatch):
    fake = FakeDb(_cache(500))
    monkeypatch.setattr(db_cache, "db_execute", fake.execute)

    stream = db_cache.db_load_match_payloads_for_baseline(0, batch_size=50)
    assert fake.queries == []  # nothing runs until iterated
    next(stream)
    stream.close()

    assert len(fake.queries) == 2  # the ID list and the first page only


def test_loader_propagates_db_errors(monkeypatch):
    monkeypatch.setattr(db_cache, "db_execute", FakeDb([], fail=True).execute)

    with pytest.raises(RuntimeError):
        list(db_cache.db_load_match_payloads_for_baseline(10))


def _ranked_payload():
    participants = [
        {"teamPosition": "MIDDLE", "totalMinionsKilled": 200, "kills": 5, "deaths": 2, "visionScore": 20}
        for _ in range(10)
    ]
    return {"info": {"queueId": 420, "gameDuration": 1800, "participants": participants}}


def test_ensure_role_baselines_consumes_stream_in_worker_thread_and_releases_memory(monkeypatch):
    main_thread = threading.get_ident()
    consumed_in = []
    releases = []

    def loader(limit):
        assert limit == 5000
        for _ in range(3):
            consumed_in.append(threading.get_ident())
            yield _ranked_payload()

    monkeypatch.setattr(baselines.cfg, "BASELINE_MATCH_LIMIT", 5000)
    monkeypatch.setattr(baselines, "release_freed_memory", lambda: releases.append(True))
    logs = []
    service = SimpleNamespace(
        db_load_match_payloads_for_baseline=loader,
        _role_baselines=None,
        _baselines_built_at=0.0,
        BASELINE_TTL_SECONDS=43200,
        log=logs.append,
    )

    asyncio.run(baselines.ensure_role_baselines(service))

    assert consumed_in and main_thread not in consumed_in
    assert "MIDDLE" in service._role_baselines
    assert any("matches=3" in line for line in logs)
    assert releases == [True]


def test_ensure_role_baselines_releases_memory_even_when_build_fails(monkeypatch):
    releases = []
    monkeypatch.setattr(baselines, "release_freed_memory", lambda: releases.append(True))

    def failing_loader(_limit):
        raise RuntimeError("db down")
        yield  # pragma: no cover

    logs = []
    service = SimpleNamespace(
        db_load_match_payloads_for_baseline=failing_loader,
        _role_baselines=None,
        _baselines_built_at=0.0,
        BASELINE_TTL_SECONDS=43200,
        log=logs.append,
    )

    asyncio.run(baselines.ensure_role_baselines(service))

    assert service._role_baselines is None
    assert any("Failed to build role baselines" in line for line in logs)
    assert releases == [True]


def _baseline_service(loader, logs):
    return SimpleNamespace(
        db_load_match_payloads_for_baseline=loader,
        _role_baselines=None,
        _baselines_built_at=0.0,
        BASELINE_TTL_SECONDS=43200,
        log=logs.append,
    )


def test_concurrent_callers_build_baselines_once(monkeypatch):
    monkeypatch.setattr(baselines, "release_freed_memory", lambda: None)
    loads = []

    def slow_loader(_limit):
        loads.append(threading.get_ident())
        time.sleep(0.05)  # keep the first build running while the second caller arrives
        for _ in range(3):  # baselines need 20+ samples per role/stat
            yield _ranked_payload()

    logs = []
    service = _baseline_service(slow_loader, logs)

    async def run():
        # Startup: the scoreboard and the refresh worker both ask for baselines.
        await asyncio.gather(baselines.ensure_role_baselines(service), baselines.ensure_role_baselines(service))

    asyncio.run(run())

    assert len(loads) == 1
    assert sum("Role baselines built" in line for line in logs) == 1
    assert "MIDDLE" in service._role_baselines


def test_callers_never_wait_on_a_rebuild_when_baselines_exist(monkeypatch):
    monkeypatch.setattr(baselines, "release_freed_memory", lambda: None)
    release = threading.Event()

    def stuck_loader(_limit):
        release.wait(5)  # a rebuild stuck in the DB read, as in production
        yield _ranked_payload()

    service = _baseline_service(stuck_loader, [])
    service._role_baselines = {"MIDDLE": {"cs_per_min": [1.0] * 20}}  # stale but usable
    service._baselines_built_at = time.monotonic() - service.BASELINE_TTL_SECONDS - 1

    async def run():
        started = time.monotonic()
        await baselines.ensure_role_baselines(service)
        returned_after = time.monotonic() - started
        # While the rebuild is still stuck, callers keep scoring with the previous baselines.
        assert service._role_baselines["MIDDLE"]["cs_per_min"]
        release.set()
        await service._baselines_task
        return returned_after

    assert asyncio.run(run()) < 0.5


def test_first_build_wait_is_bounded(monkeypatch):
    monkeypatch.setattr(baselines, "release_freed_memory", lambda: None)
    monkeypatch.setattr(baselines, "FIRST_BUILD_WAIT_SECONDS", 0.05)
    release = threading.Event()

    def stuck_loader(_limit):
        release.wait(5)
        yield _ranked_payload()

    logs = []
    service = _baseline_service(stuck_loader, logs)

    async def run():
        await baselines.ensure_role_baselines(service)
        assert service._role_baselines is None  # scoring proceeds without baselines
        release.set()
        await service._baselines_task

    asyncio.run(run())
    assert any("not ready yet" in line for line in logs)


def test_timed_out_rebuild_keeps_previous_baselines_and_backs_off(monkeypatch):
    monkeypatch.setattr(baselines, "release_freed_memory", lambda: None)
    monkeypatch.setattr(baselines, "REBUILD_TIMEOUT_SECONDS", 0)
    previous = {"MIDDLE": {"cs_per_min": [1.0] * 20}}
    loads = []

    def slow_loader(_limit):
        loads.append(1)
        time.sleep(0.01)
        for _ in range(3):
            yield _ranked_payload()

    logs = []
    service = _baseline_service(slow_loader, logs)
    service._role_baselines = previous
    service._baselines_built_at = time.monotonic() - service.BASELINE_TTL_SECONDS - 1

    async def run():
        await baselines.ensure_role_baselines(service)
        await service._baselines_task
        await baselines.ensure_role_baselines(service)  # within the retry window: no new rebuild
        assert service._baselines_task.done()

    asyncio.run(run())
    assert service._role_baselines is previous
    assert loads == [1]
    assert any("timed out" in line for line in logs)


def test_expired_baselines_are_rebuilt(monkeypatch):
    monkeypatch.setattr(baselines, "release_freed_memory", lambda: None)
    clock = [1000.0]
    monkeypatch.setattr(baselines.time, "monotonic", lambda: clock[0])
    loads = []

    def loader(_limit):
        loads.append(clock[0])
        yield _ranked_payload()

    service = _baseline_service(loader, [])

    asyncio.run(baselines.ensure_role_baselines(service))
    asyncio.run(baselines.ensure_role_baselines(service))  # still fresh: no rebuild
    clock[0] += service.BASELINE_TTL_SECONDS + 1
    asyncio.run(baselines.ensure_role_baselines(service))

    assert loads == [1000.0, 1000.0 + service.BASELINE_TTL_SECONDS + 1]


def test_release_freed_memory_trims_with_glibc(monkeypatch):
    calls = []

    class FakeLibc:
        def __init__(self, _name):
            self.malloc_trim = self._trim

        @staticmethod
        def _trim(pad):
            calls.append(pad)
            return 1

    monkeypatch.setattr(memory, "_malloc_trim_checked", False)
    monkeypatch.setattr(memory, "_malloc_trim", None)
    monkeypatch.setattr(memory.ctypes, "CDLL", FakeLibc)
    monkeypatch.setattr(memory.gc, "collect", lambda: calls.append("gc"))

    assert memory.release_freed_memory() is True
    assert calls == ["gc", 0]


def test_release_freed_memory_without_glibc_only_collects(monkeypatch):
    calls = []

    def missing_libc(_name):
        raise OSError("not glibc")

    monkeypatch.setattr(memory, "_malloc_trim_checked", False)
    monkeypatch.setattr(memory, "_malloc_trim", None)
    monkeypatch.setattr(memory.ctypes, "CDLL", missing_libc)
    monkeypatch.setattr(memory.gc, "collect", lambda: calls.append("gc"))

    assert memory.release_freed_memory() is False
    assert calls == ["gc"]
