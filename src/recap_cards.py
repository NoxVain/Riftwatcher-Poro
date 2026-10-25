"""Match recap cards (Discord Components V2): one container per match.

The card wraps the same compact header and player text as the plain-text recap,
adding a result-coloured accent and per-player components. Each player's text is its own
TextDisplay with a stable component ID, so a later LP edit targets exactly that
player. The plain-text sections are kept alongside, so a batch can still be sent as
text if Discord rejects the card.
"""

from dataclasses import dataclass, field

import discord

from src.discord_text import ARENA_QUEUE_IDS, format_match_duration, recap_queue_label

# Discord limits for a Components V2 message: 40 components in total (containers and
# their children count), 4000 characters of text across all text displays.
MAX_CARD_COMPONENTS = 40
MAX_CARD_TEXT = 4000
# Room kept free per player whose LP may be edited in later (" | `Gold I 90LP → ...`").
LP_TEXT_RESERVE = 80

ACCENT_WIN = 0x3BA55C
ACCENT_LOSS = 0xED4245
ACCENT_MIXED = 0x5865F2
ACCENT_NEUTRAL = 0x949BA4
ACCENT_ARENA = 0xF0B232


RESULT_ACCENTS = {
    "Win": ACCENT_WIN,
    "Loss": ACCENT_LOSS,
    "Mixed results": ACCENT_MIXED,
    "Placements": ACCENT_ARENA,
    "No result": ACCENT_NEUTRAL,
}


def match_result(queue_id, participants):
    """Result label for a match card from the tracked players' results."""
    if queue_id in ARENA_QUEUE_IDS:
        return "Placements"
    results = [participant.get("win") for participant in participants]
    if not results or any(type(result) is not bool for result in results):
        return "No result"
    if all(results):
        return "Win"
    if not any(results):
        return "Loss"
    return "Mixed results"


def match_accent(queue_id, participants):
    """Accent colour for a match card from the tracked players' results."""
    return RESULT_ACCENTS[match_result(queue_id, participants)]


def format_card_header(queue_id, duration_seconds, result):
    """Small grey header line: queue, result and game length (no date or clock)."""
    return f"-# {recap_queue_label(queue_id)} · **{result}** · {format_match_duration(duration_seconds)}"


@dataclass
class CardPlayer:
    riot_id: str
    text: str
    # Pending-LP entry (match_id, heading, ...) when LP may be edited in later.
    pending: dict | None = None


@dataclass
class CardMatch:
    header: str
    accent: int
    players: list[CardPlayer]


@dataclass
class CardMessage:
    view: discord.ui.LayoutView = field(default_factory=lambda: discord.ui.LayoutView(timeout=None))
    # Plain-text recap sections for this message, used if the card is rejected.
    text_sections: list[str] = field(default_factory=list)
    # [{"riot_id", "pending"}] with the player's component_id added.
    waiting: list[dict] = field(default_factory=list)
    reserved_text: int = 0


def _shorten(text, limit):
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _component_count(view):
    return sum(1 for _ in view.walk_children())


def build_card_messages(matches):
    """Pack match cards into as few messages as Discord's limits allow.

    A match that doesn't fit continues in the next message under a "(continued)"
    header; a player text too long for any message is shortened rather than failing
    the batch, so a recap always goes out.
    """
    messages = []
    current = CardMessage()
    next_id = 1

    def finish_current():
        nonlocal current
        if current.view.children:
            messages.append(current)
        current = CardMessage()

    for match in matches:
        container = None
        section_lines = []
        continued = False
        for player in match.players:
            reserve = LP_TEXT_RESERVE if player.pending else 0
            while True:
                header = match.header if not continued else _continued_header(match.header)
                new_components = 2 if container is not None else 3  # (separator|container+header) + text
                new_text = len(player.text) + (0 if container is not None else len(header))
                fits = (
                    current.view.content_length() + current.reserved_text + new_text + reserve <= MAX_CARD_TEXT
                    and _component_count(current.view) + new_components <= MAX_CARD_COMPONENTS
                )
                if fits or not current.view.children:
                    break
                # Close this message; the match continues in the next one.
                if container is not None:
                    current.text_sections.append("\n\n".join(section_lines))
                    section_lines = []
                    continued = True
                container = None
                finish_current()

            if container is None:
                header = match.header if not continued else _continued_header(match.header)
                container = discord.ui.Container(accent_colour=match.accent, id=next_id)
                container.add_item(discord.ui.TextDisplay(header, id=next_id + 1))
                next_id += 2
                current.view.add_item(container)
                section_lines = [header]
            else:
                container.add_item(
                    discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small, id=next_id)
                )
                next_id += 1

            room = MAX_CARD_TEXT - current.view.content_length() - current.reserved_text - reserve
            text = _shorten(player.text, room)
            component_id = next_id
            next_id += 1
            container.add_item(discord.ui.TextDisplay(text, id=component_id))
            section_lines.append(text)
            current.reserved_text += reserve
            if player.pending:
                current.waiting.append(
                    {"riot_id": player.riot_id, "pending": dict(player.pending, component_id=component_id)}
                )
        if container is not None:
            current.text_sections.append("\n\n".join(section_lines))
    finish_current()
    return messages


def _continued_header(header):
    first, newline, rest = header.partition("\n")
    return f"{first} (continued){newline}{rest}"


def add_lp_to_card(message, component_id, heading, summary, add_lp_to_content):
    """LayoutView for `message` with the LP summary added to one player, or None.

    None when the component is gone, no longer starts with the expected heading, or
    the edit would exceed Discord's text limit.
    """
    view = discord.ui.LayoutView.from_message(message, timeout=None)
    targets = [
        item for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay) and item.id == component_id
    ]
    if len(targets) != 1:
        return None
    updated = add_lp_to_content(targets[0].content, heading, summary)
    if updated is None:
        return None
    targets[0].content = updated
    if view.content_length() > MAX_CARD_TEXT:
        return None
    return view
