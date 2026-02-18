# MoodBot

MoodBot is a Discord bot that tracks League match mood for a tracked player list and posts a live scoreboard in one Discord channel.

## Current Structure

- `Bot.py` - compatibility launcher (calls `src.app.main()`)
- `src/app.py` - module entrypoint used in Railway and local runs
- `src/discord_bot.py` - main bot runtime (Discord handlers, Riot calls, refresh logic)
- `src/constants.py` - command constants
- `README.md` - project docs

## Features

- `!Mood` posts/updates a single scoreboard message.
- Rolling **last 24 hours** window (not calendar day).
- Per-player breakdown:
  - Total
  - Ranked Solo/Duo
  - Ranked Flex
  - Arcade
- Ranking based on Wilson lower bound.
- Displays `Gamer Score` (Wilson score x 100) per player.
- Background refresh stores/reuses data in Postgres.
- Persistent match cache and scoreboard message ID in Postgres.
- `!Add Name#Tag` adds players at runtime and persists to Postgres.
- `!DebugPlayer Name#Tag` prints queue/category mapping for recent games.

## Commands

- `!Mood`
- `!Add Name#Tag`
- `!DebugPlayer Name#Tag`
- `!test`
- `!riottest`

## Local Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.app
```

## Environment Variables

Required:

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DISCORD_CHANNEL_ID`
- `DATABASE_URL`

Optional:

- `RIOT_FRIENDS` (seed list when DB has no players)
- `REPORT_TIMEZONE` (default: `UTC`, example: `Europe/Oslo`)
- `LOG_RIOT_REQUESTS` (default: `false`)
- `MAX_MATCHES_PER_PLAYER` (default: `25`)
- `MAX_TODAY_MATCH_DETAILS` (default: `20`)
- `REPORT_CACHE_SECONDS` (default: `120`)
- `DAILY_REFRESH_SECONDS` (default: `300`)
- `DB_POOL_SIZE` (default: `5`)

## Railway

- `Procfile`: `worker: python -m src.app`
- `railway.json` start command: `python -m src.app`
- GitLab CI deploy uses Railway CLI from `.gitlab-ci.yml`

Runtime variables to set in Railway:

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DISCORD_CHANNEL_ID`
- `DATABASE_URL`
- Optional tuning variables from above
