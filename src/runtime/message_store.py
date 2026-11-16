import asyncio
from datetime import datetime

import discord


DAILY_CYCLE_STATE_KEY = "daily_report_cycle_key"
PREVIOUS_REPORT_CHANNEL_STATE_KEY = "previous_report_channel_id"
PREVIOUS_REPORT_MESSAGE_STATE_KEY = "previous_report_message_id"
PREVIOUS_REPORT_CYCLE_STATE_KEY = "previous_report_cycle_key"
PREVIOUS_WEEKLY_REPORT_CHANNEL_STATE_KEY = "previous_weekly_report_channel_id"
PREVIOUS_WEEKLY_REPORT_MESSAGE_STATE_KEY = "previous_weekly_report_message_id"
PREVIOUS_WEEKLY_REPORT_WEEK_STATE_KEY = "previous_weekly_report_week_key"

# The event loop only keeps weak references to tasks: a fire-and-forget task with no
# other reference can be garbage-collected before it finishes, silently dropping the
# DB write (and the bot would then post a duplicate scoreboard after a restart).
_BACKGROUND_TASKS = set()


def _run_in_background(loop, func, *args):
    task = loop.create_task(asyncio.to_thread(func, *args))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


class TrackedMessageReference:
    def __init__(self, *, channel, message_id, content_hint=""):
        self.channel = channel
        self.id = int(message_id)
        self.content = str(content_hint or "")

    async def edit(self, *, content):
        partial = self.channel.get_partial_message(self.id)
        await partial.edit(content=content)
        self.content = content


def create_message_state():
    return {
        "last_report_message": {"channel_id": None, "message_id": None, "cycle_key": None},
        "last_previous_report_message": {"channel_id": None, "message_id": None, "cycle_key": None},
        "last_weekly_report_message": {"channel_id": None, "message_id": None},
        "last_previous_weekly_report_message": {"channel_id": None, "message_id": None, "week_key": None},
    }


def remember_report_message(*, state, message, db_set_last_report_message):
    target = state["last_report_message"]
    target["channel_id"] = message.channel.id
    target["message_id"] = message.id
    try:
        loop = asyncio.get_running_loop()
        _run_in_background(loop, db_set_last_report_message, message.channel.id, message.id)
    except RuntimeError:
        db_set_last_report_message(message.channel.id, message.id)


def remember_previous_report_message(*, state, message, db_set_state, cycle_key=None):
    target = state["last_previous_report_message"]
    target["channel_id"] = message.channel.id
    target["message_id"] = message.id
    target["cycle_key"] = cycle_key
    try:
        loop = asyncio.get_running_loop()
        _run_in_background(loop, db_set_state, PREVIOUS_REPORT_CHANNEL_STATE_KEY, str(message.channel.id))
        _run_in_background(loop, db_set_state, PREVIOUS_REPORT_MESSAGE_STATE_KEY, str(message.id))
        if cycle_key is not None:
            _run_in_background(loop, db_set_state, PREVIOUS_REPORT_CYCLE_STATE_KEY, str(cycle_key))
    except RuntimeError:
        db_set_state(PREVIOUS_REPORT_CHANNEL_STATE_KEY, str(message.channel.id))
        db_set_state(PREVIOUS_REPORT_MESSAGE_STATE_KEY, str(message.id))
        if cycle_key is not None:
            db_set_state(PREVIOUS_REPORT_CYCLE_STATE_KEY, str(cycle_key))


def remember_weekly_report_message(*, state, message, db_set_last_weekly_report_message):
    target = state["last_weekly_report_message"]
    target["channel_id"] = message.channel.id
    target["message_id"] = message.id
    try:
        loop = asyncio.get_running_loop()
        _run_in_background(loop, db_set_last_weekly_report_message, message.channel.id, message.id)
    except RuntimeError:
        db_set_last_weekly_report_message(message.channel.id, message.id)


def remember_previous_weekly_report_message(*, state, message, db_set_state, week_key):
    target = state["last_previous_weekly_report_message"]
    target["channel_id"] = message.channel.id
    target["message_id"] = message.id
    target["week_key"] = week_key
    values = (
        (PREVIOUS_WEEKLY_REPORT_CHANNEL_STATE_KEY, str(message.channel.id)),
        (PREVIOUS_WEEKLY_REPORT_MESSAGE_STATE_KEY, str(message.id)),
        (PREVIOUS_WEEKLY_REPORT_WEEK_STATE_KEY, str(week_key)),
    )
    try:
        loop = asyncio.get_running_loop()
        for key, value in values:
            _run_in_background(loop, db_set_state, key, value)
    except RuntimeError:
        for key, value in values:
            db_set_state(key, value)


def build_previous_day_placeholder_text():
    return (
        "\u2728------ **RIFTWATCHER PORO (PREVIOUS DAY)** ------\u2728\n\n"
        "No previous-day snapshot available yet.\n\n"
        "\u2728--------------------------------------------\u2728"
    )


def format_previous_day_report_text(report_text, cycle_key):
    title = "PREVIOUS DAY"
    try:
        day_label = datetime.fromisoformat(str(cycle_key)).strftime("%d.%m.%Y")
        title = f"PREVIOUS DAY - {day_label}"
    except (TypeError, ValueError):
        pass

    lines = str(report_text or "").splitlines()
    if not lines:
        return build_previous_day_placeholder_text()
    lines[0] = f"\u2728------ **RIFTWATCHER PORO ({title})** ------\u2728"
    return "\n".join(lines)


async def get_or_create_report_message(
    *,
    state,
    channel,
    initial_content,
    poro_service,
    db_get_state,
    db_set_state,
    db_get_last_report_message,
    db_set_last_report_message,
    remember_report_message_fn,
    remember_previous_report_message_fn,
):
    last_report = state["last_report_message"]
    last_previous = state["last_previous_report_message"]
    current_cycle_key = poro_service.get_cycle_key()
    last_cycle_key = last_report.get("cycle_key")
    previous_channel_id = last_previous.get("channel_id")
    previous_message_id = last_previous.get("message_id")
    previous_cycle_key = last_previous.get("cycle_key")

    if last_cycle_key is None:
        last_cycle_key = await asyncio.to_thread(db_get_state, DAILY_CYCLE_STATE_KEY)
        last_report["cycle_key"] = last_cycle_key
    if not previous_channel_id:
        raw_previous_channel_id = await asyncio.to_thread(db_get_state, PREVIOUS_REPORT_CHANNEL_STATE_KEY)
        try:
            previous_channel_id = int(raw_previous_channel_id) if raw_previous_channel_id else None
        except ValueError:
            previous_channel_id = None
        last_previous["channel_id"] = previous_channel_id
    if not previous_message_id:
        raw_previous_message_id = await asyncio.to_thread(db_get_state, PREVIOUS_REPORT_MESSAGE_STATE_KEY)
        try:
            previous_message_id = int(raw_previous_message_id) if raw_previous_message_id else None
        except ValueError:
            previous_message_id = None
        last_previous["message_id"] = previous_message_id
    if previous_cycle_key is None:
        previous_cycle_key = await asyncio.to_thread(db_get_state, PREVIOUS_REPORT_CYCLE_STATE_KEY)
        last_previous["cycle_key"] = previous_cycle_key

    channel_id = last_report["channel_id"]
    message_id = last_report["message_id"]

    if not channel_id or not message_id:
        persisted_channel_id, persisted_message_id = await asyncio.to_thread(db_get_last_report_message)
        if persisted_channel_id and persisted_message_id:
            last_report["channel_id"] = persisted_channel_id
            last_report["message_id"] = persisted_message_id
            channel_id = persisted_channel_id
            message_id = persisted_message_id

    previous_message = None
    if previous_channel_id == channel.id and previous_message_id:
        try:
            previous_message = await channel.fetch_message(previous_message_id)
        except (discord.NotFound, discord.Forbidden):
            last_previous["channel_id"] = None
            last_previous["message_id"] = None
            last_previous["cycle_key"] = None
            previous_channel_id = None
            previous_message_id = None
            previous_cycle_key = None
            await asyncio.to_thread(db_set_state, PREVIOUS_REPORT_CHANNEL_STATE_KEY, "0")
            await asyncio.to_thread(db_set_state, PREVIOUS_REPORT_MESSAGE_STATE_KEY, "0")
            await asyncio.to_thread(db_set_state, PREVIOUS_REPORT_CYCLE_STATE_KEY, "")
        except discord.HTTPException:
            previous_message = TrackedMessageReference(
                channel=channel,
                message_id=previous_message_id,
            )

    today_message = None
    if channel_id == channel.id and message_id:
        try:
            today_message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            last_report["channel_id"] = None
            last_report["message_id"] = None
            channel_id = None
            message_id = None
            await asyncio.to_thread(db_set_last_report_message, 0, 0)
        except discord.HTTPException:
            today_message = TrackedMessageReference(
                channel=channel,
                message_id=message_id,
            )

    # Migration path: if only one tracked daily message exists, convert it into
    # the previous-day slot and create a new today message below it.
    if previous_message is None and today_message is not None:
        placeholder_text = build_previous_day_placeholder_text()
        if today_message.content != placeholder_text:
            await today_message.edit(content=placeholder_text)
        remember_previous_report_message_fn(today_message, cycle_key=None)
        today_message = None
        last_report["channel_id"] = None
        last_report["message_id"] = None
        await asyncio.to_thread(db_set_last_report_message, 0, 0)

    if previous_message is None:
        previous_message = await channel.send(build_previous_day_placeholder_text())
        remember_previous_report_message_fn(previous_message, cycle_key=previous_cycle_key)

    had_existing_today_message = today_message is not None
    if today_message is None:
        today_message = await channel.send(initial_content)
        remember_report_message_fn(today_message)

    if not last_cycle_key:
        last_report["cycle_key"] = current_cycle_key
        await asyncio.to_thread(db_set_state, DAILY_CYCLE_STATE_KEY, current_cycle_key)
    elif last_cycle_key != current_cycle_key:
        if had_existing_today_message:
            previous_text = format_previous_day_report_text(today_message.content, last_cycle_key)
            if previous_message.content != previous_text:
                await previous_message.edit(content=previous_text)
            remember_previous_report_message_fn(previous_message, cycle_key=last_cycle_key)
        last_report["cycle_key"] = current_cycle_key
        await asyncio.to_thread(db_set_state, DAILY_CYCLE_STATE_KEY, current_cycle_key)

    return today_message


async def _ensure_previous_week_message(
    *,
    state,
    channel,
    current_message,
    previous_week_key,
    build_previous_week_text,
    db_get_state,
    db_set_state,
    db_set_last_weekly_report_message,
    remember_previous_weekly_report_message_fn,
    log,
):
    """Keep last week's board in the message above this week's; returns this week's message.

    The text is only built when the tracked message is missing or still shows an older week.
    """
    last_previous = state["last_previous_weekly_report_message"]
    if not last_previous["channel_id"] or not last_previous["message_id"]:
        raw_channel_id = await asyncio.to_thread(db_get_state, PREVIOUS_WEEKLY_REPORT_CHANNEL_STATE_KEY)
        raw_message_id = await asyncio.to_thread(db_get_state, PREVIOUS_WEEKLY_REPORT_MESSAGE_STATE_KEY)
        try:
            last_previous["channel_id"] = int(raw_channel_id) if raw_channel_id else None
            last_previous["message_id"] = int(raw_message_id) if raw_message_id else None
        except ValueError:
            last_previous["channel_id"] = None
            last_previous["message_id"] = None
        last_previous["week_key"] = await asyncio.to_thread(db_get_state, PREVIOUS_WEEKLY_REPORT_WEEK_STATE_KEY)

    previous_message = None
    if last_previous["channel_id"] == channel.id and last_previous["message_id"]:
        try:
            previous_message = await channel.fetch_message(last_previous["message_id"])
        except (discord.NotFound, discord.Forbidden):
            last_previous.update({"channel_id": None, "message_id": None, "week_key": None})
            await asyncio.to_thread(db_set_state, PREVIOUS_WEEKLY_REPORT_CHANNEL_STATE_KEY, "0")
            await asyncio.to_thread(db_set_state, PREVIOUS_WEEKLY_REPORT_MESSAGE_STATE_KEY, "0")
            await asyncio.to_thread(db_set_state, PREVIOUS_WEEKLY_REPORT_WEEK_STATE_KEY, "")
        except discord.HTTPException:
            previous_message = TrackedMessageReference(channel=channel, message_id=last_previous["message_id"])

    if previous_message is not None and last_previous["week_key"] == previous_week_key:
        return current_message

    text = await build_previous_week_text()
    if previous_message is None and current_message is not None:
        # Messages show in the order they were posted, so the existing board becomes last
        # week's and this week's gets a new message below it.
        await current_message.edit(content=text)
        previous_message, current_message = current_message, None
        state["last_weekly_report_message"].update({"channel_id": None, "message_id": None})
        await asyncio.to_thread(db_set_last_weekly_report_message, 0, 0)
    elif previous_message is None:
        previous_message = await channel.send(text)
    elif previous_message.content != text:
        await previous_message.edit(content=text)
    remember_previous_weekly_report_message_fn(previous_message, week_key=previous_week_key)
    log(f"[weekly] Previous-week board shows week {previous_week_key} (message {previous_message.id}).")
    return current_message


async def get_or_create_weekly_report_message(
    *,
    state,
    channel,
    initial_content,
    previous_week_key,
    build_previous_week_text,
    db_get_state,
    db_set_state,
    db_get_last_weekly_report_message,
    db_set_last_weekly_report_message,
    remember_weekly_report_message_fn,
    remember_previous_weekly_report_message_fn,
    log,
):
    """This week's scoreboard message, with last week's board kept in the message above it."""
    # The refresh worker, the recap worker and startup can all get here at the same moment;
    # one at a time, or each would post its own copy of a missing message.
    lock = state.setdefault("_weekly_message_lock", asyncio.Lock())
    async with lock:
        return await _get_or_create_weekly_report_message(
            state=state,
            channel=channel,
            initial_content=initial_content,
            previous_week_key=previous_week_key,
            build_previous_week_text=build_previous_week_text,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_get_last_weekly_report_message=db_get_last_weekly_report_message,
            db_set_last_weekly_report_message=db_set_last_weekly_report_message,
            remember_weekly_report_message_fn=remember_weekly_report_message_fn,
            remember_previous_weekly_report_message_fn=remember_previous_weekly_report_message_fn,
            log=log,
        )


async def _get_or_create_weekly_report_message(
    *,
    state,
    channel,
    initial_content,
    previous_week_key,
    build_previous_week_text,
    db_get_state,
    db_set_state,
    db_get_last_weekly_report_message,
    db_set_last_weekly_report_message,
    remember_weekly_report_message_fn,
    remember_previous_weekly_report_message_fn,
    log,
):
    last_weekly = state["last_weekly_report_message"]
    channel_id = last_weekly["channel_id"]
    message_id = last_weekly["message_id"]

    if not channel_id or not message_id:
        persisted_channel_id, persisted_message_id = await asyncio.to_thread(db_get_last_weekly_report_message)
        if persisted_channel_id and persisted_message_id:
            last_weekly["channel_id"] = persisted_channel_id
            last_weekly["message_id"] = persisted_message_id
            channel_id = persisted_channel_id
            message_id = persisted_message_id

    current_message = None
    if channel_id == channel.id and message_id:
        try:
            current_message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            last_weekly["channel_id"] = None
            last_weekly["message_id"] = None
            await asyncio.to_thread(db_set_last_weekly_report_message, 0, 0)
        except discord.HTTPException:
            current_message = TrackedMessageReference(channel=channel, message_id=message_id)

    try:
        current_message = await _ensure_previous_week_message(
            state=state,
            channel=channel,
            current_message=current_message,
            previous_week_key=previous_week_key,
            build_previous_week_text=build_previous_week_text,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            db_set_last_weekly_report_message=db_set_last_weekly_report_message,
            remember_previous_weekly_report_message_fn=remember_previous_weekly_report_message_fn,
            log=log,
        )
    except Exception as exc:
        # Last week's board is an extra: this week's still updates.
        log(f"[weekly] Could not update the previous-week board; retrying on the next update: {type(exc).__name__}: {exc}")

    if current_message is None:
        current_message = await channel.send(initial_content)
        remember_weekly_report_message_fn(current_message)
    return current_message
