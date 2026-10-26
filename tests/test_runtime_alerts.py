import asyncio
from types import SimpleNamespace

from src.runtime import alerts as alerts_module
from src.runtime.alerts import (
    RIOT_KEY_RECOVERED_TEXT,
    RiotAlertState,
    check_and_notify_worker_stalls,
    format_riot_key_alert_text,
    mark_riot_401_alert_sent,
    riot_401_alert_already_sent,
    send_riot_key_expired_alert,
    trigger_riot_key_alert,
    trigger_riot_key_recovered,
)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


def test_riot_401_alert_already_sent_uses_memory_and_db_state():
    state = RiotAlertState()
    db = {"riot_401_alert_sent": "0"}

    assert riot_401_alert_already_sent(state=state, db_get_state=lambda key: db.get(key)) is False

    mark_riot_401_alert_sent(state=state, db_set_state=lambda key, value: db.update({key: value}))
    assert state.riot_401_alert_sent is True
    assert db["riot_401_alert_sent"] == "1"
    assert riot_401_alert_already_sent(state=state, db_get_state=lambda key: db.get(key)) is True


def test_riot_401_alert_already_sent_reads_persisted_flag():
    state = RiotAlertState()

    assert riot_401_alert_already_sent(state=state, db_get_state=lambda _key: "1") is True
    assert state.riot_401_alert_sent is True


def test_send_riot_key_expired_alert_posts_to_events_channel():
    channel = FakeChannel()
    logs = []

    async def resolve_channel(_channel_id):
        return channel

    asyncio.run(
        send_riot_key_expired_alert(
            resolve_channel=resolve_channel,
            events_channel_id=123,
            log=logs.append,
        )
    )

    assert len(channel.sent) == 1
    assert "401 Unauthorized" in channel.sent[0]
    assert any("expiry alert" in entry for entry in logs)


def test_trigger_riot_key_alert_marks_only_after_success(monkeypatch):
    db = {}
    logs = []

    class FakeClient:
        def __init__(self, loop):
            self.loop = loop

    class FakeFuture:
        pass

    def fake_run_coroutine_threadsafe(coro, loop):
        loop.create_task(coro)
        return FakeFuture()

    monkeypatch.setattr("src.runtime.alerts.asyncio.run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    async def scenario():
        state = RiotAlertState()
        channel = FakeChannel()
        client = FakeClient(asyncio.get_running_loop())
        async def resolve_channel(_channel_id):
            return channel
        trigger_riot_key_alert(
            state=state,
            client=client,
            resolve_channel=resolve_channel,
            events_channel_id=123,
            db_get_state=lambda key: db.get(key),
            db_set_state=lambda key, value: db.update({key: value}),
            log=logs.append,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert state.riot_401_alert_sent is True
        assert db.get("riot_401_alert_sent") == "1"

    asyncio.run(scenario())


def test_trigger_riot_key_alert_does_not_mark_when_send_fails(monkeypatch):
    db = {}
    logs = []

    class FakeClient:
        def __init__(self, loop):
            self.loop = loop

    class FakeFuture:
        pass

    class FailingChannel:
        async def send(self, _content):
            raise RuntimeError("send failed")

    def fake_run_coroutine_threadsafe(coro, loop):
        loop.create_task(coro)
        return FakeFuture()

    monkeypatch.setattr("src.runtime.alerts.asyncio.run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    async def scenario():
        state = RiotAlertState()
        client = FakeClient(asyncio.get_running_loop())
        async def resolve_channel(_channel_id):
            return FailingChannel()
        trigger_riot_key_alert(
            state=state,
            client=client,
            resolve_channel=resolve_channel,
            events_channel_id=123,
            db_get_state=lambda key: db.get(key),
            db_set_state=lambda key, value: db.update({key: value}),
            log=logs.append,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert state.riot_401_alert_sent is False
        assert db.get("riot_401_alert_sent") is None
        assert any("Failed to send" in entry for entry in logs)

    asyncio.run(scenario())


def test_riot_key_alert_text_is_generic_and_labels_status():
    text_401 = format_riot_key_alert_text(401)
    text_403 = format_riot_key_alert_text(403)

    assert "401 Unauthorized" in text_401
    assert "403 Forbidden" in text_403
    for text in (text_401, text_403):
        assert "@" not in text
        assert "Railway" not in text
        assert "RIOT_API_KEY" in text


class _ScheduledCoroutines:
    """Captures coroutines passed to run_coroutine_threadsafe so tests can await them."""

    def __init__(self, monkeypatch):
        self.coroutines = []
        monkeypatch.setattr(
            "src.runtime.alerts.asyncio.run_coroutine_threadsafe",
            lambda coro, _loop: self.coroutines.append(coro),
        )

    async def drain(self):
        while self.coroutines:
            await self.coroutines.pop(0)


class _FakeClient:
    loop = None


def _run_recovery(state, db, channel, logs, db_reads=None):
    def db_get_state(key):
        if db_reads is not None:
            db_reads.append(key)
        return db.get(key)

    async def resolve_channel(_channel_id):
        return channel

    trigger_riot_key_recovered(
        state=state,
        client=_FakeClient(),
        resolve_channel=resolve_channel,
        events_channel_id=123,
        db_get_state=db_get_state,
        db_set_state=lambda key, value: db.update({key: value}),
        log=logs.append,
    )


def test_trigger_riot_key_recovered_posts_once_and_clears_flag(monkeypatch):
    scheduled = _ScheduledCoroutines(monkeypatch)
    state = RiotAlertState()
    state.riot_401_alert_sent = True
    db = {"riot_401_alert_sent": "1"}
    channel = FakeChannel()
    logs = []

    async def scenario():
        _run_recovery(state, db, channel, logs)
        _run_recovery(state, db, channel, logs)  # Second success before the first finishes: no duplicate.
        await scheduled.drain()
        _run_recovery(state, db, channel, logs)  # Flag cleared: fast path, nothing scheduled.
        await scheduled.drain()

    asyncio.run(scenario())

    assert channel.sent == [RIOT_KEY_RECOVERED_TEXT]
    assert state.riot_401_alert_sent is False
    assert db["riot_401_alert_sent"] == "0"


def test_trigger_riot_key_recovered_reads_persisted_flag_after_restart(monkeypatch):
    scheduled = _ScheduledCoroutines(monkeypatch)
    state = RiotAlertState()  # Fresh process: in-memory flag is False.
    db = {"riot_401_alert_sent": "1"}
    channel = FakeChannel()

    async def scenario():
        _run_recovery(state, db, channel, [])
        await scheduled.drain()

    asyncio.run(scenario())

    assert channel.sent == [RIOT_KEY_RECOVERED_TEXT]
    assert db["riot_401_alert_sent"] == "0"


def test_trigger_riot_key_recovered_fast_path_reads_db_once(monkeypatch):
    scheduled = _ScheduledCoroutines(monkeypatch)
    state = RiotAlertState()
    db = {"riot_401_alert_sent": "0"}
    channel = FakeChannel()
    db_reads = []

    for _ in range(5):
        _run_recovery(state, db, channel, [], db_reads=db_reads)

    assert db_reads == ["riot_401_alert_sent"]
    assert scheduled.coroutines == []
    assert channel.sent == []


def test_trigger_riot_key_recovered_waits_before_retrying_failed_send(monkeypatch):
    scheduled = _ScheduledCoroutines(monkeypatch)
    now = {"value": 500.0}
    monkeypatch.setattr(alerts_module, "time", SimpleNamespace(monotonic=lambda: now["value"]))
    state = RiotAlertState()
    state.riot_401_alert_sent = True
    db = {"riot_401_alert_sent": "1"}
    logs = []

    class FailingChannel:
        async def send(self, _content):
            raise RuntimeError("send failed")

    async def scenario():
        _run_recovery(state, db, FailingChannel(), logs)
        await scheduled.drain()
        _run_recovery(state, db, FailingChannel(), logs)  # Inside cooldown: not retried.
        assert scheduled.coroutines == []
        now["value"] += alerts_module.RIOT_KEY_RECOVERY_RETRY_SECONDS
        channel = FakeChannel()
        _run_recovery(state, db, channel, logs)
        await scheduled.drain()
        return channel

    channel = asyncio.run(scenario())

    assert channel.sent == [RIOT_KEY_RECOVERED_TEXT]
    assert db["riot_401_alert_sent"] == "0"
    assert any("Failed to send RIOT_API_KEY recovery" in entry for entry in logs)


def test_check_and_notify_worker_stalls_sends_stale_and_recovery_messages():
    channel = FakeChannel()
    logs = []
    state = {"alerted_by_worker": {}}
    db = {}

    async def resolve_channel(_channel_id):
        return channel

    worker_stats = {
        "refresh": {"runs": 3, "last_success_at": 10.0},
    }
    thresholds = {"refresh": 30}

    asyncio.run(
        check_and_notify_worker_stalls(
            state=state,
            resolve_channel=resolve_channel,
            events_channel_id=123,
            worker_stats=worker_stats,
            stale_thresholds_seconds=thresholds,
            db_get_state=lambda key: db.get(key),
            db_set_state=lambda key, value: db.update({key: value}),
            now_monotonic=50.0,
            log=logs.append,
        )
    )

    assert len(channel.sent) == 1
    assert "appears stalled" in channel.sent[0]
    assert state["alerted_by_worker"]["refresh"] is True
    assert db["worker_stall_alert_sent::refresh"] == "1"

    # same stale condition should not spam duplicate alerts
    asyncio.run(
        check_and_notify_worker_stalls(
            state=state,
            resolve_channel=resolve_channel,
            events_channel_id=123,
            worker_stats=worker_stats,
            stale_thresholds_seconds=thresholds,
            db_get_state=lambda key: db.get(key),
            db_set_state=lambda key, value: db.update({key: value}),
            now_monotonic=55.0,
            log=logs.append,
        )
    )
    assert len(channel.sent) == 1

    # recovery clears alert and sends one recovery message
    worker_stats["refresh"]["last_success_at"] = 54.0
    asyncio.run(
        check_and_notify_worker_stalls(
            state=state,
            resolve_channel=resolve_channel,
            events_channel_id=123,
            worker_stats=worker_stats,
            stale_thresholds_seconds=thresholds,
            db_get_state=lambda key: db.get(key),
            db_set_state=lambda key, value: db.update({key: value}),
            now_monotonic=60.0,
            log=logs.append,
        )
    )
    assert len(channel.sent) == 2
    assert "recovered" in channel.sent[1]
    assert state["alerted_by_worker"]["refresh"] is False
    assert db["worker_stall_alert_sent::refresh"] == "0"


def test_worker_stuck_in_first_cycle_alerts_with_stack_dump():
    channel = FakeChannel()
    logs = []

    async def resolve_channel(_channel_id):
        return channel

    # Never finished a cycle (runs=0); its clock starts at process start (10.0).
    worker_stats = {
        "refresh": {"runs": 0, "last_success_at": 10.0},
        "unknown": {"runs": 0, "last_success_at": 0.0},  # no start time: skipped
    }

    asyncio.run(
        check_and_notify_worker_stalls(
            state={"alerted_by_worker": {}},
            resolve_channel=resolve_channel,
            events_channel_id=123,
            worker_stats=worker_stats,
            stale_thresholds_seconds={"refresh": 30, "unknown": 30},
            now_monotonic=50.0,
            log=logs.append,
        )
    )

    assert len(channel.sent) == 1
    assert "`refresh` appears stalled" in channel.sent[0]
    assert any(line.startswith("[diag] Stack dump (refresh stalled)") for line in logs)
