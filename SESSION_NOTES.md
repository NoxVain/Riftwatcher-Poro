# Session Notes (2026-04-04)

## Current Snapshot

- Branch: `main`
- Runtime entrypoint: `python -m src.app`
- DB mode: Postgres required (`DATABASE_URL`)
- Test status: `126 passed` (`python -m pytest -q`)

## Current Architecture

- Commands are modular:
  - `src/discord_command_handlers.py` entrypoint
  - `src/commands/` for routing + handlers
- DB is modular:
  - `src/db/` (`pool`, `schema`, `state`, `players`, `cache`, `stats`, `ranked_state`)
- Runtime loop logic is modular:
  - `src/runtime/workers.py`, `src/runtime/message_store.py`, `src/runtime/alerts.py`
- Mood service is a facade:
  - `src/mood_service.py` delegating to `src/services/`

## Behavior That Is Live

- Daily board uses two messages in `DAILY_REPORT_CHANNEL_ID`:
  - previous-day snapshot
  - current-day live report
- Weekly board is maintained in `WEEKLY_REPORT_CHANNEL_ID`.
- Recap worker posts:
  - recap messages in `MATCH_RECAP_CHANNEL_ID`
  - streak callouts as separate messages (not merged into recap batches)
- Streak TTS:
  - default ON
  - toggle with `!tts on|off|status`
  - command allowed in `EVENTS_CHANNEL_ID` and `MATCH_RECAP_CHANNEL_ID`
- `!streak` is enforced to recap channel routing (bare and with Riot ID arg).
- `!profile Name#Tag` is available in `EVENTS_CHANNEL_ID`.
- `!remove Name#Tag` is available in `EVENTS_CHANNEL_ID`.
- `!backfill YYYY-MM-DD YYYY-MM-DD` is available in `EVENTS_CHANNEL_ID`:
  - rebuilds historical `player_daily_stats` from `match_info_cache` only
  - does not call Riot APIs
  - max range per run is 366 days

## Reliability / Correctness Fixes Completed Recently

- Fixed previous-day header text corruption in runtime message store.
- Removed stale duplicate previous-day helper functions from `discord_bot.py`.
- Decoupled refresh cleanup runtime state from report message state.
- Hardened Riot 401 alert flow:
  - alert is marked sent only after successful Discord send.
- Added direct tests for runtime modules and refresher cleanup branch.
- Added command routing matrix tests.
- Added recap integration coverage for recap/streak separation + TTS toggle.
- Tightened GitLab CI test trigger rules:
  - tests run on `push` and `merge_request_event`.
- Added worker latency metrics export in `!health` (`last/avg/max`).
- Added stalled-worker watchdog alerts + recovery notices with dedupe state.
- Updated scoring implementation:
  - stricter Wilson confidence
  - weighted performance percentile
  - performance-weight ramp by game count
- Expanded `player_daily_stats` data model and write/read flows:
  - assists, gold, wards, objective takedowns
  - multi-kills (double/triple/quadra/penta)
  - kill participation numerator/denominator
- Added one-shot historical cache backfill command (`!backfill`) for rebuilding old daily rows.
- Prevented duplicate daily/weekly scoreboard posts on transient Discord message fetch failures:
  - keep tracked message IDs on `discord.HTTPException`
  - reset IDs only on `NotFound`/`Forbidden`

## CI/Deploy Notes

- `test` job runs on push + MR.
- `deploy_to_railway` runs on default-branch commits only when deploy-relevant files changed, or with `FORCE_DEPLOY=1`.
- Test-only commits do not auto-deploy by design.

## Required Env Vars

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DATABASE_URL`
- `DAILY_REPORT_CHANNEL_ID`
- `WEEKLY_REPORT_CHANNEL_ID`
- `MATCH_RECAP_CHANNEL_ID`
- `EVENTS_CHANNEL_ID`

## Quick Re-entry Checklist

1. Pull latest `main`.
2. Confirm required env vars are set.
3. Run local tests: `python -m pytest -q`.
4. Start bot: `python -m src.app`.
5. Smoke check in Discord:
   - `!health` (events channel)
   - `!Daily` (daily channel)
   - `!Weekly` (weekly channel)
   - `!tts status` (events or recap channel)
   - `!streak Name#Tag` (recap channel)
   - `!profile Name#Tag` (events channel)
   - `!remove Name#Tag` (events channel)
   - `!backfill YYYY-MM-DD YYYY-MM-DD` (events channel)

## Docs Index

- Runtime + setup: `README.md`
- Technical structure: `ARCHITECTURE.md`
- Ops runbook: `OPERATIONS.md`
- Improvement list: `IMPROVEMENT_BACKLOG.md`
