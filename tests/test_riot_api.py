import asyncio
from datetime import timezone

import requests

from src.riot_api import RiotApiClient


class DummyResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def build_client():
    return RiotApiClient(
        riot_api_key="test-key",
        riot_platform_routing="euw1",
        log=lambda _msg: None,
        log_riot_requests=False,
        report_timezone=None,
        report_day_start_hour=6,
        max_today_match_details=20,
        max_match_ids_scan=2000,
        max_in_memory_match_cache=200,
        db_get_puuid=lambda _riot_id: None,
        db_upsert_player=lambda _riot_id, _puuid: None,
        db_get_match_info=lambda _match_id: None,
        db_upsert_match_info=lambda _match_id, _match_info: None,
        db_set_last_seen_match_id=lambda _riot_id, _match_id: None,
    )


def test_riot_get_json_retries_on_transient_http(monkeypatch):
    client = build_client()
    sequence = [
        DummyResponse(503),
        DummyResponse(200, payload={"ok": True}),
    ]
    calls = {"count": 0}

    def fake_get(_url, headers=None, timeout=20):
        _ = headers, timeout
        item = sequence[calls["count"]]
        calls["count"] += 1
        return item

    monkeypatch.setattr("src.riot_api.requests.get", fake_get)
    monkeypatch.setattr("src.riot_api.time.sleep", lambda _secs: None)
    monkeypatch.setattr("src.riot_api.random.uniform", lambda _a, _b: 0.0)

    result = client.riot_get_json("https://example.test")
    assert result == {"ok": True}
    assert calls["count"] == 2


def test_riot_get_json_retries_on_request_exception(monkeypatch):
    client = build_client()
    calls = {"count": 0}

    def fake_get(_url, headers=None, timeout=20):
        _ = headers, timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.Timeout("timeout")
        return DummyResponse(200, payload={"ok": True})

    monkeypatch.setattr("src.riot_api.requests.get", fake_get)
    monkeypatch.setattr("src.riot_api.time.sleep", lambda _secs: None)
    monkeypatch.setattr("src.riot_api.random.uniform", lambda _a, _b: 0.0)

    result = client.riot_get_json("https://example.test")
    assert result == {"ok": True}
    assert calls["count"] == 2


def test_get_today_mode_records_stops_detail_fetches_at_limit(monkeypatch):
    client = RiotApiClient(
        riot_api_key="test-key",
        riot_platform_routing="euw1",
        log=lambda _msg: None,
        log_riot_requests=False,
        report_timezone=timezone.utc,
        report_day_start_hour=6,
        max_today_match_details=2,
        max_match_ids_scan=2000,
        max_in_memory_match_cache=200,
        db_get_puuid=lambda _riot_id: None,
        db_upsert_player=lambda _riot_id, _puuid: None,
        db_get_match_info=lambda _match_id: None,
        db_upsert_match_info=lambda _match_id, _match_info: None,
        db_set_last_seen_match_id=lambda _riot_id, _match_id: None,
    )

    fetched_ids = []
    match_ids = ["m5", "m4", "m3", "m2", "m1"]

    async def fake_fetch_puuid(_riot_id):
        return "puuid-alpha"

    async def fake_fetch_match_ids(_puuid, _start_time_unix):
        return match_ids

    async def fake_fetch_match_info(match_id):
        fetched_ids.append(match_id)
        return {
            "info": {
                "queueId": 420,
                "gameDuration": 1800,
                "gameEndTimestamp": 1_700_000_000_000,
                "participants": [{"puuid": "puuid-alpha", "win": True}],
            }
        }

    monkeypatch.setattr(client, "fetch_puuid", fake_fetch_puuid)
    monkeypatch.setattr(client, "fetch_match_ids", fake_fetch_match_ids)
    monkeypatch.setattr(client, "fetch_match_info", fake_fetch_match_info)

    asyncio.run(client.get_today_mode_records("Alpha#EUW"))

    assert fetched_ids == ["m5", "m4"]


def test_fetch_match_info_can_skip_in_memory_cache(monkeypatch):
    db_store = {}
    client = RiotApiClient(
        riot_api_key="test-key",
        riot_platform_routing="euw1",
        log=lambda _msg: None,
        log_riot_requests=False,
        report_timezone=timezone.utc,
        report_day_start_hour=6,
        max_today_match_details=20,
        max_match_ids_scan=2000,
        max_in_memory_match_cache=200,
        db_get_puuid=lambda _riot_id: None,
        db_upsert_player=lambda _riot_id, _puuid: None,
        db_get_match_info=lambda match_id: db_store.get(match_id),
        db_upsert_match_info=lambda match_id, match_info: db_store.__setitem__(match_id, match_info),
        db_set_last_seen_match_id=lambda _riot_id, _match_id: None,
    )

    async def fake_riot_get_json_async(_url):
        return {"info": {"queueId": 420, "participants": []}}

    monkeypatch.setattr(client, "riot_get_json_async", fake_riot_get_json_async)

    result = asyncio.run(client.fetch_match_info("m1", cache_in_memory=False))
    assert result is not None
    assert "m1" in db_store
    assert "m1" not in client.match_info_cache
