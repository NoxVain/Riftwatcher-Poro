from src.mood_service import MoodService


def test_get_new_match_ids_when_last_seen_present():
    recent_ids = ["m5", "m4", "m3", "m2"]
    assert MoodService.get_new_match_ids(recent_ids, "m3") == ["m5", "m4"]


def test_get_new_match_ids_when_last_seen_missing():
    recent_ids = ["m5", "m4"]
    assert MoodService.get_new_match_ids(recent_ids, None) == ["m5", "m4"]


def test_get_new_match_ids_when_no_new_matches():
    recent_ids = ["m5", "m4"]
    assert MoodService.get_new_match_ids(recent_ids, "m5") == []
