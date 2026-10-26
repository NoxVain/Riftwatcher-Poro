from src.runtime.message_store import (
    build_previous_day_placeholder_text,
    create_message_state,
    format_previous_day_report_text,
    get_or_create_report_message,
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
        self.content = ""
        self.edits = []

    async def edit(self, *, content):
        self.content = content
        self.edits.append(content)


class FakeFetchChannel(FakeChannel):
    def __init__(self, channel_id, messages, fetch_exceptions=None):
        super().__init__(channel_id)
        self._messages = messages
        self.sent = []
        self._fetch_exceptions = fetch_exceptions or {}

    async def fetch_message(self, message_id):
        exc = self._fetch_exceptions.get(message_id)
        if exc is not None:
            raise exc
        return self._messages[message_id]

    def get_partial_message(self, message_id):
        channel = self
        message = self._messages[message_id]

        class PartialMessage:
            def __init__(self):
                self.channel = channel
                self.id = message_id

            async def edit(self, *, content):
                await message.edit(content=content)

        return PartialMessage()

    async def send(self, content):
        message_id = max(self._messages.keys(), default=0) + 1
        msg = FakeMessage(self.id, message_id)
        msg.content = content
        self._messages[message_id] = msg
        self.sent.append(msg)
        return msg


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


def test_background_db_writes_are_held_until_done():
    import asyncio
    import threading

    from src.runtime import message_store

    writes = []
    release = threading.Event()

    def slow_db_write(channel_id, message_id):
        release.wait(2)
        writes.append((channel_id, message_id))

    async def run():
        message_store.remember_report_message(
            state=create_message_state(),
            message=FakeMessage(channel_id=111, message_id=222),
            db_enabled=True,
            db_set_last_report_message=slow_db_write,
        )
        # The only strong reference to the pending write is the module's task set.
        [task] = list(message_store._BACKGROUND_TASKS)
        release.set()
        await task
        await asyncio.sleep(0)  # let the done-callback run

    asyncio.run(run())

    assert writes == [(111, 222)]
    assert message_store._BACKGROUND_TASKS == set()


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


def test_previous_day_placeholder_text_uses_clean_header_symbols():
    text = build_previous_day_placeholder_text()
    assert "✨" in text
    assert "RIFTWATCHER PORO (PREVIOUS DAY)" in text


def test_format_previous_day_report_text_rewrites_first_line_with_cycle_date():
    text = format_previous_day_report_text("old header\nline2", "2026-02-24")
    assert text.splitlines()[0] == "✨------ **RIFTWATCHER PORO (PREVIOUS DAY - 24.02.2026)** ------✨"


def test_get_or_create_report_message_rollover_updates_previous_message():
    async def scenario():
        state = create_message_state()
        state["last_report_message"].update({"channel_id": 10, "message_id": 2, "cycle_key": "2026-02-24"})
        state["last_previous_report_message"].update({"channel_id": 10, "message_id": 1, "cycle_key": "2026-02-23"})

        previous = FakeMessage(channel_id=10, message_id=1)
        previous.content = "placeholder"
        today = FakeMessage(channel_id=10, message_id=2)
        today.content = "header today\nbody"
        channel = FakeFetchChannel(10, {1: previous, 2: today})

        class PoroService:
            @staticmethod
            def get_cycle_key():
                return "2026-02-25"

        seen_previous = {}

        def remember_previous(message, cycle_key=None):
            seen_previous["message_id"] = message.id
            seen_previous["cycle_key"] = cycle_key

        message = await get_or_create_report_message(
            state=state,
            channel=channel,
            initial_content="init",
            poro_service=PoroService(),
            db_enabled=False,
            db_get_state=lambda _k: None,
            db_set_state=lambda _k, _v: None,
            db_get_last_report_message=lambda: (0, 0),
            db_set_last_report_message=lambda _c, _m: None,
            remember_report_message_fn=lambda _m: None,
            remember_previous_report_message_fn=remember_previous,
        )

        assert message.id == 2
        assert seen_previous["message_id"] == 1
        assert seen_previous["cycle_key"] == "2026-02-24"
        assert previous.edits
        assert "24.02.2026" in previous.content
        assert state["last_report_message"]["cycle_key"] == "2026-02-25"

    import asyncio
    asyncio.run(scenario())


def test_get_or_create_report_message_keeps_today_id_on_fetch_http_exception():
    async def scenario():
        import discord
        from types import SimpleNamespace

        state = create_message_state()
        state["last_report_message"].update({"channel_id": 10, "message_id": 2, "cycle_key": "2026-02-25"})
        state["last_previous_report_message"].update({"channel_id": 10, "message_id": 1, "cycle_key": "2026-02-24"})

        previous = FakeMessage(channel_id=10, message_id=1)
        previous.content = "previous"
        today = FakeMessage(channel_id=10, message_id=2)
        today.content = "today"
        http_error = discord.HTTPException(
            SimpleNamespace(status=503, reason="Service Unavailable", headers={}),
            "upstream timeout",
        )
        channel = FakeFetchChannel(10, {1: previous, 2: today}, fetch_exceptions={2: http_error})

        class PoroService:
            @staticmethod
            def get_cycle_key():
                return "2026-02-25"

        message = await get_or_create_report_message(
            state=state,
            channel=channel,
            initial_content="init",
            poro_service=PoroService(),
            db_enabled=False,
            db_get_state=lambda _k: None,
            db_set_state=lambda _k, _v: None,
            db_get_last_report_message=lambda: (0, 0),
            db_set_last_report_message=lambda _c, _m: None,
            remember_report_message_fn=lambda _m: None,
            remember_previous_report_message_fn=lambda _m, cycle_key=None: None,
        )

        assert message.id == 2
        assert channel.sent == []
        assert state["last_report_message"]["channel_id"] == 10
        assert state["last_report_message"]["message_id"] == 2

    import asyncio
    asyncio.run(scenario())
