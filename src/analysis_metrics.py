"""Descriptive playstyle and guarded comparisons from already stored match stats."""

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

import discord

ROLE_NAMES = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
ALL_ROLES = frozenset(ROLE_NAMES)
FARM_ROLES = ALL_ROLES - {"UTILITY"}
MIN_MEASURED = 10


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    gap: float
    topic: str
    action: str
    continue_action: str
    mode: str = "raw"
    scale: float = 1
    higher_good: bool = True
    roles: frozenset = ALL_ROLES
    min_seconds: int = 0
    unit: str = ""


METRICS = (
    Metric("cs", "CS/min", 0.7, "farm", "Review missed waves, camps and recalls",
           "Keep prioritizing farm between fights", mode="minute", roles=FARM_ROLES),
    Metric("lane_cs_10", "lane CS at 10", 8, "farm", "Review early last-hitting and recalls",
           "Keep your early-wave routine", roles=FARM_ROLES - {"JUNGLE"}, min_seconds=600),
    Metric("deaths", "deaths/10 min", 0.5, "survival", "Review your recurring death situations",
           "Keep protecting your life around fights", mode="minute", scale=10, higher_good=False),
    Metric("kill_participation", "kill participation %", 10, "fights", "Review missed fights and rotations",
           "Keep looking for team-fight rotations", scale=100, unit="%"),
    Metric("damage_share", "team damage %", 6, "damage", "Review damage uptime and target access",
           "Keep looking for safe damage uptime", scale=100, roles=FARM_ROLES, unit="%"),
    Metric("vision_score", "vision/min", 0.2, "vision", "Review vision setup before objectives",
           "Keep investing in useful vision", mode="minute"),
    Metric("wards_killed", "wards cleared/10 min", 0.5, "vision", "Review sweeper use and safe vision denial",
           "Keep denying enemy vision safely", mode="minute", scale=10),
    Metric("time_dead_seconds", "time dead %", 4, "survival", "Review costly deaths and reset timing",
           "Keep protecting your time on the map", mode="duration", scale=100, higher_good=False, unit="%"),
    Metric("buffs_stolen", "buffs stolen/game", 0.4, "invades", "Review safe invade windows and lane priority",
           "Keep choosing safe invade windows", roles=frozenset({"JUNGLE"})),
    Metric("enemy_jungle_camps", "enemy jungle minions/10 min", 2, "invades",
           "Review safe invade windows and lane priority", "Keep taking enemy farm when it is safe",
           mode="minute", scale=10, roles=frozenset({"JUNGLE"})),
    Metric("ally_saves", "ally saves/game", 0.5, "peel", "Review positioning and defensive cooldowns",
           "Keep tracking defensive cooldowns for allies", roles=frozenset({"UTILITY"})),
)
BY_KEY = {metric.key: metric for metric in METRICS}


def _measure(game: dict, metric: Metric):
    value = game.get(metric.key)
    seconds = game.get("duration_seconds") or 0
    if value is None or not math.isfinite(value) or value < 0 or seconds < metric.min_seconds:
        return None
    if metric.unit == "%" and metric.mode == "raw" and value > 1:
        return None
    if metric.mode == "duration" and value > seconds:
        return None
    if metric.mode != "raw":
        if seconds <= 0:
            return None
        value = value / seconds * (60 if metric.mode == "minute" else 1)
    return value * metric.scale


def _summary(games: list, key: str) -> str:
    metric = BY_KEY[key]
    measured = [(game, value) for game in games if (value := _measure(game, metric)) is not None]
    if not measured:
        return "N/A"
    if metric.mode == "raw":
        value = statistics.mean(value for _game, value in measured)
    else:
        # Whole-period rates weight by duration rather than treating a short game as a long one.
        seconds = sum(game["duration_seconds"] for game, _value in measured)
        value = sum(value * game["duration_seconds"] for game, value in measured) / seconds
    coverage = f" ({len(measured)}/{len(games)})" if len(measured) != len(games) else ""
    return f"{value:.1f}{metric.unit}{coverage}"


def playstyle_summary(games: list) -> str:
    """Main-role facts for the whole window; no unsupported playstyle labels."""
    if not games:
        return ""
    roles = Counter(game.get("team_position") for game in games if game.get("team_position") in ALL_ROLES)
    # Match the existing analysis rules: ties keep the first role in game order.
    role = max(roles, key=roles.get) if roles else None
    selected = [game for game in games if game.get("team_position") == role] if role else games
    champions = Counter(game.get("champion_name") for game in selected if game.get("champion_name"))
    picks = []
    for champion, count in sorted(champions.items(), key=lambda entry: (-entry[1], entry[0]))[:2]:
        wins = sum(bool(game["win"]) for game in selected if game.get("champion_name") == champion)
        name = discord.utils.escape_markdown(champion)
        picks.append(f"{name} {wins}W-{count - wins}L")
    label = ROLE_NAMES.get(role, "Unknown role")
    lines = [f"**How you play · {label} · {len(selected)}/{len(games)} games**"]
    if picks:
        noun = "champion" if len(champions) == 1 else "champions"
        lines.append(f"Most played: {' · '.join(picks)} · {len(champions)} {noun}")
    if role in FARM_ROLES:
        farm = f"Farm: **{_summary(selected, 'cs')} CS/min**"
        if role != "JUNGLE":
            farm += f" · lane CS at 10 **{_summary(selected, 'lane_cs_10')}**"
        else:
            farm += f" · enemy jungle minions/10 min **{_summary(selected, 'enemy_jungle_camps')}**"
            farm += f" · buffs stolen/game **{_summary(selected, 'buffs_stolen')}**"
        lines.append(farm)
    lines.extend((
        f"Fights: KP **{_summary(selected, 'kill_participation')}** · team damage **{_summary(selected, 'damage_share')}**",
        f"Map: **{_summary(selected, 'vision_score')} vision/min** · wards cleared/10 min **{_summary(selected, 'wards_killed')}**",
        f"Survival: **{_summary(selected, 'deaths')} deaths/10 min** · time dead **{_summary(selected, 'time_dead_seconds')}**",
    ))
    if role == "UTILITY":
        lines.append(f"Peel: ally saves/game **{_summary(selected, 'ally_saves')}**")
    if any("(" in line for line in lines):
        lines.append("-# Parentheses show measured games; missing stats stay N/A.")
    return "\n".join(lines)


def recent_summary(games: list) -> str:
    """One line for the card header: the latest 20 games of the queue and window shown."""
    recent = sorted(games, key=lambda game: game["game_end_at"], reverse=True)[:20]
    if not recent:
        return ""
    wins = sum(bool(game["win"]) for game in recent)
    deaths = sum(game["deaths"] for game in recent)
    kda = sum(game["kills"] + game["assists"] for game in recent) / max(1, deaths)
    return (f"Last {len(recent)} games: {wins}W-{len(recent) - wins}L ({100 * wins / len(recent):.0f}%) · "
            f"{_summary(recent, 'cs')} CS/min · KDA {kda:.2f} · KP {_summary(recent, 'kill_participation')}")


def _comparison(first: list, second: list, metric: Metric):
    a = [value for game in first if (value := _measure(game, metric)) is not None]
    b = [value for game in second if (value := _measure(game, metric)) is not None]
    if min(len(a), len(b)) < MIN_MEASURED:
        return None
    new, old = statistics.mean(a), statistics.mean(b)
    noise = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    gap = new - old
    if abs(gap) < max(metric.gap, abs(old) * 0.15, 3 * noise):
        return None
    return new, old, len(a), len(b), gap


def metric_findings(games: list) -> tuple[list, list]:
    """Return (text, score, topic) triples: recent vs earlier games on the same champion and role.

    Wins are never compared with losses: losing games come with more deaths and less
    farm by themselves, so such a gap mostly restates the result, not a habit to fix.
    """
    strengths, tips = [], []
    groups = defaultdict(list)
    for game in games:
        if game.get("champion_name") and game.get("team_position") in ALL_ROLES:
            groups[game["champion_name"], game["team_position"]].append(game)
    for (champion, role), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda game: game["game_end_at"], reverse=True)
        split = min(20, len(ordered) // 2)
        context = f"**{discord.utils.escape_markdown(champion)}** {ROLE_NAMES[role]}"
        for metric in METRICS:
            if role not in metric.roles:
                continue
            trend = _comparison(ordered[:split], ordered[split:split + 50], metric)
            if not trend:
                continue
            new, old, n, m, gap = trend
            text = f"{context}: {metric.label} {old:.1f} → {new:.1f} (earlier {m} vs recent {n} games)"
            improved = (gap > 0) == metric.higher_good
            text += f". {metric.continue_action}" if improved else f". {metric.action}"
            score = 20 * abs(gap) / metric.gap * math.sqrt(min(n, m))
            (strengths if improved else tips).append((text, score, metric.topic))
    return strengths, tips
