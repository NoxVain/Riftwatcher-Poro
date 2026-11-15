import asyncio

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
