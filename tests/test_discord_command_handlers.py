import asyncio
import contextvars

from src.discord_command_handlers import handle_incoming_message


class FakeAuthor:
    bot = False


class FakeStatusMessage:
    def __init__(self, content=""):
        self.content = content
        self.edits = []

    async def edit(self, *, content):
        self.content = content
        self.edits.append(content)


class FakeChannel:
    def __init__(self, channel_id=123):
        self.id = channel_id
        self.sent_messages = []

    async def send(self, content):
        message = FakeStatusMessage(content=content)
        self.sent_messages.append(message)
        return message


class FakeIncomingMessage:
    def __init__(self, content, channel):
        self.content = content
        self.channel = channel
        self.author = FakeAuthor()
        self.id = 999
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeMoodService:
    def __init__(self, build_outputs):
        self._build_outputs = list(build_outputs)
        self.refresh_recent_calls = []
        self.invalidated = False

    async def build_today_win_rate_report(self):
        return self._build_outputs.pop(0)

    async def build_weekly_win_rate_report(self, bypass_cache=False):
        _ = bypass_cache
        return "weekly report"

    async def refresh_recent_matches_snapshot(self, recent_count=20):
        self.refresh_recent_calls.append(recent_count)

    def invalidate_report_cache(self):
        self.invalidated = True

    async def run_health_check(self, _start_monotonic):
        return {
            "uptime_seconds": 1,
            "tracked_players": 1,
            "db_ok": True,
            "match_cache_entries": 0,
            "request_cache_active": False,
        }


class FakeRiotClient:
    def __init__(self):
        self.fetch_puuid_calls = []

    async def fetch_puuid(self, riot_id):
        self.fetch_puuid_calls.append(riot_id)
        return "puuid-1"


def test_handle_mood_command_updates_scoreboard_from_snapshot_then_refresh():
    channel = FakeChannel(channel_id=777)
    incoming = FakeIncomingMessage("!Mood", channel)
    status_message = FakeStatusMessage(content="old content")
    mood_service = FakeMoodService(build_outputs=["snapshot report", "fresh report"])
    riot_client = FakeRiotClient()
    remembered = []
    logs = []

    async def get_or_create_report_message(_channel, _initial_content):
        return status_message

    def remember_report_message(message):
        remembered.append(message)

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=["Alpha#NA1"],
            riot_client=riot_client,
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=6,
            db_enabled=True,
            start_monotonic=0.0,
            mood_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "mood-1234",
            get_or_create_report_message=get_or_create_report_message,
            remember_report_message=remember_report_message,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=logs.append,
        )
    )

    assert incoming.deleted is True
    assert remembered == [status_message]
    assert mood_service.refresh_recent_calls == [20]
    assert status_message.edits[0].startswith("\u23F3 Gathering match results since 06:00")
    assert status_message.edits[1].endswith("_Refreshing latest matches..._")
    assert status_message.content == "fresh report"


def test_handle_add_command_happy_path_persists_and_invalidates_cache():
    channel = FakeChannel(channel_id=777)
    incoming = FakeIncomingMessage("!Add Alpha#NA1", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()
    friends = []
    upserts = []

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=friends,
            riot_client=riot_client,
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=6,
            db_enabled=True,
            start_monotonic=0.0,
            mood_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "add-1234",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id.strip(),
            db_upsert_player=lambda riot_id, puuid: upserts.append((riot_id, puuid)),
            log=lambda _msg: None,
        )
    )

    assert riot_client.fetch_puuid_calls == ["Alpha#NA1"]
    assert friends == ["Alpha#NA1"]
    assert upserts == [("Alpha#NA1", None)]
    assert mood_service.invalidated is True
    assert len(channel.sent_messages) == 1
    assert "Added `Alpha#NA1` and saved to postgres." in channel.sent_messages[0].content


def test_handle_week_command_updates_weekly_scoreboard_message():
    channel = FakeChannel(channel_id=777)
    incoming = FakeIncomingMessage("!Week", channel)
    status_message = FakeStatusMessage(content="old weekly")
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()
    remembered = []

    async def get_or_create_weekly_report_message(_channel, _initial_content):
        return status_message

    def remember_weekly_report_message(message):
        remembered.append(message)

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=["Alpha#NA1"],
            riot_client=riot_client,
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=6,
            db_enabled=True,
            start_monotonic=0.0,
            mood_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "week-1234",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=lambda _msg: None,
            get_or_create_weekly_report_message=get_or_create_weekly_report_message,
            remember_weekly_report_message=remember_weekly_report_message,
        )
    )

    assert incoming.deleted is True
    assert remembered == [status_message]
    assert status_message.content == "weekly report"
