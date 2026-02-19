# MoodBot Architecture

## Purpose
MoodBot is a Discord worker bot that tracks ranked League results for a list of tracked Riot IDs and maintains a single live daily scoreboard message in Discord.

## Runtime Layers
- `src/app.py`
  - Minimal process entrypoint (`python -m src.app`), delegates to Discord runtime startup.
- `src/discord_bot.py`
  - Bootstraps config and dependencies.
  - Initializes Discord client/event handlers.
  - Owns background task scheduling (daily refresh + optional recap polling).
  - Persists and updates the single scoreboard message.
- `src/discord_command_handlers.py`
  - Command-routing handler for channel messages (`!Mood`, `!Add`, `!health`, etc.).
  - Keeps Discord event handling logic separated from boot/runtime wiring.
- `src/discord_text.py`
  - Stateless text/format helpers (request IDs, report signatures, recap rendering).
- `src/mood_service.py`
  - Application orchestration layer for report generation and refresh flows.
  - Handles cache policy, snapshot/live fallback behavior, and health summary.
- `src/riot_api.py`
  - Riot API client with retry handling for rate limits and optional request logging.
  - Resolves Riot IDs to PUUIDs and fetches match list/details.
  - Maintains in-memory and DB-backed match-info caching.
- `src/report_logic.py`
  - Pure business logic for queue bucketing, report cycle windows, Wilson scoring, ranking sort keys, and helper formatting.
- `src/db.py`
  - Postgres connection pool and persistence operations.
  - Startup DDL/migrations for tracked players, daily stats, match cache, and bot state.
- `src/config.py`
  - Environment loading and validation for required and optional settings.

## Data Flow
1. Startup
   - `src/discord_bot.py` loads config, initializes DB schema, loads tracked players, and constructs `RiotApiClient` + `MoodService`.
   - Bot resolves target channel and initializes or refreshes the persisted scoreboard message.
2. On-demand report (`!Mood`)
   - Command handler serves a fast snapshot from stored daily stats.
   - Then performs incremental recent-match refresh and updates scoreboard if content changed.
3. Background daily refresh
   - Periodically rebuilds daily stats per tracked player from Riot data.
   - Pushes snapshot updates to the persisted scoreboard message.
   - Cleans old `match_info_cache` rows on interval.
4. Optional recap worker
   - Polls tracked players for newly seen matches.
   - Posts recap messages in `MATCH_RECAP_CHANNEL_ID`.
   - Recomputes affected players’ daily stats and forces scoreboard sync.

## Report Window and Scoring
- Daily cycle key uses `REPORT_TIMEZONE` and `REPORT_DAY_START_HOUR` (default 06:00).
- Ranked queues only:
  - `420` -> Solo/Duo
  - `440` -> Flex
- Ranking is based on Wilson lower bound (default `z=1.28`), displayed as Gamer Score (`wilson * 100`).

## State and Persistence
- `tracked_players`
  - Canonical tracked Riot IDs and optional persisted PUUID.
- `player_daily_stats`
  - Per-cycle wins/losses and performance aggregates for badges/recaps.
- `match_info_cache`
  - Cached match payloads to reduce Riot API pressure.
- `bot_state`
  - Generic key/value store for persisted message IDs, last-seen/announced match IDs, and alert flags.

## Commands
- `!Mood`: build/update daily scoreboard message.
- `!Add Name#Tag`: validate and persist tracked player.
- `!DebugPlayer Name#Tag`: queue/window diagnostics for recent matches.
- `!health`: uptime + DB/cache health.
- `!test`: Discord API send-path smoke test.
- `!riottest`: Riot connectivity smoke test on a tracked player.

## Testing
- `tests/test_report_logic.py`: queue mapping, ranking, cycle boundary/window behavior.
- `tests/test_mood_service.py`: match delta detection and badge leader assignment.

## Operational Notes
- The bot is designed as a worker process (Procfile/Railway both run `python -m src.app`).
- If Riot returns `401 Unauthorized`, the runtime sends a one-time Discord alert and records state to avoid duplicate spam.
