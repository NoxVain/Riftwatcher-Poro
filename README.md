# MoodBot

MoodBot is a Discord bot that tracks League match mood for a tracked player list and posts a live scoreboard in one Discord channel.

## Current Structure

- `src/app.py` - module entrypoint used by Railway
- `src/discord_bot.py` - Discord runtime and command handlers
- `src/config.py` - environment/config loading
- `src/db.py` - Postgres pool and persistence layer
- `src/riot_api.py` - Riot API client (account, match list, match details, retries)
- `src/mood_service.py` - report building, snapshot/cache flow, refresh orchestration
- `src/report_logic.py` - pure ranking and formatting helpers
- `src/constants.py` - command constants
- `tests/test_report_logic.py` - unit tests for ranking/window logic helpers
- `README.md` - project docs

## Features

- `!Mood` posts/updates a single scoreboard message.
- Rolling daily window starting at **06:00** in `REPORT_TIMEZONE`.
- Per-player breakdown:
  - Total
  - Ranked Solo/Duo
  - Ranked Flex
- Ranking based on Wilson lower bound (`z=1.28`).
- Displays `Gamer Score` (Wilson score x 100) per player.
- Optional new-match recap posts include:
  - Role, champion, W/L, K/D/A
  - CS/min, player damage, objective damage
  - Damage taken, healing, vision
- Ranked change alerts in the report channel:
  - Congratulates rank ups
  - Calls out rank downs
- Adds per-player daily leader badges (ranked games only):
  - `🌾` best CS/min
  - `🏰` best objective damage
  - `💥` best player damage
  - `❤️` most healing
  - `🛡️` most damage taken
  - `🗡️` most kills
  - `☠️` most deaths
  - `👁️` best vision score

## Feature Suggestions

### Gamer Score v2 (Ranking Improvements)

- Add Bayesian smoothing for win rate to reduce low-game volatility.
  - Example: `p_bayes = (wins + a) / (games + a + b)`, with `a=b=3`.
- Keep an uncertainty penalty, but smoother.
  - Example: `conf = sqrt(games / (games + k))`, with `k=8`.
- Add a role-aware performance component using tracked metrics:
  - CS/min, player damage, objective damage, vision, deaths.
  - Normalize by role/day baselines to reduce role bias.
- Add recency weighting so recent games matter more.
- Use a composite score:
  - `GamerScore = 100 * (0.65 * p_bayes * conf + 0.25 * perf_norm + 0.10 * recency_norm)`

## Commands

- `!Mood`
- `!Add Name#Tag`
- `!DebugPlayer Name#Tag`
- `!health`
- `!test`
- `!riottest`

## Environment Variables

Required:

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DISCORD_CHANNEL_ID`
- `DATABASE_URL`

Optional:

- `REPORT_TIMEZONE` (default: `UTC`, example: `Europe/Oslo`)
- `LOG_RIOT_REQUESTS` (default: `false`)
- `LOG_JSON` (default: `false`)
- `MAX_TODAY_MATCH_DETAILS` (default: `20`)
- `REPORT_DAY_START_HOUR` (default: `6`)
- `MAX_MATCH_IDS_SCAN` (default: `2000`; set `0` for no cap)
- `MAX_IN_MEMORY_MATCH_CACHE` (default: `200`)
- `REPORT_CACHE_SECONDS` (default: `120`)
- `DAILY_REFRESH_SECONDS` (default: `300`)
- `MATCH_RECAP_CHANNEL_ID` (optional, enables recap posts)
- `MATCH_RECAP_POLL_SECONDS` (default: `90`)
- `DB_POOL_SIZE` (default: `5`)
- `MATCH_CACHE_RETENTION_DAYS` (default: `730`; set `0` for no DB cache cleanup)
- `RIOT_PLATFORM_ROUTING` (default: `euw1`)

## Tests

```bash
pytest -q
```

## Railway

- `Procfile`: `worker: python -m src.app`
- `railway.json` start command: `python -m src.app`
