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
- Adds per-player daily leader badges (ranked games only):
  - `🌾` best CS/min
  - `🏰` best objective damage
  - `💥` best player damage
  - `🗡️` most kills
  - `☠️` most deaths
  - `👁️` best vision score
