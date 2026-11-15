import asyncio
import contextvars
import re
from types import SimpleNamespace

import discord
import pytest

from src.runtime import workers as runtime_workers


def test_evaluate_rank_changes_and_notify_no_channel_noop(monkeypatch):
    called = {"process_rank_cycle": 0}

    async def fake_process_rank_cycle(**_kwargs):
        called["process_rank_cycle"] += 1

    monkeypatch.setattr(runtime_workers, "process_rank_cycle", fake_process_rank_cycle)

    async def resolve_channel(_channel_id):
        return None

    asyncio.run(
        runtime_workers.evaluate_rank_changes_and_notify(
            resolve_channel=resolve_channel,
            events_channel_id=123,
            friends=["Alpha#NA1"],
            riot_client=object(),
            db_load_ranked_state=lambda: {},
            db_upsert_ranked_state=lambda *_args, **_kwargs: None,
            db_delete_ranked_state_queue=lambda *_args, **_kwargs: None,
            log=lambda _msg: None,
        )
    )

    assert called["process_rank_cycle"] == 0


def test_evaluate_rank_changes_and_notify_invokes_process_cycle(monkeypatch):
    called = {"kwargs": None}

    async def fake_process_rank_cycle(**kwargs):
        called["kwargs"] = kwargs

    monkeypatch.setattr(runtime_workers, "process_rank_cycle", fake_process_rank_cycle)

    class FakeChannel:
        id = 456

    async def resolve_channel(_channel_id):
        return FakeChannel()

    friends = ["Alpha#NA1"]
    riot_client = object()

    asyncio.run(
        runtime_workers.evaluate_rank_changes_and_notify(
            resolve_channel=resolve_channel,
            events_channel_id=123,
            friends=friends,
            riot_client=riot_client,
            db_load_ranked_state=lambda: {"x": 1},
            db_upsert_ranked_state=lambda *_args, **_kwargs: None,
            db_delete_ranked_state_queue=lambda *_args, **_kwargs: None,
            log=lambda _msg: None,
        )
    )

    assert called["kwargs"] is not None
    assert called["kwargs"]["friends"] == friends
    assert called["kwargs"]["riot_client"] is riot_client


def test_background_daily_refresher_updates_runtime_cleanup_state(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

    class FakeMessage:
        def __init__(self):
            self.id = 9
            self.content = "old"
            self.edits = []

        async def edit(self, *, content):
            self.content = content
            self.edits.append(content)

    class FakeChannel:
        id = 123

    class FakePoroService:
        async def build_today_win_rate_report(self, prefer_snapshot=False, bypass_cache=False):
            _ = (prefer_snapshot, bypass_cache)
            return "snapshot"

        async def refresh_daily_stats_once(self, progress_callback=None):
            if progress_callback is not None:
                await progress_callback(1, 1, "Alpha#NA1")

    client = FakeClient()
    message = FakeMessage()
    cleanup_calls = []
    weekly_calls = []
    logs = []
    worker_stats = {"refresh": {"cycles": 0, "errors": 0}}
    report_state = {"channel_id": 123, "message_id": 9}
    runtime_state = {"last_cache_cleanup_at": 0.0}

    async def fake_sleep(_seconds):
        client.closed = True

    async def fake_resolve_channel(_channel_id):
        return FakeChannel()

    async def fake_get_or_create_report_message(_channel, _initial_content):
        return message

    async def fake_edit_weekly(*, bypass_cache=False):
        weekly_calls.append(bypass_cache)

    def fake_cleanup(retention_days):
        cleanup_calls.append(retention_days)
        return 3

    monkeypatch.setattr(runtime_workers.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(runtime_workers.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(runtime_workers.time, "monotonic", lambda: 5000.0)

    asyncio.run(
        runtime_workers.background_daily_refresher(
            daily_refresh_seconds=30,
            client=client,
            request_id_context=__import__("contextvars").ContextVar("request_id", default=None),
            poro_service=FakePoroService(),
            resolve_channel=fake_resolve_channel,
            daily_report_channel_id=123,
            get_or_create_report_message=fake_get_or_create_report_message,
            edit_last_weekly_report_message=fake_edit_weekly,
            db_cleanup_old_match_cache=fake_cleanup,
            match_cache_retention_days=10,
            db_set_last_report_message=lambda _channel_id, _message_id: None,
            report_state=report_state,
            runtime_state=runtime_state,
            worker_stats=worker_stats,
            log=logs.append,
        )
    )

    assert worker_stats["refresh"]["cycles"] == 1
    assert worker_stats["refresh"]["errors"] == 0
    assert worker_stats["refresh"]["runs"] == 1
    assert worker_stats["refresh"]["elapsed_ms_last"] >= 0
    assert worker_stats["refresh"]["elapsed_ms_avg"] >= 0
    assert worker_stats["refresh"]["elapsed_ms_max"] >= worker_stats["refresh"]["elapsed_ms_last"]
    assert worker_stats["refresh"]["last_success_at"] > 0.0
    assert message.edits
    assert cleanup_calls == [10]
    assert weekly_calls == [True]
    assert runtime_state["last_cache_cleanup_at"] > 0.0
    assert "last_cache_cleanup_at" not in report_state


def _run_patch_loop(monkeypatch, *, last_checked, cycle_error=None, stop_after_sleeps=2):
    class FakeClient:
        closed = False

        def is_closed(self):
            return self.closed

    client = FakeClient()
    sleeps = []
    cycles = []
    worker_stats = {}

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= stop_after_sleeps:
            client.closed = True

    async def fake_cycle(**kwargs):
        cycles.append(kwargs)
        if cycle_error:
            raise cycle_error

    async def resolve_channel(_channel_id):
        return object()

    monkeypatch.setattr(runtime_workers.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(runtime_workers.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(runtime_workers.time, "time", lambda: 10_000)
    monkeypatch.setattr(runtime_workers, "process_patch_notes_cycle", fake_cycle)

    asyncio.run(
        runtime_workers.background_patch_notes_notifier(
            client=client,
            poll_seconds=21600,
            retry_seconds=3600,
            request_id_context=__import__("contextvars").ContextVar("request_id", default=None),
            resolve_channel=resolve_channel,
            events_channel_id=123,
            patch_client=object(),
            db_get_state=lambda _key: last_checked,
            db_set_state=lambda _key, _value: None,
            worker_stats=worker_stats,
            log=lambda _msg: None,
        )
    )
    return sleeps, cycles, worker_stats


def test_patch_notes_loop_waits_for_remaining_interval_after_restart(monkeypatch):
    # Last check 1000s ago: wait the remaining 20600s before the first fetch, then a full interval.
    sleeps, cycles, worker_stats = _run_patch_loop(monkeypatch, last_checked="9000", stop_after_sleeps=2)

    assert sleeps == [20600, 21600]
    assert len(cycles) == 1
    assert worker_stats["patch"]["cycles"] == 1


def test_patch_notes_loop_checks_immediately_when_due_and_retries_sooner_on_error(monkeypatch):
    sleeps, cycles, worker_stats = _run_patch_loop(
        monkeypatch, last_checked=None, cycle_error=RuntimeError("site down"), stop_after_sleeps=1
    )

    assert sleeps == [3600]
    assert len(cycles) == 1
    assert worker_stats["patch"]["errors"] == 1


# --- Worker loops -----------------------------------------------------------------
# What every loop does around its cycle: startup jitter, a request ID, counting the cycle,
# logging errors without stopping, and sleeping until the next one.


class LoopHarness:
    """Runs a worker loop with a fake clock, recorded sleeps and a fixed startup jitter."""

    def __init__(self, monkeypatch, *, jitter=0.0, stop_after_sleeps=1, closed=False):
        self.closed = closed
        self.client = SimpleNamespace(is_closed=lambda: self.closed)
        self.sleeps = []
        self.request_ids_at_sleep = []
        self.jitter_bounds = []
        self.logs = []
        self.stats = {}
        self.request_id = contextvars.ContextVar("request_id", default=None)
        self._clock = 1000.0

        async def sleep(seconds):
            self.sleeps.append(seconds)
            self.request_ids_at_sleep.append(self.request_id.get())
            if len(self.sleeps) >= stop_after_sleeps:
                self.closed = True

        def uniform(low, high):
            self.jitter_bounds.append((low, high))
            return jitter

        def monotonic():
            self._clock += 0.25
            return self._clock

        monkeypatch.setattr(runtime_workers, "asyncio", SimpleNamespace(sleep=sleep, to_thread=asyncio.to_thread))
        monkeypatch.setattr(runtime_workers, "random", SimpleNamespace(uniform=uniform))
        monkeypatch.setattr(runtime_workers, "time", SimpleNamespace(monotonic=monotonic, time=lambda: 10_000))
        monkeypatch.setattr(runtime_workers, "create_request_id", lambda prefix: f"{prefix}-test")

    def run(self, loop, **kwargs):
        asyncio.run(loop(
            client=self.client, request_id_context=self.request_id, worker_stats=self.stats, log=self.logs.append, **kwargs
        ))


class Cycle:
    """Stands in for a worker's cycle: records each call and can fail the first `fail_times` calls."""

    def __init__(self, harness, *, fail_times=0, result=None):
        self.harness = harness
        self.fail_times = fail_times
        self.result = result
        self.calls = []
        self.request_ids = []

    async def __call__(self, *_args, **kwargs):
        self.calls.append(kwargs)
        self.request_ids.append(self.harness.request_id.get())
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("boom")
        return self.result


async def _channel(_channel_id):
    return SimpleNamespace(id=55)


async def _no_channel(_channel_id):
    return None


async def _noop_async(*_args, **_kwargs):
    return None


def _rank_loop(monkeypatch, harness, cycle, *, seconds=300, resolve_channel=_channel):
    return runtime_workers.background_rank_notifier, dict(
        daily_refresh_seconds=seconds, evaluate_rank_changes_and_notify_fn=cycle,
    )


def _recap_loop(monkeypatch, harness, cycle, *, seconds=90, resolve_channel=_channel):
    monkeypatch.setattr(runtime_workers, "process_recap_cycle", cycle)
    monkeypatch.setattr(runtime_workers, "ChampionIcons", lambda client, log: SimpleNamespace(client=client, log=log))
    return runtime_workers.background_match_recap_notifier, dict(
        match_recap_poll_seconds=seconds, friends=["Alpha#EUW"], riot_client="riot", poro_service="service",
        match_recap_channel_id=5, db_get_state="get_state", db_set_state="set_state",
        db_upsert_daily_stats="upsert", edit_last_report_message="edit_daily",
        edit_last_weekly_report_message="edit_weekly", resolve_channel=resolve_channel,
        db_iter_record_rows="iter_rows", db_upsert_player_match_stats="upsert_stats",
    )


def _esports_loop(monkeypatch, harness, cycle, *, seconds=60, resolve_channel=_channel):
    monkeypatch.setattr(runtime_workers, "process_esports_cycle", cycle)
    return runtime_workers.background_esports_notifier, dict(
        esports_poll_seconds=seconds, resolve_channel=resolve_channel, news_channel_id=5, esports_client="client",
        league_slugs=["lec"], report_timezone="tz", report_day_start_hour=6, spoiler_tags=True,
        db_get_state="get_state", db_set_state="set_state",
    )


def _patch_loop(monkeypatch, harness, cycle, *, seconds=21600, resolve_channel=_channel):
    monkeypatch.setattr(runtime_workers, "process_patch_notes_cycle", cycle)
    return runtime_workers.background_patch_notes_notifier, dict(
        poll_seconds=seconds, retry_seconds=3600, resolve_channel=resolve_channel, events_channel_id=7,
        patch_client="client", db_get_state=lambda _key: None, db_set_state=lambda _key, _value: None,
    )


def _backfill_loop(monkeypatch, harness, cycle, *, seconds=300, resolve_channel=_channel):
    if cycle.result is None:
        cycle.result = SimpleNamespace(cached=0, resting=0)
    monkeypatch.setattr(runtime_workers, "process_backfill_cycle", cycle)
    return runtime_workers.background_match_cache_backfiller, dict(
        daily_refresh_seconds=seconds, friends=["Alpha#EUW", "Bravo#EUW", "Charlie#EUW"], riot_client="riot",
        db_get_state="get_state", db_set_state="set_state", db_get_match_info="get_match",
        db_load_backfill_offsets=lambda: {"alpha#euw": 300, "bravo#euw": 0},
    )


def _refresh_loop(monkeypatch, harness, cycle, *, seconds=300, resolve_channel=_channel):
    class Service:
        async def build_today_win_rate_report(self, prefer_snapshot=False, bypass_cache=False):
            return "snapshot"

        async def refresh_daily_stats_once(self, progress_callback=None):
            await cycle()

    message = SimpleNamespace(id=9, content="snapshot")

    async def get_or_create_report_message(_channel, _initial_content):
        return message

    return runtime_workers.background_daily_refresher, dict(
        daily_refresh_seconds=seconds, poro_service=Service(), resolve_channel=resolve_channel,
        daily_report_channel_id=5, get_or_create_report_message=get_or_create_report_message,
        edit_last_weekly_report_message=_noop_async, db_cleanup_old_match_cache=lambda _days: 0,
        match_cache_retention_days=0, db_set_last_report_message=lambda _channel_id, _message_id: None,
        report_state={}, runtime_state={"last_cache_cleanup_at": 1000.0},
    )


# name -> (builder, log label, request ID prefix, error log, sleep after a good cycle, after a failed one, jitter cap)
WORKER_LOOPS = {
    "rank": (_rank_loop, "rank", "rank", "[rank] Unexpected background error: boom", 300, 300, 30.0),
    "recap": (_recap_loop, "recap", "recap", "[recap] Unexpected error: boom", 90, 90, 30.0),
    "esports": (_esports_loop, "esports", "esports", "[esports] Unexpected error: boom", 60, 60, 15.0),
    "patch": (_patch_loop, "patch", "patch", "[patch] Unexpected error: boom", 21600, 3600, 60.0),
    "backfill": (_backfill_loop, "backfill", "backfill", "[backfill] Unexpected background error: boom", 600, 600, 60.0),
    "refresh": (_refresh_loop, "refresh", "bg", "[refresh] Unexpected error: boom", 300, 300, 30.0),
}
ALL_LOOPS = pytest.mark.parametrize("name", sorted(WORKER_LOOPS))


@ALL_LOOPS
def test_loop_runs_a_cycle_counts_it_and_sleeps_until_the_next(monkeypatch, name):
    builder, label, prefix, _error_log, good_sleep, _bad_sleep, jitter_cap = WORKER_LOOPS[name]
    harness = LoopHarness(monkeypatch)
    cycle = Cycle(harness)
    loop, kwargs = builder(monkeypatch, harness, cycle)
    harness.run(loop, **kwargs)

    assert len(cycle.calls) == 1
    # The cycle runs under its own request ID, which is cleared again before the sleep.
    assert cycle.request_ids == [f"{prefix}-test"]
    assert harness.request_ids_at_sleep == [None]
    assert harness.sleeps == [good_sleep]
    assert harness.jitter_bounds == [(0.0, jitter_cap)]
    assert not any("Startup jitter" in line for line in harness.logs)
    stats = harness.stats[label]
    assert (stats["cycles"], stats["errors"], stats["runs"]) == (1, 0, 1)
    assert stats["last_success_at"] > 1000.0
    assert re.fullmatch(rf"\[{label}\] Cycle complete elapsed=\d+ms next_sleep={good_sleep}s", harness.logs[-1])


@pytest.mark.parametrize("name", ["rank", "recap", "esports", "patch", "backfill"])
def test_loop_times_the_cycle_with_the_monotonic_clock(monkeypatch, name):
    # These loops read the clock once before and once after the cycle; the fake clock adds 250 ms per read.
    builder, label, _prefix, _error_log, good_sleep, _bad_sleep, _jitter_cap = WORKER_LOOPS[name]
    harness = LoopHarness(monkeypatch)
    loop, kwargs = builder(monkeypatch, harness, Cycle(harness))
    harness.run(loop, **kwargs)

    assert harness.logs[-1] == f"[{label}] Cycle complete elapsed=250ms next_sleep={good_sleep}s"
    stats = harness.stats[label]
    assert (stats["elapsed_ms_last"], stats["elapsed_ms_max"], stats["elapsed_ms_avg"]) == (250, 250, 250)
    assert stats["last_success_at"] == 1000.5


@ALL_LOOPS
def test_loop_logs_a_failed_cycle_counts_it_and_keeps_going(monkeypatch, name):
    builder, label, prefix, error_log, good_sleep, bad_sleep, _jitter_cap = WORKER_LOOPS[name]
    harness = LoopHarness(monkeypatch, stop_after_sleeps=2)
    cycle = Cycle(harness, fail_times=1)
    loop, kwargs = builder(monkeypatch, harness, cycle)
    harness.run(loop, **kwargs)

    assert len(cycle.calls) == 2
    assert error_log in harness.logs
    assert harness.sleeps == [bad_sleep, good_sleep]
    assert harness.request_ids_at_sleep == [None, None]
    stats = harness.stats[label]
    assert (stats["cycles"], stats["errors"], stats["runs"]) == (1, 1, 2)
    completed = [line for line in harness.logs if line.startswith(f"[{label}] Cycle complete")]
    assert len(completed) == 2


@ALL_LOOPS
def test_loop_does_not_count_a_failed_cycle_as_a_success(monkeypatch, name):
    builder, label, _prefix, _error_log, _good_sleep, bad_sleep, _jitter_cap = WORKER_LOOPS[name]
    harness = LoopHarness(monkeypatch)
    loop, kwargs = builder(monkeypatch, harness, Cycle(harness, fail_times=1))
    harness.run(loop, **kwargs)

    stats = harness.stats[label]
    assert (stats["cycles"], stats["errors"], stats["runs"]) == (0, 1, 1)
    # The stall watchdog reads this: a failed cycle must not look like progress.
    assert stats["last_success_at"] == 0.0
    assert harness.sleeps == [bad_sleep]


@ALL_LOOPS
def test_loop_sleeps_the_startup_jitter_before_the_first_cycle(monkeypatch, name):
    builder, label, _prefix, _error_log, _good_sleep, _bad_sleep, _jitter_cap = WORKER_LOOPS[name]
    harness = LoopHarness(monkeypatch, jitter=7.5)
    cycle = Cycle(harness)
    loop, kwargs = builder(monkeypatch, harness, cycle)
    harness.run(loop, **kwargs)

    assert harness.logs[0] == f"[{label}] Startup jitter sleep=7.5s"
    assert harness.sleeps[0] == 7.5
    # The harness closes the client during that sleep, so no cycle runs.
    assert cycle.calls == [] and harness.stats == {}


@ALL_LOOPS
def test_loop_runs_nothing_once_the_client_is_closed(monkeypatch, name):
    builder, _label, _prefix, _error_log, _good_sleep, _bad_sleep, _jitter_cap = WORKER_LOOPS[name]
    harness = LoopHarness(monkeypatch, closed=True)
    cycle = Cycle(harness)
    loop, kwargs = builder(monkeypatch, harness, cycle)
    harness.run(loop, **kwargs)

    assert cycle.calls == [] and harness.sleeps == [] and harness.stats == {}


@pytest.mark.parametrize("name, sleep, jitter_cap", [
    ("rank", 30, 15.0),
    ("recap", 30, 15.0),
    ("esports", 30, 15.0),
    ("backfill", 120, 60.0),
    ("refresh", 30, 15.0),
])
def test_loop_intervals_have_a_floor(monkeypatch, name, sleep, jitter_cap):
    builder = WORKER_LOOPS[name][0]
    harness = LoopHarness(monkeypatch)
    loop, kwargs = builder(monkeypatch, harness, Cycle(harness), seconds=10)
    harness.run(loop, **kwargs)

    assert harness.sleeps == [sleep]
    assert harness.jitter_bounds == [(0.0, jitter_cap)]


def test_recap_loop_passes_the_channel_and_one_champion_icon_cache_to_every_cycle(monkeypatch):
    harness = LoopHarness(monkeypatch, stop_after_sleeps=2)
    cycle = Cycle(harness)
    loop, kwargs = _recap_loop(monkeypatch, harness, cycle)
    harness.run(loop, **kwargs)

    first, second = cycle.calls
    assert set(first) == {
        "friends", "riot_client", "poro_service", "match_recap_channel_id", "channel", "champion_icons",
        "db_get_state", "db_set_state", "db_upsert_daily_stats", "edit_last_report_message",
        "edit_last_weekly_report_message", "db_iter_record_rows", "db_upsert_player_match_stats", "log",
    }
    assert first["channel"].id == 55 and first["match_recap_channel_id"] == 5
    assert first["db_iter_record_rows"] == "iter_rows" and first["db_upsert_player_match_stats"] == "upsert_stats"
    # Built once for the worker's lifetime, so portrait lookups stay cached between cycles.
    assert first["champion_icons"] is second["champion_icons"]
    assert first["champion_icons"].client is harness.client


def test_recap_loop_without_a_channel_waits_and_records_no_cycle(monkeypatch):
    harness = LoopHarness(monkeypatch)
    cycle = Cycle(harness)
    loop, kwargs = _recap_loop(monkeypatch, harness, cycle, resolve_channel=_no_channel)
    harness.run(loop, **kwargs)

    assert cycle.calls == []
    assert harness.sleeps == [90]
    # No cycle is counted and nothing is logged, so the stall watchdog is what reports a missing channel.
    assert harness.stats == {} and harness.logs == []


def test_esports_loop_without_a_channel_counts_an_error(monkeypatch):
    harness = LoopHarness(monkeypatch)
    cycle = Cycle(harness)
    loop, kwargs = _esports_loop(monkeypatch, harness, cycle, resolve_channel=_no_channel)
    harness.run(loop, **kwargs)

    assert cycle.calls == []
    assert harness.logs[0] == "[esports] News channel 5 unavailable; skipping cycle."
    assert harness.logs[-1].startswith("[esports] Cycle complete")
    assert (harness.stats["esports"]["cycles"], harness.stats["esports"]["errors"]) == (0, 1)
    assert harness.sleeps == [60]


def test_patch_loop_without_a_channel_counts_an_error_and_retries_sooner(monkeypatch):
    harness = LoopHarness(monkeypatch)
    cycle = Cycle(harness)
    loop, kwargs = _patch_loop(monkeypatch, harness, cycle, resolve_channel=_no_channel)
    harness.run(loop, **kwargs)

    assert cycle.calls == []
    assert harness.logs[0] == "[patch] Events channel 7 unavailable; skipping cycle."
    assert (harness.stats["patch"]["cycles"], harness.stats["patch"]["errors"]) == (0, 1)
    assert harness.sleeps == [3600]


def test_backfill_loop_logs_a_summary_from_the_stored_offsets(monkeypatch):
    harness = LoopHarness(monkeypatch)
    cycle = Cycle(harness, result=SimpleNamespace(cached=4, resting=1))
    loop, kwargs = _backfill_loop(monkeypatch, harness, cycle)
    harness.run(loop, **kwargs)

    assert cycle.calls[0]["recent_ids_count"] == 100 and cycle.calls[0]["per_player_limit"] == 3
    assert cycle.calls[0]["friends"] == ["Alpha#EUW", "Bravo#EUW", "Charlie#EUW"]
    # Offsets are stored under the lower-cased Riot ID; one of the three players has work left.
    assert harness.logs[0] == "[backfill] Cycle summary: cached=4, active_offsets=1/3, max_offset=300, resting=1/3."


def test_refresh_loop_forgets_a_deleted_scoreboard_message(monkeypatch):
    harness = LoopHarness(monkeypatch)
    loop, kwargs = _refresh_loop(monkeypatch, harness, Cycle(harness))
    cleared = []
    report_state = {"channel_id": 5, "message_id": 9}

    async def deleted_message(_channel, _initial_content):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")

    kwargs.update(
        get_or_create_report_message=deleted_message, report_state=report_state,
        db_set_last_report_message=lambda channel_id, message_id: cleared.append((channel_id, message_id)),
    )
    harness.run(loop, **kwargs)

    assert any(line.startswith("[refresh] Could not edit last report message:") for line in harness.logs)
    assert report_state == {"channel_id": None, "message_id": None}
    assert cleared == [(0, 0)]
    # A deleted message is handled inside the cycle: it still counts as a good cycle.
    assert (harness.stats["refresh"]["cycles"], harness.stats["refresh"]["errors"]) == (1, 0)
