# Self-Hosting Riftwatcher Poro

Riftwatcher Poro is designed to run as one bot instance per Discord server/community. Each self-hosted deployment uses its own Discord bot token, Riot API key, Postgres database, channel IDs, and tracked player list.

Riftwatcher Poro is source-available for non-commercial use under the PolyForm Noncommercial License 1.0.0. Commercial use requires separate permission from the project maintainer.

## Before You Start

- Python 3.14 (the version in `.python-version`, which Railway also uses)
- A Discord server where you can manage channels and invite bots
- A Discord Developer Portal application
- A Riot Developer Portal API key
- A Postgres database
- A hosting service for a long-running worker, such as Railway, Render, Fly.io, or a VPS

Riftwatcher Poro is not endorsed by Riot Games. Keep the Riot disclaimer from `README.md` visible if you publish your own fork or deployment instructions. Each operator must provide their own Riot API key and is responsible for complying with Riot Developer Portal policies.

## 1. Create The Discord Bot

1. Go to the Discord Developer Portal.
2. Create an application.
3. Add a bot user.
4. Copy the bot token into `DISCORD_TOKEN`.
5. Enable **Message Content Intent** for the bot.
6. Invite the bot with the `bot` and `applications.commands` scopes (the latter is needed for `/health`) and these permissions:
   - View Channels
   - Send Messages
   - Read Message History
   - Send TTS Messages (optional, for streak callouts with TTS)
   - Manage Messages (optional; lets the bot delete `!Daily`/`!Weekly` command messages to keep scoreboard channels clean)

Riftwatcher Poro currently uses `!commands`, so Message Content Intent is required. The bot never pings: all outgoing mentions are suppressed.

## 2. Create Discord Channels

Create or choose channels for:

- Daily scoreboard
- Weekly scoreboard
- Match recaps
- Events/admin commands (also receives rank alerts and League patch notes)
- Esports news (optional)

Enable Developer Mode in Discord, right-click each channel, and copy the channel IDs into:

- `DAILY_REPORT_CHANNEL_ID`
- `WEEKLY_REPORT_CHANNEL_ID`
- `MATCH_RECAP_CHANNEL_ID`
- `EVENTS_CHANNEL_ID`
- `NEWS_CHANNEL_ID` (optional; leave empty to disable esports news)

The same channel can be reused for multiple roles, but separate channels are easier to operate.

## 3. Create A Riot API Key

1. Go to the Riot Developer Portal.
2. Create or use a Riot API key.
3. Put it in `RIOT_API_KEY`.

For a private Discord/community bot, a personal key may be enough. Riot development keys expire every 24 hours and are not suitable for unattended deployments. Public products require Riot product registration and a production API key.

## 4. Configure Environment Variables

Copy `.env.example` and fill in values:

```powershell
Copy-Item .env.example .env
```

Required:

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DATABASE_URL`
- `DAILY_REPORT_CHANNEL_ID`
- `WEEKLY_REPORT_CHANNEL_ID`
- `MATCH_RECAP_CHANNEL_ID`
- `EVENTS_CHANNEL_ID`

Recommended for EUW communities:

```env
REPORT_TIMEZONE=Europe/Oslo
RIOT_PLATFORM_ROUTING=euw1
RIOT_REGIONAL_ROUTING=europe
```

Optional but commonly used:

- `MANUAL_COMMANDS_CHANNEL_ID` - channel where commands run (`!analyze`, `!records`, `!help`, admin commands, `/health`), so the events channel keeps only automatic posts. Unset = `EVENTS_CHANNEL_ID`.
- `ADMIN_ROLE_ID` - role allowed to run `!Add`/`!remove`/`!backfill`/`!tts on|off` (members with Manage Server or Administrator always can).
- `NEWS_CHANNEL_ID` + `ESPORTS_API_KEY` - enable esports news. `ESPORTS_API_KEY` is the `x-api-key` header lolesports.com sends to `esports-api.lolesports.com` (browser DevTools -> Network). Pick leagues with `ESPORTS_LEAGUES` (default `worlds`).
- `PATCH_NOTES_ENABLED` (default `true`), `PATCH_NOTES_POLL_SECONDS` (default `21600` = 6h), `PATCH_NOTES_LOCALE` (default `en-us`) - League patch-notes posts in `EVENTS_CHANNEL_ID`. No key needed; about 4 small page requests per day.
- Low-resource hosts (NAS/IoT): see the commented block in `.env.example` (`STARTUP_PREFER_SNAPSHOT`, `BASELINE_MATCH_LIMIT`, `RIOT_REQUEST_YIELD_SECONDS`, `WORKER_STAGGER_SECONDS`, `ASYNCIO_THREAD_POOL_SIZE`).

See `README.md` for every variable and its default.

## 5. Deploy On Railway

1. Fork or clone the repository.
2. Create a new Railway project from the repository.
3. Add a Postgres service.
4. Set all variables from `.env.example` in Railway Variables.
5. Ensure `DATABASE_URL` points to the Railway Postgres service.
6. Deploy.

In the service's **Settings**, set the start command to `python -m src.app` and the restart policy to `Always`. (The upstream deployment manages these through `.railway/railway.ts`, which names the upstream project and repository; to use it for your own deployment, change those names and run `railway config apply`. See `OPERATIONS.md` → CI & Deploy.) The `Procfile` provides the same command for Procfile-based hosts:

```text
Procfile -> worker: python -m src.app
```

The database schema is created and migrated automatically on startup.

## 6. Run Locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.app
```

On Windows, if `python` resolves to a WindowsApps alias, use the explicit Python executable installed on your system.

## 7. First-Run Checks

In the events channel:

```text
!test
!riottest
!health
```

As a server Administrator, run `!sync` once (any channel) so the `/health` slash command shows up immediately.

Use `!health` or `/health` regularly to monitor your Riot API key status, database connection, and background worker performance.

Add tracked players (requires Manage Server, Administrator, or `ADMIN_ROLE_ID`):

```text
!Add Name#Tag
```

Then smoke test:

```text
!Daily
!Weekly
!tts status
```

## Operations Notes

- The first recap cycle for a player baselines the latest match instead of posting old backlog. The same applies to rank alerts and the first esports poll per league.
- Recap worker runs every `MATCH_RECAP_POLL_SECONDS` seconds (minimum 30), default `90`.
- Daily refresh runs every `DAILY_REFRESH_SECONDS` seconds (minimum 30), default `300`.
- If Riot rejects the key (`401`/`403`), update `RIOT_API_KEY` and redeploy (see `OPERATIONS.md`). The bot posts a recovery message in the events channel once the new key works.
- If the bot posts duplicate scoreboards or esports updates, check channel permissions, make sure only one instance uses the database, and see `OPERATIONS.md`.
- Do not commit `.env`, bot tokens, Riot API keys, esports API keys, or database URLs.
