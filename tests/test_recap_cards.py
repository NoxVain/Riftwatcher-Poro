import asyncio
import json

import discord
import pytest

from card_fakes import ComponentMessage, view_text
from src.recap_cards import (
    ACCENTS, MAX_COMPONENTS, MAX_TEXT, CardMatch, CardPlayer, add_card_lp,
    build_card_messages, card_result, format_card_player,
)
from src.recap_lp import edit_recap_with_lp, handle_lp_event, lp_pending_state_key


def player_text(name="Alpha", **extra):
    participant = dict(championName="Malzahar", kills=5, deaths=4, assists=4,
                       totalMinionsKilled=220, neutralMinionsKilled=4, teamPosition="MIDDLE",
                       totalDamageDealtToChampions=24127, damageDealtToObjectives=5242,
                       totalDamageTaken=15133, totalHeal=1639, visionScore=8, **extra)
    return format_card_player(f"{name}#EUW", participant, 1546, queue_id=420, champion_display="<:Malzahar:90>")


def test_approved_layout_and_actual_discord_wire_payload():
    text = player_text()
    assert text == (
        "**Alpha** · <:Malzahar:90> Mid\n"
        "**5/4/4** KDA · **8.7** CS/min · **8** Vision\n"
        "Damage **24,127** · Objectives **5,242**\n"
        "Taken **15,133** · Healing **1,639**"
    )
    packed = build_card_messages([CardMatch(420, 1546, "Loss", [CardPlayer("Alpha#EUW", text)])])[0]
    assert view_text(packed.view).startswith("-# Ranked Solo/Duo · **Loss** · 25:46\n")
    assert "❌" not in text and "✅" not in text
    assert "Today" not in view_text(packed.view) and "2026" not in view_text(packed.view)
    assert packed.view.to_components()[0]["accent_color"] == ACCENTS["Loss"]
    params = discord.http.handle_message_parameters(view=packed.view, allowed_mentions=discord.AllowedMentions.none())
    assert params.payload["flags"] & (1 << 15)
    assert not params.payload.get("content") and not params.payload.get("embeds")
    assert params.payload["components"][0]["type"] == 17
    assert params.payload["allowed_mentions"]["parse"] == []


@pytest.mark.parametrize("info,participants,expected", [
    ({}, [("a", {"win": True})], "Win"),
    ({}, [("a", {"win": False})], "Loss"),
    ({}, [("a", {"win": True}), ("b", {"win": False})], "Mixed results"),
    ({}, [("a", {})], "No result"),
    ({}, [("a", {"win": "false"})], "No result"),
    ({"endOfGameResult": "Draw"}, [("a", {"win": False})], "Draw"),
    ({"teams": [{"win": False}, {"win": False}]}, [("a", {"win": False})], "No result"),
    ({"teams": [{"win": True}, {"win": False}]}, [("a", {"win": False})], "Loss"),
    ({"queueId": 1750}, [("a", {"win": False, "placement": 1})], "Placements"),
    ({"queueId": 1750, "teams": [{"win": False}, {"win": False}]}, [("a", {"placement": 1})], "Placements"),
    ({"teams": [{}, {}]}, [("a", {"win": False})], "No result"),
])
def test_outcomes_do_not_mistake_unknown_or_mixed_for_a_draw(info, participants, expected):
    assert card_result({"info": info}, participants) == expected


def test_mixed_results_identify_each_players_result():
    participant = dict(win=True, teamPosition="MIDDLE")
    text = format_card_player("Alpha#EUW", participant, 1800, queue_id=420,
                              champion_display="**Ahri**", mixed=True)
    assert "· **Win**" in text.splitlines()[0]
    text = format_card_player("Bravo#EUW", dict(participant, win=False), 1800, queue_id=420,
                              champion_display="**Ahri**", mixed=True)
    assert "· **Loss**" in text.splitlines()[0]


def test_long_batches_split_at_player_boundaries_and_reserve_lp_space():
    players = [CardPlayer(f"Player{i}#EUW", f"**Player{i}**\n" + "x" * 650,
                          dict(heading=f"**Player{i}**", match_id="m1")) for i in range(18)]
    pages = build_card_messages([CardMatch(420, 1800, "Win", players)])
    assert len(pages) > 1
    all_text = "\n".join(view_text(page.view) for page in pages)
    for i in range(18):
        assert all_text.count(f"**Player{i}**") == 1
    for page in pages:
        assert page.view.content_length() + page.reserved <= MAX_TEXT
        assert page.view.total_children_count <= MAX_COMPONENTS
        ids = [item.id for item in page.view.walk_children()]
        assert len(ids) == len(set(ids))
        for waiting in page.waiting:
            assert any(item.id == waiting["pending"]["component_id"] for item in page.view.walk_children())
    assert "continued" in view_text(pages[1].view)


def test_component_count_splits_a_large_batch_even_with_short_text():
    matches = [CardMatch(420, 1800, "Win", [CardPlayer("Alpha", "**Alpha**\nStats")]) for _ in range(30)]
    pages = build_card_messages(matches)
    assert len(pages) >= 3
    assert sum(len(page.view.children) for page in pages) == 30
    assert all(page.view.total_children_count <= MAX_COMPONENTS for page in pages)


def test_oversize_player_is_rejected_before_sending():
    with pytest.raises(ValueError, match="limits"):
        build_card_messages([CardMatch(420, 1800, "Win", [CardPlayer("a", "x" * MAX_TEXT)])])


def test_markdown_in_player_names_is_escaped():
    assert "\\*\\*" in player_text(name="**not a heading**")


def test_delayed_lp_targets_component_id_when_headings_repeat():
    heading = "**Alpha** · <:Malzahar:90> Mid"
    pending = dict(heading=heading, match_id="m1")
    pages = build_card_messages([
        CardMatch(420, 1800, "Loss", [CardPlayer("Alpha", heading + "\nStats", pending)]),
        CardMatch(420, 1800, "Loss", [CardPlayer("Alpha", heading + "\nStats", dict(pending, match_id="m2"))]),
    ])
    message = ComponentMessage(1, view=pages[0].view)
    target = pages[0].waiting[1]["pending"]["component_id"]
    edited = asyncio.run(edit_recap_with_lp(message, heading, "Gold II · 74LP → 56LP (-18)", component_id=target))
    assert edited is message
    assert message.content == ""
    view = discord.ui.LayoutView.from_message(message, timeout=None)
    items = [item for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
    assert "LP" not in items[1].content
    assert "**56LP\u00a0(-18)**" in items[3].content
    assert view_text(view).count("56LP") == 1


def test_nonbreaking_lp_total_and_change():
    assert add_card_lp("Alpha\nStats", "Gold II · 74LP → 56LP (-18)") == (
        "Alpha | Gold II · 74LP → **56LP\u00a0(-18)**\nStats"
    )


def test_concurrent_lp_edits_preserve_both_players_updates():
    players = [CardPlayer(name, f"**{name}**\nStats", dict(
        heading=f"**{name}**", match_id="m1", queue_type="RANKED_SOLO_5X5", won=True, end_ts=900,
    )) for name in ("Alpha", "Bravo")]
    packed = build_card_messages([CardMatch(420, 1800, "Win", players)])[0]
    state = {lp_pending_state_key(item["riot_id"]): json.dumps([dict(item["pending"], message_id=42)])
             for item in packed.waiting}

    class Channel:
        message = ComponentMessage(42, view=packed.view)

        async def fetch_message(self, message_id):
            channel = self

            class Snapshot(ComponentMessage):
                async def edit(self, **kwargs):
                    await asyncio.sleep(0)  # Two stale fetches would lose an update.
                    await super().edit(**kwargs)
                    channel.message = self
                    return self

            return Snapshot(message_id, view=discord.ui.LayoutView.from_message(self.message, timeout=None))

    channel = Channel()

    async def run():
        event = dict(queue_type="RANKED_SOLO_5X5", won=True, observed_at=1000, tier="GOLD", rank="II")
        results = await asyncio.gather(*[
            handle_lp_event(riot_id=name, event=dict(event, delta=delta, league_points=lp),
                            recap_channel=channel, db_get_state=state.get, db_set_state=state.__setitem__,
                            log=lambda _message: None, now=1000)
            for name, delta, lp in (("Alpha", 21, 56), ("Bravo", 24, 74))
        ])
        assert results == [True, True]

    asyncio.run(run())
    assert "**56LP\u00a0(+21)**" in channel.message.display
    assert "**74LP\u00a0(+24)**" in channel.message.display
