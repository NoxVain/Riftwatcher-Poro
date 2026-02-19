# Session Notes (2026-02-18)

## Current Status
- Branch: `main`
- Remote: `origin/main` pushed and up to date
- Tests: `14 passed` (`C:\Users\gardf\AppData\Local\Python\bin\python.exe -m pytest -q`)
- Recent commits (latest first):
  - `627ddf6` `feat(stats): add healing and damage-taken to recaps and badges`
  - `246eb3a` `style(recap): remove compass emoji from role line`
  - `ffe9515` `feat(recap): include role with champion in match recaps`
  - `38faba8` `fix(sync): rebuild affected daily stats after recap detection`
  - `4b366e2` `fix(sync): refresh daily scoreboard after recap match detection`
  - `8a9abb4` `fix(report): restore daily emoji rendering with unicode escapes`
  - `de4ba62` `style(report): switch damage leader badge to boom emoji`
  - `c25ebe3` `feat(recap): improve match recap layout and readability`

## Runtime Behavior (Current)
- Scoreboard is a single persisted message in `DISCORD_CHANNEL_ID`.
- Report window is daily cycle-based, starting at `REPORT_DAY_START_HOUR` in `REPORT_TIMEZONE` (default `06:00`).
- Scoring/reporting is ranked-only (`420` solo/duo and `440` flex).
- `!Mood` flow:
  - serves a stored snapshot quickly
  - performs incremental refresh for recent matches
  - updates scoreboard only if content changed
- Background refresh (`DAILY_REFRESH_SECONDS`) still runs for full consistency and window roll-off.

## Recap Channel + Daily Sync
- If `MATCH_RECAP_CHANNEL_ID` is set, bot posts grouped recaps for newly detected matches.
- Recap now includes per-player:
  - result (W/L), role, champion
  - K/D/A, CS/min
  - player damage, objective damage, damage taken, healing, vision
- When recap posts new match(es), bot now recomputes and upserts daily stats for affected tracked players, invalidates cache, and forces scoreboard edit.
- Recap dedupe key in `bot_state`:
  - `last_announced_match_id::<riot_id lower>`

## Daily Leader Badges
- Daily badge leaders are calculated from ranked matches only:
  - `🌾` best CS/min
  - `🏰` best objective damage
  - `💥` best player damage
  - `❤️` most healing
  - `🛡️` most damage taken
  - `🗡️` most kills
  - `☠️` most deaths
  - `👁️` best vision score
- Ties share badges.

## Data / Schema Notes
- `player_daily_stats` now stores:
  - `cs_total`, `minutes_total`, `objective_damage`, `player_damage`
  - `healing`, `damage_taken`
  - `kills`, `deaths`, `vision_score`
- Startup migrations are handled with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `src/db.py`.

## Important Env Vars
- Required:
  - `DISCORD_TOKEN`
  - `RIOT_API_KEY`
  - `DISCORD_CHANNEL_ID`
  - `DATABASE_URL`
- Core tuning:
  - `REPORT_TIMEZONE`
  - `REPORT_DAY_START_HOUR` (default `6`)
  - `MAX_TODAY_MATCH_DETAILS` (default `20`)
  - `MAX_MATCH_IDS_SCAN` (default `2000`; `0` means no cap)
  - `MAX_IN_MEMORY_MATCH_CACHE` (default `200`)
  - `REPORT_CACHE_SECONDS` (default `120`)
  - `DAILY_REFRESH_SECONDS` (default `300`)
  - `MATCH_CACHE_RETENTION_DAYS` (default `730`; `0` means no DB cache cleanup)
  - `MATCH_RECAP_CHANNEL_ID` (optional)
  - `MATCH_RECAP_POLL_SECONDS` (default `90`)

## Quick Resume Checklist
1. `git pull`
2. Confirm Railway env vars (required + recap vars if enabled).
3. Run tests:
   - `C:\Users\gardf\AppData\Local\Python\bin\python.exe -m pytest -q`
4. Discord smoke test:
   - `!health`
   - `!Mood`
   - play a new match and verify:
     - recap message includes role/champion + new stats
     - daily scoreboard updates shortly after recap post

## Known Environment Quirks
- Sandbox git pushes can fail on GitLab port 443; retry with escalated network permissions.
- CRLF normalization warnings appear in git on this Windows setup (non-blocking).
# Session Notes (2026-02-19)

## Current Status
- Branch: `main`
- Refactor status: `src/discord_bot.py` split to reduce runtime-file coupling and size
  - New module: `src/discord_command_handlers.py`
  - New module: `src/discord_text.py`
- Docs status:
  - Added `ARCHITECTURE.md` with runtime/data-flow map
  - Updated this notes file with latest structure summary

## Structural Changes (No Intended Behavior Change)
- Moved message-command handling from `src/discord_bot.py` into:
  - `handle_incoming_message(...)` in `src/discord_command_handlers.py`
- Moved pure text/helper functions into `src/discord_text.py`:
  - request id generation
  - report signature normalization
  - recap state key and recap formatting helpers
- `src/discord_bot.py` now focuses on:
  - dependency setup
  - Discord event registration
  - background workers
  - persisted scoreboard message lifecycle

## Validation
- Unit tests:
  - `C:\Users\gardf\AppData\Local\Python\bin\python.exe -m pytest -q`
- Expected baseline remains:
  - `14 passed`

## New Runtime Behavior (2026-02-19)
- Rank change monitoring now runs during background refresh cycles.
- Rank state is persisted per player/queue in `player_ranked_state`.
- On rank up/down, bot posts a message in `DISCORD_CHANNEL_ID`.
- Platform host for league/summoner APIs is configurable via:
  - `RIOT_PLATFORM_ROUTING` (default `euw1`)
