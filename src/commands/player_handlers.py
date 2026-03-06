import asyncio

import requests

from src.constants import ADD_COMMAND, DEBUG_PLAYER_COMMAND, PROFILE_COMMAND, REMOVE_COMMAND, STREAK_COMMAND
from src.discord_recap_worker import get_ranked_streak_info
from src.discord_text import (
    format_streak_callout,
    parse_streak_tts_enabled,
    streak_callout_state_key,
    streak_tts_enabled_state_key,
)
from src.report_logic import (
    compute_gamer_score,
    derive_primary_role,
    get_match_duration_seconds,
    get_mode_bucket,
    get_mode_totals,
    is_remake_match,
)


def _row_to_mode_records(row):
    if not row:
        return {"solo_duo": {"wins": 0, "losses": 0}, "flex": {"wins": 0, "losses": 0}}
    return {
        "solo_duo": {"wins": int(row.get("solo_wins", 0) or 0), "losses": int(row.get("solo_losses", 0) or 0)},
        "flex": {"wins": int(row.get("flex_wins", 0) or 0), "losses": int(row.get("flex_losses", 0) or 0)},
    }


def _row_to_performance_totals(row):
    if not row:
        return {
            "cs_total": 0,
            "minutes_total": 0.0,
            "objective_damage": 0,
            "player_damage": 0,
            "healing": 0,
            "damage_taken": 0,
            "kills": 0,
            "assists": 0,
            "deaths": 0,
            "vision_score": 0,
            "gold_earned": 0,
            "wards_placed": 0,
            "wards_killed": 0,
            "turret_takedowns": 0,
            "dragon_takedowns": 0,
            "baron_takedowns": 0,
            "double_kills": 0,
            "triple_kills": 0,
            "quadra_kills": 0,
            "penta_kills": 0,
            "kill_participation_num": 0,
            "kill_participation_den": 0,
        }
    return {
        "cs_total": int(row.get("cs_total", 0) or 0),
        "minutes_total": float(row.get("minutes_total", 0.0) or 0.0),
        "objective_damage": int(row.get("objective_damage", 0) or 0),
        "player_damage": int(row.get("player_damage", 0) or 0),
        "healing": int(row.get("healing", 0) or 0),
        "damage_taken": int(row.get("damage_taken", 0) or 0),
        "kills": int(row.get("kills", 0) or 0),
        "assists": int(row.get("assists", 0) or 0),
        "deaths": int(row.get("deaths", 0) or 0),
        "vision_score": int(row.get("vision_score", 0) or 0),
        "gold_earned": int(row.get("gold_earned", 0) or 0),
        "wards_placed": int(row.get("wards_placed", 0) or 0),
        "wards_killed": int(row.get("wards_killed", 0) or 0),
        "turret_takedowns": int(row.get("turret_takedowns", 0) or 0),
        "dragon_takedowns": int(row.get("dragon_takedowns", 0) or 0),
        "baron_takedowns": int(row.get("baron_takedowns", 0) or 0),
        "double_kills": int(row.get("double_kills", 0) or 0),
        "triple_kills": int(row.get("triple_kills", 0) or 0),
        "quadra_kills": int(row.get("quadra_kills", 0) or 0),
        "penta_kills": int(row.get("penta_kills", 0) or 0),
        "kill_participation_num": int(row.get("kill_participation_num", 0) or 0),
        "kill_participation_den": int(row.get("kill_participation_den", 0) or 0),
    }


async def _get_recent_ranked_kda(riot_client, puuid, recent_ids, max_matches=20):
    kills = 0
    deaths = 0
    assists = 0
    games = 0
    for match_id in recent_ids[: max(1, max_matches)]:
        match_info = await riot_client.fetch_match_info(match_id)
        if is_remake_match(match_info):
            continue
        queue_id = int(match_info.get("info", {}).get("queueId", -1))
        if get_mode_bucket(queue_id) is None:
            continue
        participant = riot_client.get_participant(match_info, puuid)
        if participant is None:
            continue
        kills += int(participant.get("kills", 0) or 0)
        deaths += int(participant.get("deaths", 0) or 0)
        assists += int(participant.get("assists", 0) or 0)
        games += 1
    kda = (kills + assists) / max(1, deaths)
    return games, kills, deaths, assists, kda


async def handle_player_commands(ctx):
    content = ctx.content
    content_lower = ctx.content_lower

    if content_lower == DEBUG_PLAYER_COMMAND.casefold():
        await ctx.message.channel.send(f"Usage: `{DEBUG_PLAYER_COMMAND} Name#Tag`")
        return True

    if content_lower.startswith(f"{DEBUG_PLAYER_COMMAND.casefold()} "):
        raw_riot_id = content[len(DEBUG_PLAYER_COMMAND):].strip()
        status_message = await ctx.message.channel.send(f"\u23F3 Building debug report for `{raw_riot_id}`...")
        try:
            report_text = await ctx.riot_client.build_debug_player_report(
                raw_riot_id,
                report_timezone_name=ctx.report_timezone_name,
                normalize_riot_id=ctx.normalize_riot_id,
            )
            if len(report_text) > 1900:
                report_text = report_text[:1900] + "\n...truncated..."
            await status_message.edit(content=f"```text\n{report_text}\n```")
        except ValueError as exc:
            await status_message.edit(content=f"Debug report failed: {exc}")
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Debug report failed: {exc}")
            ctx.log(f"[debug] Debug report failed: {exc}")
        return True

    if content_lower == STREAK_COMMAND.casefold():
        await ctx.message.channel.send(f"Usage: `{STREAK_COMMAND} Name#Tag`")
        return True

    if content_lower == PROFILE_COMMAND.casefold():
        await ctx.message.channel.send(f"Usage: `{PROFILE_COMMAND} Name#Tag`")
        return True

    if content_lower.startswith(f"{PROFILE_COMMAND.casefold()} "):
        raw_riot_id = content[len(PROFILE_COMMAND):].strip()
        try:
            riot_id = ctx.normalize_riot_id(raw_riot_id)
        except ValueError as exc:
            await ctx.message.channel.send(f"Profile lookup failed: {exc}")
            return True

        status_message = await ctx.message.channel.send(f"\u23F3 Building profile for `{riot_id}`...")
        try:
            puuid = await ctx.riot_client.fetch_puuid(riot_id)
            recent_ids = await ctx.riot_client.fetch_recent_match_ids(puuid, count=20, riot_id=riot_id)
            streak_count, is_win_streak = await get_ranked_streak_info(ctx.riot_client, puuid, recent_ids)
            recent_ranked_games, rk, rd, ra, kda = await _get_recent_ranked_kda(ctx.riot_client, puuid, recent_ids)

            today_row = None
            weekly_row = None
            if ctx.db_enabled:
                cycle_key = ctx.mood_service.get_cycle_key()
                today_rows = await asyncio.to_thread(ctx.mood_service.db_load_latest_stats, cycle_key)
                for row in today_rows:
                    if str(row.get("riot_id", "")).casefold() == riot_id.casefold():
                        today_row = row
                        break

                week_start, week_end_exclusive = ctx.mood_service.get_week_window()
                weekly_rows = await asyncio.to_thread(
                    ctx.mood_service.db_load_weekly_stats,
                    week_start.isoformat(),
                    week_end_exclusive.isoformat(),
                )
                for row in weekly_rows:
                    if str(row.get("riot_id", "")).casefold() == riot_id.casefold():
                        weekly_row = row
                        break
            else:
                mode_records, perf_totals = await ctx.riot_client.get_today_mode_records(riot_id)
                today_row = {
                    "riot_id": riot_id,
                    "solo_wins": mode_records["solo_duo"]["wins"],
                    "solo_losses": mode_records["solo_duo"]["losses"],
                    "flex_wins": mode_records["flex"]["wins"],
                    "flex_losses": mode_records["flex"]["losses"],
                    **perf_totals,
                }

            today_mode = _row_to_mode_records(today_row)
            today_perf = _row_to_performance_totals(today_row)
            today_wins, today_losses = get_mode_totals(today_mode)
            today_games = today_wins + today_losses
            today_wr = (today_wins / today_games * 100.0) if today_games > 0 else 0.0

            weekly_mode = _row_to_mode_records(weekly_row)
            week_wins, week_losses = get_mode_totals(weekly_mode)
            week_games = week_wins + week_losses
            week_wr = (week_wins / week_games * 100.0) if week_games > 0 else 0.0

            primary_role = str((today_row or {}).get("primary_role", "") or "").upper() or None
            if primary_role is None:
                primary_role = derive_primary_role(today_perf)
            if hasattr(ctx.mood_service, "_ensure_role_baselines"):
                try:
                    await ctx.mood_service._ensure_role_baselines()
                except Exception:
                    pass
            baselines = getattr(ctx.mood_service, "_role_baselines", None)
            gamer_score = compute_gamer_score(today_wins, today_losses, today_perf, primary_role, baselines)

            minutes = float(today_perf.get("minutes_total", 0.0) or 0.0)
            cs_per_min = (today_perf["cs_total"] / minutes) if minutes > 0 else 0.0
            dmg_per_min = (today_perf["player_damage"] / minutes) if minutes > 0 else 0.0
            vision_per_min = (today_perf["vision_score"] / minutes) if minutes > 0 else 0.0
            avg_duration = (minutes / recent_ranked_games) if recent_ranked_games > 0 else 0.0

            streak_text = "No active ranked streak (<3)"
            if streak_count >= 3 and is_win_streak is not None:
                streak_type = "W" if is_win_streak else "L"
                streak_text = f"{streak_type}{streak_count}"

            profile_text = (
                f"\U0001F464 **Profile: {riot_id}**\n"
                f"- Today: `{today_wins}W-{today_losses}L` ({today_wr:.1f}%) | Gamer Score: `{gamer_score:.1f}` | Role: `{primary_role or '?'}`\n"
                f"- Week: `{week_wins}W-{week_losses}L` ({week_wr:.1f}%)\n"
                f"- Streak: `{streak_text}`\n"
                f"- Recent ranked KDA (last {recent_ranked_games}): `{kda:.2f}` (`{rk}/{rd}/{ra}`)\n"
                f"- Per-min (today): `CS {cs_per_min:.2f}` | `DMG {dmg_per_min:.0f}` | `VIS {vision_per_min:.2f}`\n"
                f"- Avg ranked game length (recent): `{avg_duration:.1f} min`"
            )
            await status_message.edit(content=profile_text)
            ctx.log(f"[profile] Sent profile for {riot_id}.")
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Profile lookup failed for `{riot_id}`: {exc}")
            ctx.log(f"[profile] Lookup failed for {riot_id}: {exc}")
        return True

    if content_lower.startswith(f"{STREAK_COMMAND.casefold()} "):
        raw_riot_id = content[len(STREAK_COMMAND):].strip()
        try:
            riot_id = ctx.normalize_riot_id(raw_riot_id)
        except ValueError as exc:
            await ctx.message.channel.send(f"Streak lookup failed: {exc}")
            return True
        status_message = await ctx.message.channel.send(f"\u23F3 Looking up streak for `{riot_id}`...")
        try:
            puuid = await ctx.riot_client.fetch_puuid(riot_id)
            recent_ids = await ctx.riot_client.fetch_recent_match_ids(puuid, count=20, riot_id=riot_id)
            streak_count, is_win_streak = await get_ranked_streak_info(ctx.riot_client, puuid, recent_ids)
            if streak_count < 3 or is_win_streak is None:
                await status_message.edit(content=f"`{riot_id}` has no active ranked streak (fewer than 3 in a row).")
            else:
                callout = format_streak_callout(riot_id, streak_count, is_win_streak)
                await status_message.delete()
                tts_enabled = True
                if ctx.db_get_state is not None:
                    raw_tts = await asyncio.to_thread(ctx.db_get_state, streak_tts_enabled_state_key())
                    tts_enabled = parse_streak_tts_enabled(raw_tts, default=True)
                await ctx.message.channel.send(callout, tts=tts_enabled)
                if ctx.db_set_state is not None:
                    streak_token = f"{'W' if is_win_streak else 'L'}:{streak_count}"
                    await asyncio.to_thread(ctx.db_set_state, streak_callout_state_key(riot_id), streak_token)
                ctx.log(f"[streak] Manual streak callout posted for {riot_id} ({streak_count} {'wins' if is_win_streak else 'losses'}).")
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Streak lookup failed for `{riot_id}`: {exc}")
            ctx.log(f"[streak] Streak lookup failed for {riot_id}: {exc}")
        return True

    if content_lower == ADD_COMMAND.casefold():
        await ctx.message.channel.send(f"Usage: `{ADD_COMMAND} Name#Tag`")
        return True

    if content_lower == REMOVE_COMMAND.casefold():
        await ctx.message.channel.send(f"Usage: `{REMOVE_COMMAND} Name#Tag`")
        return True

    if content_lower.startswith(f"{ADD_COMMAND.casefold()} "):
        raw_riot_id = content[len(ADD_COMMAND):].strip()
        try:
            riot_id = ctx.normalize_riot_id(raw_riot_id)
        except ValueError as exc:
            await ctx.message.channel.send(f"Add failed: {exc}")
            return True

        if any(existing.casefold() == riot_id.casefold() for existing in ctx.friends):
            await ctx.message.channel.send(f"`{riot_id}` is already tracked.")
            return True

        status_message = await ctx.message.channel.send(f"\u23F3 Validating `{riot_id}` with Riot API...")
        try:
            await ctx.riot_client.fetch_puuid(riot_id)
        except (KeyError, requests.RequestException) as exc:
            await status_message.edit(content=f"Add failed for `{riot_id}`: {exc}")
            return True

        ctx.friends.append(riot_id)
        try:
            await asyncio.to_thread(ctx.db_upsert_player, riot_id, None)
        except Exception as exc:
            await status_message.edit(content=f"Added `{riot_id}`, but failed to persist to postgres: {exc}")
            return True

        await status_message.edit(
            content=(
                f"\u2705 Added `{riot_id}` and saved to postgres. "
                f"Total tracked players: {len(ctx.friends)}"
            )
        )
        ctx.mood_service.invalidate_report_cache()
        ctx.log(f"[add] Added player {riot_id}.")
        return True

    if content_lower.startswith(f"{REMOVE_COMMAND.casefold()} "):
        raw_riot_id = content[len(REMOVE_COMMAND):].strip()
        try:
            riot_id = ctx.normalize_riot_id(raw_riot_id)
        except ValueError as exc:
            await ctx.message.channel.send(f"Remove failed: {exc}")
            return True

        if not any(existing.casefold() == riot_id.casefold() for existing in ctx.friends):
            await ctx.message.channel.send(f"`{riot_id}` is not currently tracked.")
            return True

        status_message = await ctx.message.channel.send(f"\u23F3 Removing `{riot_id}` from tracked players...")
        try:
            if ctx.db_remove_player is not None:
                await asyncio.to_thread(ctx.db_remove_player, riot_id)
        except Exception as exc:
            await status_message.edit(content=f"Remove failed for `{riot_id}`: {exc}")
            return True

        ctx.friends[:] = [existing for existing in ctx.friends if existing.casefold() != riot_id.casefold()]
        ctx.mood_service.invalidate_report_cache()
        await status_message.edit(
            content=(
                f"\u2705 Removed `{riot_id}` from tracked players. "
                f"Total tracked players: {len(ctx.friends)}"
            )
        )
        ctx.log(f"[remove] Removed player {riot_id}.")
        return True

    return False
