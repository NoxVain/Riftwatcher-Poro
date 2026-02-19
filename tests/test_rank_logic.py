from src.rank_logic import compare_rank_direction, format_rank_change_message


def test_compare_rank_direction_detects_up_and_down():
    old_rank = {"tier": "GOLD", "rank_division": "III"}
    up_rank = {"tier": "GOLD", "rank_division": "II"}
    down_rank = {"tier": "SILVER", "rank_division": "I"}

    assert compare_rank_direction(old_rank, up_rank) == 1
    assert compare_rank_direction(old_rank, down_rank) == -1
    assert compare_rank_direction(old_rank, {"tier": "GOLD", "rank_division": "III"}) == 0


def test_compare_rank_direction_handles_ranked_to_unranked():
    old_rank = {"tier": "PLATINUM", "rank_division": "IV"}
    assert compare_rank_direction(old_rank, None) == -1
    assert compare_rank_direction(None, old_rank) == 1


def test_format_rank_change_message_contains_tone_and_queue():
    message_up = format_rank_change_message(
        "Alpha#NA1",
        "RANKED_SOLO_5x5",
        {"tier": "SILVER", "rank_division": "I", "league_points": 90},
        {"tier": "GOLD", "rank_division": "IV", "league_points": 12},
    )
    message_down = format_rank_change_message(
        "Alpha#NA1",
        "RANKED_FLEX_SR",
        {"tier": "GOLD", "rank_division": "IV", "league_points": 5},
        {"tier": "SILVER", "rank_division": "I", "league_points": 75},
    )

    assert "Rank Up" in message_up
    assert "Ranked Solo/Duo" in message_up
    assert "Rank Down" in message_down
    assert "Ranked Flex" in message_down
