import discord
import pytest

from discord_fakes import ComponentMessage, view_text
from src import recap_cards as cards
from src.recap_lp import add_lp_to_content

HEADER = "-# Ranked Solo/Duo · **Win** · 30:00"


def _player(name, *, pending=False, extra=""):
    heading = f"✅ **{name}** · **Ahri** Mid"
    text = f"{heading}\n   stats{extra}"
    return cards.CardPlayer(f"{name}#EUW", text, {"match_id": "EUW1_1", "heading": heading} if pending else None)


def _text_displays(view):
    return [item for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]


def _accent(container):
    # Built cards hold an int; cards read back from Discord hold a Colour.
    return getattr(container.accent_colour, "value", container.accent_colour)


def _within_limits(message):
    assert message.view.content_length() + message.reserved_text <= cards.MAX_CARD_TEXT
    assert sum(1 for _ in message.view.walk_children()) <= cards.MAX_CARD_COMPONENTS


@pytest.mark.parametrize(
    "queue_id,results,expected",
    [
        (420, [True, True], cards.ACCENT_WIN),
        (420, [False], cards.ACCENT_LOSS),
        (420, [True, False], cards.ACCENT_MIXED),
        (420, [True, None], cards.ACCENT_NEUTRAL),
        (420, [], cards.ACCENT_NEUTRAL),
        (1700, [True], cards.ACCENT_ARENA),
    ],
)
def test_match_accent(queue_id, results, expected):
    assert cards.match_accent(queue_id, [{"win": result} for result in results]) == expected


def test_single_match_card_keeps_recap_text_and_tracks_lp_components():
    match = cards.CardMatch(HEADER, cards.ACCENT_WIN, [_player("Alpha", pending=True), _player("Bravo")])

    [message] = cards.build_card_messages([match])

    [container] = message.view.children
    assert _accent(container) == cards.ACCENT_WIN
    texts = _text_displays(message.view)
    assert [t.content.split("\n", 1)[0] for t in texts] == [HEADER.split("\n")[0], *(p.text.split("\n")[0] for p in match.players)]
    assert len({t.id for t in texts}) == len(texts)
    # Plain-text fallback reads exactly like the old recap section.
    assert message.text_sections == ["\n\n".join([HEADER] + [p.text for p in match.players])]
    [waiting] = message.waiting
    assert waiting["riot_id"] == "Alpha#EUW"
    assert waiting["pending"]["component_id"] == texts[1].id
    assert message.reserved_text == cards.LP_TEXT_RESERVE


def test_matches_pack_into_messages_within_text_limit_and_continue_headers():
    long_extra = "x" * 1400  # three players overflow one message, forcing a continuation
    matches = [
        cards.CardMatch(HEADER, cards.ACCENT_LOSS, [_player(f"P{m}{i}", pending=True, extra=long_extra) for i in range(3)])
        for m in range(3)
    ]

    messages = cards.build_card_messages(matches)

    assert len(messages) > 1
    for message in messages:
        _within_limits(message)
    all_text = "\n".join(view_text(m.view) for m in messages)
    for match in matches:
        for player in match.players:
            assert player.text.split("\n")[0] in all_text
    assert "(continued)" in all_text
    assert sum(len(m.waiting) for m in messages) == 9


def test_component_limit_splits_large_batches():
    players = [_player(f"P{i}") for i in range(30)]
    messages = cards.build_card_messages([cards.CardMatch(HEADER, cards.ACCENT_WIN, players)])

    assert len(messages) == 2
    for message in messages:
        _within_limits(message)
    assert sum(len(_text_displays(m.view)) - len(m.view.children) for m in messages) == 30


def test_oversized_player_text_is_shortened_not_rejected():
    huge = _player("Huge", pending=True, extra="y" * 10_000)

    [message] = cards.build_card_messages([cards.CardMatch(HEADER, cards.ACCENT_WIN, [huge])])

    _within_limits(message)
    assert _text_displays(message.view)[1].content.endswith("…")


def test_add_lp_to_card_edits_only_the_target_player_and_keeps_style():
    match = cards.CardMatch(HEADER, cards.ACCENT_WIN, [_player("Alpha", pending=True), _player("Bravo", pending=True)])
    [built] = cards.build_card_messages([match])
    sent = ComponentMessage(1, view=built.view)
    target = built.waiting[1]["pending"]

    view = cards.add_lp_to_card(sent, target["component_id"], target["heading"], "Gold II · 40LP → 61LP (+21)", add_lp_to_content)

    texts = _text_displays(view)
    assert "Bravo** · **Ahri** Mid · Gold II · 40LP → 61LP (+21)\n" in texts[2].content
    assert "Gold II" not in texts[1].content
    assert _accent(view.children[0]) == cards.ACCENT_WIN


def test_add_lp_to_card_refuses_unknown_component_or_changed_heading():
    match = cards.CardMatch(HEADER, cards.ACCENT_WIN, [_player("Alpha", pending=True)])
    [built] = cards.build_card_messages([match])
    sent = ComponentMessage(1, view=built.view)
    pending = built.waiting[0]["pending"]

    assert cards.add_lp_to_card(sent, 999, pending["heading"], "x", add_lp_to_content) is None
    assert cards.add_lp_to_card(sent, pending["component_id"], "different heading", "x", add_lp_to_content) is None


@pytest.mark.parametrize(
    "queue_id,results,expected",
    [
        (420, [True, True], "Win"),
        (440, [False], "Loss"),
        (420, [True, False], "Mixed results"),
        (420, [None], "No result"),
        (1750, [False], "Placements"),
    ],
)
def test_match_result_labels_match_accents(queue_id, results, expected):
    participants = [{"win": result} for result in results]
    assert cards.match_result(queue_id, participants) == expected
    assert cards.match_accent(queue_id, participants) == cards.RESULT_ACCENTS[expected]


def test_card_header_shows_queue_result_and_game_length_only():
    assert cards.format_card_header(420, 1902, "Mixed results") == "-# Ranked Solo/Duo · **Mixed results** · 31:42"
    assert cards.format_card_header(1750, 1200, "Placements") == "-# Arena 3x6 · **Placements** · 20:00"
    assert cards.format_card_header(400, 600, "Win") == "-# Normal Draft · **Win** · 10:00"
