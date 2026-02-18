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


def test_get_leader_badges_by_player_marks_category_leads():
    ranked_results = [
        (
            "Alpha",
            {},
            5,
            2,
            71.4,
            {
                "cs_total": 180,
                "minutes_total": 30.0,
                "objective_damage": 10000,
                "player_damage": 20000,
                "kills": 10,
                "deaths": 4,
                "vision_score": 20,
            },
        ),
        (
            "Bravo",
            {},
            4,
            3,
            57.1,
            {
                "cs_total": 120,
                "minutes_total": 20.0,
                "objective_damage": 9000,
                "player_damage": 22000,
                "kills": 8,
                "deaths": 6,
                "vision_score": 25,
            },
        ),
    ]

    badges = MoodService.get_leader_badges_by_player(ranked_results)
    assert "🌾" in badges["Alpha"]  # tied CS/min lead
    assert "🏰" in badges["Alpha"]
    assert "🗡️" in badges["Alpha"]
    assert "💥" in badges["Bravo"]
    assert "☠️" in badges["Bravo"]  # most deaths
    assert "👁️" in badges["Bravo"]
