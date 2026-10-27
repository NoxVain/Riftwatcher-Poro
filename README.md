# Riftwatcher Poro

<p align="center">
  <img src="assets/Riftwatcher-Poro_Full.png" width="800" alt="Riftwatcher Poro Showcase">
</p>

Riftwatcher Poro is a Discord bot for tracking ranked League of Legends performance for a list of tracked Riot IDs.
It maintains daily and weekly scoreboard messages, posts match recaps, posts rank up/down alerts, and announces new League patch notes.

## License

Riftwatcher Poro is source-available for non-commercial use under the PolyForm Noncommercial License 1.0.0. See `LICENSE.md`.

Commercial use is not granted by this license. Contact the project maintainer for separate commercial permission.

## Disclaimer

Riftwatcher Poro is an independent community project and is not endorsed by, directly affiliated with, maintained, authorized, or sponsored by Riot Games.
Riot Games, League of Legends, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
This project does not use Riot's official logos.

## Runtime Entry

- Run locally: `python -m src.app`
- Railway/Procfile worker: `python -m src.app`

## Self-Hosting

Riftwatcher Poro is intended to be self-hosted as one bot instance per Discord server/community. Each deployment uses its own Discord bot token, Riot API key, Postgres database, channel IDs, and tracked player list.

Start with:

- `.env.example` - copyable environment variable template.
- `SELF_HOSTING.md` - Discord/Riot/Postgres/Railway setup guide.

## Project Structure

- `src/app.py` - process entrypoint
- `src/discord_bot.py` - runtime wiring, startup logs, worker scheduling
- `src/config.py` - environment loading and validation
- `src/constants.py` - `!command` names
- `src/discord_command_handlers.py` - command entrypoint/delegator
- `src/commands/` - command context, channel routing, author permissions, and domain-specific handlers (`ops`, `player`, `report`, `slash`)
- `src/discord_text.py` - Discord text/render helpers
- `src/poro_service.py` - report orchestration and refresh logic
- `src/services/` - report service internals (report builder, refresh, baselines)
- `src/riot_api.py` - Riot API client, retries, match fetch/cache behavior
- `src/discord_recap_worker.py` - recap polling and recap -> stats sync
- `src/discord_rank_worker.py` - ranked state comparison and notifications
- `src/discord_backfill_worker.py` - low-priority historical cache backfill
- `src/discord_esports_worker.py` - pro play news cycle (daily schedule, live/game/series updates)
- `src/esports_api.py` / `src/esports_logic.py` - LoL Esports API client and pure parsing/diff/format helpers
- `src/discord_patch_worker.py` - patch-notes cycle (new article detection and posting)
- `src/patch_notes_api.py` / `src/patch_notes_logic.py` - leagueoflegends.com patch-notes client and pure parsing/format helpers
- `src/runtime/` - shared worker loops, tracked-message persistence, alerts and stall watchdog, memory release (`memory.py`), stack dumps (`diagnostics.py`)
- `src/db/` - Postgres pool, schema, and persistence helpers
- `src/report_logic.py` - pure ranking/window helpers
- `src/rank_logic.py` - rank queue normalization and rank-change message formatting
- `src/recap_lp.py` - LP gain/loss for ranked recaps from the rank worker's polls
- `src/arena_static.py` - Arena augment/item display-name loader (CommunityDragon/Data Dragon)
- `src/recap_cards.py` - match recap cards (Components V2): packing within Discord limits, per-player components for LP edits
- `src/champion_icons.py` - recap champion display mode and portrait emojis (Data Dragon -> Discord application emojis)
- `src/puuid_remap.py` - one-off background rewrite of old (pre key-change) PUUIDs in cached matches
- `src/match_stats.py` - per-player match stats extracted from cached matches into `player_match_stats` (background sync)
- `tests/` - unit tests

## Features

- `!Daily` keeps two tracked messages in `DAILY_REPORT_CHANNEL_ID`:
  - top message: previous day snapshot
  - second message: current day live scoreboard
- At daily rollover (`REPORT_DAY_START_HOUR` in `REPORT_TIMEZONE`), previous day message is replaced with the last completed daily report and its header includes the cycle date.
- `!Weekly` keeps a single weekly scoreboard message updated in `WEEKLY_REPORT_CHANNEL_ID`.
- Daily window starts at `REPORT_DAY_START_HOUR` in `REPORT_TIMEZONE`.
- Weekly window aggregates existing daily stats from Monday at `REPORT_DAY_START_HOUR` through next Monday at the same hour in `REPORT_TIMEZONE`.
- Ranked queues tracked in report:
  - Solo/Duo (`420`)
  - Flex (`440`)
  - Ranked 5s (`710`) - counted in the Flex bucket
- Scoreboards are ordered by `Gamer Score` (0-100):
  - 65% Wilson lower bound of the ranked win rate (z = 1.40) + 35% role-relative performance percentile
  - the performance share ramps in over the first 8 ranked games of the window
  - performance percentiles compare per-minute stats against same-role baselines built from cached ranked matches (`BASELINE_MATCH_LIMIT`)
  - falls back to pure Wilson when no baseline or role is available yet
  - `!score` shows the breakdown per player
- Match recap posts in `MATCH_RECAP_CHANNEL_ID` for any queue a tracked player finishes (remakes are skipped):
  - small header line: queue, result (`Win`, `Loss`, `Mixed results`, `Placements`, `No result`) and game length (no date or clock)
  - ranked labels for Solo/Duo, Flex and Ranked 5s; named labels for common non-ranked queues (Normal Draft/Blind, Swiftplay, Quickplay, ARAM, ARAM Mayhem, Clash, ARAM Clash, ARURF, URF, One for All, Nexus Blitz, Co-op vs AI); other queues show `Queue <id>`
  - per player: name, champion (portrait by default), role, LP; no result mark, since the card edge and header show a shared win/loss. In mixed-result games each player line adds · **Win** or · **Loss**
  - compact bold stats: KDA, CS/min and vision; damage and objectives; damage taken and healing
  - Arena (`1700`/`1710`/`1750`, with `1750` shown as `Arena 3x6`): placement/team ordering, damage + team damage share, taken/healing/shielded, gold
  - Arena augments and items by name when static data is available (raw IDs otherwise)
  - Arena skillshots hit/dodged from match challenge stats
  - each match is a card (Discord Components V2) with a result-coloured edge: green all wins, red all losses, blurple mixed, gold Arena, grey unknown
  - large batches are split across messages at player boundaries (40 components / 4000 characters per message); a match that doesn't fit continues under a "(continued)" header
  - if Discord rejects a card, that batch is posted as the plain-text recap instead, so recaps are never lost
  - Solo/Duo and Flex lines show rank and LP change, after the player line, e.g. `Gold II · 40LP → **22LP (-18)**` (resulting LP and change in bold):
    - no extra Riot requests: the rank worker's existing poll supplies the before/after rank
    - if the rank worker sees the game first, LP is in the recap when it posts; otherwise the recap is edited when the next rank poll sees it (usually within `DAILY_REFRESH_SECONDS`)
    - LP is only shown when exactly one game in that queue happened between two rank polls with a matching result; otherwise (two quick games, season reset, dodges/decay) the line stays without LP
    - recaps are left alone if no LP arrives within 30 minutes
  - champion display, set with `!champdisplay name|icon|both|status` (default `icon`):
    - `name`: champion name (`**Ahri**`); `icon`: portrait only; `both`: portrait then name
    - without a portrait (new champion, Discord/Data Dragon failure, lookup budget used up) the champion name is shown
    - portraits come from Riot Data Dragon and are uploaded once as application-owned Discord emojis named after the champion (e.g. `:Ahri:`, `:KaiSa:`), reused across restarts; portraits uploaded under the earlier `rw_champ_<id>` names are renamed automatically
    - a missing portrait, Discord/Data Dragon failure, or timeout falls back to the name; failures pause new lookups for 5 minutes
    - one recap batch spends at most 15 seconds fetching new portraits; after that it uses cached portraits only
- Ranked win/loss streak callouts post in `MATCH_RECAP_CHANNEL_ID` as separate messages:
  - 3-4 wins: Momentum
  - 5-7 wins: Heater Alert
  - 8-9 wins: LEGENDARY
  - 10+ wins: FINAL BOSS UNLOCKED
  - 3-4 losses: Cold Streak
  - 5-7 losses: Tilt Watch
  - 8-9 losses: FULL TILT
  - 10-12 losses: UNINSTALL PROTOCOL
  - 13+ losses: BIOHAZARD IN THE LOBBY
  - streaks are counted over the last 20 matches, ranked queues only (remakes ignored)
  - TTS is enabled by default and can be toggled with `!tts on|off|status`
- Rank alerts post in `EVENTS_CHANNEL_ID`:
  - rank up: congratulatory message
  - rank down: flame message
  - first-seen unranked -> ranked transitions are baseline only (no alert)
- Patch notes post in `EVENTS_CHANNEL_ID` when Riot publishes a new League patch (on by default):
  - title, publish date (shown in each reader's locale), Riot's short blurb, and the article link
  - names of changed champions and items, plus other sections in the patch (e.g. ARAM: Mayhem, Arena)
  - falls back to title/blurb/link if the article body can't be read
  - data source: the leagueoflegends.com patch-notes page (Riot has no official patch-notes API)
  - first check is a silent baseline, so the current patch isn't posted on deploy; mid-patch updates aren't re-posted
- Daily performance badges are computed from ranked matches only.
  - Categories: 🌾 CS, 🏰 objective damage, 💥 champion damage, ❤️ healing, 🛡️ damage taken, 🗡️ kills, ☠️ deaths, 👁️ vision.
  - Badges compare per-minute rates (not totals), blended toward the group average by games played so short hot streaks don't dominate.
  - Players need at least 2 games (daily) / 3 games (weekly) to earn badges.
  - 🔁 Grinder badge goes to whoever played the most games.
- Expanded daily stats now track:
  - assists, gold earned, wards placed/killed
  - turret/dragon/baron takedowns
  - double/triple/quadra/penta kills
  - kill participation numerator/denominator
- Optional esports news in `NEWS_CHANNEL_ID` (default league: Worlds):
  - daily "Esports today" schedule at the report day start, with times rendered in each reader's local timezone
  - weekly "Esports this week" schedule on Monday's report day start (Mon-Sun, grouped by day), posted before the daily one and edited in place as TBD matchups are decided
  - live announcement when a match starts
  - game win updates with the running series score (the deciding game is covered by the series announcement)
  - series (match) win announcement with the final score
  - scores/winners wrapped in spoiler tags by default (`ESPORTS_SPOILER_TAGS`)
  - data source: the LoL Esports API that powers lolesports.com (unofficial; no signup needed)

## Background Workers

All workers start with jitter to avoid bursty startup traffic and log cycle heartbeat timing. `WORKER_STAGGER_SECONDS` adds an extra fixed offset per worker (rank 1x, recap 2x, backfill 3x, esports 4x, patch 5x).

- Refresh worker (`max(30, DAILY_REFRESH_SECONDS)`, default `300`)
  - rebuilds daily stats
  - updates the current-day scoreboard snapshot with throttling (at most every 30s on change, forced at least every 120s and at cycle end)
  - ensures daily two-message layout exists and handles day rollover copy (today -> previous day)
  - refreshes the weekly scoreboard message
  - runs DB match-cache cleanup (`MATCH_CACHE_RETENTION_DAYS`) at most once per hour
- Rank worker (`max(30, DAILY_REFRESH_SECONDS)`)
  - compares current ranked entries vs persisted `player_ranked_state`
  - turns a one-game win/loss counter change into an LP event and edits it into the matching recap (or stores it for the recap worker)
- Recap worker (`max(30, MATCH_RECAP_POLL_SECONDS)`, default `90`)
  - detects newly finished matches (last 20 per player) and posts recaps
  - posts streak callouts as separate messages (not merged into recap batches)
  - refreshes affected players' daily stats and forces daily + weekly scoreboard sync
- Backfill worker (`max(120, DAILY_REFRESH_SECONDS * 2)`)
  - runs every cycle, paging backwards through each player's match history
  - caches up to 3 uncached older matches per player per cycle into the DB
  - uses `cache_in_memory=False` to avoid polluting in-memory cache
  - uses a smaller Riot request budget and pauses after `429`s so live features keep priority
- Stall watchdog (every 30s)
  - alerts in `EVENTS_CHANNEL_ID` when a worker has no successful cycle within its stale threshold, and posts a recovery message
  - thresholds: refresh/rank/recap `max(90, interval * 3)`, backfill `max(300, interval * 6)` (or `BACKFILL_STALE_THRESHOLD_SECONDS`), esports `max(300, poll * 5)`, patch `poll * 4`
- Esports worker (`max(30, ESPORTS_POLL_SECONDS)`, default `60`; only when `NEWS_CHANNEL_ID` is set)
  - polls the schedule for each league in `ESPORTS_LEAGUES` and diffs it against a snapshot in `bot_state`
  - first poll per league is a silent baseline (no backlog spam)
- Patch notes worker (`PATCH_NOTES_POLL_SECONDS`, default `21600` = 6h, min `3600`; on unless `PATCH_NOTES_ENABLED=false`)
  - one gzip page request (~60 KB) per check; the article itself is only fetched when a new patch appears
  - last check time is persisted, so restarts/redeploys don't trigger extra requests; failed checks retry after `min(poll, 1h)`
  - Riot publishes notes about every 2 weeks (Tuesdays ~18:00 UTC), so the default posts within 6 hours

## Commands

- `!Daily`
- `!Weekly`
- `!score`
- `!streak Name#Tag`
- `!tts on|off|status`
- `!champdisplay name|icon|both|status`
- `!Add Name#Tag`
- `!remove Name#Tag`
- `!profile Name#Tag`
- `!DebugPlayer Name#Tag`
- `!health`
- `!backfill YYYY-MM-DD YYYY-MM-DD`
- `!test`
- `!riottest`
- `!help`
- `!sync` (server administrators only) - copies slash commands to the current server for instant availability

Slash commands:

- `/health` - same output as `!health`; only answers in `EVENTS_CHANNEL_ID` (elsewhere it replies privately with a pointer)

Run `!sync` once after deploying a new bot so slash commands appear immediately in your server.

Command names are case-insensitive (`!daily` works the same as `!Daily`). `!Daily` and `!Weekly` delete the invoking message when the bot has permission to, so the scoreboard channels stay clean.

Command routing:
- `!Daily` in `DAILY_REPORT_CHANNEL_ID`
- `!Weekly` in `WEEKLY_REPORT_CHANNEL_ID`
- `!streak` in `MATCH_RECAP_CHANNEL_ID`
- `!tts` in `EVENTS_CHANNEL_ID` or `MATCH_RECAP_CHANNEL_ID`
- `!champdisplay` in `EVENTS_CHANNEL_ID` or `MATCH_RECAP_CHANNEL_ID`
- `!sync` in any server channel
- all other commands (and `/health`) in `EVENTS_CHANNEL_ID`

Permissions:
- `!Add`, `!remove`, `!backfill`, `!tts on|off`, and `!champdisplay name|icon|both` change shared state and require the **Manage Server** permission (or Administrator), or the optional `ADMIN_ROLE_ID` role
- usage hints (e.g. bare `!Add`) `!tts status`, and `!champdisplay status` stay open to everyone
- the bot never pings: all outgoing messages suppress `@everyone`, role, and user mentions
- command errors show a short, safe reason plus a `ref` ID; full details are only in the bot logs (search for the ref)

## Environment Variables

Required:

- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DATABASE_URL`
- `DAILY_REPORT_CHANNEL_ID`
- `WEEKLY_REPORT_CHANNEL_ID`
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
- `MATCH_CACHE_RETENTION_DAYS` (`730`, set `0` to disable cleanup; age counts from when a match was cached, not played. Riot deletes matches after about two years and only lists a player's recent games, so cleaned-up games may be gone for good. Production uses `0`; see OPERATIONS.md)
- `DB_POOL_SIZE` (`5`)
- `LOG_RIOT_REQUESTS` (`false`)
- `LOG_JSON` (`false`)
- `RIOT_PLATFORM_ROUTING` (`euw1`)
- `RIOT_REGIONAL_ROUTING` (`europe`) - regional host for account/match-v5 API calls (`europe`, `americas`, `asia`)
- `BACKFILL_STALE_THRESHOLD_SECONDS` (`0` = auto: `max(300, backfill interval * 6)`) - stall watchdog threshold for the backfill worker
- `ADMIN_ROLE_ID` (unset = permission-only) - role allowed to run `!Add`/`!remove`/`!backfill`/`!tts on|off`/`!champdisplay name|icon|both` in addition to Manage Server/Administrator

Low-resource hosting (NAS/IoT), all optional:

- `STARTUP_PREFER_SNAPSHOT` (`false`) - build the startup scoreboard from stored stats instead of live Riot calls
- `BASELINE_MATCH_LIMIT` (`5000`, `0` = no limit) - max cached matches scanned to build Gamer Score baselines (streamed in batches, so memory stays flat regardless of the limit)
- `RIOT_REQUEST_YIELD_SECONDS` (`0.0`) - pause between Riot requests to reduce CPU/network bursts
- `WORKER_STAGGER_SECONDS` (`0`) - extra startup offset between background workers
- `ASYNCIO_THREAD_POOL_SIZE` (`0` = Python default) - size of the default thread pool used for DB calls

Esports news, all optional:

- `NEWS_CHANNEL_ID` (unset = disabled) - channel for the daily schedule and live/game/series updates
- `ESPORTS_LEAGUES` (`worlds`) - comma-separated lolesports league slugs, e.g. `worlds,msi,first_stand`
- `ESPORTS_POLL_SECONDS` (`60`, min `30`)
- `ESPORTS_SPOILER_TAGS` (`true`) - wrap scores/winners in Discord spoiler tags
- `ESPORTS_API_KEY` (no default; required when `NEWS_CHANNEL_ID` is set) - the `x-api-key` header lolesports.com sends to `esports-api.lolesports.com`. If it is missing or rejected (`401`/`403`), the bot posts an alert in `NEWS_CHANNEL_ID`

Patch notes, all optional:

- `PATCH_NOTES_ENABLED` (`true`) - post new League patch notes in `EVENTS_CHANNEL_ID`
- `PATCH_NOTES_POLL_SECONDS` (`21600`, min `3600`) - seconds between checks of the patch-notes page
- `PATCH_NOTES_LOCALE` (`en-us`) - leagueoflegends.com locale for article language and links (e.g. `en-gb`, `de-de`)

## Data Tables

- `tracked_players` - tracked Riot IDs + optional PUUID
- `player_daily_stats` - per-day wins/losses + performance aggregates
- `match_info_cache` - cached match payload JSON
- `bot_state` - generic state keys (message ids, last seen ids, flags, patch-notes seen articles)
- `player_ranked_state` - last known rank state per player/queue
- `lp_history` - every rank/LP change the rank worker observes (LP delta, result, rank before/after), for analysis; see OPERATIONS.md
- `player_match_stats` - one row of numbers per tracked player per cached match (derived from `match_info_cache`), for fast per-player analysis

## Testing

```bash
python -m pytest -q
```

On Windows, if `python` resolves to a WindowsApps alias, run tests with the Python launcher or an explicit interpreter path:

```powershell
py -3.14 -m pytest -q
```

Tests need no Discord, Riot, or Postgres access (`tests/conftest.py` provides dummy environment values). GitHub Actions (`.github/workflows/test.yml`) runs the suite on the Python version in `.python-version` (the same one Railway builds with) for pushes and pull requests to `main`. Install test dependencies with `pip install -r requirements-dev.txt`. Dependabot (`.github/dependabot.yml`) opens weekly PRs for pinned dependency and GitHub Actions updates.

## Operations Notes

- If `MAX_TODAY_MATCH_DETAILS` is high, refresh cycles can become long on heavy accounts.
- The Riot client throttles itself to personal-key limits (20 requests/1s, 100 requests/120s), retries timeouts and transient `5xx` with exponential backoff, and honors `Retry-After` on `429` (3 attempts per request).
- If Riot rejects `RIOT_API_KEY` (`401` from any endpoint, or `403` confirmed by a throttled status-v4 probe), the bot posts one alert in `EVENTS_CHANNEL_ID` and a recovery message once a request succeeds again.
- Warning when recap and daily channel IDs are equal is informational only.
- Daily/weekly tracked message IDs are now preserved across transient Discord API fetch failures (`HTTPException`) to avoid accidental duplicate scoreboard posts.
- `!backfill` rebuilds from `match_info_cache` only and does not call Riot APIs.
- See `OPERATIONS.md` for incident response, deploy rules, and health triage steps.

## Support the Project

If you find Riftwatcher Poro useful and would like to support its development, you can buy me a coffee!

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-Support%20My%20Work-orange?style=for-the-badge&logo=buy-me-a-coffee)](https://www.buymeacoffee.com/noxvain)

## Additional Documentation

- `AGENTS.md` for contributor and coding-agent guidelines (structure, conventions, workflow).
- `ARCHITECTURE.md` for design principles and module/runtime structure.
- `OPERATIONS.md` for incident response and runbook steps.
- `IMPROVEMENT_BACKLOG.md` for prioritized future work.
