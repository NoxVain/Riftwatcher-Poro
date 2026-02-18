import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from src.report_logic import (
    create_mode_records,
    create_performance_totals,
    get_match_end_unix_seconds,
    get_mode_bucket,
    get_mode_totals,
    get_report_cycle_start_unix_seconds,
    is_match_in_report_cycle,
)


def split_riot_id(riot_id):
    game_name, tag_line = riot_id.split("#", 1)
    return game_name, tag_line


def get_lol_name(riot_id):
    game_name, _ = split_riot_id(riot_id)
    return game_name


class RiotApiClient:
    def __init__(
        self,
        *,
        riot_api_key,
        log,
        log_riot_requests,
        report_timezone,
        report_day_start_hour,
        max_today_match_details,
        max_match_ids_scan,
        max_in_memory_match_cache,
        db_get_puuid,
        db_upsert_player,
        db_get_match_info,
        db_upsert_match_info,
        db_set_last_seen_match_id,
        on_unauthorized=None,
    ):
        self.riot_api_key = riot_api_key
        self.log = log
        self.log_riot_requests = log_riot_requests
        self.report_timezone = report_timezone
        self.report_day_start_hour = max(0, min(23, int(report_day_start_hour)))
        self.max_today_match_details = max_today_match_details
        self.max_match_ids_scan = max(1, max_match_ids_scan)
        self.max_in_memory_match_cache = max(0, max_in_memory_match_cache)
        self.db_get_puuid = db_get_puuid
        self.db_upsert_player = db_upsert_player
        self.db_get_match_info = db_get_match_info
        self.db_upsert_match_info = db_upsert_match_info
        self.db_set_last_seen_match_id = db_set_last_seen_match_id
        self.on_unauthorized = on_unauthorized
        self.puuid_cache = {}
        self.match_info_cache = OrderedDict()

    def clear_match_cache(self):
        self.match_info_cache.clear()

    def cache_match_info(self, match_id, match_info):
        if self.max_in_memory_match_cache <= 0:
            return
        self.match_info_cache[match_id] = match_info
        self.match_info_cache.move_to_end(match_id)
        while len(self.match_info_cache) > self.max_in_memory_match_cache:
            self.match_info_cache.popitem(last=False)

    def riot_get_json(self, url):
        headers = {"X-Riot-Token": self.riot_api_key}
        max_attempts = 4

        for attempt in range(1, max_attempts + 1):
            start_time = time.perf_counter()
            response = requests.get(url, headers=headers, timeout=20)
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if self.log_riot_requests:
                self.log(f"[riot] {response.status_code} in {elapsed_ms}ms: {url}")

            if response.status_code != 429:
                if response.status_code == 401 and self.on_unauthorized is not None:
                    self.on_unauthorized()
                response.raise_for_status()
                return response.json()

            retry_after_header = response.headers.get("Retry-After", "1")
            try:
                retry_after = float(retry_after_header)
            except ValueError:
                retry_after = 1.0

            if attempt == max_attempts:
                response.raise_for_status()

            sleep_seconds = max(1.0, retry_after)
            self.log(f"[riot] 429 received. attempt={attempt}/{max_attempts}, sleep={sleep_seconds}s")
            time.sleep(sleep_seconds)

    async def riot_get_json_async(self, url):
        return await asyncio.to_thread(self.riot_get_json, url)

    async def fetch_puuid(self, riot_id):
        cache_key = riot_id.casefold()
        if cache_key in self.puuid_cache:
            return self.puuid_cache[cache_key]

        persisted_puuid = await asyncio.to_thread(self.db_get_puuid, riot_id)
        if persisted_puuid:
            self.puuid_cache[cache_key] = persisted_puuid
            return persisted_puuid

        game_name, tag_line = split_riot_id(riot_id)
        encoded_name = quote(game_name, safe="")
        encoded_tag = quote(tag_line, safe="")
        url = (
            "https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
            f"{encoded_name}/{encoded_tag}"
        )
        data = await self.riot_get_json_async(url)
        puuid = data["puuid"]
        self.puuid_cache[cache_key] = puuid
        await asyncio.to_thread(self.db_upsert_player, riot_id, puuid)
        return puuid

    async def fetch_match_ids(self, puuid, start_time_unix):
        page_size = 100
        start = 0
        all_match_ids = []

        while True:
            url = (
                "https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/"
                f"{puuid}/ids?startTime={start_time_unix}&start={start}&count={page_size}"
            )
            page_match_ids = await self.riot_get_json_async(url)
            if not page_match_ids:
                break

            all_match_ids.extend(page_match_ids)
            if len(all_match_ids) >= self.max_match_ids_scan:
                all_match_ids = all_match_ids[: self.max_match_ids_scan]
                self.log(
                    f"[riot] Reached MAX_MATCH_IDS_SCAN={self.max_match_ids_scan}; "
                    "stopping additional paging."
                )
                break
            if len(page_match_ids) < page_size:
                break
            start += page_size

        return all_match_ids

    async def fetch_recent_match_ids(self, puuid, count=20):
        safe_count = max(1, min(count, 100))
        url = (
            "https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/"
            f"{puuid}/ids?count={safe_count}"
        )
        return await self.riot_get_json_async(url)

    async def fetch_match_info(self, match_id):
        cached = self.match_info_cache.get(match_id)
        if cached is not None:
            self.match_info_cache.move_to_end(match_id)
            return cached

        persisted = await asyncio.to_thread(self.db_get_match_info, match_id)
        if persisted is not None:
            self.cache_match_info(match_id, persisted)
            return persisted

        url = f"https://europe.api.riotgames.com/lol/match/v5/matches/{match_id}"
        match_info = await self.riot_get_json_async(url)
        self.cache_match_info(match_id, match_info)
        await asyncio.to_thread(self.db_upsert_match_info, match_id, match_info)
        return match_info

    @staticmethod
    def get_participant_win(match_info, puuid):
        for participant in match_info["info"]["participants"]:
            if participant["puuid"] == puuid:
                return participant["win"]
        return None

    @staticmethod
    def get_participant(match_info, puuid):
        for participant in match_info["info"]["participants"]:
            if participant["puuid"] == puuid:
                return participant
        return None

    async def get_today_mode_records(self, riot_id):
        player_start = time.perf_counter()
        puuid = await self.fetch_puuid(riot_id)
        start_time_unix = get_report_cycle_start_unix_seconds(
            self.report_timezone,
            day_start_hour=self.report_day_start_hour,
        )
        match_ids = await self.fetch_match_ids(puuid, start_time_unix)
        if match_ids:
            await asyncio.to_thread(self.db_set_last_seen_match_id, riot_id, match_ids[0])

        mode_records = create_mode_records()
        performance_totals = create_performance_totals()
        today_details_processed = 0
        for match_id in match_ids:
            match_info = await self.fetch_match_info(match_id)
            if not is_match_in_report_cycle(
                match_info,
                self.report_timezone,
                day_start_hour=self.report_day_start_hour,
            ):
                continue

            if today_details_processed >= max(1, self.max_today_match_details):
                self.log(
                    f"[mood] {riot_id}: reached MAX_TODAY_MATCH_DETAILS={self.max_today_match_details}, "
                    "stopping further today match processing."
                )
                break

            queue_id = match_info["info"].get("queueId", -1)
            bucket_name = get_mode_bucket(queue_id)
            if bucket_name is None:
                continue
            participant = self.get_participant(match_info, puuid)
            if participant is None:
                continue
            result = participant.get("win")
            if result is True:
                mode_records[bucket_name]["wins"] += 1
            elif result is False:
                mode_records[bucket_name]["losses"] += 1

            duration_seconds = int(match_info["info"].get("gameDuration", 0) or 0)
            if duration_seconds > 10_000:
                duration_seconds = int(duration_seconds / 1000)
            performance_totals["minutes_total"] += max(0.0, duration_seconds / 60.0)
            performance_totals["cs_total"] += int(participant.get("totalMinionsKilled", 0) or 0)
            performance_totals["cs_total"] += int(participant.get("neutralMinionsKilled", 0) or 0)
            performance_totals["objective_damage"] += int(participant.get("damageDealtToObjectives", 0) or 0)
            performance_totals["player_damage"] += int(participant.get("totalDamageDealtToChampions", 0) or 0)
            performance_totals["kills"] += int(participant.get("kills", 0) or 0)
            performance_totals["deaths"] += int(participant.get("deaths", 0) or 0)
            performance_totals["vision_score"] += int(participant.get("visionScore", 0) or 0)
            today_details_processed += 1

        wins, losses = get_mode_totals(mode_records)
        elapsed_ms = int((time.perf_counter() - player_start) * 1000)
        self.log(
            f"[mood] {riot_id}: matches={len(match_ids)} total={wins}W-{losses}L "
            f"solo={mode_records['solo_duo']['wins']}W-{mode_records['solo_duo']['losses']}L "
            f"flex={mode_records['flex']['wins']}W-{mode_records['flex']['losses']}L "
            f"elapsed={elapsed_ms}ms"
        )
        return mode_records, performance_totals

    async def run_riot_connectivity_test(self, riot_id):
        puuid = await self.fetch_puuid(riot_id)
        start_time_unix = get_report_cycle_start_unix_seconds(
            self.report_timezone,
            day_start_hour=self.report_day_start_hour,
        )
        match_ids = await self.fetch_match_ids(puuid, start_time_unix)
        return riot_id, puuid, len(match_ids)

    async def build_debug_player_report(self, riot_id, report_timezone_name, normalize_riot_id):
        window_start_unix = get_report_cycle_start_unix_seconds(
            self.report_timezone,
            day_start_hour=self.report_day_start_hour,
        )
        normalized = normalize_riot_id(riot_id)
        puuid = await self.fetch_puuid(normalized)
        recent_ids = await self.fetch_recent_match_ids(puuid, count=20)
        window_label = f"since {self.report_day_start_hour:02d}:00"
        lines = [
            f"Player debug (timezone={report_timezone_name}, window={window_label}):",
            normalized,
            "match_id | queue | end_time | bucket | in_window",
        ]
        inspected = 0
        for match_id in recent_ids:
            match_info = await self.fetch_match_info(match_id)
            end_ts = get_match_end_unix_seconds(match_info)
            end_local = datetime.fromtimestamp(end_ts, tz=timezone.utc).astimezone(self.report_timezone)
            queue_id = match_info["info"].get("queueId", -1)
            bucket = get_mode_bucket(queue_id) or "ignored"
            in_window = "yes" if end_ts >= window_start_unix else "no"
            lines.append(f"{match_id} | {queue_id} | {end_local:%d.%m.%Y %H:%M} | {bucket} | {in_window}")
            inspected += 1
            if inspected >= 12:
                break

        return "\n".join(lines)
