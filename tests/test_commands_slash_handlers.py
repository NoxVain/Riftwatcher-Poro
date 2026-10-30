import asyncio

from src.commands.slash_handlers import handle_health_slash, handle_sync_command
from src.discord_text import format_health_report


HEALTH_STATS = {
    "uptime_seconds": 3661,
    "tracked_players": 4,
    "db_ok": True,
    "match_cache_entries": 1200,
    "request_cache_active": False,
    "players_with_backfill_offset": 2,
    "max_backfill_offset": 300,
    "top_backfill_offsets": ["A#1=300", "B#2=100"],
    "baseline_age_seconds": None,
    "worker_stats": {"refresh": {"cycles": 5, "errors": 1, "elapsed_ms_last": 10, "elapsed_ms_avg": 12, "elapsed_ms_max": 20}},
}


# --- format_health_report ---------------------------------------------------


def test_format_health_report_includes_core_fields():
    text = format_health_report(HEALTH_STATS)
    assert text.startswith("Health OK\n")
    assert "- Uptime: `1:01:01`" in text
    assert "- Tracked players: `4`" in text
    assert "- DB: `ok`" in text
    assert "- Backfill cursors active: `2/4`" in text
    assert "- Backfill top offsets: `A#1=300, B#2=100`" in text
    assert "- Gamer Score baselines: `not yet built`" in text
    assert "refresh: 5ok/1err (10ms last/12ms avg/20ms max)" in text


def test_format_health_report_custom_title_and_baseline():
    stats = dict(HEALTH_STATS, baseline_age_seconds=60, baseline_roles=5, baseline_samples=900, worker_stats={})
    text = format_health_report(stats, title="**Health OK**")
    assert text.startswith("**Health OK**\n")
    assert "`5 roles, 900 samples (built 0:01:00 ago)`" in text
    assert "Workers" not in text


# --- /health ----------------------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))

    async def defer(self, thinking=False):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


class FakeInteraction:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakePoroService:
    def __init__(self, error=None):
        self.error = error

    async def run_health_check(self, _start_monotonic, worker_stats=None):
        if self.error:
            raise self.error
        return HEALTH_STATS


def _run_health(interaction, service):
    asyncio.run(
        handle_health_slash(
            interaction,
            commands_channel_id=111,
            poro_service=service,
            start_monotonic=0.0,
            worker_stats={},
            log=lambda _msg: None,
        )
    )


def test_health_slash_rejects_wrong_channel_ephemerally():
    interaction = FakeInteraction(channel_id=999)
    _run_health(interaction, FakePoroService())
    assert interaction.response.sent == [("Use `/health` in <#111>.", True)]
    assert interaction.response.deferred is False
    assert interaction.followup.sent == []


def test_health_slash_defers_then_sends_report():
    interaction = FakeInteraction(channel_id=111)
    _run_health(interaction, FakePoroService())
    assert interaction.response.deferred is True
    assert len(interaction.followup.sent) == 1
    assert interaction.followup.sent[0].startswith("**Health OK**")


def test_health_slash_reports_failure_after_defer():
    interaction = FakeInteraction(channel_id=111)
    _run_health(interaction, FakePoroService(error=RuntimeError("db down")))
    assert interaction.response.deferred is True
    assert interaction.followup.sent == ["Health check failed: internal error."]
    assert "db down" not in interaction.followup.sent[0]


# --- !sync ------------------------------------------------------------------


class FakeStatusMessage:
    def __init__(self, content):
        self.content = content

    async def edit(self, *, content):
        self.content = content


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        message = FakeStatusMessage(content)
        self.sent.append(message)
        return message


class FakePermissions:
    def __init__(self, administrator):
        self.administrator = administrator


class FakeMember:
    def __init__(self, administrator):
        self.guild_permissions = FakePermissions(administrator)


class FakeDmUser:
    """discord.User in DMs has no guild_permissions attribute."""


class FakeMessage:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.channel = FakeChannel()


class FakeTree:
    def __init__(self, error=None):
        self.error = error
        self.copied_to = None
        self.synced_to = None

    def copy_global_to(self, *, guild):
        self.copied_to = guild

    async def sync(self, *, guild):
        if self.error:
            raise self.error
        self.synced_to = guild
        return ["health"]


def _run_sync(message, tree):
    asyncio.run(handle_sync_command(message, tree=tree, log=lambda _msg: None))


def test_sync_in_dm_does_not_crash():
    message = FakeMessage(author=FakeDmUser(), guild=None)
    tree = FakeTree()
    _run_sync(message, tree)
    assert [m.content for m in message.channel.sent] == ["`!sync` can only be used in a server channel."]
    assert tree.copied_to is None


def test_sync_requires_administrator():
    message = FakeMessage(author=FakeMember(administrator=False), guild="guild-1")
    tree = FakeTree()
    _run_sync(message, tree)
    assert [m.content for m in message.channel.sent] == ["You need administrator permissions to sync commands."]
    assert tree.synced_to is None


def test_sync_copies_and_syncs_to_guild():
    message = FakeMessage(author=FakeMember(administrator=True), guild="guild-1")
    tree = FakeTree()
    _run_sync(message, tree)
    assert tree.copied_to == "guild-1"
    assert tree.synced_to == "guild-1"
    assert message.channel.sent[0].content == "Successfully synced 1 commands to this server."


def test_sync_reports_failure():
    message = FakeMessage(author=FakeMember(administrator=True), guild="guild-1")
    tree = FakeTree(error=RuntimeError("rate limited"))
    _run_sync(message, tree)
    assert message.channel.sent[0].content == "Failed to sync commands: internal error."
