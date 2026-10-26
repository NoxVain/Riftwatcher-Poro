import uuid
from datetime import timedelta

import discord
import requests


def create_request_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def describe_error_for_user(exc):
    """Return a short, safe description of `exc` for posting in Discord.

    Raw exception text can include DB hosts/usernames (psycopg) or full API URLs (requests),
    so only well-known, non-sensitive categories are surfaced. Log the full exception separately.
    """
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status in {401, 403}:
            return f"Riot API rejected the API key (HTTP {status}); check RIOT_API_KEY"
        if status == 404:
            return "not found on Riot API (HTTP 404); check the Riot ID"
        if status == 429:
            return "Riot API rate limit reached (HTTP 429); try again shortly"
        if status >= 500:
            return f"Riot API is having issues (HTTP {status}); try again later"
        return f"Riot API returned HTTP {status}"
    if isinstance(exc, requests.Timeout):
        return "Riot API request timed out"
    if isinstance(exc, requests.RequestException):
        return "could not reach Riot API"
    if isinstance(exc, KeyError):
        return "unexpected response from Riot API"
    if isinstance(exc, discord.HTTPException):
        return f"Discord API error (HTTP {exc.status})"
    return "internal error"


def format_user_error(prefix, exc, request_id=None):
    """`"<prefix>: <safe description>"`, plus a log reference when a request ID is available."""
    text = f"{prefix}: {describe_error_for_user(exc)}."
    if request_id:
        text += f" (ref `{request_id}`)"
    return text


def report_signature(text):
    lines = [line for line in text.splitlines() if not line.startswith("_Last updated:")]
    return "\n".join(lines)


def format_health_report(stats, *, title="Health OK"):
    uptime = str(timedelta(seconds=stats["uptime_seconds"]))
    top_backfill_offsets = ", ".join(stats.get("top_backfill_offsets", [])) or "none"
    wstats = stats.get("worker_stats") or {}
    workers_line = ""
    if wstats:
        parts = []
        for key, value in wstats.items():
            segment = f"{key}: {value['cycles']}ok/{value['errors']}err"
            if "elapsed_ms_last" in value:
                segment += (
                    f" ({value.get('elapsed_ms_last', 0)}ms last/"
                    f"{value.get('elapsed_ms_avg', 0)}ms avg/"
                    f"{value.get('elapsed_ms_max', 0)}ms max)"
                )
            parts.append(segment)
        workers_line = f"\n- Workers: `{' | '.join(parts)}`"
    baseline_age_seconds = stats.get("baseline_age_seconds")
    if baseline_age_seconds is None:
        baseline_line = "\n- Gamer Score baselines: `not yet built`"
    else:
        age = str(timedelta(seconds=baseline_age_seconds))
        baseline_line = (
            f"\n- Gamer Score baselines: "
            f"`{stats['baseline_roles']} roles, {stats['baseline_samples']} samples (built {age} ago)`"
        )
    return (
        f"{title}\n"
        f"- Uptime: `{uptime}`\n"
        f"- Tracked players: `{stats['tracked_players']}`\n"
        f"- DB: `{'ok' if stats['db_ok'] else 'down'}`\n"
        f"- Match cache entries: `{stats['match_cache_entries']}`\n"
        f"- Report cache active: `{'yes' if stats['request_cache_active'] else 'no'}`\n"
        f"- Backfill cursors active: `{stats.get('players_with_backfill_offset', 0)}/{stats['tracked_players']}`\n"
        f"- Backfill max offset: `{stats.get('max_backfill_offset', 0)}`\n"
        f"- Backfill top offsets: `{top_backfill_offsets}`"
        f"{baseline_line}"
        f"{workers_line}"
    )


ARENA_QUEUE_IDS = frozenset({1700, 1710, 1750})


def match_recap_state_key(riot_id):
    return f"last_announced_match_id::{riot_id.casefold()}"


def streak_callout_state_key(riot_id):
    return f"last_announced_streak::{riot_id.casefold()}"


def streak_tts_enabled_state_key():
    return "streak_tts_enabled"


def parse_streak_tts_enabled(raw_value, default=True):
    if raw_value is None:
        return bool(default)
    value = str(raw_value).strip().lower()
    if value in {"0", "false", "off", "disabled", "no"}:
        return False
    if value in {"1", "true", "on", "enabled", "yes"}:
        return True
    return bool(default)


def format_recap_queue_name(queue_id):
    if queue_id == 420:
        return "\U0001F3C6 Ranked Solo/Duo"
    if queue_id == 440:
        return "\U0001F3C6 Ranked Flex"
    if queue_id == 710:
        return "\U0001F3C6 Ranked 5s"
    if queue_id == 1750:
        return "\U0001F3DF\uFE0F Arena 3x6"
    if queue_id in ARENA_QUEUE_IDS:
        return "\U0001F3DF\uFE0F Arena"
    label = RECAP_QUEUE_LABELS.get(queue_id)
    if label:
        return label
    return f"\U0001F3AF Queue {queue_id}"


def recap_queue_label(queue_id):
    """Queue name without its leading icon, for the compact recap header."""
    name = format_recap_queue_name(queue_id)
    icon, _space, label = name.partition(" ")
    return label if label and not any(char.isalnum() for char in icon) else name


RECAP_QUEUE_LABELS = {
    400: "\U0001F3AF Normal Draft",
    430: "\U0001F3AF Normal Blind",
    480: "\u26A1 Swiftplay",
    490: "\u26A1 Quickplay",
    450: "\u2744\uFE0F ARAM",
    2400: "\u2744\uFE0F ARAM Mayhem",
    700: "\u2694\uFE0F Clash",
    720: "\u2694\uFE0F ARAM Clash",
    900: "\U0001F3B2 ARURF",
    1900: "\U0001F3B2 URF",
    1020: "\U0001F3B2 One for All",
    1300: "\U0001F3B2 Nexus Blitz",
    830: "\U0001F916 Co-op vs AI",
    840: "\U0001F916 Co-op vs AI",
    850: "\U0001F916 Co-op vs AI",
    870: "\U0001F916 Co-op vs AI",
    880: "\U0001F916 Co-op vs AI",
    890: "\U0001F916 Co-op vs AI",
}


def format_match_duration(match_duration_seconds):
    total_seconds = max(0, int(match_duration_seconds or 0))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def format_recap_role(participant):
    role_value = (
        participant.get("teamPosition")
        or participant.get("individualPosition")
        or participant.get("lane")
        or participant.get("role")
        or ""
    )
    role_key = str(role_value).strip().upper()
    role_labels = {
        "TOP": "Top",
        "JUNGLE": "Jungle",
        "MIDDLE": "Mid",
        "MID": "Mid",
        "BOTTOM": "Bot",
        "BOT": "Bot",
        "UTILITY": "Support",
        "SUPPORT": "Support",
    }
    return role_labels.get(role_key, "Unknown Role")


def get_arena_placement(participant):
    for field_name in ("placement", "subteamPlacement", "teamPlacement"):
        value = participant.get(field_name)
        if value is None:
            continue
        try:
            placement = int(value)
        except (TypeError, ValueError):
            continue
        if placement > 0:
            return placement
    return None


def get_arena_subteam_id(participant):
    for field_name in ("playerSubteamId", "subteamId", "teamId"):
        value = participant.get(field_name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)
    return None


def get_arena_augments(participant, augment_names=None):
    augment_names = augment_names or {}
    augments = []
    for index in range(1, 7):
        value = participant.get(f"playerAugment{index}")
        if value in (None, 0, "0", ""):
            continue
        augment_id = str(value)
        augments.append(augment_names.get(augment_id, augment_id))
    return augments


def get_participant_items(participant, item_names=None):
    item_names = item_names or {}
    items = []
    for index in range(7):
        value = participant.get(f"item{index}")
        if value in (None, 0, "0", ""):
            continue
        item_id = str(value)
        items.append(item_names.get(item_id, item_id))
    return items


def get_arena_skillshot_stats(participant):
    challenges = participant.get("challenges") or {}
    hit = int(challenges.get("skillshotsHit", 0) or 0)
    dodged = int(challenges.get("skillshotsDodged", 0) or 0)
    return hit, dodged


def get_arena_team_damage_percentage(participant):
    challenges = participant.get("challenges") or {}
    value = challenges.get("teamDamagePercentage")
    if value is None:
        return None
    try:
        return float(value) * 100.0
    except (TypeError, ValueError):
        return None


def format_recap_player_line(
    riot_id,
    participant,
    match_duration_seconds,
    queue_id=None,
    augment_names=None,
    item_names=None,
    champion_display=None,
    show_result=False,
):
    lol_name = riot_id.split("#", 1)[0]
    champion = participant.get("championName", "Unknown")
    # Pre-rendered by the recap worker (name, portrait, or both); defaults to the bold name.
    champion_display = champion_display or f"**{champion}**"
    kills = int(participant.get("kills", 0) or 0)
    deaths = int(participant.get("deaths", 0) or 0)
    assists = int(participant.get("assists", 0) or 0)
    player_damage = int(participant.get("totalDamageDealtToChampions", 0) or 0)
    healing = int(participant.get("totalHeal", 0) or 0)
    shielded = int(participant.get("totalDamageShieldedOnTeammates", 0) or 0)
    damage_taken = int(participant.get("totalDamageTaken", 0) or 0)

    if queue_id in ARENA_QUEUE_IDS:
        placement = get_arena_placement(participant)
        placement_label = f"#{placement}" if placement is not None else "Unknown"
        result_emoji = "\U0001F947" if placement == 1 else "\U0001F3DF\uFE0F"
        subteam_id = get_arena_subteam_id(participant)
        team_label = f"Team {subteam_id}" if subteam_id is not None else "Team ?"
        gold_earned = int(participant.get("goldEarned", 0) or 0)
        damage_share = get_arena_team_damage_percentage(participant)
        share_text = f" ({damage_share:.1f}% of team)" if damage_share is not None else ""
        lines = [
            f"{result_emoji} **{lol_name}** \u00B7 {champion_display} \u00B7 {team_label} \u00B7 **Place {placement_label}**",
            f"**{kills}/{deaths}/{assists}** KDA \u00B7 Gold **{gold_earned:,}**",
            f"Damage **{player_damage:,}**{share_text} \u00B7 Taken **{damage_taken:,}**",
            f"Healing **{healing:,}** \u00B7 Shielded **{shielded:,}**",
        ]
        augments = get_arena_augments(participant, augment_names=augment_names)
        items = get_participant_items(participant, item_names=item_names)
        skillshots_hit, skillshots_dodged = get_arena_skillshot_stats(participant)
        if skillshots_hit or skillshots_dodged:
            lines.append(f"Skillshots hit **{skillshots_hit}** \u00B7 Dodged **{skillshots_dodged}**")
        if augments:
            lines.append(f"Augments \u00B7 {', '.join(augments)}")
        if items:
            lines.append(f"Items \u00B7 {', '.join(items)}")
        return "\n".join(lines)

    won = bool(participant.get("win"))
    # The card edge shows a shared win/loss; only mixed games name each player's result.
    result_text = f" \u00B7 **{'Win' if won else 'Loss'}**" if show_result else ""
    role_name = format_recap_role(participant)
    cs = int(participant.get("totalMinionsKilled", 0) or 0) + int(participant.get("neutralMinionsKilled", 0) or 0)
    minutes = max(1.0, float(match_duration_seconds) / 60.0)
    cs_per_min = cs / minutes
    objective_damage = int(participant.get("damageDealtToObjectives", 0) or 0)
    vision_score = int(participant.get("visionScore", 0) or 0)
    # The first line is the player's heading: LP is appended to it later (recap_lp).
    return (
        f"**{lol_name}** \u00B7 {champion_display} {role_name}{result_text}\n"
        f"**{kills}/{deaths}/{assists}** KDA \u00B7 **{cs_per_min:.1f}** CS/min \u00B7 **{vision_score}** Vision\n"
        f"Damage **{player_damage:,}** \u00B7 Objectives **{objective_damage:,}**\n"
        f"Taken **{damage_taken:,}** \u00B7 Healing **{healing:,}**"
    )


def format_streak_callout(riot_id, streak_count, is_win_streak):
    name = riot_id.split("#", 1)[0]
    if is_win_streak:
        if streak_count >= 10:
            return (
                f"\U0001F409 **FINAL BOSS UNLOCKED** `{name}` has won `{streak_count}` ranked games in a row.\n"
                "Enemy teams are dodging on sight. The lobby fears you."
            )
        if streak_count >= 8:
            return (
                f"\U0001F451 **LEGENDARY** `{name}` is on a `{streak_count}`-game ranked win streak!\n"
                "This is not a drill. Someone call Riot!"
            )
        if streak_count >= 5:
            return (
                f"\U0001F525 **Heater Alert** `{name}` is on a `{streak_count}`-game ranked win streak.\n"
                "Queue confidence is at dangerous levels."
            )
        return (
            f"\u2728 **Momentum** `{name}` is now `{streak_count}` wins in a row in ranked.\n"
            "Keep the streak alive."
        )
    if streak_count >= 13:
        return (
            f"\u2623\uFE0F **BIOHAZARD IN THE LOBBY** `{name}` just locked in `{streak_count}` ranked losses in a row.\n"
            "Queuing up again is considered domestic terrorism. Step away from the PC."
        )
    if streak_count >= 10:
        return (
            f"\U0001F5D1\uFE0F **UNINSTALL PROTOCOL** `{name}` is officially on a `{streak_count}`-game ranked loss streak.\n"
            "You are converting expensive electricity into pure defeat. Delete the game and apologize to your router."
        )
    if streak_count >= 8:
        return (
            f"\U0001F6D1 **FULL TILT** `{name}` is on a `{streak_count}`-game ranked loss streak.\n"
            "Log off. Touch grass. This is a cry for help."
        )
    if streak_count >= 5:
        return (
            f"\U0001F6A8 **Tilt Watch** `{name}` is on a `{streak_count}`-game ranked loss streak.\n"
            "Step away from queue before this becomes history."
        )
    return (
        f"\U0001F480 **Cold Streak** `{name}` just hit `{streak_count}` ranked losses in a row.\n"
        "Time for a reset and a better draft."
    )
