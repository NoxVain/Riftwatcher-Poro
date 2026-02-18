import asyncio
import time
from datetime import datetime, timedelta, timezone

import requests

from src.report_logic import (
    create_mode_records,
    format_mode_line,
    get_mode_totals,
    rank_sort_key,
    wilson_lower_bound,
)
from src.riot_api import get_lol_name


class MoodService:
    def __init__(
        self,
        *,
        log,
        friends,
        riot_client,
        report_timezone,
        report_cache_seconds,
        daily_refresh_seconds,
        db_enabled,
        db_load_latest_stats,
        db_upsert_daily_stats,
        db_get_last_seen_match_id,
        db_health_stats,
    ):
        self.log = log
        self.friends = friends
        self.riot_client = riot_client
        self.report_timezone = report_timezone
        self.report_cache_seconds = report_cache_seconds
        self.daily_refresh_seconds = daily_refresh_seconds
        self.db_enabled = db_enabled
        self.db_load_latest_stats = db_load_latest_stats
        self.db_upsert_daily_stats = db_upsert_daily_stats
        self.db_get_last_seen_match_id = db_get_last_seen_match_id
        self.db_health_stats = db_health_stats
        self.report_cache = {"text": None, "expires_at": 0.0, "day": None}

    def invalidate_report_cache(self):
        self.report_cache["text"] = None
        self.report_cache["day"] = None
        self.report_cache["expires_at"] = 0.0

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

    def format_report_from_results(self, ranked_results, error_results, report_start):
        report_lines = ["✨------ **LEAGUE MOOD (LAST 24 HOURS)** ------✨", ""]
        updated_at = datetime.now(tz=self.report_timezone).strftime("%d.%m.%Y %H:%M")
        if not ranked_results and not error_results:
            report_lines.append("Looks like everyone has a life today.")
            report_lines.append("We will keep you up to date of anyone crawls back into the hole.")
            report_lines.append("")
            report_lines.append("✨--------------------------------------------✨")
            report_lines.append(f"_Last updated: {updated_at}_")
            total_elapsed_ms = int((time.perf_counter() - report_start) * 1000)
            self.log(
                f"[mood] Report complete: players={len(self.friends)} "
                "ranked=0 hidden_no_matches=all errors=0 "
                f"elapsed={total_elapsed_ms}ms"
            )
            return "\n".join(report_lines)

        for index, (lol_name, mode_records, wins, losses, win_rate) in enumerate(ranked_results):
            wilson_score = wilson_lower_bound(wins, losses)
            gamer_score = wilson_score * 100
            if wins + losses > 0 and wilson_score <= 0:
                mood_emoji = "💀"
            elif wilson_score >= 0.75:
                mood_emoji = "😁"
            elif wilson_score >= 0.60:
                mood_emoji = "😊"
            elif wilson_score >= 0.50:
                mood_emoji = "🙂"
            elif wilson_score >= 0.40:
                mood_emoji = "😐"
            elif wilson_score >= 0.30:
                mood_emoji = "😕"
            elif wilson_score >= 0.20:
                mood_emoji = "😞"
            else:
                mood_emoji = "😭"

            display_emoji = "⭐" if index == 0 else mood_emoji
            report_lines.append(f"{display_emoji}  **{lol_name}**  |  Gamer Score: **{gamer_score:.1f}**")
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
            self.append_mode_line_if_games(
                report_lines,
                "Arcade",
                mode_records["arcade"]["wins"],
                mode_records["arcade"]["losses"],
            )
            report_lines.append(f"   Total: `{wins}W-{losses}L` - **{win_rate:.1f}%**")
            report_lines.append("")

        for lol_name, error_text in sorted(error_results, key=lambda row: row[0].lower()):
            report_lines.append(f"⚫  **{lol_name}**")
            report_lines.append(f"   Riot error: `{error_text}`")
            report_lines.append("")

        report_lines.append("✨--------------------------------------------✨")
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

        compact_lines = ["✨------ **LEAGUE MOOD (LAST 24 HOURS)** ------✨", ""]
        for index, (lol_name, _mode_records, wins, losses, win_rate) in enumerate(ranked_results):
            display_emoji = "⭐" if index == 0 else "🙂"
            compact_lines.append(f"{display_emoji}  **{lol_name}**  **`{wins}W-{losses}L` - {win_rate:.1f}%**")

        if error_results:
            compact_lines.append("")
            error_names = ", ".join(name for name, _ in error_results[:6])
            more = "" if len(error_results) <= 6 else f" (+{len(error_results) - 6} more)"
            compact_lines.append(f"⚫ Riot errors for {len(error_results)} players: {error_names}{more}")

        compact_lines.append("")
        compact_lines.append("✨--------------------------------------------✨")
        compact_lines.append(f"_Last updated: {updated_at}_")
        compact_text = "\n".join(compact_lines)
        if len(compact_text) > 2000:
            compact_text = compact_text[:1950] + "\n..."
        return compact_text

    async def build_today_win_rate_report(self, progress_callback=None, prefer_snapshot=False, bypass_cache=False):
        today_key = datetime.now(tz=self.report_timezone).date().isoformat()
        now_monotonic = time.monotonic()
        if (
            not bypass_cache
            and
            self.report_cache["text"] is not None
            and self.report_cache["day"] == today_key
            and now_monotonic < self.report_cache["expires_at"]
        ):
            self.log("[mood] Returning cached report.")
            return self.report_cache["text"]

        report_start = time.perf_counter()
        ranked_results = []
        error_results = []

        if self.db_enabled:
            stored_rows = await asyncio.to_thread(self.db_load_latest_stats, today_key)
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
                for row in stored_rows:
                    riot_id = row[0]
                    if not any(p.casefold() == riot_id.casefold() for p in self.friends):
                        continue
                    mode_records = {
                        "solo_duo": {"wins": row[1], "losses": row[2]},
                        "flex": {"wins": row[3], "losses": row[4]},
                        "arcade": {"wins": row[5], "losses": row[6]},
                    }
                    wins = row[7]
                    losses = row[8]
                    total = wins + losses
                    if total == 0:
                        continue
                    win_rate = (wins / total) * 100
                    ranked_results.append((get_lol_name(riot_id), mode_records, wins, losses, win_rate))

                ranked_results.sort(key=rank_sort_key)
                if ranked_results or prefer_snapshot:
                    if not prefer_snapshot and len(ranked_results) == 1 and len(self.friends) > 1:
                        self.log("[mood] Snapshot looked sparse (1 ranked player); falling back to live rebuild.")
                    else:
                        self.log("[mood] Returning report from postgres daily stats.")
                        report_text = self.format_report_from_results(ranked_results, error_results, report_start)
                        if not bypass_cache:
                            self.report_cache["text"] = report_text
                            self.report_cache["day"] = today_key
                            self.report_cache["expires_at"] = time.monotonic() + max(0, self.report_cache_seconds)
                        return report_text
            elif prefer_snapshot:
                report_text = self.format_report_from_results(ranked_results, error_results, report_start)
                if not bypass_cache:
                    self.report_cache["text"] = report_text
                    self.report_cache["day"] = today_key
                    self.report_cache["expires_at"] = time.monotonic() + max(0, self.report_cache_seconds)
                return report_text

        self.riot_client.clear_match_cache()
        total_players = len(self.friends)
        processed_players = 0

        for riot_id in self.friends:
            lol_name = get_lol_name(riot_id)
            self.log(f"[mood] Processing player {lol_name} ({riot_id})")
            try:
                mode_records = await self.riot_client.get_today_mode_records(riot_id)
            except requests.RequestException as exc:
                error_results.append((lol_name, self.simplify_riot_error(exc)))
                self.log(f"[mood] Player failed {lol_name}: {exc}")
                processed_players += 1
                if progress_callback is not None:
                    await progress_callback(processed_players, total_players, lol_name)
                continue

            wins, losses = get_mode_totals(mode_records)
            total = wins + losses
            today_key = datetime.now(tz=self.report_timezone).date().isoformat()
            await asyncio.to_thread(self.db_upsert_daily_stats, today_key, riot_id, mode_records)
            if total == 0:
                processed_players += 1
                if progress_callback is not None:
                    await progress_callback(processed_players, total_players, lol_name)
                continue

            win_rate = (wins / total) * 100
            ranked_results.append((lol_name, mode_records, wins, losses, win_rate))
            processed_players += 1
            if progress_callback is not None:
                await progress_callback(processed_players, total_players, lol_name)

        ranked_results.sort(key=rank_sort_key)
        report_text = self.format_report_from_results(ranked_results, error_results, report_start)
        self.report_cache["text"] = report_text
        self.report_cache["day"] = today_key
        self.report_cache["expires_at"] = time.monotonic() + max(0, self.report_cache_seconds)
        return report_text

    async def refresh_daily_stats_once(self, progress_callback=None):
        if not self.db_enabled:
            return
        self.log("[refresh] Starting daily stats refresh.")
        self.riot_client.clear_match_cache()
        total_players = len(self.friends)
        processed_players = 0
        for riot_id in self.friends:
            try:
                mode_records = await self.riot_client.get_today_mode_records(riot_id)
                today_key = datetime.now(tz=self.report_timezone).date().isoformat()
                await asyncio.to_thread(self.db_upsert_daily_stats, today_key, riot_id, mode_records)
            except requests.RequestException as exc:
                self.log(f"[refresh] Failed for {riot_id}: {exc}")
            finally:
                processed_players += 1
                if progress_callback is not None:
                    await progress_callback(processed_players, total_players, riot_id)
        self.invalidate_report_cache()
        self.log("[refresh] Daily stats refresh complete.")

    async def refresh_recent_matches_snapshot(self, recent_count=1):
        if not self.db_enabled:
            return
        self.log(f"[refresh] Running on-demand recent refresh (count={recent_count})")
        for riot_id in self.friends:
            try:
                puuid = await self.riot_client.fetch_puuid(riot_id)
                recent_ids = await self.riot_client.fetch_recent_match_ids(puuid, count=max(1, recent_count))
                if not recent_ids:
                    continue

                latest_match_id = recent_ids[0]
                last_seen_match_id = await asyncio.to_thread(self.db_get_last_seen_match_id, riot_id)
                if last_seen_match_id == latest_match_id:
                    continue

                mode_records = await self.riot_client.get_today_mode_records(riot_id)
                today_key = datetime.now(tz=self.report_timezone).date().isoformat()
                await asyncio.to_thread(self.db_upsert_daily_stats, today_key, riot_id, mode_records)
            except requests.RequestException as exc:
                self.log(f"[refresh] On-demand refresh failed for {riot_id}: {exc}")
        self.invalidate_report_cache()

    async def run_health_check(self, start_monotonic):
        uptime_seconds = int(time.monotonic() - start_monotonic)
        try:
            db_stats = await asyncio.to_thread(self.db_health_stats)
            db_ok = db_stats["db_ok"]
            cache_entries = db_stats["match_cache_entries"]
        except Exception as exc:
            db_ok = False
            cache_entries = 0
            self.log(f"[health] DB health check failed: {exc}")

        return {
            "uptime_seconds": uptime_seconds,
            "tracked_players": len(self.friends),
            "db_ok": db_ok,
            "match_cache_entries": cache_entries,
            "request_cache_active": self.report_cache["text"] is not None,
        }
