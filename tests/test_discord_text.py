from src.discord_text import format_match_duration, format_recap_player_line, format_streak_callout


def test_format_recap_player_line_uses_bullet_separator():
    participant = {
        "win": True,
        "championName": "Ahri",
        "teamPosition": "MIDDLE",
        "kills": 1,
        "deaths": 2,
        "assists": 3,
        "totalMinionsKilled": 120,
        "neutralMinionsKilled": 20,
        "totalDamageDealtToChampions": 10000,
        "damageDealtToObjectives": 2000,
        "totalHeal": 300,
        "totalDamageTaken": 4000,
        "visionScore": 10,
    }

    line = format_recap_player_line("Alpha#NA1", participant, 1800)

    assert " • **Ahri**" in line
    assert "â€¢" not in line


def test_format_streak_callout_has_banter_tone():
    win_text = format_streak_callout("Alpha#NA1", 3, True)
    loss_text = format_streak_callout("Alpha#NA1", 5, False)

    assert "Alpha" in win_text
    assert "wins in a row" in win_text
    assert "Tilt Watch" in loss_text


def test_format_match_duration_renders_mm_ss():
    assert format_match_duration(1800) == "30:00"
    assert format_match_duration(1831) == "30:31"
