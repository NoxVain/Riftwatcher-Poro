import asyncio

from src.runtime.alerts import (
    RiotAlertState,
    mark_riot_401_alert_sent,
    riot_401_alert_already_sent,
    send_riot_key_expired_alert,
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
