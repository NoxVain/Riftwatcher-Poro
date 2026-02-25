from src.runtime.message_store import (
    create_message_state,
    remember_previous_report_message,
    remember_report_message,
    remember_weekly_report_message,
)


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id


class FakeMessage:
    def __init__(self, channel_id, message_id):
        self.channel = FakeChannel(channel_id)
        self.id = message_id


def test_create_message_state_has_expected_shape():
    state = create_message_state()

    assert set(state.keys()) == {
        "last_report_message",
        "last_previous_report_message",
        "last_weekly_report_message",
    }
    assert state["last_report_message"]["channel_id"] is None
    assert state["last_report_message"]["message_id"] is None


def test_remember_report_message_updates_state_without_db():
    state = create_message_state()
    message = FakeMessage(channel_id=111, message_id=222)

    remember_report_message(
        state=state,
        message=message,
        db_enabled=False,
        db_set_last_report_message=lambda _channel_id, _message_id: None,
    )

    assert state["last_report_message"]["channel_id"] == 111
    assert state["last_report_message"]["message_id"] == 222


def test_remember_previous_report_message_updates_state_and_cycle_without_db():
    state = create_message_state()
    message = FakeMessage(channel_id=333, message_id=444)

    remember_previous_report_message(
        state=state,
        message=message,
        db_enabled=False,
        db_set_state=lambda _key, _value: None,
        cycle_key="2026-02-25",
    )

    assert state["last_previous_report_message"]["channel_id"] == 333
    assert state["last_previous_report_message"]["message_id"] == 444
    assert state["last_previous_report_message"]["cycle_key"] == "2026-02-25"


def test_remember_weekly_report_message_updates_state_without_db():
    state = create_message_state()
    message = FakeMessage(channel_id=555, message_id=666)

    remember_weekly_report_message(
        state=state,
        message=message,
        db_enabled=False,
        db_set_last_weekly_report_message=lambda _channel_id, _message_id: None,
    )

    assert state["last_weekly_report_message"]["channel_id"] == 555
    assert state["last_weekly_report_message"]["message_id"] == 666
