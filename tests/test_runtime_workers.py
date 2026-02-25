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
