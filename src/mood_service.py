import asyncio
import time
from datetime import datetime, timedelta, timezone

import requests

from src.report_logic import (
    compute_gamer_score,
    derive_primary_role,
    format_mode_line,
    get_mode_totals,
    get_report_cycle_key,
    rank_sort_key,
)
from src.riot_api import get_lol_name
from src.services.baselines import ensure_role_baselines
from src.services.refresh import (
    get_players_ordered_by_oldest_stats as service_get_players_ordered_by_oldest_stats,
    refresh_daily_stats_once as service_refresh_daily_stats_once,
    refresh_recent_matches_snapshot as service_refresh_recent_matches_snapshot,
)
from src.services.report_builder import (
    LEADERBOARD_BADGES as SERVICE_LEADERBOARD_BADGES,
    build_score_breakdown_report as service_build_score_breakdown_report,
    format_report_from_results as service_format_report_from_results,
    get_leader_badges_by_player as service_get_leader_badges_by_player,
    rows_to_ranked_results as service_rows_to_ranked_results,
    with_derived_performance as service_with_derived_performance,
)


class MoodService:
    BASELINE_TTL_SECONDS = 12 * 3600

    LEADERBOARD_BADGES = SERVICE_LEADERBOARD_BADGES

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
        db_load_match_payloads_for_baseline=None,
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
        self.db_load_match_payloads_for_baseline = db_load_match_payloads_for_baseline
        self._role_baselines = None
        self._baselines_built_at = 0.0
        self.report_cache = {"text": None, "expires_at": 0.0, "day": None}
        self.weekly_report_cache = {"text": None, "expires_at": 0.0, "window": None}

    def invalidate_report_cache(self):
        self.report_cache["text"] = None
        self.report_cache["day"] = None
        self.report_cache["expires_at"] = 0.0
        self.weekly_report_cache["text"] = None
        self.weekly_report_cache["window"] = None
        self.weekly_report_cache["expires_at"] = 0.0

    async def _ensure_role_baselines(self):
        await ensure_role_baselines(self)

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
    def rows_to_ranked_results(rows, tracked_friends, baselines=None):
        return service_rows_to_ranked_results(rows, tracked_friends, baselines=baselines)

    async def get_players_ordered_by_oldest_stats(self, cycle_key):
        return await service_get_players_ordered_by_oldest_stats(self, cycle_key)

    @staticmethod
    def with_derived_performance(performance_totals):
        return service_with_derived_performance(performance_totals)

    @classmethod
    def get_leader_badges_by_player(cls, ranked_results):
        return service_get_leader_badges_by_player(ranked_results)

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
        return service_format_report_from_results(
            self,
            ranked_results,
            error_results,
            report_start,
            header_title=header_title,
            empty_line_1=empty_line_1,
            empty_line_2=empty_line_2,
        )

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

        await self._ensure_role_baselines()
        report_start = time.perf_counter()
        ranked_results = []
        error_results = []

        if self.db_enabled:
            stored_rows = await asyncio.to_thread(self.db_load_latest_stats, cycle_key)
            if stored_rows:
                if not prefer_snapshot:
                    latest_updated_at = None
                    for row in stored_rows:
                        row_updated_at = row["updated_at"]
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
                ranked_results = self.rows_to_ranked_results(stored_rows, self.friends, baselines=self._role_baselines)
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
            primary_role = derive_primary_role(performance_totals)
            await asyncio.to_thread(
                self.db_upsert_daily_stats,
                cycle_key,
                riot_id,
                mode_records,
                performance_totals,
                primary_role,
            )
            if total == 0:
                processed_players += 1
                if progress_callback is not None:
                    await progress_callback(processed_players, total_players, lol_name)
                continue

            win_rate = (wins / total) * 100
            gamer_score = compute_gamer_score(wins, losses, performance_totals, primary_role, self._role_baselines)
            ranked_results.append((lol_name, mode_records, wins, losses, win_rate, performance_totals, gamer_score))
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
        await service_refresh_daily_stats_once(self, progress_callback=progress_callback)

    async def refresh_recent_matches_snapshot(self, recent_count=20):
        await service_refresh_recent_matches_snapshot(self, recent_count=recent_count)

    async def build_score_breakdown_report(self):
        return await service_build_score_breakdown_report(self)

    async def run_health_check(self, start_monotonic, worker_stats=None):
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

        baseline_roles = 0
        baseline_samples = 0
        baseline_age_seconds = None
        if self._role_baselines is not None:
            baseline_roles = len(self._role_baselines)
            baseline_samples = sum(
                len(v) for stats in self._role_baselines.values() for v in stats.values()
            )
            baseline_age_seconds = int(time.monotonic() - self._baselines_built_at)

        return {
            "uptime_seconds": uptime_seconds,
            "tracked_players": len(self.friends),
            "db_ok": db_ok,
            "match_cache_entries": cache_entries,
            "request_cache_active": self.report_cache["text"] is not None,
            "players_with_backfill_offset": players_with_backfill_offset,
            "max_backfill_offset": max_backfill_offset,
            "top_backfill_offsets": top_backfill,
            "worker_stats": worker_stats,
            "baseline_roles": baseline_roles,
            "baseline_samples": baseline_samples,
            "baseline_age_seconds": baseline_age_seconds,
        }
