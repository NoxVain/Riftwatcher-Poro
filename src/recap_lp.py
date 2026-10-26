"""LP gain/loss for ranked match recaps, with no extra Riot requests.

The rank worker already polls League-V4 for every tracked player. A poll that sees
exactly one new game in a queue becomes an LP event. A recap line is joined with its
event when the recap is built (event first), or the posted recap is edited when the
event arrives later (recap first).
"""

import asyncio
import json

from src.rank_logic import APEX_TIERS, DIVISION_ORDER, TIER_ORDER, format_rank, normalize_queue_type
from src.recap_cards import add_lp_to_card


QUEUE_TYPE_BY_QUEUE_ID = {420: "RANKED_SOLO_5X5", 440: "RANKED_FLEX_SR"}
LP_MATCH_WINDOW_SECONDS = 30 * 60
# Riot match end times and our clock can disagree slightly.
LP_CLOCK_SKEW_SECONDS = 60
MAX_MESSAGE_LENGTH = 2000

# Rank and recap workers share one event loop; this keeps their read-modify-write
# of the pending/event lists from interleaving.
LP_STATE_LOCK = asyncio.Lock()
# Serializes fetch-then-edit of posted recaps, so two players' LP edits on the same
# message can't overwrite each other.
LP_EDIT_LOCK = asyncio.Lock()


def lp_pending_state_key(riot_id):
    return f"recap_lp_pending::{riot_id.casefold()}"


def lp_events_state_key(riot_id):
    return f"recap_lp_events::{riot_id.casefold()}"


def rank_points(tier, division, league_points):
    tier = str(tier or "").strip().upper()
    if tier not in TIER_ORDER:
        return None
    try:
        lp = int(league_points)
    except (TypeError, ValueError):
        return None
    # Master/Grandmaster/Challenger share one LP ladder above Diamond I.
    if tier in APEX_TIERS:
        return TIER_ORDER["MASTER"] * 400 + lp
    division = str(division or "").strip().upper()
    if division not in DIVISION_ORDER:
        return None
    return TIER_ORDER[tier] * 400 + DIVISION_ORDER[division] * 100 + lp


def build_lp_event(previous_entry, current_entry, observed_at):
    """LP event for one queue, or None when the change is not one attributable game.

    previous_entry uses the player_ranked_state row shape; current_entry is a raw
    League-V4 entry.
    """
    if not previous_entry or not current_entry:
        return None
    queue_type = normalize_queue_type(current_entry.get("queueType"))
    if queue_type not in QUEUE_TYPE_BY_QUEUE_ID.values():
        return None
    try:
        wins_delta = int(current_entry.get("wins")) - int(previous_entry.get("wins"))
        losses_delta = int(current_entry.get("losses")) - int(previous_entry.get("losses"))
    except (TypeError, ValueError):
        return None
    if (wins_delta, losses_delta) == (1, 0):
        won = True
    elif (wins_delta, losses_delta) == (0, 1):
        won = False
    else:
        # No game (dodge/decay), several games, or a season reset.
        return None
    old_points = rank_points(
        previous_entry.get("tier"), previous_entry.get("rank_division"), previous_entry.get("league_points")
    )
    new_points = rank_points(current_entry.get("tier"), current_entry.get("rank"), current_entry.get("leaguePoints"))
    if old_points is None or new_points is None:
        return None
    delta = new_points - old_points
    # A loss can be 0 LP (demotion protection); a win never is.
    if (won and delta <= 0) or (not won and delta > 0):
        return None
    return {
        "queue_type": queue_type,
        "won": won,
        "delta": delta,
        "tier": str(current_entry.get("tier")).upper(),
        "rank": str(current_entry.get("rank") or "").upper(),
        "league_points": int(current_entry.get("leaguePoints")),
        "previous_tier": str(previous_entry.get("tier")).upper(),
        "previous_rank": str(previous_entry.get("rank_division") or "").upper(),
        "previous_league_points": int(previous_entry.get("league_points")),
        "observed_at": float(observed_at),
    }


def build_lp_history_row(riot_id, queue_type, previous_entry, current_entry):
    """Row for lp_history when a queue's rank, LP or win/loss counts changed, else None.

    Unlike build_lp_event this keeps changes that aren't one clean game (dodge/decay
    LP with no game, several games between polls, season resets) so they can be
    analysed or filtered later: `won` is only set for exactly one game.
    previous_entry uses the player_ranked_state row shape; current_entry is a raw
    League-V4 entry.
    """
    if not previous_entry or not current_entry:
        return None

    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    wins, losses = as_int(current_entry.get("wins")), as_int(current_entry.get("losses"))
    previous_wins, previous_losses = as_int(previous_entry.get("wins")), as_int(previous_entry.get("losses"))
    if None in (wins, losses, previous_wins, previous_losses):
        return None
    wins_delta, losses_delta = wins - previous_wins, losses - previous_losses
    tier = str(current_entry.get("tier") or "").upper() or None
    division = str(current_entry.get("rank") or "").upper() or None
    previous_tier = str(previous_entry.get("tier") or "").upper() or None
    previous_division = str(previous_entry.get("rank_division") or "").upper() or None
    old_points = rank_points(previous_tier, previous_division, previous_entry.get("league_points"))
    new_points = rank_points(tier, division, current_entry.get("leaguePoints"))
    lp_delta = new_points - old_points if old_points is not None and new_points is not None else None
    unchanged = (
        wins_delta == 0 and losses_delta == 0 and lp_delta == 0
        and (tier, division) == (previous_tier, previous_division)
    )
    if unchanged:
        return None
    won = {(1, 0): True, (0, 1): False}.get((wins_delta, losses_delta))
    return {
        "riot_id": riot_id,
        "queue_type": queue_type,
        "won": won,
        "wins_delta": wins_delta,
        "losses_delta": losses_delta,
        "lp_delta": lp_delta,
        "previous_tier": previous_tier,
        "previous_rank_division": previous_division,
        "previous_league_points": as_int(previous_entry.get("league_points")),
        "tier": tier,
        "rank_division": division,
        "league_points": as_int(current_entry.get("leaguePoints")),
        "wins": wins,
        "losses": losses,
    }


def format_lp_summary(event):
    """Recap LP text; the resulting LP and change are bold and kept on one line."""
    rank = format_rank({"tier": event["tier"], "rank": event["rank"]})
    delta = int(event["delta"])
    lp = int(event["league_points"])
    # Non-breaking space: mobile Discord shouldn't wrap between the total and the change.
    result_text = f"**{lp}LP\u00a0({delta:+d})**" if delta else f"**{lp}LP\u00a0(0)**"
    previous_lp = int(event.get("previous_league_points", lp - delta))
    previous_rank = format_rank({
        "tier": event.get("previous_tier", event["tier"]),
        "rank": event.get("previous_rank", event["rank"]),
    })
    if previous_rank != rank:
        return f"{previous_rank} {previous_lp}LP → {rank} {result_text}"
    # Events stored before this format change lack the old rank. Do not invent
    # a starting LP value across a division boundary.
    if "previous_league_points" not in event and (
        previous_lp < 0 or (event["tier"] not in APEX_TIERS and previous_lp >= 100)
    ):
        return f"{rank} · {result_text}"
    return f"{rank} · {previous_lp}LP → {result_text}"


def add_lp_to_content(content, heading, summary):
    """Append the LP summary to the recap heading line, or None if it can't be placed."""
    marker = f"{heading}\n"
    if content.count(marker) != 1:
        return None
    updated = content.replace(marker, f"{heading} · {summary}\n", 1)
    if len(updated) > MAX_MESSAGE_LENGTH:
        return None
    return updated


def event_matches_game(event, *, queue_type, won, end_ts):
    if event.get("queue_type") != queue_type or bool(event.get("won")) != bool(won):
        return False
    try:
        observed_at = float(event["observed_at"])
        end_ts = float(end_ts)
    except (KeyError, TypeError, ValueError):
        return False
    return end_ts - LP_CLOCK_SKEW_SECONDS <= observed_at <= end_ts + LP_MATCH_WINDOW_SECONDS


def load_state_list(raw):
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _prune(items, time_field, now):
    kept = []
    for item in items:
        try:
            if float(item[time_field]) >= now - LP_MATCH_WINDOW_SECONDS:
                kept.append(item)
        except (KeyError, TypeError, ValueError):
            continue
    return kept


async def _load(db_get_state, key, time_field, now):
    return _prune(load_state_list(await asyncio.to_thread(db_get_state, key)), time_field, now)


async def _save(db_set_state, key, items):
    await asyncio.to_thread(db_set_state, key, json.dumps(items))


async def take_lp_event_for_game(*, db_get_state, db_set_state, riot_id, queue_type, won, end_ts, now):
    """Remove and return a stored LP event for this game, if the rank worker saw it first."""
    key = lp_events_state_key(riot_id)
    async with LP_STATE_LOCK:
        events = await _load(db_get_state, key, "observed_at", now)
        matches = [e for e in events if event_matches_game(e, queue_type=queue_type, won=won, end_ts=end_ts)]
        if not matches:
            return None
        event = min(matches, key=lambda e: float(e["observed_at"]))
        events.remove(event)
        await _save(db_set_state, key, events)
        return event


async def register_pending_recap(*, db_get_state, db_set_state, riot_id, pending, now):
    """Record a posted recap line that still needs LP.

    If an event arrived while the recap was being sent, it is taken and returned
    instead, so the caller can edit the message right away.
    """
    event = await take_lp_event_for_game(
        db_get_state=db_get_state,
        db_set_state=db_set_state,
        riot_id=riot_id,
        queue_type=pending["queue_type"],
        won=pending["won"],
        end_ts=pending["end_ts"],
        now=now,
    )
    if event is not None:
        return event
    key = lp_pending_state_key(riot_id)
    async with LP_STATE_LOCK:
        items = await _load(db_get_state, key, "end_ts", now)
        items = [item for item in items if item.get("match_id") != pending["match_id"]]
        items.append(pending)
        await _save(db_set_state, key, items)
    return None


async def edit_recap_with_lp(message, heading, summary, *, component_id=None):
    """Edit the LP summary into a recap message; returns the updated message or None.

    Card recaps target the player's text component by ID; plain-text recaps (and card
    batches that fell back to text) match the player's heading line.
    """
    if component_id is not None:
        view = add_lp_to_card(message, component_id, heading, summary, add_lp_to_content)
        if view is None:
            return None
        edited = await message.edit(view=view)
        return edited if edited is not None else message
    updated = add_lp_to_content(message.content or "", heading, summary)
    if updated is None:
        return None
    edited = await message.edit(content=updated)
    return edited if edited is not None else message


def _pending_for_event(event, pending_items):
    matches = [
        item for item in pending_items
        if event_matches_game(event, queue_type=item.get("queue_type"), won=item.get("won"), end_ts=item.get("end_ts"))
    ]
    if not matches:
        return None
    # Counters moved by exactly one game, so the newest finished game is the one counted.
    return max(matches, key=lambda item: float(item["end_ts"]))


async def handle_lp_event(*, riot_id, event, recap_channel, db_get_state, db_set_state, log, now):
    """Attach an LP event to its already-posted recap, or store it for later."""
    pending_key = lp_pending_state_key(riot_id)
    events_key = lp_events_state_key(riot_id)
    async with LP_STATE_LOCK:
        pending_items = await _load(db_get_state, pending_key, "end_ts", now)
        pending = _pending_for_event(event, pending_items)
        # Without the recap channel the edit can't happen now; keep both sides so
        # retry_stored_lp_events can pair them once the channel resolves again.
        if pending is None or recap_channel is None:
            events = await _load(db_get_state, events_key, "observed_at", now)
            events.append(event)
            await _save(db_set_state, events_key, events)
            return False
        pending_items.remove(pending)
        await _save(db_set_state, pending_key, pending_items)

    return await _edit_pending_recap(riot_id, pending, event, recap_channel, log)


async def retry_stored_lp_events(*, riot_id, recap_channel, db_get_state, db_set_state, log, now):
    """Pair stored LP events with pending recaps (e.g. after the recap channel was unavailable)."""
    if recap_channel is None:
        return 0
    pending_key = lp_pending_state_key(riot_id)
    events_key = lp_events_state_key(riot_id)
    pairs = []
    async with LP_STATE_LOCK:
        events = await _load(db_get_state, events_key, "observed_at", now)
        if not events:
            return 0
        pending_items = await _load(db_get_state, pending_key, "end_ts", now)
        for event in sorted(events, key=lambda e: float(e["observed_at"])):
            pending = _pending_for_event(event, pending_items)
            if pending is None:
                continue
            pending_items.remove(pending)
            events.remove(event)
            pairs.append((pending, event))
        if not pairs:
            return 0
        await _save(db_set_state, pending_key, pending_items)
        await _save(db_set_state, events_key, events)

    edited = 0
    for pending, event in pairs:
        if await _edit_pending_recap(riot_id, pending, event, recap_channel, log):
            edited += 1
    return edited


async def _edit_pending_recap(riot_id, pending, event, recap_channel, log):
    try:
        async with LP_EDIT_LOCK:
            message = await recap_channel.fetch_message(int(pending["message_id"]))
            edited = await edit_recap_with_lp(
                message, pending["heading"], format_lp_summary(event), component_id=pending.get("component_id")
            )
    except Exception as exc:
        log(f"[rank] Could not add LP to recap for {riot_id} ({pending.get('match_id')}): {exc}")
        return False
    if edited is None:
        return False
    log(f"[rank] Added LP to recap for {riot_id} ({pending.get('match_id')}).")
    return True
