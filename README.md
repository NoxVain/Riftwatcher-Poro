# MoodBot

MoodBot is a Discord bot for tracking ranked League of Legends performance for a list of tracked Riot IDs.
It maintains daily and weekly scoreboard messages, posts match recaps, and posts rank up/down alerts.

## Runtime Entry

- Run locally: `python -m src.app`
- Railway/Procfile worker: `python -m src.app`

## Project Structure

- `src/app.py` - process entrypoint
- `src/discord_bot.py` - runtime wiring, startup logs, worker scheduling
- `src/discord_command_handlers.py` - command routing for Discord messages
- `src/discord_text.py` - Discord text/render helpers
- `src/mood_service.py` - report orchestration and refresh logic
- `src/riot_api.py` - Riot API client, retries, match fetch/cache behavior
- `src/discord_recap_worker.py` - recap polling and recap -> stats sync
- `src/discord_rank_worker.py` - ranked state comparison and notifications
- `src/discord_backfill_worker.py` - low-priority historical cache backfill
- `src/db.py` - Postgres schema + persistence helpers
- `src/report_logic.py` - pure ranking/window helpers
- `src/rank_logic.py` - rank queue normalization and rank-change message formatting
- `tests/` - unit tests

## Features

- `!Mood` keeps a single scoreboard message updated in `DAILY_REPORT_CHANNEL_ID`.
- `!Week` keeps a single weekly scoreboard message updated in `WEEKLY_REPORT_CHANNEL_ID`.
- Daily window starts at `REPORT_DAY_START_HOUR` in `REPORT_TIMEZONE`.
- Weekly window aggregates existing daily stats from Monday through Friday in `REPORT_TIMEZONE`.
- Ranked queues tracked in report:
  - Solo/Duo (`420`)
  - Flex (`440`)
- Scoreboard rank ordering uses Wilson lower bound (displayed as `Gamer Score`).
- Match recap posts in `MATCH_RECAP_CHANNEL_ID` with:
  - queue + end time + match duration
  - champion/role
  - W/L, K/D/A, CS/min
  - player/objective damage, damage taken, healing, vision
- Rank alerts post in `EVENTS_CHANNEL_ID`:
  - rank up: congratulatory message
  - rank down: flame message
  - first-seen unranked -> ranked transitions are baseline only (no alert)
- Daily performance badges are computed from ranked matches only.

## Background Workers

All workers start with jitter to avoid bursty startup traffic and log cycle heartbeat timing.

- Refresh worker (`DAILY_REFRESH_SECONDS`, default `300`)
  - rebuilds daily stats
  - updates scoreboard snapshot with throttling
  - runs DB match-cache cleanup (`MATCH_CACHE_RETENTION_DAYS`)
- Rank worker (`max(30, DAILY_REFRESH_SECONDS)`)
  - compares current ranked entries vs persisted `player_ranked_state`
- Recap worker (`MATCH_RECAP_POLL_SECONDS`, default `90`)
  - detects newly finished matches and posts recaps
  - refreshes affected players' daily stats and forces scoreboard sync
- Backfill worker (`max(120, DAILY_REFRESH_SECONDS * 2)`)
  - only when no new matches are detected
  - fetches a limited number of older matches into DB cache
  - uses `cache_in_memory=False` to avoid polluting in-memory cache

## Commands

- `!Mood`
- `!Week`
- `!Add Name#Tag`
- `!DebugPlayer Name#Tag`
- `!health`
- `!test`
- `!riottest`

Commands are handled only in `DAILY_REPORT_CHANNEL_ID`.

## Environment Variables

Required:

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DATABASE_URL`
- `DAILY_REPORT_CHANNEL_ID`
- `MATCH_RECAP_CHANNEL_ID`
- `EVENTS_CHANNEL_ID`

Optional (with defaults):

- `REPORT_TIMEZONE` (`UTC`)
- `REPORT_DAY_START_HOUR` (`6`)
- `REPORT_CACHE_SECONDS` (`120`)
- `DAILY_REFRESH_SECONDS` (`300`)
- `MATCH_RECAP_POLL_SECONDS` (`90`)
- `MAX_TODAY_MATCH_DETAILS` (`100`)
- `MAX_MATCH_IDS_SCAN` (`2000`, set `0` for no cap)
- `MAX_IN_MEMORY_MATCH_CACHE` (`200`)
- `MATCH_CACHE_RETENTION_DAYS` (`730`, set `0` to disable cleanup)
- `DB_POOL_SIZE` (`5`)
- `LOG_RIOT_REQUESTS` (`false`)
- `LOG_JSON` (`false`)
- `RIOT_PLATFORM_ROUTING` (`euw1`)
- `WEEKLY_REPORT_CHANNEL_ID` (`DAILY_REPORT_CHANNEL_ID` when unset)

## Data Tables

- `tracked_players` - tracked Riot IDs + optional PUUID
- `player_daily_stats` - per-day wins/losses + performance aggregates
- `match_info_cache` - cached match payload JSON
- `bot_state` - generic state keys (message ids, last seen ids, flags)
- `player_ranked_state` - last known rank state per player/queue

## Testing

```bash
python -m pytest -q
```

On Windows, if `python` resolves to a WindowsApps alias, run tests with an explicit interpreter path:

```powershell
& 'C:\Users\gardf\AppData\Local\Python\bin\python.exe' -m pytest -q
```

## Operations Notes

- If `MAX_TODAY_MATCH_DETAILS` is high, refresh cycles can become long on heavy accounts.
- If Riot rate-limits (`429`), client retries with backoff.
- `RANK_ALERT_CHANNEL_ID` in logs is an alias of `EVENTS_CHANNEL_ID`.
- Warning when recap and daily channel IDs are equal is informational only.
