# MoodBot Architecture

## Purpose

MoodBot is a Discord worker service that tracks ranked League results for tracked Riot IDs and maintains:

- one live daily scoreboard message
- one live weekly scoreboard message (Monday-Friday aggregate)
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
  - command routing and command execution flow
- `src/discord_text.py`
  - pure text helpers (request id formatters, recap text helpers, report signatures)
- `src/mood_service.py`
  - report generation and cache policy
  - snapshot/live fallback logic
  - daily refresh orchestration
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
- `src/db.py`
  - connection pool
  - schema creation/migrations
  - persistence helpers
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
2. Command flow (`!Mood`)
   - fetch report (snapshot/cache/live as needed)
   - update persisted scoreboard message
3. Command flow (`!Week`)
   - aggregate stored `player_daily_stats` rows from Monday-Friday
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
- `match_info_cache`
  - cached Riot match payloads
- `bot_state`
  - key/value operational state:
    - scoreboard message ids
    - last seen match ids
    - recap dedupe keys
    - one-time Riot 401 alert flag
- `player_ranked_state`
  - last known rank state per `(riot_id, queue_type)`

## Channels

- `DAILY_REPORT_CHANNEL_ID`: scoreboard + command channel
- `MATCH_RECAP_CHANNEL_ID`: match recap posts
- `EVENTS_CHANNEL_ID`: rank/news events

All three are required and no channel fallback chain is used.

## Key Operational Behaviors

- Snapshot throttling avoids unnecessary Discord edits in refresh cycles.
- Oldest-data-first player ordering is used in full refresh cycles.
- Riot `401` triggers one persisted alert flag to prevent notification spam.
- Riot `429` retries are handled in the Riot client.
