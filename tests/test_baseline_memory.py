"""Streaming match payloads for baselines/backfill, and returning freed memory to the OS."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from src.db import cache as db_cache
from src.runtime import memory
from src.services import baselines


class FakeCursor:
    def __init__(self, conn, name):
        self.conn = conn
        self.name = name
        self.itersize = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self.conn.executed.append((self.name, query, params, self.itersize))
        if self.conn.fail_on_execute:
            raise RuntimeError("db down")

    def __iter__(self):
        for row in self.conn.rows:
            self.conn.rows_read += 1
            yield row


class FakeConnection:
    def __init__(self, rows, fail_on_execute=False):
        self.rows = rows
        self.fail_on_execute = fail_on_execute
        self.executed = []
        self.rows_read = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, name=None):
        return FakeCursor(self, name)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def fake_db(monkeypatch):
    released = []

    def install(conn):
        monkeypatch.setattr(db_cache, "db_acquire_connection", lambda: conn)
        monkeypatch.setattr(db_cache, "db_release_connection", lambda c, discard=False: released.append((c, discard)))
        return released

    return install


def _rows(count):
    return [(json.dumps({"metadata": {"matchId": f"EUW1_{i}"}}),) for i in range(count)]


def test_loader_streams_with_named_cursor_and_skips_bad_rows(fake_db):
    conn = FakeConnection(_rows(3) + [(None,), ("not json",)])
    released = fake_db(conn)

    payloads = list(db_cache.db_load_match_payloads_for_baseline(5000, batch_size=200))

    assert [p["metadata"]["matchId"] for p in payloads] == ["EUW1_0", "EUW1_1", "EUW1_2"]
    name, query, params, itersize = conn.executed[0]
    assert name == "match_payload_scan"  # named = server-side cursor
    assert itersize == 200
    assert query.endswith("LIMIT %s") and params == (5000,)
    assert conn.commits == 1
    assert released == [(conn, False)]


def test_loader_without_limit_reads_everything(fake_db):
    conn = FakeConnection(_rows(2))
    fake_db(conn)

    assert len(list(db_cache.db_load_match_payloads_for_baseline(0))) == 2
    _name, query, params, _itersize = conn.executed[0]
    assert "LIMIT" not in query and params == ()


def test_loader_is_lazy_and_reuses_connection_when_stopped_early(fake_db):
    conn = FakeConnection(_rows(1000))
    released = fake_db(conn)

    stream = db_cache.db_load_match_payloads_for_baseline(0)
    assert conn.executed == []  # nothing runs until iterated
    next(stream)
    stream.close()

    assert conn.rows_read == 1
    assert conn.rollbacks == 1
    assert released == [(conn, False)]


def test_loader_discards_connection_on_db_error(fake_db):
    conn = FakeConnection([], fail_on_execute=True)
    released = fake_db(conn)

    with pytest.raises(RuntimeError):
        list(db_cache.db_load_match_payloads_for_baseline(10))

    assert conn.rollbacks == 1
    assert released == [(conn, True)]


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
