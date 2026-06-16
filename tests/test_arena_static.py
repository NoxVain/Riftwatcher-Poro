import requests

from src import arena_static


def test_load_arena_display_names_parses_augments_and_items(monkeypatch):
    arena_static.reset_arena_display_name_cache()

    def fake_fetch_json(url):
        if url == arena_static.COMMUNITY_DRAGON_ARENA_URL:
            return {"augments": [{"id": 93, "name": "Warmup Routine"}, {"id": 323, "name": "Cerberus"}]}
        if url == arena_static.DATA_DRAGON_VERSIONS_URL:
            return ["16.12.1"]
        if url == arena_static.DATA_DRAGON_ITEM_URL_TEMPLATE.format(version="16.12.1"):
            return {"data": {"3157": {"name": "Zhonya's Hourglass"}, "3089": {"name": "Rabadon's Deathcap"}}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(arena_static, "_fetch_json", fake_fetch_json)

    names = arena_static.load_arena_display_names()

    assert names["augment_names"] == {"93": "Warmup Routine", "323": "Cerberus"}
    assert names["item_names"] == {"3157": "Zhonya's Hourglass", "3089": "Rabadon's Deathcap"}

    arena_static.reset_arena_display_name_cache()


def test_load_arena_display_names_falls_back_to_empty_maps(monkeypatch):
    arena_static.reset_arena_display_name_cache()

    def fake_fetch_json(_url):
        raise requests.RequestException("temporary failure")

    monkeypatch.setattr(arena_static, "_fetch_json", fake_fetch_json)

    names = arena_static.load_arena_display_names()

    assert names == {"augment_names": {}, "item_names": {}}

    arena_static.reset_arena_display_name_cache()
