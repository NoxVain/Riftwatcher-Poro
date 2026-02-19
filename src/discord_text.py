import uuid


def create_request_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def report_signature(text):
    lines = [line for line in text.splitlines() if not line.startswith("_Last updated:")]
    return "\n".join(lines)


def match_recap_state_key(riot_id):
    return f"last_announced_match_id::{riot_id.casefold()}"


def format_recap_queue_name(queue_id):
    if queue_id == 420:
        return "\U0001F3C6 Ranked Solo/Duo"
    if queue_id == 440:
        return "\U0001F3C6 Ranked Flex"
    return f"\U0001F3AF Queue {queue_id}"


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


def format_recap_player_line(riot_id, participant, match_duration_seconds):
    lol_name = riot_id.split("#", 1)[0]
    won = bool(participant.get("win"))
    result_label = "Win" if won else "Loss"
    result_emoji = "\u2705" if won else "\u274C"
    champion = participant.get("championName", "Unknown")
    role_name = format_recap_role(participant)
    kills = int(participant.get("kills", 0) or 0)
    deaths = int(participant.get("deaths", 0) or 0)
    assists = int(participant.get("assists", 0) or 0)
    cs = int(participant.get("totalMinionsKilled", 0) or 0) + int(participant.get("neutralMinionsKilled", 0) or 0)
    minutes = max(1.0, float(match_duration_seconds) / 60.0)
    cs_per_min = cs / minutes
    player_damage = int(participant.get("totalDamageDealtToChampions", 0) or 0)
    objective_damage = int(participant.get("damageDealtToObjectives", 0) or 0)
    healing = int(participant.get("totalHeal", 0) or 0)
    damage_taken = int(participant.get("totalDamageTaken", 0) or 0)
    vision_score = int(participant.get("visionScore", 0) or 0)
    return (
        f"{result_emoji} **{lol_name}** | `{role_name}` • **{champion}** ({result_label})\n"
        f"   \u2694\uFE0F `K/D/A {kills}/{deaths}/{assists}`  \U0001F33E `CS/min {cs_per_min:.1f}`\n"
        f"   \U0001F4A5 `Damage {player_damage:,}`  \U0001F3F0 `Objectives {objective_damage:,}`  \U0001F6E1\uFE0F `Taken {damage_taken:,}`\n"
        f"   \u2764\uFE0F `Healing {healing:,}`  \U0001F441\uFE0F `Vision {vision_score}`"
    )
