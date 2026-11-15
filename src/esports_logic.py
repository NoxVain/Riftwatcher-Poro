"""Pure helpers for esports news: event parsing, snapshot diffing, and message formatting."""

from datetime import datetime, timedelta

from src.constants import DISCORD_MESSAGE_LIMIT

# Matches first seen already completed are only announced if they started this recently.
NEW_COMPLETED_MAX_AGE_SECONDS = 24 * 3600
# The API can flip a match to "completed" before its game tally catches up (e.g. 2-1 in a Bo5).
# Such results are held this long waiting for a final score, then posted with a settled score.
COMPLETION_GRACE_SECONDS = 15 * 60

ANNOUNCEMENT_ORDER = {"live": 0, "game": 1, "match": 2}
STATE_RANK = {"unstarted": 0, "inProgress": 1, "completed": 2}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SCHEDULE_FOOTER = "\n_Times shown in your local timezone._"


def _parse_start_unix(raw_start):
    if not raw_start:
        return None
    try:
        return int(datetime.fromisoformat(str(raw_start).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_schedule_event(raw_event):
    """Normalize one getSchedule event; returns None for non-match or malformed events."""
    if not isinstance(raw_event, dict) or raw_event.get("type") != "match":
        return None
    match = raw_event.get("match") or {}
    match_id = match.get("id")
    raw_teams = match.get("teams") or []
    if not match_id or len(raw_teams) != 2:
        return None

    teams = []
    for raw_team in raw_teams:
        result = raw_team.get("result") or {}
        name = raw_team.get("name") or raw_team.get("code") or "TBD"
        teams.append(
            {
                "name": name,
                "code": raw_team.get("code") or name,
                "wins": int(result.get("gameWins") or 0),
                "outcome": result.get("outcome"),
            }
        )

    return {
        "match_id": str(match_id),
        "start_unix": _parse_start_unix(raw_event.get("startTime")),
        "state": raw_event.get("state") or "unstarted",
        "league": (raw_event.get("league") or {}).get("name") or "",
        "block": raw_event.get("blockName") or "",
        "best_of": int((match.get("strategy") or {}).get("count") or 1),
        "series_type": (match.get("strategy") or {}).get("type") or "bestOf",
        "teams": teams,
    }


def _codes(event):
    return [team["code"] for team in event["teams"]]


def _wins(event):
    return [team["wins"] for team in event["teams"]]


def _is_tbd(code_or_name):
    return str(code_or_name or "").strip().upper() in ("", "TBD")


def has_tbd_team(event):
    return any(_is_tbd(team["code"]) or _is_tbd(team["name"]) for team in event["teams"])


def _entry_resolved(entry):
    codes = (entry or {}).get("codes") or []
    return len(codes) == 2 and not any(_is_tbd(code) for code in codes)


def snapshot_entry(event, *, state=None, pending_since=None):
    entry = {
        "state": state or event["state"],
        "wins": _wins(event),
        "start_unix": event["start_unix"],
        "codes": _codes(event),
        "names": [team["name"] for team in event["teams"]],
    }
    if pending_since is not None:
        entry["pending_since"] = pending_since
    return entry


def restore_resolved_teams(event, entry):
    """Undo a stale TBD view: reuse the team names stored once the matchup was known."""
    if not has_tbd_team(event) or not _entry_resolved(entry):
        return event
    names = entry.get("names") or []
    if len(names) != 2:
        return event
    teams = [
        {"name": name, "code": code, "wins": 0, "outcome": None}
        for name, code in zip(names, entry["codes"])
    ]
    return {**event, "teams": teams}


def align_event(event, previous):
    """Keep the team order first stored for a match; the API swaps sides between games."""
    stored = (previous or {}).get("codes")
    codes = _codes(event)
    if not stored or len(stored) != 2 or stored[0] == stored[1] or codes[0] == codes[1]:
        return event
    if codes == [stored[1], stored[0]]:
        return {**event, "teams": [event["teams"][1], event["teams"][0]]}
    return event


def wins_needed(event):
    """Game wins that take the series, or None for non best-of formats."""
    if event.get("series_type", "bestOf") != "bestOf":
        return None
    return event["best_of"] // 2 + 1


def series_decided(event):
    needed = wins_needed(event)
    return needed is not None and max(_wins(event)) >= needed


def match_winner_index(event):
    """Index (0/1) of the series winner, or None if undetermined."""
    teams = event["teams"]
    for index, team in enumerate(teams):
        if team.get("outcome") == "win":
            return index
    if teams[0]["wins"] != teams[1]["wins"]:
        return 0 if teams[0]["wins"] > teams[1]["wins"] else 1
    return None


def result_is_final(event):
    """True once a completed event's score actually shows the series winner."""
    winner = match_winner_index(event)
    if winner is None:
        return False
    needed = wins_needed(event)
    return needed is None or event["teams"][winner]["wins"] >= needed


def _with_settled_score(event):
    """Copy of a completed event with the winner's tally raised to the wins needed."""
    winner = match_winner_index(event)
    needed = wins_needed(event)
    if winner is None or needed is None or event["teams"][winner]["wins"] >= needed:
        return event
    teams = [dict(team) for team in event["teams"]]
    teams[winner]["wins"] = needed
    return {**event, "teams": teams}


def _same_teams(previous, event):
    codes = previous.get("codes")
    return codes is None or list(codes) == _codes(event)


def _is_stale(previous, event):
    """True when the API served an older view than one already processed (CDN cache lag)."""
    if not previous:
        return False
    if STATE_RANK.get(event["state"], 0) < STATE_RANK.get(previous.get("state"), 0):
        return True
    if _entry_resolved(previous) and has_tbd_team(event):
        return True  # A known matchup never reverts to TBD; this is an old cached view.
    if not _same_teams(previous, event):
        return False  # e.g. TBD slots filled in; scores aren't comparable.
    previous_wins = previous.get("wins") or [0, 0]
    return any(current < int(prior or 0) for current, prior in zip(_wins(event), previous_wins))


def _game_announcement(previous, event):
    if previous is None or not _same_teams(previous, event):
        return None  # No baseline score to diff against.
    previous_wins = list(previous.get("wins") or [0, 0])
    current_wins = _wins(event)
    gained = [current_wins[i] - int(previous_wins[i] or 0) for i in (0, 1)]
    if gained[0] <= 0 and gained[1] <= 0:
        return None
    if series_decided(event):
        return None  # The series announcement covers the deciding game.
    if gained[0] > 0 and gained[1] > 0:
        winner_index = None  # Missed polls; both teams took games.
    else:
        winner_index = 0 if gained[0] > 0 else 1
    return {"kind": "game", "event": event, "winner_index": winner_index, "game_number": sum(current_wins)}


def reconcile_events(previous_matches, events, *, now_unix):
    """Compare events (already aligned via align_event) against the stored snapshot.

    Returns (announcements, entries): announcements are dicts
    {"kind": "live"|"game"|"match", "event": event, ...}; entries maps match_id to the
    snapshot entry to store. Stale responses never regress stored state, so flapping
    API caches cannot re-trigger announcements.
    """
    announcements = []
    entries = {}
    for event in events:
        match_id = event["match_id"]
        previous = previous_matches.get(match_id)
        if _is_stale(previous, event):
            entries[match_id] = previous
            continue
        state = event["state"]
        previous_state = previous.get("state") if previous else None

        if state == "inProgress":
            if previous_state in (None, "unstarted"):
                announcements.append({"kind": "live", "event": event})
            game = _game_announcement(previous, event)
            if game:
                announcements.append(game)
            entries[match_id] = snapshot_entry(event)
            continue

        if state == "completed" and previous_state != "completed":
            too_old = previous is None and now_unix - (event["start_unix"] or 0) > NEW_COMPLETED_MAX_AGE_SECONDS
            if not too_old:
                if not result_is_final(event):
                    pending_since = (previous or {}).get("pending_since") or now_unix
                    if now_unix - pending_since < COMPLETION_GRACE_SECONDS:
                        # Hold the result (stored as still in progress) until the score catches up.
                        entries[match_id] = snapshot_entry(event, state="inProgress", pending_since=pending_since)
                        continue
                    event = _with_settled_score(event)
                announcements.append({"kind": "match", "event": event, "winner_index": match_winner_index(event)})

        entries[match_id] = snapshot_entry(event)

    announcements.sort(
        key=lambda item: (item["event"]["start_unix"] or 0, item["event"]["match_id"], ANNOUNCEMENT_ORDER[item["kind"]])
    )
    return announcements, entries


def events_in_window(events, start_unix, end_unix):
    selected = [e for e in events if e["start_unix"] is not None and start_unix <= e["start_unix"] < end_unix]
    return sorted(selected, key=lambda e: (e["start_unix"], e["match_id"]))


def _spoiler(text, enabled):
    return f"||{text}||" if enabled else text


def _header(event):
    return " ".join(part for part in (event["league"], event["block"]) if part) or "Esports"


def _score_line(event):
    a, b = event["teams"]
    return f"{a['code']} {a['wins']}-{b['wins']} {b['code']}"


def _matchup(event):
    a, b = event["teams"]
    return f"**{a['name']}** vs **{b['name']}**"


def format_live_announcement(event):
    return (
        f"\U0001F534 **LIVE** \u00B7 {_header(event)}\n"
        f"{_matchup(event)} \u00B7 Bo{event['best_of']}"
    )


def format_game_announcement(event, winner_index, game_number, *, spoilers=True):
    if winner_index is None:
        result = f"Score update: {_score_line(event)}"
        label = "Update"
    else:
        winner = event["teams"][winner_index]["name"]
        result = f"**{winner}** wins \u00B7 {_score_line(event)}"
        label = f"Game {game_number}"
    return (
        f"\U0001F3AE {_header(event)} \u00B7 {_matchup(event)}\n"
        f"{label}: {_spoiler(result, spoilers)}"
    )


def format_match_announcement(event, winner_index, *, spoilers=True):
    if winner_index is None:
        result = f"Match finished \u00B7 {_score_line(event)}"
    else:
        winner = event["teams"][winner_index]
        loser = event["teams"][1 - winner_index]
        if event["best_of"] <= 1:
            result = f"**{winner['name']}** wins"
        else:
            result = f"**{winner['name']}** wins the series {winner['wins']}-{loser['wins']}"
    return (
        f"\U0001F3C6 {_header(event)} \u00B7 {_matchup(event)}\n"
        f"{_spoiler(result, spoilers)}"
    )


def format_announcement(announcement, *, spoilers=True):
    kind = announcement["kind"]
    event = announcement["event"]
    if kind == "live":
        return format_live_announcement(event)
    if kind == "game":
        return format_game_announcement(
            event, announcement["winner_index"], announcement["game_number"], spoilers=spoilers
        )
    return format_match_announcement(event, announcement["winner_index"], spoilers=spoilers)


def _schedule_line(event, spoilers, show_status=True):
    line = f"\u2022 <t:{event['start_unix']}:t> \u00B7 {_matchup(event)} \u00B7 Bo{event['best_of']}"
    if not show_status:
        return line
    if event["state"] == "inProgress":
        line += " \u00B7 \U0001F534 LIVE"
    elif event["state"] == "completed":
        line += f" \u00B7 \u2705 {_spoiler(_score_line(event), spoilers)}"
    return line


def format_daily_schedule(events, cycle_date, *, spoilers=True):
    """Render today's matches grouped by league/stage; returns None when there are none."""
    if not events:
        return None
    lines = [f"\U0001F4C5 **Esports today** \u00B7 {cycle_date}"]
    current_header = None
    for event in events:
        header = _header(event)
        if header != current_header:
            lines.append(f"\n**{header}**")
            current_header = header
        lines.append(_schedule_line(event, spoilers))
    lines.append(SCHEDULE_FOOTER)
    return "\n".join(lines)


def _short_date(day):
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def _pack_messages(header, blocks, footer, limit):
    """Join line blocks into as few messages as fit `limit`, preferring to split between blocks."""
    messages = []
    current = [header]

    def fits(lines):
        return len("\n".join(lines)) <= limit

    for block in blocks:
        if len(current) > 1 and not fits(current + block):
            messages.append("\n".join(current))
            current = []
        for line in block:
            if current and not fits(current + [line]):
                messages.append("\n".join(current))
                current = []
            current.append(line)
    if current and not fits(current + [footer]):
        messages.append("\n".join(current))
        current = []
    current.append(footer)
    messages.append("\n".join(current))
    return [message.strip("\n") for message in messages]


def format_weekly_schedule(days, week_start, *, spoilers=True, show_status=True, limit=DISCORD_MESSAGE_LIMIT):
    """Render a week of matches grouped by report day.

    `days` is a chronological list of (date, events); days without events are skipped.
    With show_status=False, lines omit LIVE/score markers so the text only changes when
    matchups or times change. Returns a list of Discord messages (empty when no matches).
    """
    days = [(day, events) for day, events in days if events]
    if not days:
        return []
    week_end = week_start + timedelta(days=6)
    header = f"\U0001F5D3\uFE0F **Esports this week** \u00B7 {_short_date(week_start)} \u2013 {_short_date(week_end)}"
    blocks = []
    for day, events in days:
        stages = list(dict.fromkeys(_header(event) for event in events))
        block = [f"\n**{_short_date(day)}** \u00B7 {' / '.join(stages)}"]
        block.extend(_schedule_line(event, spoilers, show_status) for event in events)
        blocks.append(block)
    return _pack_messages(header, blocks, SCHEDULE_FOOTER, limit)
