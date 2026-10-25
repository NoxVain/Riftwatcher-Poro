"""Champion display in recaps: name, portrait (application-owned Discord emoji), or both.

Portraits come from Riot Data Dragon and are uploaded once as application emojis named
after the champion (`:Ahri:`, `:KaiSa:`), then reused across restarts. Emojis uploaded
under the earlier `rw_champ_<championId>` names are renamed in place. Any failure falls
back to the champion name, so a recap is never blocked or lost.
"""

import asyncio
import json
import re
import time

import discord
import requests


CHAMPION_DISPLAY_STATE_KEY = "champion_display_mode"
CHAMPION_DISPLAY_MODES = ("name", "icon", "both")
# Portrait by default; format_champion_display falls back to the name without one.
DEFAULT_CHAMPION_DISPLAY_MODE = "icon"

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


def emoji_name_for_champion(champion_name, champion_id):
    """Discord emoji name (2-32 chars of A-Z, a-z, 0-9, _): "Kai'Sa" -> "KaiSa"."""
    name = re.sub(r"[^A-Za-z0-9_]", "", str(champion_name or ""))[:32]
    return name if len(name) >= 2 else f"champ_{int(champion_id)}"


def _legacy_emoji_name(champion_id):
    return f"rw_champ_{champion_id}"


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
    catalog = {}
    for champion in data["data"].values():
        filename = champion["image"]["full"]
        if not re.fullmatch(r"[A-Za-z0-9_]+\.png", filename):
            continue
        # Keyed by numeric ID (what match data carries); names avoid FiddleSticks-style mismatches.
        champion_id = int(champion["key"])
        catalog[champion_id] = {
            "emoji_name": emoji_name_for_champion(champion.get("name"), champion_id),
            "url": f"{CDN}/cdn/{version}/img/champion/{filename}",
        }
    return catalog


class ChampionIcons:
    def __init__(self, client, log):
        self.client = client
        self.log = log
        self._lock = asyncio.Lock()
        self._emojis = {}  # emoji name -> application emoji (all of this app's emojis)
        self._markup_by_id = {}  # champion ID -> "<:Ahri:123>", valid until _emoji_expires
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
        async with self._lock:
            now = time.monotonic()
            if now < self._emoji_expires and champion_id in self._markup_by_id:
                return self._markup_by_id[champion_id]
            if cached_only or now < self._retry_at:
                return None
            try:
                return await asyncio.wait_for(self._resolve(champion_id), timeout=LOOKUP_TIMEOUT_SECONDS)
            except (discord.HTTPException, requests.RequestException, TimeoutError,
                    ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                # A timed-out upload may still succeed remotely; re-fetch the emoji list
                # before uploading again.
                self._emoji_expires = 0
                self._retry_at = time.monotonic() + RETRY_SECONDS
                self.log(f"[champicons] Using champion names; retry in {RETRY_SECONDS}s: {type(exc).__name__}")
                return None

    async def _resolve(self, champion_id):
        if time.monotonic() >= self._emoji_expires:
            emojis = await self.client.fetch_application_emojis()
            self._emojis = {emoji.name: emoji for emoji in emojis}
            self._markup_by_id = {}
            self._emoji_expires = time.monotonic() + EMOJI_CACHE_SECONDS
        # The catalog maps the numeric ID from match data to the champion's emoji name.
        if time.monotonic() >= self._catalog_expires:
            self._catalog = await asyncio.to_thread(_load_catalog)
            self._catalog_expires = time.monotonic() + CATALOG_CACHE_SECONDS
        entry = self._catalog.get(champion_id)
        if entry is None:
            return None
        name = entry["emoji_name"]
        emoji = self._emojis.get(name)
        if emoji is None:
            legacy = self._emojis.pop(_legacy_emoji_name(champion_id), None)
            if legacy is not None:
                # Uploaded before emojis were named after champions: rename, don't re-upload.
                emoji = await legacy.edit(name=name)
                self.log(f"[champicons] Renamed {legacy.name} to {name}.")
            else:
                image = await asyncio.to_thread(_download, entry["url"], MAX_PORTRAIT_BYTES)
                emoji = await self.client.create_application_emoji(name=name, image=image)
            self._emojis[name] = emoji
        self._markup_by_id[champion_id] = str(emoji)
        return self._markup_by_id[champion_id]
