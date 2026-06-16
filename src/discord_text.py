import uuid


def create_request_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def report_signature(text):
    lines = [line for line in text.splitlines() if not line.startswith("_Last updated:")]
    return "\n".join(lines)


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
    if queue_id == 1750:
        return "\U0001F3DF\uFE0F Arena 3x6"
    if queue_id in ARENA_QUEUE_IDS:
        return "\U0001F3DF\uFE0F Arena"
    return f"\U0001F3AF Queue {queue_id}"


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


def format_recap_player_line(
    riot_id,
    participant,
    match_duration_seconds,
    queue_id=None,
    augment_names=None,
    item_names=None,
):
    lol_name = riot_id.split("#", 1)[0]
    champion = participant.get("championName", "Unknown")
    kills = int(participant.get("kills", 0) or 0)
    deaths = int(participant.get("deaths", 0) or 0)
    assists = int(participant.get("assists", 0) or 0)
    player_damage = int(participant.get("totalDamageDealtToChampions", 0) or 0)
    healing = int(participant.get("totalHeal", 0) or 0)
    damage_taken = int(participant.get("totalDamageTaken", 0) or 0)

    if queue_id in ARENA_QUEUE_IDS:
        placement = get_arena_placement(participant)
        placement_label = f"#{placement}" if placement is not None else "Unknown"
        result_emoji = "\U0001F947" if placement == 1 else "\U0001F3DF\uFE0F"
        subteam_id = get_arena_subteam_id(participant)
        team_label = f"Team {subteam_id}" if subteam_id is not None else "Team ?"
        gold_earned = int(participant.get("goldEarned", 0) or 0)
        lines = [
            f"{result_emoji} **{lol_name}** | `{team_label}` \u2022 **{champion}** - **Place {placement_label}**\n"
            f"   \u2694\uFE0F `K/D/A {kills}/{deaths}/{assists}`  \U0001F4A5 `Damage {player_damage:,}`\n"
            f"   \U0001F6E1\uFE0F `Taken {damage_taken:,}`  \u2764\uFE0F `Healing {healing:,}`  \U0001FA99 `Gold {gold_earned:,}`"
        ]
        augments = get_arena_augments(participant, augment_names=augment_names)
        items = get_participant_items(participant, item_names=item_names)
        skillshots_hit, skillshots_dodged = get_arena_skillshot_stats(participant)
        if skillshots_hit or skillshots_dodged:
            lines.append(f"   \U0001F3AF `Skillshots hit {skillshots_hit}`  \U0001F483 `Dodged {skillshots_dodged}`")
        if augments:
            lines.append(f"   \U0001F52E `Augments {', '.join(augments)}`")
        if items:
            lines.append(f"   \U0001F6D2 `Items {', '.join(items)}`")
        return "\n".join(lines)

    won = bool(participant.get("win"))
    result_label = "Win" if won else "Loss"
    result_emoji = "\u2705" if won else "\u274C"
    role_name = format_recap_role(participant)
    cs = int(participant.get("totalMinionsKilled", 0) or 0) + int(participant.get("neutralMinionsKilled", 0) or 0)
    minutes = max(1.0, float(match_duration_seconds) / 60.0)
    cs_per_min = cs / minutes
    objective_damage = int(participant.get("damageDealtToObjectives", 0) or 0)
    vision_score = int(participant.get("visionScore", 0) or 0)
    return (
        f"{result_emoji} **{lol_name}** | `{role_name}` • **{champion}** ({result_label})\n"
        f"   \u2694\uFE0F `K/D/A {kills}/{deaths}/{assists}`  \U0001F33E `CS/min {cs_per_min:.1f}`\n"
        f"   \U0001F4A5 `Damage {player_damage:,}`  \U0001F3F0 `Objectives {objective_damage:,}`  \U0001F6E1\uFE0F `Taken {damage_taken:,}`\n"
        f"   \u2764\uFE0F `Healing {healing:,}`  \U0001F441\uFE0F `Vision {vision_score}`"
    )


def format_streak_callout(riot_id, streak_count, is_win_streak):
    name = riot_id.split("#", 1)[0]
    if is_win_streak:
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
