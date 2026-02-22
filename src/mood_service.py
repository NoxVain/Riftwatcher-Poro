import asyncio
import time
from datetime import datetime, timedelta, timezone

import requests

from src.report_logic import (
    accumulate_participant_performance,
    format_mode_line,
    get_match_duration_seconds,
    get_mode_bucket,
    get_mode_totals,
    get_report_cycle_key,
    is_remake_match,
    is_match_in_report_cycle,
    rank_sort_key,
    wilson_lower_bound,
)
from src.riot_api import get_lol_name


class MoodService:
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

    def __init__(
        self,
        *,
        log,
        friends,
        riot_client,
        report_timezone,
        report_day_start_hour,
        report_cache_seconds,
        daily_refresh_seconds,
        db_enabled,
        db_load_latest_stats,
        db_load_weekly_stats=None,
        db_upsert_daily_stats,
        db_get_daily_stats_for_player,
        db_get_last_seen_match_id,
        db_set_last_seen_match_id,
        db_health_stats,
        db_load_backfill_offsets=None,
    ):
        self.log = log
        self.friends = friends
        self.riot_client = riot_client
        self.report_timezone = report_timezone
        self.report_day_start_hour = max(0, min(23, int(report_day_start_hour)))
        self.report_cache_seconds = report_cache_seconds
        self.daily_refresh_seconds = daily_refresh_seconds
        self.db_enabled = db_enabled
        self.db_load_latest_stats = db_load_latest_stats
        self.db_load_weekly_stats = db_load_weekly_stats or (lambda _start, _end: [])
        self.db_upsert_daily_stats = db_upsert_daily_stats
        self.db_get_daily_stats_for_player = db_get_daily_stats_for_player
        self.db_get_last_seen_match_id = db_get_last_seen_match_id
        self.db_set_last_seen_match_id = db_set_last_seen_match_id
        self.db_health_stats = db_health_stats
        self.db_load_backfill_offsets = db_load_backfill_offsets or (lambda: {})
        self.report_cache = {"text": None, "expires_at": 0.0, "day": None}
        self.weekly_report_cache = {"text": None, "expires_at": 0.0, "window": None}

    def invalidate_report_cache(self):
        self.report_cache["text"] = None
        self.report_cache["day"] = None
        self.report_cache["expires_at"] = 0.0
        self.weekly_report_cache["text"] = None
        self.weekly_report_cache["window"] = None
        self.weekly_report_cache["expires_at"] = 0.0

    @staticmethod
    def append_mode_line_if_games(report_lines, label, wins, losses):
        if wins + losses == 0:
            return
        report_lines.append(format_mode_line(label, wins, losses))

    @staticmethod
    def simplify_riot_error(exc):
        text = str(exc)
        if "401" in text and "Unauthorized" in text:
            return "401 Unauthorized (check RIOT_API_KEY)"
        if len(text) > 140:
            return text[:137] + "..."
        return text

    @staticmethod
    def get_new_match_ids(recent_ids, last_seen_match_id):
        if not recent_ids:
            return []
        if not last_seen_match_id:
            return list(recent_ids)

        new_ids = []
        for match_id in recent_ids:
            if match_id == last_seen_match_id:
                break
            new_ids.append(match_id)
        return new_ids

    def get_cycle_key(self):
        return get_report_cycle_key(
            self.report_timezone,
            day_start_hour=self.report_day_start_hour,
        )

    def get_week_window(self, now_utc=None):
        cycle_date = datetime.fromisoformat(
            get_report_cycle_key(
                self.report_timezone,
                day_start_hour=self.report_day_start_hour,
                now_utc=now_utc,
            )
        ).date()
        week_start_date = cycle_date - timedelta(days=cycle_date.weekday())
        week_end_exclusive_date = week_start_date + timedelta(days=7)
        return week_start_date, week_end_exclusive_date

    @staticmethod
    def rows_to_ranked_results(rows, tracked_friends):
        tracked_lookup = {friend.casefold() for friend in tracked_friends}
        ranked_results = []
        for row in rows:
            riot_id = row[0]
            if riot_id.casefold() not in tracked_lookup:
                continue
            mode_records = {
                "solo_duo": {"wins": row[1], "losses": row[2]},
                "flex": {"wins": row[3], "losses": row[4]},
            }
            performance_totals = {
                "cs_total": int(row[10] or 0),
                "minutes_total": float(row[11] or 0.0),
                "objective_damage": int(row[12] or 0),
                "player_damage": int(row[13] or 0),
                "healing": int(row[14] or 0),
                "damage_taken": int(row[15] or 0),
                "kills": int(row[16] or 0),
                "deaths": int(row[17] or 0),
                "vision_score": int(row[18] or 0),
            }
            wins, losses = get_mode_totals(mode_records)
            total = wins + losses
            if total == 0:
                continue
            win_rate = (wins / total) * 100
            ranked_results.append((get_lol_name(riot_id), mode_records, wins, losses, win_rate, performance_totals))
        ranked_results.sort(key=rank_sort_key)
        return ranked_results

    async def get_players_ordered_by_oldest_stats(self, cycle_key):
        players = list(self.friends)
        if not self.db_enabled:
            return players

        try:
            stored_rows = await asyncio.to_thread(self.db_load_latest_stats, cycle_key)
        except Exception as exc:
            self.log(f"[refresh] Could not load stats ordering from DB: {exc}")
            return players

        updated_at_by_riot_id = {}
        for row in stored_rows or []:
            riot_id = row[0]
            updated_at = row[9] if len(row) > 9 else None
            updated_at_by_riot_id[str(riot_id).casefold()] = updated_at

        def sort_key(riot_id):
            updated_at = updated_at_by_riot_id.get(riot_id.casefold())
            if updated_at is None:
                return (0, datetime.min.replace(tzinfo=timezone.utc), riot_id.casefold())
            return (1, updated_at.astimezone(timezone.utc), riot_id.casefold())

        return sorted(players, key=sort_key)

    @staticmethod
    def with_derived_performance(performance_totals):
        data = dict(performance_totals)
        minutes = float(data.get("minutes_total", 0.0) or 0.0)
        cs_total = int(data.get("cs_total", 0) or 0)
        data["cs_per_min"] = (cs_total / minutes) if minutes > 0 else 0.0
        return data

    @classmethod
    def get_leader_badges_by_player(cls, ranked_results):
        if not ranked_results:
            return {}

        metrics = []
        for entry in ranked_results:
            performance = cls.with_derived_performance(entry[5])
            metrics.append((entry[0], performance))

        leaders = {name: [] for name, _ in metrics}
        for metric_key, badge in cls.LEADERBOARD_BADGES:
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

    def format_report_from_results(
        self,
        ranked_results,
        error_results,
        report_start,
        *,
        header_title="DAILY",
        empty_line_1="Looks like everyone has a life today.",
        empty_line_2="We will keep you up to date if anyone crawls back into the hole.",
    ):
        report_lines = [f"\u2728------ **LEAGUE MOOD ({header_title})** ------\u2728", ""]
        updated_at = datetime.now(tz=self.report_timezone).strftime("%d.%m.%Y %H:%M")
        if not ranked_results and not error_results:
            report_lines.append(empty_line_1)
            report_lines.append(empty_line_2)
            report_lines.append("")
            report_lines.append("\u2728--------------------------------------------\u2728")
            report_lines.append(f"_Last updated: {updated_at}_")
            total_elapsed_ms = int((time.perf_counter() - report_start) * 1000)
            self.log(
                f"[mood] Report complete: players={len(self.friends)} "
                "ranked=0 hidden_no_matches=all errors=0 "
                f"elapsed={total_elapsed_ms}ms"
            )
            return "\n".join(report_lines)

        badges_by_player = self.get_leader_badges_by_player(ranked_results)
        for index, (lol_name, mode_records, wins, losses, win_rate, _performance) in enumerate(ranked_results):
            wilson_score = wilson_lower_bound(wins, losses)
            gamer_score = wilson_score * 100
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
            self.append_mode_line_if_games(
                report_lines,
                "Ranked Solo/Duo",
                mode_records["solo_duo"]["wins"],
                mode_records["solo_duo"]["losses"],
            )
            self.append_mode_line_if_games(
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
        self.log(
            f"[mood] Report complete: players={len(self.friends)} "
            f"ranked={len(ranked_results)} hidden_no_matches={len(self.friends) - len(ranked_results) - len(error_results)} "
            f"errors={len(error_results)} elapsed={total_elapsed_ms}ms"
        )
        report_text = "\n".join(report_lines)
        if len(report_text) <= 2000:
            return report_text

        compact_lines = [f"\u2728------ **LEAGUE MOOD ({header_title})** ------\u2728", ""]
        for index, (lol_name, _mode_records, wins, losses, win_rate, _performance) in enumerate(ranked_results):
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

    async def build_today_win_rate_report(self, progress_callback=None, prefer_snapshot=False, bypass_cache=False):
        cycle_key = self.get_cycle_key()
        now_monotonic = time.monotonic()
        if (
            not bypass_cache
            and
            self.report_cache["text"] is not None
            and self.report_cache["day"] == cycle_key
            and now_monotonic < self.report_cache["expires_at"]
        ):
            self.log("[mood] Returning cached report.")
            return self.report_cache["text"]

        report_start = time.perf_counter()
        ranked_results = []
        error_results = []

        if self.db_enabled:
            stored_rows = await asyncio.to_thread(self.db_load_latest_stats, cycle_key)
            if stored_rows:
                if not prefer_snapshot:
                    latest_updated_at = None
                    for row in stored_rows:
                        row_updated_at = row[9]
                        if row_updated_at is None:
                            continue
                        if latest_updated_at is None or row_updated_at > latest_updated_at:
                            latest_updated_at = row_updated_at

                    snapshot_max_age_seconds = max(600, self.daily_refresh_seconds * 2)
                    snapshot_stale = True
                    if latest_updated_at is not None:
                        latest_updated_utc = latest_updated_at.astimezone(timezone.utc)
                        snapshot_age = datetime.now(tz=timezone.utc) - latest_updated_utc
                        snapshot_stale = snapshot_age > timedelta(seconds=snapshot_max_age_seconds)

                    if snapshot_stale:
                        self.log("[mood] Snapshot data is stale; falling back to live rebuild.")
                        stored_rows = []

            if stored_rows:
                ranked_results = self.rows_to_ranked_results(stored_rows, self.friends)
                if ranked_results or prefer_snapshot:
                    self.log("[mood] Returning report from postgres daily stats.")
                    report_text = self.format_report_from_results(ranked_results, error_results, report_start)
                    if not bypass_cache:
                        self.report_cache["text"] = report_text
                        self.report_cache["day"] = cycle_key
                        self.report_cache["expires_at"] = time.monotonic() + max(0, self.report_cache_seconds)
                    return report_text
            elif prefer_snapshot:
                report_text = self.format_report_from_results(ranked_results, error_results, report_start)
                if not bypass_cache:
                    self.report_cache["text"] = report_text
                    self.report_cache["day"] = cycle_key
                    self.report_cache["expires_at"] = time.monotonic() + max(0, self.report_cache_seconds)
                return report_text

        self.riot_client.clear_match_cache()
        total_players = len(self.friends)
        processed_players = 0

        ordered_players = await self.get_players_ordered_by_oldest_stats(cycle_key)
        for riot_id in ordered_players:
            lol_name = get_lol_name(riot_id)
            self.log(f"[mood] Processing player {lol_name} ({riot_id})")
            try:
                mode_records, performance_totals = await self.riot_client.get_today_mode_records(riot_id)
            except requests.RequestException as exc:
                error_results.append((lol_name, self.simplify_riot_error(exc)))
                self.log(f"[mood] Player failed {lol_name}: {exc}")
                processed_players += 1
                if progress_callback is not None:
                    await progress_callback(processed_players, total_players, lol_name)
                continue

            wins, losses = get_mode_totals(mode_records)
            total = wins + losses
            await asyncio.to_thread(
                self.db_upsert_daily_stats,
                cycle_key,
                riot_id,
                mode_records,
                performance_totals,
            )
            if total == 0:
                processed_players += 1
                if progress_callback is not None:
                    await progress_callback(processed_players, total_players, lol_name)
                continue

            win_rate = (wins / total) * 100
            ranked_results.append((lol_name, mode_records, wins, losses, win_rate, performance_totals))
            processed_players += 1
            if progress_callback is not None:
                await progress_callback(processed_players, total_players, lol_name)

        ranked_results.sort(key=rank_sort_key)
        report_text = self.format_report_from_results(ranked_results, error_results, report_start)
        self.report_cache["text"] = report_text
        self.report_cache["day"] = cycle_key
        self.report_cache["expires_at"] = time.monotonic() + max(0, self.report_cache_seconds)
        return report_text

    async def build_weekly_win_rate_report(self, bypass_cache=False):
        if not self.db_enabled:
            return "Weekly report requires database-backed daily stats."

        week_start, week_end_exclusive = self.get_week_window()
        window_key = f"{week_start.isoformat()}::{week_end_exclusive.isoformat()}"
        now_monotonic = time.monotonic()
        if (
            not bypass_cache
            and self.weekly_report_cache["text"] is not None
            and self.weekly_report_cache["window"] == window_key
            and now_monotonic < self.weekly_report_cache["expires_at"]
        ):
            self.log("[mood] Returning cached weekly report.")
            return self.weekly_report_cache["text"]

        report_start = time.perf_counter()
        stored_rows = await asyncio.to_thread(
            self.db_load_weekly_stats,
            week_start.isoformat(),
            week_end_exclusive.isoformat(),
        )
        ranked_results = self.rows_to_ranked_results(stored_rows, self.friends)
        report_text = self.format_report_from_results(
            ranked_results,
            [],
            report_start,
            header_title="WEEKLY",
            empty_line_1=(
                f"No ranked games yet for week {week_start:%d.%m} "
                f"to {(week_end_exclusive - timedelta(days=1)):%d.%m}."
            ),
            empty_line_2=(
                f"Weekly report covers Monday {self.report_day_start_hour:02d}:00 "
                f"through next Monday {self.report_day_start_hour:02d}:00."
            ),
        )
        self.weekly_report_cache["text"] = report_text
        self.weekly_report_cache["window"] = window_key
        self.weekly_report_cache["expires_at"] = time.monotonic() + max(0, self.report_cache_seconds)
        return report_text

    async def refresh_daily_stats_once(self, progress_callback=None):
        if not self.db_enabled:
            return
        self.log("[refresh] Starting daily stats refresh.")
        self.riot_client.clear_match_cache()
        cycle_key = self.get_cycle_key()
        total_players = len(self.friends)
        processed_players = 0
        ordered_players = await self.get_players_ordered_by_oldest_stats(cycle_key)
        for riot_id in ordered_players:
            try:
                mode_records, performance_totals = await self.riot_client.get_today_mode_records(riot_id)
                await asyncio.to_thread(
                    self.db_upsert_daily_stats,
                    cycle_key,
                    riot_id,
                    mode_records,
                    performance_totals,
                )
            except requests.RequestException as exc:
                self.log(f"[refresh] Failed for {riot_id}: {exc}")
            finally:
                processed_players += 1
                if progress_callback is not None:
                    await progress_callback(processed_players, total_players, riot_id)
        self.invalidate_report_cache()
        self.log("[refresh] Daily stats refresh complete.")

    async def refresh_recent_matches_snapshot(self, recent_count=20):
        if not self.db_enabled:
            return
        self.log(f"[refresh] Running on-demand recent refresh (count={recent_count})")
        cycle_key = self.get_cycle_key()
        changed_any = False
        candidates = []

        for riot_id in self.friends:
            try:
                puuid = await self.riot_client.fetch_puuid(riot_id)
                recent_ids = await self.riot_client.fetch_recent_match_ids(
                    puuid,
                    count=max(1, recent_count),
                    riot_id=riot_id,
                )
                if not recent_ids:
                    continue

                last_seen_match_id = await asyncio.to_thread(self.db_get_last_seen_match_id, riot_id)
                new_match_ids = self.get_new_match_ids(recent_ids, last_seen_match_id)
                if not new_match_ids:
                    continue
                candidates.append((riot_id, puuid, recent_ids[0], new_match_ids))
            except requests.RequestException as exc:
                self.log(f"[refresh] On-demand refresh failed for {riot_id}: {exc}")

        for riot_id, puuid, latest_match_id, new_match_ids in candidates:
            try:
                row = await asyncio.to_thread(self.db_get_daily_stats_for_player, cycle_key, riot_id)
                if row is None:
                    self.log(
                        f"[refresh] No baseline daily stats for {riot_id}; "
                        "running full player refresh fallback."
                    )
                    mode_records, performance_totals = await self.riot_client.get_today_mode_records(riot_id)
                    await asyncio.to_thread(
                        self.db_upsert_daily_stats,
                        cycle_key,
                        riot_id,
                        mode_records,
                        performance_totals,
                    )
                    await asyncio.to_thread(self.db_set_last_seen_match_id, riot_id, latest_match_id)
                    changed_any = True
                    continue

                mode_records = {
                    "solo_duo": {"wins": int(row[0]), "losses": int(row[1])},
                    "flex": {"wins": int(row[2]), "losses": int(row[3])},
                }
                performance_totals = {
                    "cs_total": int(row[6] or 0),
                    "minutes_total": float(row[7] or 0.0),
                    "objective_damage": int(row[8] or 0),
                    "player_damage": int(row[9] or 0),
                    "healing": int(row[10] or 0),
                    "damage_taken": int(row[11] or 0),
                    "kills": int(row[12] or 0),
                    "deaths": int(row[13] or 0),
                    "vision_score": int(row[14] or 0),
                }
                player_changed = False
                for match_id in reversed(new_match_ids):
                    match_info = await self.riot_client.fetch_match_info(match_id)
                    if not is_match_in_report_cycle(
                        match_info,
                        self.report_timezone,
                        day_start_hour=self.report_day_start_hour,
                    ):
                        continue
                    if is_remake_match(match_info):
                        continue
                    queue_id = match_info["info"].get("queueId", -1)
                    bucket_name = get_mode_bucket(queue_id)
                    if bucket_name is None:
                        continue
                    participant = self.riot_client.get_participant(match_info, puuid)
                    if participant is None:
                        continue
                    result = participant.get("win")
                    if result is True:
                        mode_records[bucket_name]["wins"] += 1
                        player_changed = True
                    elif result is False:
                        mode_records[bucket_name]["losses"] += 1
                        player_changed = True
                    duration_seconds = get_match_duration_seconds(match_info)
                    accumulate_participant_performance(performance_totals, participant, duration_seconds)

                if player_changed:
                    await asyncio.to_thread(
                        self.db_upsert_daily_stats,
                        cycle_key,
                        riot_id,
                        mode_records,
                        performance_totals,
                    )
                    changed_any = True

                await asyncio.to_thread(self.db_set_last_seen_match_id, riot_id, latest_match_id)
            except requests.RequestException as exc:
                self.log(f"[refresh] On-demand refresh failed for {riot_id}: {exc}")
        if changed_any:
            self.invalidate_report_cache()

    async def run_health_check(self, start_monotonic):
        uptime_seconds = int(time.monotonic() - start_monotonic)
        backfill_offsets = {}
        try:
            db_stats = await asyncio.to_thread(self.db_health_stats)
            db_ok = db_stats["db_ok"]
            cache_entries = db_stats["match_cache_entries"]
            backfill_offsets = await asyncio.to_thread(self.db_load_backfill_offsets)
        except Exception as exc:
            db_ok = False
            cache_entries = 0
            self.log(f"[health] DB health check failed: {exc}")

        tracked_by_key = {riot_id.casefold(): riot_id for riot_id in self.friends}
        tracked_offsets = []
        for key, riot_id in tracked_by_key.items():
            offset = int(backfill_offsets.get(key, 0) or 0)
            tracked_offsets.append((riot_id, max(0, offset)))

        players_with_backfill_offset = sum(1 for _riot_id, offset in tracked_offsets if offset > 0)
        max_backfill_offset = max((offset for _riot_id, offset in tracked_offsets), default=0)
        top_backfill = sorted(tracked_offsets, key=lambda row: (-row[1], row[0].casefold()))
        top_backfill = [f"{riot_id}={offset}" for riot_id, offset in top_backfill if offset > 0][:3]

        return {
            "uptime_seconds": uptime_seconds,
            "tracked_players": len(self.friends),
            "db_ok": db_ok,
            "match_cache_entries": cache_entries,
            "request_cache_active": self.report_cache["text"] is not None,
            "players_with_backfill_offset": players_with_backfill_offset,
            "max_backfill_offset": max_backfill_offset,
            "top_backfill_offsets": top_backfill,
        }

