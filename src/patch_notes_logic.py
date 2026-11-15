"""Pure helpers for League patch-notes announcements.

Riot has no official patch-notes API. The leagueoflegends.com patch-notes tag page embeds its
article list as Next.js page data (`__NEXT_DATA__`), and each article's HTML has one `<h2>` per
section with `<h3 class="change-title">` entries (champion/item names). Everything here is
network-free so it can be unit tested against small HTML samples.
"""

import json
import re
from datetime import datetime
from html import unescape
from html.parser import HTMLParser

from src.constants import DISCORD_MESSAGE_LIMIT

LEAGUE_SITE_BASE_URL = "https://www.leagueoflegends.com"
DESCRIPTION_MAX_CHARS = 300
MAX_NAMES_PER_SECTION = 40
SEEN_STATE_MAX_ENTRIES = 50

# Sections that appear in (nearly) every article and say nothing about balance changes.
BOILERPLATE_SECTIONS = frozenset(
    {
        "mid-patch updates",
        "patch highlights",
        "replay download limits",
        "mythic shop update",
        "minimum system requirements",
        "upcoming skins & chromas",
        "related articles",
    }
)
SECTION_EMOJIS = {
    "champions": "\U0001F9D9",  # mage
    "items": "\U0001F6E1\uFE0F",  # shield
}
DEFAULT_SECTION_EMOJI = "\U0001F527"  # wrench

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
_PATCH_TITLE_RE = re.compile(r"\bpatch\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class PatchNotesParseError(Exception):
    """The patch-notes page no longer has the structure we expect (likely a site redesign)."""


def _clean_text(value):
    text = unescape(_TAG_RE.sub(" ", str(value or "")))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _parse_iso_unix(value):
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _normalize_url(url, base_url):
    url = str(url or "").strip()
    if not url:
        return ""
    if url.startswith("/"):
        url = f"{base_url}{url}"
    return url if url.endswith("/") else f"{url}/"


def _iter_dicts(node):
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def parse_patch_listing(html, *, base_url=LEAGUE_SITE_BASE_URL):
    """Return patch articles from the tag page, newest first.

    Each article: {"id", "url", "title", "description", "published_at"} (published_at in unix
    seconds). Raises PatchNotesParseError when the embedded page data is missing or holds no
    articles, so a redesign surfaces as a worker error instead of silently posting nothing.
    """
    match = _NEXT_DATA_RE.search(html or "")
    if match is None:
        raise PatchNotesParseError("Patch-notes page has no __NEXT_DATA__ payload.")
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        raise PatchNotesParseError(f"Patch-notes page data is not valid JSON: {exc}") from exc

    articles = {}
    for node in _iter_dicts(data):
        title = node.get("title")
        published_at = _parse_iso_unix(node.get("publishedAt"))
        action = node.get("action")
        payload = action.get("payload") if isinstance(action, dict) else None
        raw_url = payload.get("url") if isinstance(payload, dict) else None
        if not isinstance(title, str) or published_at is None or not raw_url:
            continue
        if not _PATCH_TITLE_RE.search(title):
            continue
        url = _normalize_url(raw_url, base_url)
        analytics = node.get("analytics") if isinstance(node.get("analytics"), dict) else {}
        article_id = str(analytics.get("contentId") or url)
        description = node.get("description")
        if isinstance(description, dict):
            description = description.get("body")
        articles[article_id] = {
            "id": article_id,
            "url": url,
            "title": _clean_text(title),
            "description": _clean_text(description),
            "published_at": published_at,
        }

    if not articles:
        raise PatchNotesParseError("Patch-notes page data contained no patch articles.")
    return sorted(articles.values(), key=lambda article: article["published_at"], reverse=True)


class _ArticleSectionParser(HTMLParser):
    """Collect `<h2>` section titles and the `<h3 class="change-title">` entries under them."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = []
        self._capture = None  # "section" | "entry" | None
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        if tag == "h2":
            self._capture, self._buffer = "section", []
        elif tag == "h3":
            classes = (dict(attrs).get("class") or "").split()
            if "change-title" in classes and self.sections:
                self._capture, self._buffer = "entry", []

    def handle_endtag(self, tag):
        if self._capture == "section" and tag == "h2":
            title = _WHITESPACE_RE.sub(" ", "".join(self._buffer)).strip()
            if title:
                self.sections.append((title, []))
            self._capture = None
        elif self._capture == "entry" and tag == "h3":
            name = _WHITESPACE_RE.sub(" ", "".join(self._buffer)).strip()
            if name and name not in self.sections[-1][1]:
                self.sections[-1][1].append(name)
            self._capture = None

    def handle_data(self, data):
        if self._capture is not None:
            self._buffer.append(data)


def parse_patch_article_sections(html):
    """Return [(section_title, [entry names])] in article order, without boilerplate sections."""
    parser = _ArticleSectionParser()
    parser.feed(html or "")
    parser.close()
    return [
        (title, entries)
        for title, entries in parser.sections
        if title.casefold() not in BOILERPLATE_SECTIONS
    ]


def _format_names(names):
    shown = names[:MAX_NAMES_PER_SECTION]
    text = ", ".join(shown)
    hidden = len(names) - len(shown)
    if hidden > 0:
        text += f", +{hidden} more"
    return text


def _pack_lines(lines, max_len=DISCORD_MESSAGE_LIMIT):
    messages = []
    current = ""
    for line in lines:
        line = line[:max_len]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current.strip():
            messages.append(current)
        current = line
    if current.strip():
        messages.append(current)
    return messages


def format_patch_post(article, sections=None):
    """Render the Discord message(s) for one patch article.

    `sections` is the parsed article body; None/empty means the article could not be read, in
    which case the post falls back to title, date, blurb and link.
    """
    lines = [f"\U0001F4DC **{article['title']}**"]
    published_at = article.get("published_at")
    if published_at:
        lines.append(f"\U0001F5D3\uFE0F Published <t:{int(published_at)}:D>")
    description = article.get("description") or ""
    if description:
        if len(description) > DESCRIPTION_MAX_CHARS:
            description = description[: DESCRIPTION_MAX_CHARS - 3].rstrip() + "..."
        lines.append(f"> {description}")

    named = [(title, entries) for title, entries in (sections or []) if entries]
    unnamed = [title for title, entries in (sections or []) if not entries]
    if named or unnamed:
        lines.append("")
    for title, entries in named:
        emoji = SECTION_EMOJIS.get(title.casefold(), DEFAULT_SECTION_EMOJI)
        lines.append(f"{emoji} **{title} ({len(entries)}):** {_format_names(entries)}")
    if unnamed:
        lines.append(f"\U0001F9E9 **Also in this patch:** {', '.join(unnamed)}")

    lines.append("")
    lines.append(f"\U0001F517 {article['url']}")
    return _pack_lines(lines)


def load_seen_state(raw_value):
    """Parse the persisted seen-article state. None means "never baselined"."""
    if not raw_value:
        return None
    try:
        data = json.loads(raw_value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return [str(key) for key in data.get("keys") or []]


def dump_seen_state(keys):
    # Newest keys are appended last; keep only the most recent entries.
    unique = list(dict.fromkeys(keys))
    return json.dumps({"keys": unique[-SEEN_STATE_MAX_ENTRIES:]}, separators=(",", ":"))


def article_keys(article):
    """Both the CMS id and the URL count as identity, so a change to either one doesn't cause a repost."""
    return [key for key in (article.get("id"), article.get("url")) if key]


def select_new_articles(articles, seen_keys, *, now_unix, max_age_seconds):
    """Return unseen articles published within `max_age_seconds`, oldest first.

    Older unseen articles are ignored (not tracked): this keeps the seen-state small and stops
    old patches from being posted after downtime, a state reset, or a CMS id/URL migration.
    """
    seen = set(seen_keys)
    to_post = [
        article
        for article in articles
        if not any(key in seen for key in article_keys(article))
        and now_unix - int(article["published_at"]) <= max_age_seconds
    ]
    to_post.sort(key=lambda article: article["published_at"])
    return to_post


def seconds_until_next_check(last_checked_unix, *, now_unix, interval_seconds):
    """Seconds to wait before the next listing fetch (0 = due now); survives restarts/redeploys."""
    if not last_checked_unix:
        return 0
    return max(0, int(last_checked_unix) + int(interval_seconds) - int(now_unix))
