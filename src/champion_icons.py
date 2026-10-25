"""Champion display in recaps: name, portrait (application-owned Discord emoji), or both.

Portraits come from Riot Data Dragon and are uploaded once as application emojis
named `rw_champ_<championId>`, then reused across restarts. Any failure falls back to
the champion name, so a recap is never blocked or lost.
"""

import asyncio
import json
import re
import time

import discord
import requests


CHAMPION_DISPLAY_STATE_KEY = "champion_display_mode"
CHAMPION_DISPLAY_MODES = ("name", "icon", "both")
DEFAULT_CHAMPION_DISPLAY_MODE = "name"

CDN = "https://ddragon.leagueoflegends.com"
EMOJI_CACHE_SECONDS = 300
CATALOG_CACHE_SECONDS = 86400
RETRY_SECONDS = 300
LOOKUP_TIMEOUT_SECONDS = 10
# Total time one recap batch may spend fetching/uploading new portraits. After that,
# only cached portraits are used, so the first batch after enabling stays prompt.
RECAP_LOOKUP_BUDGET_SECONDS = 15
MAX_PORTRAIT_BYTES = 256 * 1024


def parse_champion_display_mode(raw):
    mode = str(raw or "").strip().casefold()
    return mode if mode in CHAMPION_DISPLAY_MODES else DEFAULT_CHAMPION_DISPLAY_MODE


def format_champion_display(champion_name, mode, icon=None):
    """Recap text for a champion; portrait modes fall back to the name without an icon."""
    name = f"**{champion_name}**"
    if not icon or mode == "name":
        return name
    if mode == "icon":
        return icon
    return f"{icon} {name}"


def _download(url, limit):
    with requests.get(url, timeout=(3, 8), stream=True) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(8192):
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("Champion asset exceeds size limit")
        return bytes(data)


def _load_catalog():
    versions = json.loads(_download(f"{CDN}/api/versions.json", 64 * 1024))
    version = versions[0]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid Data Dragon version")
    data = json.loads(_download(f"{CDN}/cdn/{version}/data/en_US/champion.json", 2 * 1024 * 1024))
    icons = {}
    for champion in data["data"].values():
        filename = champion["image"]["full"]
        if not re.fullmatch(r"[A-Za-z0-9_]+\.png", filename):
            continue
        # Numeric IDs avoid name mismatches such as FiddleSticks vs Fiddlesticks.
        icons[int(champion["key"])] = f"{CDN}/cdn/{version}/img/champion/{filename}"
    return icons


class ChampionIcons:
    def __init__(self, client, log):
        self.client = client
        self.log = log
        self._lock = asyncio.Lock()
        self._emojis = {}
        self._emoji_expires = 0
        self._catalog = {}
        self._catalog_expires = 0
        self._retry_at = 0

    async def get(self, champion_id, *, cached_only=False):
        """Emoji markup for a champion, or None to show the name instead.

        cached_only skips Discord/Data Dragon requests and returns only a known emoji.
        """
        try:
            champion_id = int(champion_id)
        except (TypeError, ValueError):
            return None
        if champion_id <= 0:
            return None
        name = f"rw_champ_{champion_id}"
        async with self._lock:
            now = time.monotonic()
            if now < self._emoji_expires and name in self._emojis:
                return self._emojis[name]
            if cached_only or now < self._retry_at:
                return None
            try:
                return await asyncio.wait_for(self._resolve(champion_id, name), timeout=LOOKUP_TIMEOUT_SECONDS)
            except (discord.HTTPException, requests.RequestException, TimeoutError,
                    ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                # A timed-out upload may still succeed remotely; re-fetch the emoji list
                # before uploading again.
                self._emoji_expires = 0
                self._retry_at = time.monotonic() + RETRY_SECONDS
                self.log(f"[champicons] Using champion names; retry in {RETRY_SECONDS}s: {type(exc).__name__}")
                return None

    async def _resolve(self, champion_id, name):
        if time.monotonic() >= self._emoji_expires:
            emojis = await self.client.fetch_application_emojis()
            self._emojis = {emoji.name: str(emoji) for emoji in emojis if emoji.name.startswith("rw_champ_")}
            self._emoji_expires = time.monotonic() + EMOJI_CACHE_SECONDS
        if name in self._emojis:
            return self._emojis[name]
        if time.monotonic() >= self._catalog_expires:
            self._catalog = await asyncio.to_thread(_load_catalog)
            self._catalog_expires = time.monotonic() + CATALOG_CACHE_SECONDS
        url = self._catalog.get(champion_id)
        if url is None:
            return None
        image = await asyncio.to_thread(_download, url, MAX_PORTRAIT_BYTES)
        emoji = await self.client.create_application_emoji(name=name, image=image)
        self._emojis[name] = str(emoji)
        return self._emojis[name]
