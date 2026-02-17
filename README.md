# MoodBot

MoodBot is a Discord bot that tracks your group's League of Legends performance for today and posts a clean mood board in Discord.

It pulls live data from Riot's APIs, calculates each player's daily record, ranks everyone by win rate, and keeps no-match players at the bottom.

## What It Does

- Connects to Discord and listens in one configured channel.
- Fetches player account and match data from Riot API.
- Builds a "today only" report:
  - Separate win rates for `Ranked Solo/Duo`, `Ranked Flex`, and `Arcade`
  - Total win rate across all modes
  - Mood emoji scale (with skull at 0%)
- Sorts players from best to worst total win rate.
- Shows a `⭐` for the top-ranked player.
- Shows players with no matches today at the end.
- Responds to `!Mood` with:
  - A quick "working..." message
  - Then edits that message into the final formatted report
- Also posts a scheduled daily report at 20:00 local time.

## Commands

- `!Mood` - Generate and post today's ranked mood report.
- `!Add Name#Tag` - Validate and add a new tracked player at runtime.
- `!test` - Simple Discord send test.
- `!riottest` - Basic Riot connectivity test.

## Tech Stack

- Python 3.11+
- `discord.py`
- `requests`

## Run Locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
set DISCORD_TOKEN=your_token
set RIOT_API_KEY=your_riot_key
set DISCORD_CHANNEL_ID=your_channel_id
python Bot.py
```

## How The Report Is Calculated

1. Resolve each Riot ID (`gameName#tagLine`) to `puuid`.
2. Pull today's matches from Match-V5 (`startTime` filter + timezone-aware day cutoff).
3. Bucket each match into `Ranked Solo/Duo` (420), `Ranked Flex` (440), or `Arcade` (all other queues).
4. Count wins and losses per bucket and in total.
5. Compute total win rate and sort descending.
6. Append no-match players at the bottom.

## Configuration

Required environment variables:

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DISCORD_CHANNEL_ID`

Optional environment variables:

- `RIOT_FRIENDS` (comma-separated Riot IDs, for example `PlayerOne#EUW,PlayerTwo#NA1`)
- `PLAYERS_FILE` (path to persisted tracked players file, default: `tracked_players.txt`)
- `REPORT_TIMEZONE` (IANA timezone for daily cutoff, default: `UTC`, example: `Europe/Oslo`)
- `DATABASE_URL` (Postgres connection string; when set, players + puuids persist in Postgres)
- `DAILY_REFRESH_SECONDS` (background refresh interval when DB is enabled, default: `300`)

## Player Persistence

- Preferred: set `DATABASE_URL` (Railway Postgres).  
  - `!Add` persists `riot_id` in Postgres.
  - `puuid` is also stored in Postgres and reused across restarts/redeploys.
  - Startup loads tracked players from Postgres.
  - Background updater continuously refreshes today's per-player stats in Postgres.
  - `!Mood` reads those stored daily stats first for faster responses and fewer Riot requests.
- Fallback (when `DATABASE_URL` is not set): file-based `tracked_players.txt` via `PLAYERS_FILE`.
- `tracked_players.txt` is ignored by git.
- On Railway, file-based storage is ephemeral, so use Postgres for persistent state.

## Railway Deployment (GitLab CI)

This repository includes:

- `.gitlab-ci.yml` - deploy job for Railway on pushes to your default branch.
- `railway.json` - Railway start command and restart policy.
- `Procfile` - worker process declaration (`python Bot.py`).

Set these CI/CD variables in GitLab (`Settings -> CI/CD -> Variables`):

- `RAILWAY_TOKEN` (Railway token)
- `RAILWAY_SERVICE_ID` (target Railway service ID) or `RAILWAY_PROJECT_ID`
- `RAILWAY_ENVIRONMENT_ID` (optional, deploy target environment)

Set these runtime variables in Railway service settings:

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DISCORD_CHANNEL_ID`
- `RIOT_FRIENDS` (optional)
- `PLAYERS_FILE` (optional)
- `REPORT_TIMEZONE` (optional)
- `DATABASE_URL` (recommended for persistence across redeploys)
