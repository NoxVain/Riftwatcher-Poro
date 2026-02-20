import asyncio
import random
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from src.report_logic import (
    create_mode_records,
    create_performance_totals,
    get_match_duration_seconds,
    get_match_end_unix_seconds,
    get_mode_bucket,
    get_mode_totals,
    get_report_cycle_start_unix_seconds,
    is_remake_match,
    is_match_in_report_cycle,
)


def split_riot_id(riot_id):
    game_name, tag_line = riot_id.split("#", 1)
    return game_name, tag_line


def get_lol_name(riot_id):
    game_name, _ = split_riot_id(riot_id)
    return game_name


class RiotApiClient:
    TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
    BACKFILL_MIN_PAUSE_SECONDS = 120.0
    BACKFILL_PAUSE_MULTIPLIER = 3.0
    RIOT_LIMIT_SHORT_COUNT = 20
    RIOT_LIMIT_SHORT_WINDOW_SECONDS = 1.0
    RIOT_LIMIT_LONG_COUNT = 100
    RIOT_LIMIT_LONG_WINDOW_SECONDS = 120.0
    BACKFILL_LONG_WINDOW_BUDGET = 80

    def __init__(
        self,
        *,
        riot_api_key,
        riot_platform_routing,
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
        self.riot_platform_routing = riot_platform_routing.strip().lower()
        self.log = log
        self.log_riot_requests = log_riot_requests
        self.report_timezone = report_timezone
        self.report_day_start_hour = max(0, min(23, int(report_day_start_hour)))
        self.max_today_match_details = max_today_match_details
        self.max_match_ids_scan = int(max_match_ids_scan)
        self.max_in_memory_match_cache = max(0, max_in_memory_match_cache)
        self.db_get_puuid = db_get_puuid
        self.db_upsert_player = db_upsert_player
        self.db_get_match_info = db_get_match_info
        self.db_upsert_match_info = db_upsert_match_info
        self.db_set_last_seen_match_id = db_set_last_seen_match_id
        self.on_unauthorized = on_unauthorized
        self.puuid_cache = {}
        self.summoner_id_cache = {}
        self.match_info_cache = OrderedDict()
        self._backfill_pause_until = 0.0
        self._backfill_pause_lock = threading.Lock()
        self._request_timestamps = []
        self._rate_limit_lock = threading.Lock()

    def clear_match_cache(self):
        self.match_info_cache.clear()

    def cache_match_info(self, match_id, match_info):
        if self.max_in_memory_match_cache <= 0:
            return
        self.match_info_cache[match_id] = match_info
        self.match_info_cache.move_to_end(match_id)
        while len(self.match_info_cache) > self.max_in_memory_match_cache:
            self.match_info_cache.popitem(last=False)

    def schedule_backfill_pause(self, retry_after_seconds):
        pause_seconds = max(
            self.BACKFILL_MIN_PAUSE_SECONDS,
            float(retry_after_seconds) * self.BACKFILL_PAUSE_MULTIPLIER,
        )
        now = time.monotonic()
        pause_until = now + pause_seconds
        updated = False
        with self._backfill_pause_lock:
            if pause_until > self._backfill_pause_until:
                self._backfill_pause_until = pause_until
                updated = True
        if updated:
            self.log(
                f"[backfill] Rate-limit pressure detected; pausing backfill requests "
                f"for {pause_seconds:.0f}s."
            )

    def get_backfill_pause_remaining(self):
        with self._backfill_pause_lock:
            return max(0.0, self._backfill_pause_until - time.monotonic())

    async def wait_for_backfill_window(self):
        remaining = self.get_backfill_pause_remaining()
        if remaining <= 0:
            return
        self.log(f"[backfill] Throttled; waiting {remaining:.1f}s before next Riot request.")
        await asyncio.sleep(remaining)

    def _prune_request_timestamps(self, now):
        cutoff = now - self.RIOT_LIMIT_LONG_WINDOW_SECONDS
        keep_from = 0
        for index, ts in enumerate(self._request_timestamps):
            if ts >= cutoff:
                keep_from = index
                break
        else:
            self._request_timestamps = []
            return
        if keep_from > 0:
            self._request_timestamps = self._request_timestamps[keep_from:]

    def _wait_for_rate_limit_slot(self, *, request_tier):
        while True:
            now = time.monotonic()
            with self._rate_limit_lock:
                self._prune_request_timestamps(now)
                short_count = 0
                short_cutoff = now - self.RIOT_LIMIT_SHORT_WINDOW_SECONDS
                for ts in reversed(self._request_timestamps):
                    if ts >= short_cutoff:
                        short_count += 1
                    else:
                        break
                long_count = len(self._request_timestamps)

                wait_seconds = 0.0
                if short_count >= self.RIOT_LIMIT_SHORT_COUNT:
                    short_oldest = self._request_timestamps[-short_count]
                    wait_seconds = max(wait_seconds, (short_oldest + self.RIOT_LIMIT_SHORT_WINDOW_SECONDS) - now)
                if long_count >= self.RIOT_LIMIT_LONG_COUNT:
                    long_oldest = self._request_timestamps[0]
                    wait_seconds = max(wait_seconds, (long_oldest + self.RIOT_LIMIT_LONG_WINDOW_SECONDS) - now)
                if request_tier == "backfill" and long_count >= self.BACKFILL_LONG_WINDOW_BUDGET:
                    budget_oldest = self._request_timestamps[long_count - self.BACKFILL_LONG_WINDOW_BUDGET]
                    wait_seconds = max(wait_seconds, (budget_oldest + self.RIOT_LIMIT_LONG_WINDOW_SECONDS) - now)

                if wait_seconds <= 0:
                    self._request_timestamps.append(now)
                    return

            time.sleep(max(0.01, wait_seconds))

    def riot_get_json(self, url, *, request_tier="priority"):
        headers = {"X-Riot-Token": self.riot_api_key}
        max_attempts = 5

        for attempt in range(1, max_attempts + 1):
            try:
                self._wait_for_rate_limit_slot(request_tier=request_tier)
                start_time = time.perf_counter()
                response = requests.get(url, headers=headers, timeout=20)
                elapsed_ms = int((time.perf_counter() - start_time) * 1000)
                if self.log_riot_requests:
                    self.log(f"[riot] {response.status_code} in {elapsed_ms}ms: {url}")
            except requests.RequestException as exc:
                if attempt == max_attempts:
                    raise
                sleep_seconds = self.retry_backoff_seconds(attempt)
                self.log(
                    f"[riot] Request error ({type(exc).__name__}). "
                    f"attempt={attempt}/{max_attempts}, sleep={sleep_seconds:.2f}s"
                )
                time.sleep(sleep_seconds)
                continue

            if response.status_code == 401 and self.on_unauthorized is not None:
                self.on_unauthorized()

            if response.status_code == 429:
                retry_after_header = response.headers.get("Retry-After", "1")
                try:
                    retry_after = float(retry_after_header)
                except ValueError:
                    retry_after = 1.0
                self.schedule_backfill_pause(retry_after)

                if attempt == max_attempts:
                    response.raise_for_status()
                sleep_seconds = max(1.0, retry_after)
                self.log(f"[riot] 429 received. attempt={attempt}/{max_attempts}, sleep={sleep_seconds}s")
                time.sleep(sleep_seconds)
                continue

            if response.status_code in self.TRANSIENT_HTTP_STATUSES:
                if attempt == max_attempts:
                    response.raise_for_status()
                sleep_seconds = self.retry_backoff_seconds(attempt)
                self.log(
                    f"[riot] Transient HTTP {response.status_code}. "
                    f"attempt={attempt}/{max_attempts}, sleep={sleep_seconds:.2f}s"
                )
                time.sleep(sleep_seconds)
                continue

            response.raise_for_status()
            return response.json()

    @staticmethod
    def retry_backoff_seconds(attempt):
        base = min(8.0, 0.5 * (2 ** max(0, attempt - 1)))
        jitter = random.uniform(0.0, 0.25)
        return base + jitter

    async def riot_get_json_async(self, url, *, request_tier="priority"):
        if request_tier == "backfill":
            await self.wait_for_backfill_window()
        return await asyncio.to_thread(self.riot_get_json, url, request_tier=request_tier)

    async def fetch_puuid(self, riot_id, *, request_tier="priority"):
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
        data = await self.riot_get_json_async(url, request_tier=request_tier)
        puuid = data["puuid"]
        self.puuid_cache[cache_key] = puuid
        await asyncio.to_thread(self.db_upsert_player, riot_id, puuid)
        return puuid

    async def fetch_match_ids(self, puuid, start_time_unix, *, request_tier="priority"):
        page_size = 100
        start = 0
        all_match_ids = []

        while True:
            url = (
                "https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/"
                f"{puuid}/ids?startTime={start_time_unix}&start={start}&count={page_size}"
            )
            page_match_ids = await self.riot_get_json_async(url, request_tier=request_tier)
            if not page_match_ids:
                break

            all_match_ids.extend(page_match_ids)
            if self.max_match_ids_scan > 0 and len(all_match_ids) >= self.max_match_ids_scan:
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

    async def fetch_recent_match_ids(self, puuid, count=20, *, request_tier="priority"):
        safe_count = max(1, min(count, 100))
        url = (
            "https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/"
            f"{puuid}/ids?count={safe_count}"
        )
        return await self.riot_get_json_async(url, request_tier=request_tier)

    async def fetch_match_info(self, match_id, *, cache_in_memory=True, request_tier="priority"):
        if cache_in_memory:
            cached = self.match_info_cache.get(match_id)
            if cached is not None:
                self.match_info_cache.move_to_end(match_id)
                return cached

        persisted = await asyncio.to_thread(self.db_get_match_info, match_id)
        if persisted is not None:
            if cache_in_memory:
                self.cache_match_info(match_id, persisted)
            return persisted

        url = f"https://europe.api.riotgames.com/lol/match/v5/matches/{match_id}"
        match_info = await self.riot_get_json_async(url, request_tier=request_tier)
        if cache_in_memory:
            self.cache_match_info(match_id, match_info)
        await asyncio.to_thread(self.db_upsert_match_info, match_id, match_info)
        return match_info

    async def fetch_summoner_id(self, puuid):
        if puuid in self.summoner_id_cache:
            return self.summoner_id_cache[puuid]
        url = (
            f"https://{self.riot_platform_routing}.api.riotgames.com"
            f"/lol/summoner/v4/summoners/by-puuid/{puuid}"
        )
        data = await self.riot_get_json_async(url)
        summoner_id = data.get("id") or data.get("summonerId")
        if not summoner_id:
            if isinstance(data, dict):
                keys = ", ".join(sorted(data.keys()))
                raise RuntimeError(f"Summoner lookup missing encrypted id fields; keys={keys}")
            raise RuntimeError(f"Summoner lookup returned unexpected payload type: {type(data).__name__}")
        self.summoner_id_cache[puuid] = summoner_id
        return summoner_id

    async def fetch_ranked_entries(self, riot_id):
        puuid = await self.fetch_puuid(riot_id)
        by_puuid_url = (
            f"https://{self.riot_platform_routing}.api.riotgames.com"
            f"/lol/league/v4/entries/by-puuid/{puuid}"
        )
        try:
            return await self.riot_get_json_async(by_puuid_url)
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in {404, 405}:
                raise

        summoner_id = await self.fetch_summoner_id(puuid)
        url = (
            f"https://{self.riot_platform_routing}.api.riotgames.com"
            f"/lol/league/v4/entries/by-summoner/{summoner_id}"
        )
        return await self.riot_get_json_async(url)

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
        detail_fetch_limit = max(1, self.max_today_match_details)
        today_details_fetched = 0
        for match_id in match_ids:
            if today_details_fetched >= detail_fetch_limit:
                self.log(
                    f"[mood] {riot_id}: reached MAX_TODAY_MATCH_DETAILS={self.max_today_match_details}, "
                    "stopping further today match processing."
                )
                break

            match_info = await self.fetch_match_info(match_id)
            today_details_fetched += 1
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
            participant = self.get_participant(match_info, puuid)
            if participant is None:
                continue
            result = participant.get("win")
            if result is True:
                mode_records[bucket_name]["wins"] += 1
            elif result is False:
                mode_records[bucket_name]["losses"] += 1

            duration_seconds = get_match_duration_seconds(match_info)
            performance_totals["minutes_total"] += max(0.0, duration_seconds / 60.0)
            performance_totals["cs_total"] += int(participant.get("totalMinionsKilled", 0) or 0)
            performance_totals["cs_total"] += int(participant.get("neutralMinionsKilled", 0) or 0)
            performance_totals["objective_damage"] += int(participant.get("damageDealtToObjectives", 0) or 0)
            performance_totals["player_damage"] += int(participant.get("totalDamageDealtToChampions", 0) or 0)
            performance_totals["healing"] += int(participant.get("totalHeal", 0) or 0)
            performance_totals["damage_taken"] += int(participant.get("totalDamageTaken", 0) or 0)
            performance_totals["kills"] += int(participant.get("kills", 0) or 0)
            performance_totals["deaths"] += int(participant.get("deaths", 0) or 0)
            performance_totals["vision_score"] += int(participant.get("visionScore", 0) or 0)

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
