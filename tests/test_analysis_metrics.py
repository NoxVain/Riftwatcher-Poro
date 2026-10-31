"""Offline playstyle facts and conservative, actionable metric comparisons."""

from datetime import datetime, timedelta, timezone

from src.analysis_metrics import metric_findings, playstyle_summary, recent_summary

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def match(index=0, **overrides):
    game = {
        "queue_id": 420, "game_end_at": NOW - timedelta(hours=40 - index),
        "champion_name": "Lux", "team_position": "MIDDLE", "win": index % 2 == 0,
        "duration_seconds": 1800, "kills": 3, "deaths": 3, "assists": 6,
        "cs": 180, "lane_cs_10": 60, "kill_participation": 0.6, "damage_share": 0.25,
        "vision_score": 30, "wards_killed": 3, "time_dead_seconds": 90,
        "buffs_stolen": 0, "ally_saves": 0, "enemy_jungle_camps": 12,
    }
    game.update(overrides)
    return game


def test_profile_explains_main_role_and_pool_without_mixing_other_roles():
    games = [match(i) for i in range(20)]
    games += [match(i, team_position="UTILITY", champion="Sona", cs=0) for i in range(10)]
    profile = playstyle_summary(games)
    assert "Mid · 20/30 games" in profile and "Lux 10W-10L" in profile
    assert "1 champions" not in profile  # singular and plural should both read naturally
    assert "**6.0 CS/min**" in profile and "**60.0**" in profile
    assert "KP **60.0%**" in profile and "team damage **25.0%**" in profile
    assert "**1.0 vision/min**" in profile and "time dead **5.0%**" in profile
    assert "Sona" not in profile


def test_tied_role_counts_follow_existing_analysis_main_role_choice():
    from src.player_analysis import _main_role

    games = [match(0, team_position="UTILITY"), match(1, team_position="MIDDLE")]
    assert _main_role(games)[0] == "UTILITY"
    assert "How you play · Support · 1/2 games" in playstyle_summary(games)


def test_rates_weight_by_duration_and_missing_values_show_coverage():
    games = [match(0, cs=60, duration_seconds=600), match(1, cs=240, duration_seconds=1200),
             match(2, cs=None, kill_participation=None, lane_cs_10=None)]
    profile = playstyle_summary(games)
    assert "**10.0 (2/3) CS/min**" in profile
    assert "KP **60.0% (2/3)**" in profile
    assert "Parentheses show measured games" in profile


def test_missing_measurements_are_never_fabricated_as_zero():
    games = [match(i, cs=None, kill_participation=None, vision_score=None, time_dead_seconds=None)
             for i in range(20)]
    profile = playstyle_summary(games)
    assert "N/A CS/min" in profile and "KP **N/A**" in profile
    assert "N/A%" not in profile
    assert metric_findings(games) == ([], [])


def test_last_twenty_selects_latest_games_and_keeps_all_summary_fields():
    games = [match(i, win=i >= 10) for i in range(30)]
    summary = recent_summary(list(reversed(games)))
    assert summary == "Last 20 games: 20W-0L (100%) · 6.0 CS/min · KDA 3.00 · KP 60.0%"
    assert recent_summary([]) == "" and playstyle_summary([]) == ""


def test_clear_improvement_is_something_to_continue():
    games = [match(i, cs=150 if i < 20 else 240) for i in range(40)]
    strengths, tips = metric_findings(games)
    assert tips == [] and len(strengths) == 1
    assert "CS/min 5.0 → 8.0" in strengths[0][0]
    assert "earlier 20 vs recent 20 games" in strengths[0][0]
    assert "Keep prioritizing farm" in strengths[0][0]


def test_multiple_declines_have_evidence_and_review_actions():
    games = [match(i, cs=240 if i < 20 else 150, vision_score=45 if i < 20 else 9,
                   deaths=2 if i < 20 else 6) for i in range(40)]
    strengths, tips = metric_findings(games)
    assert strengths == [] and len(tips) == 3
    assert all("Review" in text and "20 games" in text for text, _score, _topic in tips)
    assert {topic for _text, _score, topic in tips} == {"farm", "vision", "survival"}


def test_wins_and_losses_are_never_compared():
    # Lost games have less farm by themselves; that gap restates the result, so it's no tip.
    games = [match(i, cs=240 if i % 2 == 0 else 120) for i in range(40)]
    assert metric_findings(games) == ([], [])


def test_champion_role_changes_small_samples_and_noise_do_not_create_advice():
    for changed_key, value in (("champion_name", "Ahri"), ("team_position", "JUNGLE")):
        games = [match(i, cs=150 if i < 20 else 240) for i in range(40)]
        for game in games[20:]:
            game[changed_key] = value
        assert metric_findings(games) == ([], [])
    assert metric_findings([match(i, cs=100 if i < 9 else 250) for i in range(19)]) == ([], [])
    noisy = [match(i, win=i % 4 < 2, cs=(0 if i % 2 else 600) + (30 if i >= 20 else 0)) for i in range(40)]
    assert metric_findings(noisy) == ([], [])


def test_fewer_than_ten_valid_measurements_on_either_side_is_insufficient():
    games = [match(i, cs=150 if i < 20 else None) for i in range(40)]
    for game in games[20:29]:
        game["cs"] = 240
    assert metric_findings(games) == ([], [])


def test_early_surrenders_do_not_become_cs_at_ten_weakness():
    games = [match(i, cs=None, lane_cs_10=90 if i < 20 else 20,
                   duration_seconds=1800 if i < 20 else 480,
                   deaths=0, vision_score=None, wards_killed=None, time_dead_seconds=None) for i in range(40)]
    assert metric_findings(games) == ([], [])


def test_supports_get_peel_data_and_never_farming_or_invading_advice():
    games = [match(i, team_position="UTILITY", cs=240 if i < 20 else 30,
                   buffs_stolen=2 if i < 20 else 0, ally_saves=3 if i < 20 else 0) for i in range(40)]
    _, tips = metric_findings(games)
    assert len(tips) == 1 and tips[0][2] == "peel"
    profile = playstyle_summary(games)
    assert "Peel: ally saves/game" in profile and "Farm:" not in profile


def test_junglers_get_buff_data_and_no_lane_cs_advice():
    games = [match(i, team_position="JUNGLE", buffs_stolen=2 if i < 20 else 0,
                   lane_cs_10=90 if i < 20 else 20) for i in range(40)]
    _, tips = metric_findings(games)
    assert len(tips) == 1 and tips[0][2] == "invades"
    profile = playstyle_summary(games)
    assert "buffs stolen/game" in profile and "lane CS at 10" not in profile


def test_champion_names_are_escaped_and_unknown_roles_never_get_advice():
    games = [match(i, champion_name="A_test", team_position=None) for i in range(20)]
    assert "A\\_test" in playstyle_summary(games)
    assert "Unknown role" in playstyle_summary(games)
    assert metric_findings(games) == ([], [])


def test_invalid_percentages_and_non_finite_values_are_treated_as_missing():
    games = [match(i, cs=float("nan"), kill_participation=1.2,
                   damage_share=float("inf"), time_dead_seconds=2000) for i in range(20)]
    text = playstyle_summary(games)
    assert "KP **N/A**" in text and "time dead **N/A**" in text
    assert "nan" not in text and "inf" not in text
    assert metric_findings(games) == ([], [])
