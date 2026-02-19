from src.discord_text import format_recap_player_line


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
