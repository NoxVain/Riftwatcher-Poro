"""Compact Components V2 recaps; one container per match, split at player boundaries."""

from dataclasses import dataclass, field
from itertools import count
import re

import discord

from src.discord_text import (
    ARENA_QUEUE_IDS, format_match_duration, format_recap_queue_name, format_recap_role,
    get_arena_placement, get_arena_subteam_id, get_arena_team_damage_percentage,
    get_arena_augments, get_participant_items, get_arena_skillshot_stats,
)


MAX_COMPONENTS = 40
MAX_TEXT = 4000
LP_RESERVE = 160  # Room for every pending player's later LP edit.
ACCENTS = {"Win": 0x3BA55C, "Loss": 0xED4245, "Draw": 0x949BA4,
           "No result": 0x949BA4, "Mixed results": 0x5865F2, "Placements": 0xF0B232}


def safe_text(value, limit=120):
    text = " ".join(str(value).split())[:limit]
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def no_confirmed_result(match_info):
    info = match_info.get("info", {})
    explicit = str(info.get("endOfGameResult", "")).casefold().replace("_", "")
    if explicit in {"draw", "matchdraw"}:
        return "Draw"
    if info.get("queueId") in ARENA_QUEUE_IDS:
        return None  # Arena uses placement, not Summoner's Rift team win flags.
    teams = info.get("teams") or []
    if len(teams) >= 2 and not any(team.get("win") is True for team in teams):
        return "No result"
    return None


def card_result(match_info, participants):
    missing = no_confirmed_result(match_info)
    if missing:
        return missing
    if match_info.get("info", {}).get("queueId") in ARENA_QUEUE_IDS:
        return "Placements"
    results = [participant.get("win") for _riot_id, participant in participants]
    if not results or any(type(result) is not bool for result in results):
        return "No result"
    if all(results):
        return "Win"
    if not any(results):
        return "Loss"
    return "Mixed results"


def card_lp_summary(summary):
    # A nonbreaking space keeps the resulting total and delta together on mobile.
    return re.sub(r"(\d+LP) (\([+-]?\d+\))$", lambda m: f"**{m[1]}\u00a0{m[2]}**", summary)


def add_card_lp(text, summary):
    heading, separator, stats = text.partition("\n")
    return f"{heading} | {card_lp_summary(summary)}{separator}{stats}"


def format_card_player(riot_id, participant, duration, *, queue_id, champion_display,
                       mixed=False, augment_names=None, item_names=None):
    name = safe_text(riot_id.split("#", 1)[0])
    heading = f"**{name}** · {champion_display} {format_recap_role(participant)}"
    if mixed:
        heading += f" · **{'Win' if participant.get('win') else 'Loss'}**"
    number = lambda key: int(participant.get(key, 0) or 0)
    kda = f"{number('kills')}/{number('deaths')}/{number('assists')}"
    damage, taken, healing = number("totalDamageDealtToChampions"), number("totalDamageTaken"), number("totalHeal")
    if queue_id not in ARENA_QUEUE_IDS:
        cs = number("totalMinionsKilled") + number("neutralMinionsKilled")
        cs_per_min = cs / max(1.0, duration / 60)
        return (
            f"{heading}\n"
            f"**{kda}** KDA · **{cs_per_min:.1f}** CS/min · **{number('visionScore')}** Vision\n"
            f"Damage **{damage:,}** · Objectives **{number('damageDealtToObjectives'):,}**\n"
            f"Taken **{taken:,}** · Healing **{healing:,}**"
        )
    placement, team = get_arena_placement(participant), get_arena_subteam_id(participant)
    heading = f"**{name}** · {champion_display} · Team {team if team is not None else '?'}"
    heading += f" · **#{placement}**" if placement is not None else " · Placement unavailable"
    share = get_arena_team_damage_percentage(participant)
    share_text = f" ({share:.1f}% of team)" if share is not None else ""
    lines = [heading, f"**{kda}** KDA · Gold **{number('goldEarned'):,}**",
             f"Damage **{damage:,}**{share_text} · Taken **{taken:,}**",
             f"Healing **{healing:,}** · Shielded **{number('totalDamageShieldedOnTeammates'):,}**"]
    hit, dodged = get_arena_skillshot_stats(participant)
    if hit or dodged:
        lines.append(f"Skillshots hit **{hit}** · Dodged **{dodged}**")
    augments = get_arena_augments(participant, augment_names=augment_names)
    items = get_participant_items(participant, item_names=item_names)
    if augments:
        lines.append("Augments · " + ", ".join(safe_text(item) for item in augments))
    if items:
        lines.append("Items · " + ", ".join(safe_text(item) for item in items))
    return "\n".join(lines)


@dataclass
class CardPlayer:
    riot_id: str
    text: str
    pending: dict | None = None


@dataclass
class CardMatch:
    queue_id: int
    duration: float
    result: str
    players: list[CardPlayer]


@dataclass
class CardMessage:
    view: discord.ui.LayoutView = field(default_factory=lambda: discord.ui.LayoutView(timeout=None))
    waiting: list[dict] = field(default_factory=list)
    reserved: int = 0


def build_card_messages(matches):
    """Pack matches within Discord's 40-component/4000-character limits, reserving LP space."""
    messages = []
    current = CardMessage()
    ids = count(1)
    for match in matches:
        queue_label = re.sub(r"^[^A-Za-z0-9]+\s*", "", format_recap_queue_name(match.queue_id))
        container = None
        continued = False
        for player in match.players:
            metadata = (f"-# {queue_label} · **{match.result}**"
                        f" · {format_match_duration(match.duration)}" + (" · continued" if continued else ""))
            reserve = LP_RESERVE if player.pending else 0
            # Container + metadata + player for a new card; spacer + player otherwise.
            extra_count = 3 if container is None else 2
            extra_text = len(player.text) + (len(metadata) if container is None else 0)
            if (current.view.content_length() + current.reserved + extra_text + reserve > MAX_TEXT
                    or len(list(current.view.walk_children())) + extra_count > MAX_COMPONENTS):
                if not current.view.children:
                    raise ValueError("A recap player exceeds Discord's message limits")
                messages.append(current)
                current = CardMessage()
                continued = container is not None or continued
                container = None
                metadata = (f"-# {queue_label} · **{match.result}**"
                            f" · {format_match_duration(match.duration)}" + (" · continued" if continued else ""))
                if len(metadata) + len(player.text) + reserve > MAX_TEXT:
                    raise ValueError("A recap player exceeds Discord's message limits")
            if container is None:
                container = discord.ui.Container(accent_colour=ACCENTS[match.result], id=next(ids))
                container.add_item(discord.ui.TextDisplay(metadata, id=next(ids)))
                current.view.add_item(container)
            else:
                container.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small, id=next(ids)))
            component_id = next(ids)
            container.add_item(discord.ui.TextDisplay(player.text, id=component_id))
            current.reserved += reserve
            if player.pending:
                current.waiting.append({"riot_id": player.riot_id,
                                        "pending": dict(player.pending, component_id=component_id)})
    if current.view.children:
        messages.append(current)
    return messages
