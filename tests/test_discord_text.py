from src.discord_text import format_match_duration, format_recap_player_line, format_recap_queue_name, format_streak_callout


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


def test_format_recap_queue_name_labels_arena_3x6():
    assert format_recap_queue_name(1750) == "🏟️ Arena 3x6"


def test_format_recap_player_line_uses_arena_placement():
    participant = {
        "championName": "Sett",
        "placement": 2,
        "playerSubteamId": 4,
        "kills": 9,
        "deaths": 4,
        "assists": 12,
        "totalDamageDealtToChampions": 45000,
        "totalDamageTaken": 38000,
        "totalHeal": 9000,
        "goldEarned": 15000,
        "playerAugment1": 101,
        "playerAugment2": 202,
        "playerAugment3": 0,
        "item0": 3157,
        "item1": 3089,
        "item2": 0,
        "challenges": {
            "skillshotsHit": 13,
            "skillshotsDodged": 42,
        },
    }

    line = format_recap_player_line(
        "Alpha#NA1",
        participant,
        1400,
        queue_id=1750,
        augment_names={"101": "Warmup Routine", "202": "Scoped Weapons"},
        item_names={"3157": "Zhonya's Hourglass", "3089": "Rabadon's Deathcap"},
    )

    assert "**Alpha**" in line
    assert "`Team 4`" in line
    assert "**Sett** - **Place #2**" in line
    assert "K/D/A 9/4/12" in line
    assert "Damage 45,000" in line
    assert "Skillshots hit 13" in line
    assert "Dodged 42" in line
    assert "Augments Warmup Routine, Scoped Weapons" in line
    assert "Items Zhonya's Hourglass, Rabadon's Deathcap" in line
    assert "`\n   🛒 `Items" in line
    assert "Ã¢â‚¬Â¢" not in line
    assert "CS/min" not in line
    assert "Vision" not in line


def test_format_match_duration_renders_mm_ss():
    assert format_match_duration(1800) == "30:00"
    assert format_match_duration(1831) == "30:31"
