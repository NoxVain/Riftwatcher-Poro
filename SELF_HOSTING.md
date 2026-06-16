# Self-Hosting MoodBot

MoodBot is designed to run as one bot instance per Discord server/community. Each self-hosted deployment uses its own Discord bot token, Riot API key, Postgres database, channel IDs, and tracked player list.

## Before You Start

- Python 3.11+
- A Discord server where you can manage channels and invite bots
- A Discord Developer Portal application
- A Riot Developer Portal API key
- A Postgres database
- A hosting service for a long-running worker, such as Railway, Render, Fly.io, or a VPS

MoodBot is not endorsed by Riot Games. Keep the Riot disclaimer from `README.md` visible if you publish your own fork or deployment instructions.

## 1. Create The Discord Bot

1. Go to the Discord Developer Portal.
2. Create an application.
3. Add a bot user.
4. Copy the bot token into `DISCORD_TOKEN`.
5. Enable **Message Content Intent** for the bot.
6. Invite the bot to your server with permissions to:
   - View channels
   - Send messages
   - Read message history
   - Manage messages is not required
   - Use TTS if you want streak callouts with TTS

MoodBot currently uses `!commands`, so Message Content Intent is required.

## 2. Create Discord Channels

Create or choose channels for:

- Daily scoreboard
- Weekly scoreboard
- Match recaps
- Events/admin commands

Enable Developer Mode in Discord, right-click each channel, and copy the channel IDs into:

- `DAILY_REPORT_CHANNEL_ID`
- `WEEKLY_REPORT_CHANNEL_ID`
- `MATCH_RECAP_CHANNEL_ID`
- `EVENTS_CHANNEL_ID`

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

## 5. Deploy On Railway

1. Fork or clone the repository.
2. Create a new Railway project from the repository.
3. Add a Postgres service.
4. Set all variables from `.env.example` in Railway Variables.
5. Ensure `DATABASE_URL` points to the Railway Postgres service.
6. Deploy.

Railway uses:

```text
Procfile -> worker: python -m src.app
```

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

Add tracked players:

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

- The first recap cycle for a player can baseline the latest match instead of posting old backlog.
- Recap worker runs every `MATCH_RECAP_POLL_SECONDS` seconds, default `90`.
- Daily refresh runs every `DAILY_REFRESH_SECONDS` seconds, default `300`.
- If Riot returns `401`, update `RIOT_API_KEY` and redeploy.
- If the bot posts duplicate scoreboards, check channel permissions and `OPERATIONS.md`.
- Do not commit `.env`, bot tokens, Riot API keys, or database URLs.
