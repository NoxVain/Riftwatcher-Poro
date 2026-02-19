RANKED_QUEUE_LABELS = {
    "RANKED_SOLO_5x5": "Ranked Solo/Duo",
    "RANKED_FLEX_SR": "Ranked Flex",
}

TIER_ORDER = {
    "IRON": 0,
    "BRONZE": 1,
    "SILVER": 2,
    "GOLD": 3,
    "PLATINUM": 4,
    "EMERALD": 5,
    "DIAMOND": 6,
    "MASTER": 7,
    "GRANDMASTER": 8,
    "CHALLENGER": 9,
}

DIVISION_ORDER = {"IV": 0, "III": 1, "II": 2, "I": 3}
APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}


def normalize_queue_type(queue_type):
    queue = str(queue_type or "").strip().upper()
    if queue in RANKED_QUEUE_LABELS:
        return queue
    return None


def rank_level(entry):
    if not entry:
        return None
    tier = str(entry.get("tier", "")).strip().upper()
    if tier not in TIER_ORDER:
        return None
    tier_score = TIER_ORDER[tier]
    if tier in APEX_TIERS:
        return (tier_score, 4)
    division = str(entry.get("rank_division", entry.get("rank", ""))).strip().upper()
    return (tier_score, DIVISION_ORDER.get(division, 0))


def compare_rank_direction(previous_entry, current_entry):
    previous_level = rank_level(previous_entry)
    current_level = rank_level(current_entry)
    if previous_level is None and current_level is None:
        return 0
    if previous_level is None and current_level is not None:
        return 1
    if previous_level is not None and current_level is None:
        return -1
    if current_level > previous_level:
        return 1
    if current_level < previous_level:
        return -1
    return 0


def format_rank(entry):
    if not entry:
        return "Unranked"
    tier = str(entry.get("tier", "")).strip().upper()
    if not tier:
        return "Unranked"
    if tier in APEX_TIERS:
        return f"{tier.title()}"
    division = str(entry.get("rank_division", entry.get("rank", ""))).strip().upper() or "IV"
    return f"{tier.title()} {division}"


def format_rank_change_message(riot_id, queue_type, previous_entry, current_entry):
    name = riot_id.split("#", 1)[0]
    queue_label = RANKED_QUEUE_LABELS.get(queue_type, queue_type)
    direction = compare_rank_direction(previous_entry, current_entry)
    old_rank = format_rank(previous_entry)
    new_rank = format_rank(current_entry)
    if direction > 0:
        return (
            f"\U0001F389 **Rank Up!** `{name}` climbed in **{queue_label}**\n"
            f"`{old_rank}` \u27A1\uFE0F `{new_rank}`\n"
            "Huge W. Keep the momentum going."
        )
    if direction < 0:
        return (
            f"\U0001F4A9 **Rank Down.** `{name}` slipped in **{queue_label}**\n"
            f"`{old_rank}` \u27A1\uFE0F `{new_rank}`\n"
            "Absolute int performance. Queue again and fix it."
        )
    return None
