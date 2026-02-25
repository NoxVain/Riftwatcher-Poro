import asyncio
import time
from datetime import datetime

from src.report_logic import (
    GAMER_SCORE_PERF_WEIGHT,
    GAMER_SCORE_WIN_WEIGHT,
    compute_gamer_score,
    compute_perf_percentile,
    format_mode_line,
    get_mode_totals,
    rank_sort_key,
    wilson_lower_bound,
)
from src.riot_api import get_lol_name


LEADERBOARD_BADGES = (
    ("cs_per_min", "\U0001F33E"),
    ("objective_damage", "\U0001F3F0"),
    ("player_damage", "\U0001F4A5"),
    ("healing", "\u2764\uFE0F"),
    ("damage_taken", "\U0001F6E1\uFE0F"),
    ("kills", "\U0001F5E1\uFE0F"),
    ("deaths", "\u2620\uFE0F"),
    ("vision_score", "\U0001F441\uFE0F"),
)


def rows_to_ranked_results(rows, tracked_friends, baselines=None):
    tracked_lookup = {friend.casefold() for friend in tracked_friends}
    ranked_results = []
    for row in rows:
        riot_id = row["riot_id"]
        if riot_id.casefold() not in tracked_lookup:
            continue
        mode_records = {
            "solo_duo": {"wins": row["solo_wins"], "losses": row["solo_losses"]},
            "flex": {"wins": row["flex_wins"], "losses": row["flex_losses"]},
        }
        performance_totals = {
            "cs_total": int(row["cs_total"] or 0),
            "minutes_total": float(row["minutes_total"] or 0.0),
            "objective_damage": int(row["objective_damage"] or 0),
            "player_damage": int(row["player_damage"] or 0),
            "healing": int(row["healing"] or 0),
            "damage_taken": int(row["damage_taken"] or 0),
            "kills": int(row["kills"] or 0),
            "deaths": int(row["deaths"] or 0),
            "vision_score": int(row["vision_score"] or 0),
        }
        wins, losses = get_mode_totals(mode_records)
        total = wins + losses
        if total == 0:
            continue
        win_rate = (wins / total) * 100
        primary_role = str(row.get("primary_role") or "").upper() or None
        gamer_score = compute_gamer_score(wins, losses, performance_totals, primary_role, baselines)
        ranked_results.append(
            (get_lol_name(riot_id), mode_records, wins, losses, win_rate, performance_totals, gamer_score)
        )
    ranked_results.sort(key=rank_sort_key)
    return ranked_results


def with_derived_performance(performance_totals):
    data = dict(performance_totals)
    minutes = float(data.get("minutes_total", 0.0) or 0.0)
    cs_total = int(data.get("cs_total", 0) or 0)
    data["cs_per_min"] = (cs_total / minutes) if minutes > 0 else 0.0
    return data


def get_leader_badges_by_player(ranked_results):
    if not ranked_results:
        return {}

    metrics = []
    for entry in ranked_results:
        performance = with_derived_performance(entry[5])
        metrics.append((entry[0], performance))

    leaders = {name: [] for name, _ in metrics}
    for metric_key, badge in LEADERBOARD_BADGES:
        values = [float(perf.get(metric_key, 0.0) or 0.0) for _, perf in metrics]
        if not values:
            continue
        target = max(values)
        if target <= 0:
            continue
        for name, perf in metrics:
            value = float(perf.get(metric_key, 0.0) or 0.0)
            if abs(value - target) < 1e-9:
                leaders[name].append(badge)
    return leaders


def _append_mode_line_if_games(report_lines, label, wins, losses):
    if wins + losses == 0:
        return
    report_lines.append(format_mode_line(label, wins, losses))


def format_report_from_results(
    service,
    ranked_results,
    error_results,
    report_start,
    *,
    header_title="DAILY",
    empty_line_1="Looks like everyone has a life today.",
    empty_line_2="We will keep you up to date if anyone crawls back into the hole.",
):
    report_lines = [f"\u2728------ **LEAGUE MOOD ({header_title})** ------\u2728", ""]
    updated_at = datetime.now(tz=service.report_timezone).strftime("%d.%m.%Y %H:%M")
    if not ranked_results and not error_results:
        report_lines.append(empty_line_1)
        report_lines.append(empty_line_2)
        report_lines.append("")
        report_lines.append("\u2728--------------------------------------------\u2728")
        report_lines.append(f"_Last updated: {updated_at}_")
        total_elapsed_ms = int((time.perf_counter() - report_start) * 1000)
        service.log(
            f"[mood] Report complete: players={len(service.friends)} "
            "ranked=0 hidden_no_matches=all errors=0 "
            f"elapsed={total_elapsed_ms}ms"
        )
        return "\n".join(report_lines)

    badges_by_player = get_leader_badges_by_player(ranked_results)
    for index, (lol_name, mode_records, wins, losses, win_rate, _performance, *rest) in enumerate(ranked_results):
        wilson_score = wilson_lower_bound(wins, losses)
        gamer_score = rest[0] if rest else wilson_score * 100
        if wins + losses > 0 and wilson_score <= 0:
            mood_emoji = "\U0001F480"
        elif wilson_score >= 0.75:
            mood_emoji = "\U0001F601"
        elif wilson_score >= 0.60:
            mood_emoji = "\U0001F60A"
        elif wilson_score >= 0.50:
            mood_emoji = "\U0001F642"
        elif wilson_score >= 0.40:
            mood_emoji = "\U0001F610"
        elif wilson_score >= 0.30:
            mood_emoji = "\U0001F615"
        elif wilson_score >= 0.20:
            mood_emoji = "\U0001F61E"
        else:
            mood_emoji = "\U0001F62D"

        display_emoji = "\u2B50" if index == 0 else mood_emoji
        badges = "".join(badges_by_player.get(lol_name, []))
        badges_suffix = f"  {badges}" if badges else ""
        report_lines.append(
            f"{display_emoji}  **{lol_name}**  |  Gamer Score: **{gamer_score:.1f}**{badges_suffix}"
        )
        _append_mode_line_if_games(
            report_lines,
            "Ranked Solo/Duo",
            mode_records["solo_duo"]["wins"],
            mode_records["solo_duo"]["losses"],
        )
        _append_mode_line_if_games(
            report_lines,
            "Ranked Flex",
            mode_records["flex"]["wins"],
            mode_records["flex"]["losses"],
        )
        report_lines.append(f"   Total: `{wins}W-{losses}L` - **{win_rate:.1f}%**")
        report_lines.append("")

    for lol_name, error_text in sorted(error_results, key=lambda row: row[0].lower()):
        report_lines.append(f"\u26AB  **{lol_name}**")
        report_lines.append(f"   Riot error: `{error_text}`")
        report_lines.append("")

    report_lines.append("\u2728--------------------------------------------\u2728")
    report_lines.append(f"_Last updated: {updated_at}_")
    total_elapsed_ms = int((time.perf_counter() - report_start) * 1000)
    service.log(
        f"[mood] Report complete: players={len(service.friends)} "
        f"ranked={len(ranked_results)} hidden_no_matches={len(service.friends) - len(ranked_results) - len(error_results)} "
        f"errors={len(error_results)} elapsed={total_elapsed_ms}ms"
    )
    report_text = "\n".join(report_lines)
    if len(report_text) <= 2000:
        return report_text

    compact_lines = [f"\u2728------ **LEAGUE MOOD ({header_title})** ------\u2728", ""]
    for index, (lol_name, _mode_records, wins, losses, win_rate, _performance, *_rest) in enumerate(ranked_results):
        display_emoji = "\u2B50" if index == 0 else "\U0001F642"
        badges = "".join(badges_by_player.get(lol_name, []))
        badges_suffix = f" {badges}" if badges else ""
        compact_lines.append(
            f"{display_emoji}  **{lol_name}**{badges_suffix}  **`{wins}W-{losses}L` - {win_rate:.1f}%**"
        )

    if error_results:
        compact_lines.append("")
        error_names = ", ".join(name for name, _ in error_results[:6])
        more = "" if len(error_results) <= 6 else f" (+{len(error_results) - 6} more)"
        compact_lines.append(f"\u26AB Riot errors for {len(error_results)} players: {error_names}{more}")

    compact_lines.append("")
    compact_lines.append("\u2728--------------------------------------------\u2728")
    compact_lines.append(f"_Last updated: {updated_at}_")
    compact_text = "\n".join(compact_lines)
    if len(compact_text) > 2000:
        compact_text = compact_text[:1950] + "\n..."
    return compact_text


async def build_score_breakdown_report(service):
    """Return a human-readable breakdown of how each player's Gamer Score is computed today."""
    from src.services.baselines import ensure_role_baselines

    await ensure_role_baselines(service)
    cycle_key = service.get_cycle_key()

    lines = [
        "\U0001F3AE **GAMER SCORE** \u2014 how it\u2019s calculated",
        "",
        "**Score = win-rate confidence \u00d7 65% + role performance \u00d7 35%**",
        "Win confidence: Wilson lower bound \u2014 low game counts are penalised.",
        "Performance: your per-min stats vs same-role players drawn from stored match history.",
        "",
    ]

    if not service._role_baselines:
        lines.append("_Role baselines not yet built \u2014 scores currently reflect win rate only._")
        lines.append("_Run `!Daily` once to trigger a build._")
        lines.append("")

    if not service.db_enabled:
        return "\n".join(lines + ["_DB not enabled \u2014 per-player breakdown unavailable._"])

    stored_rows = await asyncio.to_thread(service.db_load_latest_stats, cycle_key)
    row_by_key = {str(row["riot_id"]).casefold(): row for row in (stored_rows or [])}

    for riot_id in sorted(service.friends, key=str.casefold):
        lol_name = get_lol_name(riot_id)
        row = row_by_key.get(riot_id.casefold())
        if row is None:
            lines.append(f"\u26AB  **{lol_name}** \u2014 no data today")
            continue

        mode_records = {
            "solo_duo": {"wins": int(row["solo_wins"] or 0), "losses": int(row["solo_losses"] or 0)},
            "flex": {"wins": int(row["flex_wins"] or 0), "losses": int(row["flex_losses"] or 0)},
        }
        wins, losses = get_mode_totals(mode_records)
        if wins + losses == 0:
            lines.append(f"\u26AB  **{lol_name}** \u2014 no ranked games today")
            continue

        performance_totals = {
            "cs_total": int(row["cs_total"] or 0),
            "minutes_total": float(row["minutes_total"] or 0.0),
            "objective_damage": int(row["objective_damage"] or 0),
            "player_damage": int(row["player_damage"] or 0),
            "healing": int(row["healing"] or 0),
            "damage_taken": int(row["damage_taken"] or 0),
            "kills": int(row["kills"] or 0),
            "deaths": int(row["deaths"] or 0),
            "vision_score": int(row["vision_score"] or 0),
        }
        primary_role = str(row.get("primary_role") or "").upper() or None
        win_rate = (wins / (wins + losses)) * 100
        wilson = wilson_lower_bound(wins, losses)
        win_pts = wilson * GAMER_SCORE_WIN_WEIGHT * 100
        gamer_score = compute_gamer_score(wins, losses, performance_totals, primary_role, service._role_baselines)

        role_label = primary_role or "?"
        minutes = float(performance_totals["minutes_total"])
        if service._role_baselines and primary_role and minutes > 0:
            perf_per_min = {
                "cs_per_min": performance_totals["cs_total"] / minutes,
                "player_damage_per_min": performance_totals["player_damage"] / minutes,
                "objective_damage_per_min": performance_totals["objective_damage"] / minutes,
                "healing_per_min": performance_totals["healing"] / minutes,
                "damage_taken_per_min": performance_totals["damage_taken"] / minutes,
                "kills_per_min": performance_totals["kills"] / minutes,
                "deaths_per_min": performance_totals["deaths"] / minutes,
                "vision_per_min": performance_totals["vision_score"] / minutes,
            }
            perf = compute_perf_percentile(perf_per_min, primary_role, service._role_baselines)
            perf_pts = perf * GAMER_SCORE_PERF_WEIGHT * 100
            perf_str = f"perf vs {role_label}: `{int(perf * 100)}th pct` \u2192 `{perf_pts:.1f}`"
        else:
            reason = "no baseline" if not service._role_baselines else "role unknown"
            perf_str = f"perf: `N/A ({reason})`"

        lines.append(
            f"**{lol_name}**  `{role_label}`  \u2192  Score: **{gamer_score:.1f}**\n"
            f"   `{wins}W\u2011{losses}L` ({win_rate:.1f}% wr) \u2192 win pts: `{win_pts:.1f}` | {perf_str}"
        )

    return "\n".join(lines)
