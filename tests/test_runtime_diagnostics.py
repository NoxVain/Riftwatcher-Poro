import asyncio
import threading

from src.runtime import diagnostics


def test_stack_dump_shows_threads_and_waiting_tasks():
    stuck = threading.Event()
    worker = threading.Thread(target=lambda: stuck.wait(5), name="stuck-db-read", daemon=True)
    worker.start()
    logs = []

    async def waiting_on_baselines():
        await asyncio.sleep(5)

    async def run():
        task = asyncio.create_task(waiting_on_baselines(), name="refresh-worker")
        await asyncio.sleep(0)
        diagnostics.log_stack_dump(logs.append, reason="refresh stalled")
        task.cancel()

    try:
        asyncio.run(run())
    finally:
        stuck.set()

    text = "\n".join(logs)
    assert logs[0].startswith("[diag] Stack dump (refresh stalled):")
    assert "--- thread stuck-db-read" in text
    assert "--- task refresh-worker" in text
    assert "waiting_on_baselines" in text
    assert all(line.startswith("[diag] ") for line in logs)


def test_stack_dump_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(diagnostics, "format_stack_dump", broken)
    logs = []

    diagnostics.log_stack_dump(logs.append, reason="x")

    assert logs == ["[diag] Stack dump failed (x): boom"]
