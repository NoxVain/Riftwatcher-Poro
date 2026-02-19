# Session Notes (2026-02-19)

## Service Snapshot

- Branch: `main`
- Runtime model: single Discord worker process (`python -m src.app`)
- Tracked player persistence: Postgres (`tracked_players`)
- Scoreboard model: one persisted message in `DAILY_REPORT_CHANNEL_ID`

## What Is Running

- Refresh worker: periodic full daily stats refresh + scoreboard sync + cache cleanup
- Rank worker: rank up/down detection against `player_ranked_state`
- Recap worker: new match recap posts + affected-player stats resync
- Backfill worker: low-priority older match DB cache backfill

All workers include startup jitter and per-cycle heartbeat logs:

- `Startup jitter sleep=...s`
- `Cycle complete elapsed=...ms next_sleep=...s`

## Current Defaults Worth Remembering

- `MAX_TODAY_MATCH_DETAILS=100`
- `MAX_MATCH_IDS_SCAN=2000`
- `MAX_IN_MEMORY_MATCH_CACHE=200`
- `MATCH_CACHE_RETENTION_DAYS=730`
- `DAILY_REFRESH_SECONDS=300`
- `MATCH_RECAP_POLL_SECONDS=90`
- `REPORT_CACHE_SECONDS=120`
- `REPORT_DAY_START_HOUR=6`

## Important Behavior Notes

- Rank alerts are only for ranked -> ranked level changes.
- Unranked -> ranked first observations are baseline only (no alert).
- Rank up message uses celebration copy.
- Rank down message uses flame copy with poop emoji.
- Backfill fetches use `cache_in_memory=False` so historical backfill does not fill in-memory cache.
- Snapshot update pushes are throttled to reduce Discord edit spam.

## Known Operational Characteristics

- High `MAX_TODAY_MATCH_DETAILS` increases refresh cycle time significantly for heavy accounts.
- Occasional Riot `429` is expected; retries/backoff are built in.
- `RANK_ALERT_CHANNEL_ID` in startup logs is an alias label for `EVENTS_CHANNEL_ID`.
- If recap/events share the same channel ID, message bursts can appear clustered.

## Quick Resume Checklist

1. Pull latest `main`.
2. Verify required env vars:
   - `DISCORD_TOKEN`
   - `RIOT_API_KEY`
   - `DATABASE_URL`
   - `DAILY_REPORT_CHANNEL_ID`
   - `MATCH_RECAP_CHANNEL_ID`
   - `EVENTS_CHANNEL_ID`
3. Start bot and confirm startup logs show all worker jitter lines.
4. Run smoke checks in Discord:
   - `!health`
   - `!Mood`
5. Verify heartbeat logs continue:
   - `refresh`, `rank`, `recap`, `backfill`

## Next Improvement Candidates

- Add per-worker success/error counters to health output.
- Add log sampling/structured metrics export for cycle latency trends.
