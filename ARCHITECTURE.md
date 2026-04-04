# MoodBot Architecture

## Purpose

MoodBot is a Discord worker service that tracks ranked League results for tracked Riot IDs and maintains:

- two daily scoreboard messages in the daily channel:
  - previous day snapshot message (top)
  - current day live scoreboard message (second)
- one live weekly scoreboard message (Monday 06:00 -> next Monday 06:00 aggregate)
- match recap messages
- rank change event messages

## Runtime Layers

- `src/app.py`
  - process entrypoint (`python -m src.app`)
- `src/discord_bot.py`
  - dependency wiring
  - Discord client events
  - background worker scheduling
  - startup logging and scoreboard lifecycle
- `src/discord_command_handlers.py`
  - command entrypoint that delegates into modular handlers
- `src/commands/`
  - command context, routing/channel policy, and split handlers (`ops`, `player`, `report`)
- `src/discord_text.py`
  - pure text helpers (request id formatters, recap text helpers, report signatures)
- `src/mood_service.py`
  - service facade for report generation/cache policy
  - delegates refresh/report internals to `src/services/`
- `src/services/`
  - `report_builder.py`, `refresh.py`, `baselines.py`
- `src/riot_api.py`
  - Riot API calls (account/summoner/league/matches)
  - retry/backoff behavior
  - in-memory + DB match cache integration
- `src/discord_recap_worker.py`
  - new-match detection and recap posting
  - post-recap stats resync
- `src/discord_rank_worker.py`
  - rank-state diffing and alert posting
- `src/discord_backfill_worker.py`
  - low-priority older-match DB cache fill
- `src/runtime/`
  - shared runtime worker loops, message-store helpers, and alert helpers
- `src/db/`
  - connection pool (`pool.py`)
  - schema creation/migrations (`schema.py`)
  - persistence helpers split by concern (`state.py`, `players.py`, `cache.py`, `stats.py`, `ranked_state.py`)
- `src/report_logic.py`
  - pure queue/window/ranking helpers
- `src/rank_logic.py`
  - rank normalization/comparison and rank-message text

## Worker Model

All workers run continuously while connected, each with startup jitter and cycle heartbeat logs.

- Refresh worker
  - interval: `max(30, DAILY_REFRESH_SECONDS)`
  - rebuilds daily stats for all tracked players
  - pushes snapshot scoreboard updates with throttling
  - runs DB match cache cleanup on interval
- Rank worker
  - interval: `max(30, DAILY_REFRESH_SECONDS)`
  - compares current ranked entries with `player_ranked_state`
  - sends rank up/down messages to `EVENTS_CHANNEL_ID`
  - first observation is baseline only (no alert)
- Recap worker
  - interval: `max(30, MATCH_RECAP_POLL_SECONDS)`
  - finds new matches using persisted per-player recap key
  - posts recap messages in `MATCH_RECAP_CHANNEL_ID` (queue, local end time, duration, per-player line items)
  - posts win/loss streak callouts in `MATCH_RECAP_CHANNEL_ID` as separate messages (3-4: Momentum/Cold Streak, 5-7: Heater Alert/Tilt Watch, 8+: LEGENDARY/FULL TILT); deduped by `last_announced_streak::{riot_id}` token
  - streak callouts use Discord TTS by default; toggle with `!tts on|off|status`
  - lookback window: 20 matches
  - refreshes affected players' daily stats and forces scoreboard update
- Backfill worker
  - interval: `max(120, DAILY_REFRESH_SECONDS * 2)`
  - when no new matches are detected, backfills a small number of older match payloads
  - stores only in DB cache for backfill fetches (`cache_in_memory=False`)

## Data Flow

1. Startup
   - load env/config
   - initialize DB schema
   - load tracked players
   - construct Riot client + mood service
   - initialize scoreboard message and schedule workers
2. Command flow (`!Daily`)
   - fetch report (snapshot/cache/live as needed)
   - ensure both daily tracked messages exist
   - update current-day message
   - on day rollover, copy prior day final content into previous-day message (dated header), then continue updating current-day message
3. Command flow (`!Weekly`)
   - aggregate stored `player_daily_stats` rows from Monday day-start cutoff through next Monday cutoff
   - update persisted weekly scoreboard message
4. Refresh flow
   - pull ranked match info for current report day
   - upsert per-player daily stats
   - push daily snapshot scoreboard updates during/after cycle
   - refresh weekly scoreboard snapshot from DB aggregates
5. Recap flow
   - detect newly seen matches
   - post recap text
   - resync daily stats for affected players
   - refresh daily + weekly scoreboard messages
6. Rank flow
   - load persisted ranked baseline
   - fetch live ranked entries
   - post up/down messages if rank level changed
   - persist new baseline

## Persistence Model

- `tracked_players`
  - source of tracked Riot IDs and PUUID cache
- `player_daily_stats`
  - per cycle/day wins/losses and aggregate performance stats
  - includes expanded fields: assists, gold, wards, objective takedowns, multi-kills, and KP numerator/denominator
- `match_info_cache`
  - cached Riot match payloads
- `bot_state`
  - key/value operational state:
    - scoreboard message ids (including daily current + daily previous)
    - daily cycle keys for rollover
    - last seen match ids
    - recap dedupe keys
    - one-time Riot 401 alert flag
- `player_ranked_state`
  - last known rank state per `(riot_id, queue_type)`

## Channels

- `DAILY_REPORT_CHANNEL_ID`: daily scoreboard + `!Daily` command
- `WEEKLY_REPORT_CHANNEL_ID`: weekly scoreboard + `!Weekly` command
- `EVENTS_CHANNEL_ID`: ops/admin commands (`!help`, `!health`, `!score`, `!test`, `!riottest`, `!Add`, `!remove`, `!DebugPlayer`, `!profile`, `!backfill`, `!tts`)
- `MATCH_RECAP_CHANNEL_ID`: match recap posts, streak callouts, `!streak` and `!tts` commands

All channels are required and no channel fallback chain is used.

## Key Operational Behaviors

- Snapshot throttling avoids unnecessary Discord edits in refresh cycles.
- Oldest-data-first player ordering is used in full refresh cycles.
- Daily and weekly tracked message references survive transient Discord fetch errors; message state is reset only on `NotFound`/`Forbidden`.
- Riot `401` triggers one persisted alert flag to prevent notification spam.
- Riot `429` retries are handled in the Riot client.
