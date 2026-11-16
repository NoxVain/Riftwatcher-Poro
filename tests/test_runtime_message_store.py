from src.runtime.message_store import (
    build_previous_day_placeholder_text,
    create_message_state,
    format_previous_day_report_text,
    get_or_create_report_message,
    get_or_create_weekly_report_message,
    remember_previous_report_message,
    remember_previous_weekly_report_message,
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
        "last_previous_weekly_report_message",
    }
    assert state["last_report_message"]["channel_id"] is None
    assert state["last_report_message"]["message_id"] is None


def test_remember_report_message_updates_state():
    state = create_message_state()
    message = FakeMessage(channel_id=111, message_id=222)

    remember_report_message(
        state=state,
        message=message,
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


def test_remember_previous_report_message_updates_state_and_cycle():
    state = create_message_state()
    message = FakeMessage(channel_id=333, message_id=444)

    remember_previous_report_message(
        state=state,
        message=message,
        db_set_state=lambda _key, _value: None,
        cycle_key="2026-02-25",
    )

    assert state["last_previous_report_message"]["channel_id"] == 333
    assert state["last_previous_report_message"]["message_id"] == 444
    assert state["last_previous_report_message"]["cycle_key"] == "2026-02-25"


def test_remember_weekly_report_message_updates_state():
    state = create_message_state()
    message = FakeMessage(channel_id=555, message_id=666)

    remember_weekly_report_message(
        state=state,
        message=message,
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


# --- Weekly board with last week's board above it --------------------------------------


def _run_weekly(state, channel, *, previous_week_key="2026-11-09", previous_text="LAST WEEK", stored=None,
                build_error=None):
    """Runs get_or_create_weekly_report_message with recording fakes."""
    import asyncio

    seen = {"builds": 0, "logs": [], "previous": [], "weekly": [], "cleared_weekly": [], "state_writes": []}
    stored = stored or {}

    async def build_previous_week_text():
        seen["builds"] += 1
        if build_error is not None:
            raise build_error
        return previous_text

    def remember_previous(message, week_key):
        seen["previous"].append((message.id, week_key))
        state["last_previous_weekly_report_message"].update(
            {"channel_id": message.channel.id, "message_id": message.id, "week_key": week_key}
        )

    def remember_weekly(message):
        seen["weekly"].append(message.id)
        state["last_weekly_report_message"].update({"channel_id": message.channel.id, "message_id": message.id})

    message = asyncio.run(get_or_create_weekly_report_message(
        state=state,
        channel=channel,
        initial_content="THIS WEEK",
        previous_week_key=previous_week_key,
        build_previous_week_text=build_previous_week_text,
        db_get_state=stored.get,
        db_set_state=lambda key, value: seen["state_writes"].append((key, value)),
        db_get_last_weekly_report_message=lambda: (None, None),
        db_set_last_weekly_report_message=lambda channel_id, message_id: seen["cleared_weekly"].append((channel_id, message_id)),
        remember_weekly_report_message_fn=remember_weekly,
        remember_previous_weekly_report_message_fn=remember_previous,
        log=seen["logs"].append,
    ))
    return message, seen


def _weekly_message(message_id, content):
    message = FakeMessage(channel_id=10, message_id=message_id)
    message.content = content
    return message


def test_weekly_first_run_turns_the_existing_board_into_last_weeks_and_posts_this_week_below():
    state = create_message_state()
    state["last_weekly_report_message"].update({"channel_id": 10, "message_id": 1})
    existing = _weekly_message(1, "this week's board")
    channel = FakeFetchChannel(10, {1: existing})

    message, seen = _run_weekly(state, channel)

    # The older message is above the newer one in Discord, so it takes last week's board.
    assert existing.content == "LAST WEEK"
    assert seen["previous"] == [(1, "2026-11-09")]
    assert message.id == 2 and message.content == "THIS WEEK"
    assert seen["weekly"] == [2] and seen["cleared_weekly"] == [(0, 0)]
    assert seen["logs"] == ["[weekly] Previous-week board shows week 2026-11-09 (message 1)."]


def test_weekly_with_nothing_tracked_posts_last_week_first_then_this_week():
    state = create_message_state()
    channel = FakeFetchChannel(10, {})

    message, seen = _run_weekly(state, channel)

    assert [sent.content for sent in channel.sent] == ["LAST WEEK", "THIS WEEK"]
    assert seen["previous"] == [(1, "2026-11-09")] and message.id == 2


def test_weekly_boards_are_left_alone_within_the_same_week():
    state = create_message_state()
    state["last_weekly_report_message"].update({"channel_id": 10, "message_id": 2})
    state["last_previous_weekly_report_message"].update({"channel_id": 10, "message_id": 1, "week_key": "2026-11-09"})
    previous, current = _weekly_message(1, "LAST WEEK"), _weekly_message(2, "this week's board")
    channel = FakeFetchChannel(10, {1: previous, 2: current})

    message, seen = _run_weekly(state, channel)

    assert message is current
    # Last week's board isn't rebuilt on every update, only when the week changes.
    assert seen["builds"] == 0 and previous.edits == [] and channel.sent == [] and seen["logs"] == []


def test_weekly_rollover_moves_last_weeks_board_on():
    state = create_message_state()
    state["last_weekly_report_message"].update({"channel_id": 10, "message_id": 2})
    state["last_previous_weekly_report_message"].update({"channel_id": 10, "message_id": 1, "week_key": "2026-11-02"})
    previous, current = _weekly_message(1, "the week before last"), _weekly_message(2, "last update of last week")
    channel = FakeFetchChannel(10, {1: previous, 2: current})

    message, seen = _run_weekly(state, channel, previous_week_key="2026-11-09", previous_text="LAST WEEK FINAL")

    assert previous.content == "LAST WEEK FINAL"
    assert seen["previous"] == [(1, "2026-11-09")]
    # This week's message stays where it is; the caller fills in the new week.
    assert message is current and channel.sent == []


def test_weekly_previous_board_is_found_again_after_a_restart():
    state = create_message_state()
    state["last_weekly_report_message"].update({"channel_id": 10, "message_id": 2})
    previous, current = _weekly_message(1, "LAST WEEK"), _weekly_message(2, "this week's board")
    channel = FakeFetchChannel(10, {1: previous, 2: current})
    stored = {
        "previous_weekly_report_channel_id": "10",
        "previous_weekly_report_message_id": "1",
        "previous_weekly_report_week_key": "2026-11-09",
    }

    message, seen = _run_weekly(state, channel, stored=stored)

    assert message is current and seen["builds"] == 0 and channel.sent == []
    assert state["last_previous_weekly_report_message"] == {"channel_id": 10, "message_id": 1, "week_key": "2026-11-09"}


def test_weekly_deleted_previous_board_is_replaced_in_order():
    import discord
    from types import SimpleNamespace

    state = create_message_state()
    state["last_weekly_report_message"].update({"channel_id": 10, "message_id": 2})
    state["last_previous_weekly_report_message"].update({"channel_id": 10, "message_id": 1, "week_key": "2026-11-09"})
    current = _weekly_message(2, "this week's board")
    gone = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
    channel = FakeFetchChannel(10, {2: current}, fetch_exceptions={1: gone})

    message, seen = _run_weekly(state, channel)

    # The surviving message becomes last week's board and this week's is posted below it again.
    assert current.content == "LAST WEEK" and seen["previous"] == [(2, "2026-11-09")]
    assert message.id == 3 and message.content == "THIS WEEK"
    assert ("previous_weekly_report_message_id", "0") in seen["state_writes"]


def test_weekly_board_still_updates_when_last_weeks_board_cannot_be_built():
    state = create_message_state()
    state["last_weekly_report_message"].update({"channel_id": 10, "message_id": 1})
    existing = _weekly_message(1, "this week's board")
    channel = FakeFetchChannel(10, {1: existing})

    message, seen = _run_weekly(state, channel, build_error=RuntimeError("db down"))

    # Nothing was converted or posted: this week's board is returned as it was.
    assert message is existing and existing.edits == [] and channel.sent == []
    assert seen["previous"] == []
    assert seen["logs"] == [
        "[weekly] Could not update the previous-week board; retrying on the next update: RuntimeError: db down"
    ]


def test_remember_previous_weekly_report_message_stores_the_message_and_its_week():
    state = create_message_state()
    writes = []

    remember_previous_weekly_report_message(
        state=state,
        message=FakeMessage(channel_id=10, message_id=7),
        db_set_state=lambda key, value: writes.append((key, value)),
        week_key="2026-11-09",
    )

    assert state["last_previous_weekly_report_message"] == {"channel_id": 10, "message_id": 7, "week_key": "2026-11-09"}
    assert writes == [
        ("previous_weekly_report_channel_id", "10"),
        ("previous_weekly_report_message_id", "7"),
        ("previous_weekly_report_week_key", "2026-11-09"),
    ]


def test_weekly_updates_at_the_same_moment_post_this_weeks_board_once():
    # The refresh worker, the recap worker and startup can all update the weekly board together.
    import asyncio

    state = create_message_state()
    state["last_weekly_report_message"].update({"channel_id": 10, "message_id": 1})
    existing = _weekly_message(1, "this week's board")
    channel = FakeFetchChannel(10, {1: existing})

    async def build_previous_week_text():
        await asyncio.sleep(0)
        return "LAST WEEK"

    def remember_previous(message, week_key):
        state["last_previous_weekly_report_message"].update(
            {"channel_id": message.channel.id, "message_id": message.id, "week_key": week_key}
        )

    def remember_weekly(message):
        state["last_weekly_report_message"].update({"channel_id": message.channel.id, "message_id": message.id})

    async def update():
        return await get_or_create_weekly_report_message(
            state=state, channel=channel, initial_content="THIS WEEK", previous_week_key="2026-11-09",
            build_previous_week_text=build_previous_week_text, db_get_state=lambda _key: None,
            db_set_state=lambda _key, _value: None, db_get_last_weekly_report_message=lambda: (None, None),
            db_set_last_weekly_report_message=lambda _channel_id, _message_id: None,
            remember_weekly_report_message_fn=remember_weekly,
            remember_previous_weekly_report_message_fn=remember_previous, log=lambda _line: None,
        )

    async def scenario():
        return await asyncio.gather(update(), update(), update())

    messages = asyncio.run(scenario())

    assert [sent.content for sent in channel.sent] == ["THIS WEEK"]
    assert {message.id for message in messages} == {2}
    assert existing.content == "LAST WEEK"
