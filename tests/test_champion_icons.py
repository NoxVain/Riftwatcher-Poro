import asyncio
import json

import discord
import pytest
import requests

from src import champion_icons as icons


class Emoji:
    def __init__(self, name, emoji_id=123):
        self.name = name
        self.id = emoji_id

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


class Client:
    def __init__(self, emojis=()):
        self.emojis = list(emojis)
        self.fetches = 0
        self.uploads = []

    async def fetch_application_emojis(self):
        self.fetches += 1
        return list(self.emojis)

    async def create_application_emoji(self, *, name, image):
        self.uploads.append((name, image))
        emoji = Emoji(name)
        self.emojis.append(emoji)
        return emoji


def _service(client, logs=None):
    return icons.ChampionIcons(client, (logs if logs is not None else []).append)


@pytest.mark.parametrize(
    "raw,expected",
    [(None, "name"), ("", "name"), ("garbage", "name"), ("name", "name"), ("ICON", "icon"), (" both ", "both")],
)
def test_parse_champion_display_mode_defaults_to_name(raw, expected):
    assert icons.parse_champion_display_mode(raw) == expected


def test_format_champion_display_modes_and_fallback():
    icon = "<:rw_champ_103:1>"
    assert icons.format_champion_display("Ahri", "name", icon) == "**Ahri**"
    assert icons.format_champion_display("Ahri", "icon", icon) == icon
    assert icons.format_champion_display("Ahri", "both", icon) == f"{icon} **Ahri**"
    # No portrait available: every mode falls back to the name.
    for mode in icons.CHAMPION_DISPLAY_MODES:
        assert icons.format_champion_display("Ahri", mode, None) == "**Ahri**"


def test_reuses_existing_emoji_after_restart_without_download(monkeypatch):
    monkeypatch.setattr(icons, "_load_catalog", lambda: pytest.fail("unexpected Data Dragon request"))
    client = Client([Emoji("rw_champ_90")])
    for _restart in range(2):
        assert asyncio.run(_service(client).get(90)) == "<:rw_champ_90:123>"
    assert client.uploads == []


def test_concurrent_lookups_upload_once_and_cache_catalog(monkeypatch):
    calls = []
    monkeypatch.setattr(icons, "_load_catalog", lambda: calls.append("catalog") or {90: "malz", 103: "ahri"})
    monkeypatch.setattr(icons, "_download", lambda url, limit: calls.append(url) or b"png")
    client = Client()
    service = _service(client)

    async def run():
        assert await asyncio.gather(service.get(90), service.get(90)) == ["<:rw_champ_90:123>"] * 2
        assert await service.get(103) == "<:rw_champ_103:123>"

    asyncio.run(run())
    assert client.fetches == 1
    assert client.uploads == [("rw_champ_90", b"png"), ("rw_champ_103", b"png")]
    assert calls == ["catalog", "malz", "ahri"]


def test_cached_only_never_makes_requests(monkeypatch):
    monkeypatch.setattr(icons, "_load_catalog", lambda: pytest.fail("unexpected Data Dragon request"))
    client = Client([Emoji("rw_champ_90")])
    service = _service(client)

    async def run():
        # Nothing cached yet: no Discord or Data Dragon request is made.
        assert await service.get(90, cached_only=True) is None
        assert client.fetches == 0
        assert await service.get(90) == "<:rw_champ_90:123>"
        assert await service.get(90, cached_only=True) == "<:rw_champ_90:123>"

    asyncio.run(run())
    assert client.fetches == 1


def test_failure_falls_back_backs_off_then_recovers(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(icons.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(icons, "_load_catalog", lambda: {90: "malz"})
    downloads = []

    def failing_download(url, limit):
        downloads.append(url)
        raise requests.Timeout("offline")

    monkeypatch.setattr(icons, "_download", failing_download)
    logs = []
    client = Client()
    service = _service(client, logs)

    assert asyncio.run(service.get(90)) is None
    assert asyncio.run(service.get(90)) is None  # backing off: no second download
    assert downloads == ["malz"]
    assert any("Using champion names" in line for line in logs)

    clock[0] += icons.RETRY_SECONDS + 1
    monkeypatch.setattr(icons, "_download", lambda url, limit: b"png")
    assert asyncio.run(service.get(90)) == "<:rw_champ_90:123>"


def test_discord_permission_failure_falls_back():
    class ForbiddenClient(Client):
        async def fetch_application_emojis(self):
            raise discord.HTTPException(type("Resp", (), {"status": 403, "reason": "Forbidden"})(), "no access")

    assert asyncio.run(_service(ForbiddenClient()).get(90)) is None


def test_timeout_falls_back(monkeypatch):
    monkeypatch.setattr(icons, "LOOKUP_TIMEOUT_SECONDS", 0.01)

    class SlowClient(Client):
        async def fetch_application_emojis(self):
            await asyncio.sleep(1)
            return []

    assert asyncio.run(_service(SlowClient()).get(90)) is None


@pytest.mark.parametrize("champion_id", [None, "", "abc", 0, -5])
def test_invalid_champion_ids_return_none_without_requests(champion_id):
    client = Client()
    assert asyncio.run(_service(client).get(champion_id)) is None
    assert client.fetches == 0


def test_unknown_champion_returns_none_without_upload(monkeypatch):
    monkeypatch.setattr(icons, "_load_catalog", lambda: {90: "malz"})
    client = Client()
    assert asyncio.run(_service(client).get(9999)) is None
    assert client.uploads == []


def test_catalog_uses_numeric_ids_and_safe_filenames(monkeypatch):
    payloads = {
        f"{icons.CDN}/api/versions.json": json.dumps(["15.21.1", "15.20.1"]).encode(),
        f"{icons.CDN}/cdn/15.21.1/data/en_US/champion.json": json.dumps(
            {
                "data": {
                    "Fiddlesticks": {"key": "9", "image": {"full": "FiddleSticks.png"}},
                    "Evil": {"key": "1", "image": {"full": "../../evil.png"}},
                }
            }
        ).encode(),
    }
    monkeypatch.setattr(icons, "_download", lambda url, limit: payloads[url])
    assert icons._load_catalog() == {9: f"{icons.CDN}/cdn/15.21.1/img/champion/FiddleSticks.png"}


def test_catalog_rejects_unexpected_version(monkeypatch):
    monkeypatch.setattr(icons, "_download", lambda url, limit: json.dumps(["../evil"]).encode())
    with pytest.raises(ValueError):
        icons._load_catalog()


def test_download_enforces_size_limit(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            yield b"x" * 600
            yield b"x" * 600

    monkeypatch.setattr(icons.requests, "get", lambda *a, **k: Response())
    with pytest.raises(ValueError):
        icons._download("https://example.invalid/a.png", 1000)
