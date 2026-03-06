import asyncio
import contextvars

from src.discord_command_handlers import handle_incoming_message


class FakeAuthor:
    bot = False


class FakeStatusMessage:
    def __init__(self, content=""):
        self.content = content
        self.edits = []
        self.deleted = False

    async def edit(self, *, content):
        self.content = content
        self.edits.append(content)

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, channel_id=123):
        self.id = channel_id
        self.sent_messages = []

    async def send(self, content, tts=False):
        message = FakeStatusMessage(content=content)
        message.tts = bool(tts)
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
        self.backfill_calls = []
        self.invalidated = False
        self._role_baselines = None

    async def build_today_win_rate_report(self):
        return self._build_outputs.pop(0)

    async def build_weekly_win_rate_report(self, bypass_cache=False):
        _ = bypass_cache
        return "weekly report"

    async def refresh_recent_matches_snapshot(self, recent_count=20):
        self.refresh_recent_calls.append(recent_count)

    def invalidate_report_cache(self):
        self.invalidated = True

    async def run_health_check(self, _start_monotonic, worker_stats=None):
        return {
            "uptime_seconds": 1,
            "tracked_players": 1,
            "db_ok": True,
            "match_cache_entries": 0,
            "request_cache_active": False,
            "players_with_backfill_offset": 1,
            "max_backfill_offset": 400,
            "top_backfill_offsets": ["Alpha#NA1=400"],
            "worker_stats": worker_stats,
        }

    @staticmethod
    def get_cycle_key():
        return "2026-02-25"

    @staticmethod
    def get_week_window(_now_utc=None):
        from datetime import date

        return date(2026, 2, 24), date(2026, 3, 3)

    @staticmethod
    def db_load_latest_stats(_cycle_key):
        return []

    @staticmethod
    def db_load_weekly_stats(_start, _end):
        return []

    async def _ensure_role_baselines(self):
        return None

    async def backfill_daily_stats_from_cache(self, start_day, end_day, max_payloads=0):
        self.backfill_calls.append((start_day, end_day, max_payloads))
        return {
            "scanned_payloads": 42,
            "matched_matches": 8,
            "upserts": 5,
            "days_written": 3,
            "players_without_puuid": 1,
        }


class FakeRiotClient:
    def __init__(self):
        self.fetch_puuid_calls = []
        self.recent_ids_by_puuid = {}
        self.match_info_by_id = {}

    async def fetch_puuid(self, riot_id):
        self.fetch_puuid_calls.append(riot_id)
        return "puuid-1"

    async def fetch_recent_match_ids(self, puuid, count=20, riot_id=None):
        return self.recent_ids_by_puuid.get(puuid, [])

    async def fetch_match_info(self, match_id):
        return self.match_info_by_id.get(match_id, {})

    def get_participant(self, match_info, puuid):
        for p in match_info.get("info", {}).get("participants", []):
            if p.get("puuid") == puuid:
                return p
        return None


def test_handle_mood_command_updates_scoreboard_from_snapshot_then_refresh():
    channel = FakeChannel(channel_id=777)
    incoming = FakeIncomingMessage("!Daily", channel)
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
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert incoming.deleted is True
    assert remembered == [status_message]
    assert mood_service.refresh_recent_calls == [20]
    assert status_message.edits[0].startswith("\u23F3 Gathering match results since 06:00")
    assert status_message.edits[1].endswith("_Refreshing latest matches..._")
    assert status_message.content == "fresh report"


def test_handle_add_command_happy_path_persists_and_invalidates_cache():
    channel = FakeChannel(channel_id=999)
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
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert riot_client.fetch_puuid_calls == ["Alpha#NA1"]
    assert friends == ["Alpha#NA1"]
    assert upserts == [("Alpha#NA1", None)]
    assert mood_service.invalidated is True
    assert len(channel.sent_messages) == 1
    assert "Added `Alpha#NA1` and saved to postgres." in channel.sent_messages[0].content


def test_handle_remove_command_happy_path_removes_persists_and_invalidates_cache():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!remove Alpha#NA1", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()
    friends = ["Alpha#NA1", "Bravo#NA1"]
    removals = []

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
            create_request_id=lambda _prefix: "remove-1234",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id.strip(),
            db_upsert_player=lambda _riot_id, _puuid: None,
            db_remove_player=lambda riot_id: removals.append(riot_id),
            log=lambda _msg: None,
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert friends == ["Bravo#NA1"]
    assert removals == ["Alpha#NA1"]
    assert mood_service.invalidated is True
    assert len(channel.sent_messages) == 1
    assert "Removed `Alpha#NA1` from tracked players." in channel.sent_messages[0].content


def test_handle_week_command_updates_weekly_scoreboard_message():
    channel = FakeChannel(channel_id=888)
    incoming = FakeIncomingMessage("!Weekly", channel)
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
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert incoming.deleted is True
    assert remembered == [status_message]
    assert status_message.content == "weekly report"


def test_handle_help_command_shows_usage_and_channels():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!help", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=["Alpha#NA1"],
            riot_client=riot_client,
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=9,
            db_enabled=True,
            start_monotonic=0.0,
            mood_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "help-1234",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=lambda _msg: None,
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert len(channel.sent_messages) == 1
    help_text = channel.sent_messages[0].content
    assert "!help" in help_text
    assert "<#777>" in help_text
    assert "<#888>" in help_text
    assert "<#999>" in help_text
    assert "09:00" in help_text


def test_known_command_in_wrong_channel_prompts_to_use_daily_channel():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!Daily", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()

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
            create_request_id=lambda _prefix: "wrong-1234",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=lambda _msg: None,
            weekly_report_channel_id=888,
            events_channel_id=9999,
        )
    )

    assert len(channel.sent_messages) == 1
    assert "<#777>" in channel.sent_messages[0].content


def test_handle_week_command_loading_text_uses_configured_hour():
    channel = FakeChannel(channel_id=888)
    incoming = FakeIncomingMessage("!Weekly", channel)
    status_message = FakeStatusMessage(content="old weekly")
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()

    async def get_or_create_weekly_report_message(_channel, _initial_content):
        return status_message

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=["Alpha#NA1"],
            riot_client=riot_client,
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=9,
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
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert "Monday 09:00 -> next Monday 09:00" in status_message.edits[0]


def _make_match(puuid, win, queue_id=420, duration=1800):
    return {
        "info": {
            "queueId": queue_id,
            "gameDuration": duration,
            "gameEndTimestamp": 0,
            "participants": [{"puuid": puuid, "win": win}],
        }
    }


def _base_streak_kwargs(channel, friends=None):
    return dict(
        channel_id=777,
        friends=friends or ["Alpha#NA1"],
        mood_service=FakeMoodService(build_outputs=["unused"]),
        report_timezone_name="UTC",
        report_day_start_hour=6,
        db_enabled=True,
        start_monotonic=0.0,
        mood_request_lock=asyncio.Lock(),
        request_id_context=contextvars.ContextVar("request_id", default=None),
        create_request_id=lambda _prefix: "streak-1234",
        get_or_create_report_message=lambda _ch, _ic: None,
        remember_report_message=lambda _m: None,
        normalize_riot_id=lambda riot_id: riot_id.strip(),
        db_upsert_player=lambda _r, _p: None,
        log=lambda _msg: None,
        weekly_report_channel_id=888,
        events_channel_id=channel.id,
    )


def test_streak_command_posts_callout_for_active_win_streak():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!streak Alpha#NA1", channel)
    riot_client = FakeRiotClient()
    riot_client.recent_ids_by_puuid["puuid-1"] = ["M3", "M2", "M1"]
    riot_client.match_info_by_id = {
        "M3": _make_match("puuid-1", win=True),
        "M2": _make_match("puuid-1", win=True),
        "M1": _make_match("puuid-1", win=True),
    }
    state = {}

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            riot_client=riot_client,
            db_set_state=lambda k, v: state.update({k: v}),
            **_base_streak_kwargs(channel),
        )
    )

    streak_messages = [
        m for m in channel.sent_messages
        if "Momentum" in m.content or "Heater" in m.content or "LEGENDARY" in m.content
    ]
    assert streak_messages
    assert all(getattr(m, "tts", False) for m in streak_messages)
    assert any(v.startswith("W:") for v in state.values())


def test_streak_command_uses_tts_setting_when_disabled():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!streak Alpha#NA1", channel)
    riot_client = FakeRiotClient()
    riot_client.recent_ids_by_puuid["puuid-1"] = ["M3", "M2", "M1"]
    riot_client.match_info_by_id = {
        "M3": _make_match("puuid-1", win=True),
        "M2": _make_match("puuid-1", win=True),
        "M1": _make_match("puuid-1", win=True),
    }

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            riot_client=riot_client,
            db_get_state=lambda _k: "0",
            db_set_state=None,
            **_base_streak_kwargs(channel),
        )
    )

    streak_messages = [
        m for m in channel.sent_messages
        if "Momentum" in m.content or "Heater" in m.content or "LEGENDARY" in m.content
    ]
    assert streak_messages
    assert all(getattr(m, "tts", True) is False for m in streak_messages)


def test_streak_command_reports_no_streak_when_fewer_than_3():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!streak Alpha#NA1", channel)
    riot_client = FakeRiotClient()
    riot_client.recent_ids_by_puuid["puuid-1"] = ["M2", "M1"]
    riot_client.match_info_by_id = {
        "M2": _make_match("puuid-1", win=True),
        "M1": _make_match("puuid-1", win=False),
    }

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            riot_client=riot_client,
            db_set_state=None,
            **_base_streak_kwargs(channel),
        )
    )

    assert any("no active ranked streak" in m.content for m in channel.sent_messages)


def test_streak_command_bare_shows_usage():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!streak", channel)
    riot_client = FakeRiotClient()

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            riot_client=riot_client,
            db_set_state=None,
            **_base_streak_kwargs(channel),
        )
    )

    assert len(channel.sent_messages) == 1
    assert "Usage" in channel.sent_messages[0].content


def test_streak_command_wrong_channel_prompts_recap_channel():
    channel = FakeChannel(channel_id=777)
    incoming = FakeIncomingMessage("!streak", channel)

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            riot_client=FakeRiotClient(),
            db_set_state=None,
            **{
                **_base_streak_kwargs(channel),
                "events_channel_id": 999,
                "match_recap_channel_id": 555,
            },
        )
    )

    assert len(channel.sent_messages) == 1
    assert "Use `!streak` in <#555>." == channel.sent_messages[0].content


def test_tts_command_updates_and_reports_state():
    channel = FakeChannel(channel_id=999)
    state = {}

    asyncio.run(
        handle_incoming_message(
            message=FakeIncomingMessage("!tts off", channel),
            riot_client=FakeRiotClient(),
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.update({key: value}),
            **_base_streak_kwargs(channel),
        )
    )
    assert any("now `OFF`" in m.content for m in channel.sent_messages)

    asyncio.run(
        handle_incoming_message(
            message=FakeIncomingMessage("!tts status", channel),
            riot_client=FakeRiotClient(),
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.update({key: value}),
            **_base_streak_kwargs(channel),
        )
    )
    assert any("currently `OFF`" in m.content for m in channel.sent_messages)


def test_tts_command_allowed_in_match_recap_channel():
    channel = FakeChannel(channel_id=555)
    state = {}

    asyncio.run(
        handle_incoming_message(
            message=FakeIncomingMessage("!tts on", channel),
            riot_client=FakeRiotClient(),
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.update({key: value}),
            **{
                **_base_streak_kwargs(channel),
                "events_channel_id": 999,
                "match_recap_channel_id": 555,
            },
        )
    )

    assert any("now `ON`" in m.content for m in channel.sent_messages)


def test_tts_command_wrong_channel_prompts_events_or_recap():
    channel = FakeChannel(channel_id=777)
    state = {}

    asyncio.run(
        handle_incoming_message(
            message=FakeIncomingMessage("!tts status", channel),
            riot_client=FakeRiotClient(),
            db_get_state=lambda key: state.get(key),
            db_set_state=lambda key, value: state.update({key: value}),
            **{
                **_base_streak_kwargs(channel),
                "events_channel_id": 999,
                "match_recap_channel_id": 555,
            },
        )
    )

    assert len(channel.sent_messages) == 1
    assert "<#999>" in channel.sent_messages[0].content
    assert "<#555>" in channel.sent_messages[0].content

def test_health_command_includes_backfill_status():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!health", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=["Alpha#NA1"],
            riot_client=riot_client,
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=9,
            db_enabled=True,
            start_monotonic=0.0,
            mood_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "health-1234",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=lambda _msg: None,
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert len(channel.sent_messages) == 1
    text = channel.sent_messages[0].content
    assert "Backfill cursors active" in text
    assert "Backfill max offset" in text
    assert "Alpha#NA1=400" in text


def test_health_command_includes_worker_latency_metrics_when_available():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!health", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()
    worker_stats = {
        "refresh": {
            "cycles": 10,
            "errors": 1,
            "runs": 11,
            "elapsed_ms_last": 1200,
            "elapsed_ms_avg": 980,
            "elapsed_ms_max": 3000,
            "elapsed_ms_total": 10780,
        }
    }

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=["Alpha#NA1"],
            riot_client=riot_client,
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=9,
            db_enabled=True,
            start_monotonic=0.0,
            mood_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "health-5678",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=lambda _msg: None,
            weekly_report_channel_id=888,
            events_channel_id=999,
            worker_stats=worker_stats,
        )
    )

    assert len(channel.sent_messages) == 1
    text = channel.sent_messages[0].content
    assert "refresh: 10ok/1err" in text
    assert "1200ms last/980ms avg/3000ms max" in text


def test_backfill_command_runs_cache_backfill_and_reports_summary():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!backfill 2026-01-01 2026-01-31", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=["Alpha#NA1"],
            riot_client=FakeRiotClient(),
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=9,
            db_enabled=True,
            start_monotonic=0.0,
            mood_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "backfill-1234",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=lambda _msg: None,
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert len(mood_service.backfill_calls) == 1
    assert len(channel.sent_messages) == 1
    text = channel.sent_messages[0].content
    assert "Backfill complete" in text
    assert "Player entries updated: `5`" in text


def test_backfill_command_rejects_invalid_date_format():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!backfill 2026-01-XX 2026-01-31", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])

    asyncio.run(
        handle_incoming_message(
            message=incoming,
            channel_id=777,
            friends=["Alpha#NA1"],
            riot_client=FakeRiotClient(),
            mood_service=mood_service,
            report_timezone_name="UTC",
            report_day_start_hour=9,
            db_enabled=True,
            start_monotonic=0.0,
            mood_request_lock=asyncio.Lock(),
            request_id_context=contextvars.ContextVar("request_id", default=None),
            create_request_id=lambda _prefix: "backfill-5678",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id,
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=lambda _msg: None,
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert mood_service.backfill_calls == []
    assert len(channel.sent_messages) == 1
    assert "invalid date format" in channel.sent_messages[0].content


def test_profile_command_returns_summary_for_player():
    channel = FakeChannel(channel_id=999)
    incoming = FakeIncomingMessage("!profile Alpha#NA1", channel)
    mood_service = FakeMoodService(build_outputs=["unused"])
    riot_client = FakeRiotClient()

    now_ms = 1_738_500_000_000
    riot_client.recent_ids_by_puuid["puuid-1"] = ["M3", "M2", "M1"]
    riot_client.match_info_by_id = {
        "M3": {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": now_ms,
                "participants": [{"puuid": "puuid-1", "win": True, "kills": 5, "deaths": 2, "assists": 6}],
            }
        },
        "M2": {
            "info": {
                "queueId": 420,
                "gameDuration": 1700,
                "gameEndTimestamp": now_ms - 200000,
                "participants": [{"puuid": "puuid-1", "win": True, "kills": 4, "deaths": 3, "assists": 5}],
            }
        },
        "M1": {
            "info": {
                "queueId": 420,
                "gameDuration": 1600,
                "gameEndTimestamp": now_ms - 400000,
                "participants": [{"puuid": "puuid-1", "win": True, "kills": 6, "deaths": 4, "assists": 4}],
            }
        },
    }

    mood_service.db_load_latest_stats = lambda _cycle: [
        {
            "riot_id": "Alpha#NA1",
            "solo_wins": 3,
            "solo_losses": 1,
            "flex_wins": 0,
            "flex_losses": 0,
            "cs_total": 560,
            "minutes_total": 120.0,
            "objective_damage": 20000,
            "player_damage": 60000,
            "healing": 10000,
            "damage_taken": 25000,
            "kills": 30,
            "deaths": 18,
            "vision_score": 90,
            "primary_role": "MIDDLE",
        }
    ]
    mood_service.db_load_weekly_stats = lambda _start, _end: [
        {
            "riot_id": "Alpha#NA1",
            "solo_wins": 8,
            "solo_losses": 5,
            "flex_wins": 2,
            "flex_losses": 1,
            "cs_total": 1400,
            "minutes_total": 310.0,
            "objective_damage": 40000,
            "player_damage": 140000,
            "healing": 30000,
            "damage_taken": 70000,
            "kills": 70,
            "deaths": 40,
            "vision_score": 200,
        }
    ]

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
            create_request_id=lambda _prefix: "profile-1234",
            get_or_create_report_message=lambda _channel, _initial_content: None,
            remember_report_message=lambda _message: None,
            normalize_riot_id=lambda riot_id: riot_id.strip(),
            db_upsert_player=lambda _riot_id, _puuid: None,
            log=lambda _msg: None,
            weekly_report_channel_id=888,
            events_channel_id=999,
        )
    )

    assert len(channel.sent_messages) == 1
    text = channel.sent_messages[0].content
    assert "Profile: Alpha#NA1" in text
    assert "Today: `3W-1L`" in text
    assert "Week: `10W-6L`" in text
    assert "Streak: `W3`" in text
    assert "Recent ranked KDA" in text
