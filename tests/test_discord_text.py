import requests

from src.discord_text import (
    describe_error_for_user,
    format_match_duration,
    format_recap_player_line,
    format_recap_queue_name,
    format_streak_callout,
    format_user_error,
    recap_queue_label,
)


def test_recap_queue_label_drops_the_leading_icon():
    assert recap_queue_label(420) == "Ranked Solo/Duo"
    assert recap_queue_label(1750) == "Arena 3x6"
    assert recap_queue_label(450) == "ARAM"
    assert recap_queue_label(9999) == "Queue 9999"


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    response.url = "https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/Secret/Name"
    return requests.HTTPError(f"{status} Error for url: {response.url}", response=response)


def test_describe_error_for_user_maps_riot_http_statuses_without_url():
    assert "check RIOT_API_KEY" in describe_error_for_user(_http_error(403))
    assert "HTTP 404" in describe_error_for_user(_http_error(404))
    assert "rate limit" in describe_error_for_user(_http_error(429))
    assert "HTTP 503" in describe_error_for_user(_http_error(503))
    assert describe_error_for_user(_http_error(418)) == "Riot API returned HTTP 418"
    for status in (403, 404, 429, 503, 418):
        assert "riotgames.com" not in describe_error_for_user(_http_error(status))


def test_describe_error_for_user_hides_unknown_exception_text():
    exc = RuntimeError('connection to server at "10.0.0.5", port 5432 failed: password authentication failed for user "poro"')
    assert describe_error_for_user(exc) == "internal error"
    assert describe_error_for_user(requests.Timeout("read timed out")) == "Riot API request timed out"
    assert describe_error_for_user(requests.ConnectionError("dns")) == "could not reach Riot API"
    assert describe_error_for_user(KeyError("puuid")) == "unexpected response from Riot API"


def test_format_user_error_appends_request_ref_when_available():
    assert format_user_error("Add failed", RuntimeError("x")) == "Add failed: internal error."
    assert format_user_error("Add failed", RuntimeError("x"), "cmd-1234") == "Add failed: internal error. (ref `cmd-1234`)"


def test_format_recap_player_line_compact_layout():
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

    assert line.split("\n") == [
        "\u2705 **Alpha** \u00b7 **Ahri** Mid",
        "**1/2/3** KDA \u00b7 **4.7** CS/min \u00b7 **10** Vision",
        "Damage **10,000** \u00b7 Objectives **2,000**",
        "Taken **4,000** \u00b7 Healing **300**",
    ]
    assert "â€¢" not in line


def test_format_streak_callout_has_banter_tone():
    win_text = format_streak_callout("Alpha#NA1", 3, True)
    loss_text = format_streak_callout("Alpha#NA1", 5, False)
    loss_text_10 = format_streak_callout("Alpha#NA1", 10, False)
    loss_text_13 = format_streak_callout("Alpha#NA1", 13, False)

    assert "Alpha" in win_text
    assert "wins in a row" in win_text
    assert "Tilt Watch" in loss_text
    assert "UNINSTALL PROTOCOL" in loss_text_10
    assert "BIOHAZARD IN THE LOBBY" in loss_text_13


def test_format_streak_callout_loss_tier_boundaries():
    assert "FULL TILT" in format_streak_callout("Alpha#NA1", 9, False)
    assert "UNINSTALL PROTOCOL" in format_streak_callout("Alpha#NA1", 12, False)
    assert "BIOHAZARD IN THE LOBBY" in format_streak_callout("Alpha#NA1", 20, False)


def test_format_streak_callout_win_tier_boundaries():
    assert "LEGENDARY" in format_streak_callout("Alpha#NA1", 9, True)
    assert "FINAL BOSS UNLOCKED" in format_streak_callout("Alpha#NA1", 10, True)
    assert "FINAL BOSS UNLOCKED" in format_streak_callout("Alpha#NA1", 15, True)


def test_format_recap_queue_name_labels_arena_3x6():
    assert format_recap_queue_name(1750) == "🏟️ Arena 3x6"


def test_format_recap_queue_name_labels_ranked_5s():
    assert format_recap_queue_name(710) == "🏆 Ranked 5s"


def test_format_recap_queue_name_labels_normal_draft():
    assert format_recap_queue_name(400) == "🎯 Normal Draft"


def test_format_recap_queue_name_labels_aram():
    assert format_recap_queue_name(450) == "❄️ ARAM"


def test_format_recap_queue_name_falls_back_for_unknown_queue():
    assert format_recap_queue_name(99999) == "🎯 Queue 99999"


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
        "totalDamageShieldedOnTeammates": 1234,
        "goldEarned": 15000,
        "playerAugment1": 101,
        "playerAugment2": 202,
        "playerAugment3": 0,
        "item0": 3157,
        "item1": 3089,
        "item2": 0,
        "challenges": {
            "teamDamagePercentage": 0.321,
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

    lines = line.split("\n")
    assert lines[0].endswith("**Alpha** · **Sett** · Team 4 · **Place #2**")
    assert lines[1:] == [
        "**9/4/12** KDA · Gold **15,000**",
        "Damage **45,000** (32.1% of team) · Taken **38,000**",
        "Healing **9,000** · Shielded **1,234**",
        "Skillshots hit **13** · Dodged **42**",
        "Augments · Warmup Routine, Scoped Weapons",
        "Items · Zhonya's Hourglass, Rabadon's Deathcap",
    ]
    assert "Ã¢â‚¬Â¢" not in line
    assert "CS/min" not in line
    assert "Vision" not in line


def test_format_match_duration_renders_mm_ss():
    assert format_match_duration(1800) == "30:00"
    assert format_match_duration(1831) == "30:31"
