"""Rewriting old (pre key-change) PUUIDs in cached match payloads to current ones."""

import asyncio
import json

from src import puuid_remap
from src.db import cache as db_cache
from src.puuid_remap import (
    REMAP_STATE_KEY,
    build_puuid_remap,
    collect_puuid_observations,
    remap_cached_puuids,
    run_puuid_remap_once,
    tracked_fingerprint,
)

NOX_NEW = "new-nox_puuid"
NOX_OLD = "old-nox_puuid"
FRESH_NEW = "new-fresh_puuid"
FRESH_OLD = "old-fresh_puuid"
STRANGER = "stranger_puuid"


def participant(name, tag, puuid):
    return {"riotIdGameName": name, "riotIdTagline": tag, "puuid": puuid}


def match(*participants):
    return {
        "metadata": {"participants": [p["puuid"] for p in participants]},
        "info": {"participants": list(participants)},
    }


TRACKED = {"noxvain#euw": NOX_NEW, "freshwhite#euw": FRESH_NEW}


def test_collect_puuid_observations_matches_riot_id_case_insensitively():
    payloads = [
        match(participant("NoxVain", "EUW", NOX_OLD), participant("Someone", "EUW", STRANGER)),
        match(participant("noxvain", "euw", NOX_NEW)),
        {"info": {}},
        match({"puuid": "no-riot-id"}),
    ]

    seen = collect_puuid_observations(payloads, TRACKED)

    assert seen == {("noxvain#euw", NOX_OLD): 1, ("noxvain#euw", NOX_NEW): 1}


def test_build_puuid_remap_maps_each_player_old_puuid_to_current():
    seen = {
        ("noxvain#euw", NOX_OLD): 900,
        ("noxvain#euw", NOX_NEW): 250,
        ("freshwhite#euw", FRESH_OLD): 800,
    }

    assert build_puuid_remap(seen, TRACKED) == {NOX_OLD: NOX_NEW, FRESH_OLD: FRESH_NEW}


def test_build_puuid_remap_skips_ambiguous_players_and_current_puuids():
    logs = []
    seen = {
        # Two different old PUUIDs under one Riot ID: someone else may have held it.
        ("noxvain#euw", NOX_OLD): 5,
        ("noxvain#euw", "another_old"): 3,
        # A tracked player's current PUUID is never rewritten.
        ("freshwhite#euw", NOX_NEW): 1,
    }

    assert build_puuid_remap(seen, TRACKED, logs.append) == {}
    assert logs == ["[puuid] Skipping noxvain#euw: seen with 2 different old PUUIDs."]


def test_build_puuid_remap_skips_old_puuid_claimed_by_two_players():
    seen = {("noxvain#euw", "shared_old"): 2, ("freshwhite#euw", "shared_old"): 1}

    assert build_puuid_remap(seen, TRACKED) == {}


def test_tracked_fingerprint_changes_with_players_and_puuids():
    base = tracked_fingerprint(TRACKED)

    assert tracked_fingerprint(dict(reversed(list(TRACKED.items())))) == base
    assert tracked_fingerprint({**TRACKED, "noxvain#euw": "rotated"}) != base
    assert tracked_fingerprint({**TRACKED, "newplayer#euw": "x"}) != base


class FakeState:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


def run_remap(state, payloads, *, rewrite=None, tracked_rows=None):
    rewrites = []

    def rewrite_puuids(remap):
        rewrites.append(remap)
        return rewrite(remap) if rewrite else 7

    summary = remap_cached_puuids(
        load_tracked=lambda: tracked_rows if tracked_rows is not None else [("NoxVain#EUW", NOX_NEW), ("FreshWhite#EUW", FRESH_NEW)],
        load_payloads=lambda limit: iter(payloads),
        rewrite_puuids=rewrite_puuids,
        get_state=state.get,
        set_state=state.set,
        log=lambda message: None,
    )
    return summary, rewrites


def test_remap_cached_puuids_rewrites_then_records_fingerprint():
    state = FakeState()
    payloads = [match(participant("NoxVain", "EUW", NOX_OLD)), match(participant("NoxVain", "EUW", NOX_NEW))]

    summary, rewrites = run_remap(state, payloads)

    assert rewrites == [{NOX_OLD: NOX_NEW}]
    assert summary["matches"] == 2 and summary["players"] == 1 and summary["rewritten"] == 7
    assert state.values[REMAP_STATE_KEY] == tracked_fingerprint(TRACKED)


def test_remap_cached_puuids_skips_when_fingerprint_unchanged():
    state = FakeState({REMAP_STATE_KEY: tracked_fingerprint(TRACKED)})

    summary, rewrites = run_remap(state, [match(participant("NoxVain", "EUW", NOX_OLD))])

    assert summary is None and rewrites == []


def test_remap_cached_puuids_records_fingerprint_when_nothing_to_rewrite():
    state = FakeState()

    summary, rewrites = run_remap(state, [match(participant("NoxVain", "EUW", NOX_NEW))])

    assert rewrites == [] and summary["rewritten"] == 0
    assert REMAP_STATE_KEY in state.values


def test_remap_cached_puuids_ignores_players_without_puuid():
    summary, rewrites = run_remap(FakeState(), [], tracked_rows=[("Pending#EUW", None)])

    assert summary is None and rewrites == []


def test_failed_rewrite_leaves_fingerprint_unset_so_it_retries():
    state = FakeState()

    def broken(remap):
        raise RuntimeError("db down")

    try:
        run_remap(state, [match(participant("NoxVain", "EUW", NOX_OLD))], rewrite=broken)
    except RuntimeError:
        pass

    assert REMAP_STATE_KEY not in state.values


def test_run_puuid_remap_once_logs_failures_without_raising(monkeypatch):
    monkeypatch.setattr(puuid_remap, "release_freed_memory", lambda: False)
    logs = []

    def broken_load():
        raise RuntimeError("db down")

    result = asyncio.run(
        run_puuid_remap_once(
            db_load_tracked_puuids=broken_load,
            db_load_match_payloads=lambda limit: iter(()),
            db_rewrite_cached_puuids=lambda remap: 0,
            db_get_state=lambda key: None,
            db_set_state=lambda key, value: None,
            log=logs.append,
            start_delay_seconds=0,
        )
    )

    assert result is None
    assert logs == ["[puuid] PUUID remap failed; will retry on next startup: db down"]


def test_run_puuid_remap_once_logs_summary(monkeypatch):
    monkeypatch.setattr(puuid_remap, "release_freed_memory", lambda: False)
    logs = []
    state = FakeState()

    asyncio.run(
        run_puuid_remap_once(
            db_load_tracked_puuids=lambda: [("NoxVain#EUW", NOX_NEW)],
            db_load_match_payloads=lambda limit: iter([match(participant("NoxVain", "EUW", NOX_OLD))]),
            db_rewrite_cached_puuids=lambda remap: 1,
            db_get_state=state.get,
            db_set_state=state.set,
            log=logs.append,
            start_delay_seconds=0,
        )
    )

    assert len(logs) == 1
    assert logs[0].startswith("[puuid] Remapped old PUUIDs: players=1 rewritten=1 scanned=1 elapsed=")


class FakeRewriteDb:
    """Emulates the paged UPDATE ... replace() the way Postgres would run it."""

    def __init__(self, payloads):
        self.payloads = dict(payloads)
        self.updates = 0

    def execute(self, query, params=None, fetch=False, fetchone=False):
        if query == "SELECT match_id FROM match_info_cache;":
            return [(match_id,) for match_id in self.payloads]
        assert query.startswith("UPDATE match_info_cache SET payload = replace(")
        assert "updated_at" not in query
        self.updates += 1
        pair_count = query.count("strpos(")
        replace_params = params[: 2 * pair_count]
        page_ids = params[2 * pair_count]
        condition_params = params[2 * pair_count + 1:]
        changed = []
        for match_id in page_ids:
            text = self.payloads[match_id]
            if not any(old in text for old in condition_params):
                continue
            for index in range(0, len(replace_params), 2):
                text = text.replace(replace_params[index], replace_params[index + 1])
            self.payloads[match_id] = text
            changed.append((match_id,))
        return changed


def test_db_rewrite_cached_puuids_replaces_whole_values_in_pages(monkeypatch):
    stored = {
        "EUW1_1": json.dumps(match(participant("NoxVain", "EUW", NOX_OLD), participant("FreshWhite", "EUW", FRESH_OLD))),
        # An earlier Riot ID: the old PUUID still identifies the player.
        "EUW1_2": json.dumps(match(participant("OldName", "EUW", NOX_OLD))),
        "EUW1_3": json.dumps(match(participant("NoxVain", "EUW", NOX_NEW))),
        # A PUUID that merely contains the old one as a substring is left alone.
        "EUW1_4": json.dumps(match(participant("Other", "EUW", NOX_OLD + "x"))),
    }
    fake = FakeRewriteDb(stored)
    monkeypatch.setattr(db_cache, "db_execute", fake.execute)

    changed = db_cache.db_rewrite_cached_puuids({NOX_OLD: NOX_NEW, FRESH_OLD: FRESH_NEW}, batch_size=2)

    assert changed == 2
    assert fake.updates == 2
    first = json.loads(fake.payloads["EUW1_1"])
    assert first["metadata"]["participants"] == [NOX_NEW, FRESH_NEW]
    assert [p["puuid"] for p in first["info"]["participants"]] == [NOX_NEW, FRESH_NEW]
    assert json.loads(fake.payloads["EUW1_2"])["info"]["participants"][0]["puuid"] == NOX_NEW
    assert fake.payloads["EUW1_3"] == stored["EUW1_3"]
    assert fake.payloads["EUW1_4"] == stored["EUW1_4"]


def test_db_rewrite_cached_puuids_does_nothing_without_remap(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("no query expected")

    monkeypatch.setattr(db_cache, "db_execute", unexpected)

    assert db_cache.db_rewrite_cached_puuids({}) == 0
