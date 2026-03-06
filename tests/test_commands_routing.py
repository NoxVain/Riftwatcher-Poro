import asyncio

import pytest

from src.commands.routing import command_channel_id, enforce_command_channel, is_supported_command


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent_messages = []

    async def send(self, content):
        self.sent_messages.append(content)


class FakeMessage:
    def __init__(self, content, channel_id):
        self.content = content
        self.channel = FakeChannel(channel_id)


class FakeContext:
    def __init__(self, *, content, channel_id, daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404):
        self.content = content
        self.content_lower = content.casefold()
        self.message = FakeMessage(content, channel_id)
        self.daily_channel_id = daily_channel_id
        self.weekly_channel_id = weekly_channel_id
        self.events_channel_id = events_channel_id
        self.match_recap_channel_id = match_recap_channel_id


@pytest.mark.parametrize(
    "content_lower, expected",
    [
        ("!daily", True),
        ("!weekly", True),
        ("!streak", True),
        ("!streak alpha#na1", True),
        ("!profile", True),
        ("!profile alpha#na1", True),
        ("!tts", True),
        ("!tts on", True),
        ("!add", True),
        ("!add alpha#na1", True),
        ("!remove", True),
        ("!remove alpha#na1", True),
        ("!debugplayer alpha#na1", True),
        ("!backfill", True),
        ("!backfill 2026-01-01 2026-01-31", True),
        ("!help", True),
        ("!unknown", False),
        ("hello", False),
    ],
)
def test_is_supported_command_matrix(content_lower, expected):
    assert is_supported_command(content_lower) is expected


def test_command_channel_id_matrix_with_recap_channel():
    assert command_channel_id("!daily", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 101
    assert command_channel_id("!weekly", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 202
    assert command_channel_id("!streak", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 404
    assert command_channel_id("!streak alpha#na1", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 404
    assert command_channel_id("!profile alpha#na1", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 303
    assert command_channel_id("!remove alpha#na1", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 303
    assert command_channel_id("!backfill 2026-01-01 2026-01-31", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 303
    assert command_channel_id("!tts", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == (303, 404)
    assert command_channel_id("!tts off", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == (303, 404)
    assert command_channel_id("!help", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 303


def test_command_channel_id_fallbacks_without_recap_channel():
    assert command_channel_id("!streak", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=None) == 303
    assert command_channel_id("!tts", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=None) == (303,)


def test_enforce_command_channel_allows_right_channel():
    ctx = FakeContext(content="!Daily", channel_id=101)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is False
    assert ctx.message.channel.sent_messages == []


def test_enforce_command_channel_prompts_daily_channel():
    ctx = FakeContext(content="!Daily", channel_id=303)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is True
    assert ctx.message.channel.sent_messages == ["Use `!Daily` in <#101>."]


def test_enforce_command_channel_prompts_recap_for_bare_streak():
    ctx = FakeContext(content="!streak", channel_id=303)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is True
    assert ctx.message.channel.sent_messages == ["Use `!streak` in <#404>."]


def test_enforce_command_channel_prompts_events_or_recap_for_tts():
    ctx = FakeContext(content="!tts status", channel_id=101)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is True
    assert ctx.message.channel.sent_messages == ["Use `!tts` in <#303> or <#404>."]


def test_enforce_command_channel_ignores_non_commands():
    ctx = FakeContext(content="hello world", channel_id=101)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is False
    assert ctx.message.channel.sent_messages == []
