# Repository Guidelines

## Start Here
Riftwatcher Poro (formerly MoodBot) is a Discord bot that follows a group of League of Legends players. It posts match recaps with LP changes, daily and weekly Gamer Score scoreboards, rank-up and streak callouts, patch notes, and optional esports news. It runs as one Python process on Railway, backed by Postgres.

Read these before making changes:
1. `ARCHITECTURE.md` -> **Design Principles**: the priorities behind every decision (core posts never block, no extra Riot requests, memory is money, pure logic + offline tests, observability, maintainer owns visible UX, security, small verified PRs).
2. `ARCHITECTURE.md` (rest): runtime layers, worker model, data flow, persistence.
3. `OPERATIONS.md`: logs to look for, stall alerts, incidents, LP-history analysis, memory/hosting cost.
4. `IMPROVEMENT_BACKLOG.md`: what shipped recently and what's planned.

Design priorities in one line each (details in ARCHITECTURE.md):
- Recaps and scoreboards must never be lost or stuck. Optional enrichment (LP, portraits, baselines, cards) degrades or times out and never blocks posting.
- Never add Riot requests when existing polls already carry the data. The personal key allows 100 requests per 120 s, shared by all workers.
- Memory is ~97% of the Railway bill. Page large DB reads and never hold many match payloads.
- Logic lives in pure, unit-tested modules; workers take dependencies as parameters; `src/discord_bot.py` is wiring only.
- Every behaviour change gets a log line, offline tests, and doc updates in the same PR.
- Visible layout and defaults are the maintainer's decision; propose with a mockup first.

## Project Structure & Module Organization
This repository is a modular Python Discord bot.

- `src/app.py`: Entrypoint used by local runs and Railway.
- `src/discord_bot.py`: Dependency wiring, Discord client events, slash command registration, and background task scheduling.
- `src/config.py`: Environment/config loading and validation.
- `src/constants.py`: Command constants.
- `src/discord_command_handlers.py`: `!command` entrypoint that delegates into `src/commands/`.
- `src/commands/`: Command context, channel routing policy (`routing.py`), author permission policy for mutating commands (`permissions.py`), and handlers split by domain (`ops_handlers.py`, `player_handlers.py`, `report_handlers.py`, `slash_handlers.py`).
- `src/discord_text.py`: Pure Discord text/render helpers (recap lines, streak callouts, health report).
- `src/recap_cards.py`: Pure Components V2 recap card builder (accent by result, 40-component/4000-char limits, LP edits on cards); the recap worker falls back to plain text if Discord rejects a card.
- `src/poro_service.py`: Report service facade (report generation, cache policy, health checks).
- `src/services/`: Report service internals (`report_builder.py`, `refresh.py`, `baselines.py`).
- `src/riot_api.py`: Riot API client, retries/backoff, and match caching behavior.
- `src/discord_recap_worker.py`, `src/discord_rank_worker.py`, `src/discord_backfill_worker.py`, `src/discord_esports_worker.py`, `src/discord_patch_worker.py`: Per-cycle worker logic (recaps/streaks, rank changes, historical cache fill, esports news, patch notes).
- `src/esports_api.py`: LoL Esports web API client; `src/esports_logic.py`: pure esports parsing/diff/format helpers.
- `src/patch_notes_api.py`: leagueoflegends.com patch-notes client; `src/patch_notes_logic.py`: pure patch-notes parsing/format/seen-state helpers.
- `src/runtime/`: Shared worker loops (`workers.py`), tracked-message persistence (`message_store.py`), Riot/stall alerts and the stall watchdog (`alerts.py`), returning freed memory to the OS (`memory.py`), and stack dumps logged with stall alerts (`diagnostics.py`).
- `src/db/`: Postgres pool (`pool.py`, connect/checkout timeouts), schema/migrations (`schema.py`), and persistence helpers split by concern (`state.py` for the `bot_state` key/value store, `players.py`, `cache.py` for match payloads and paged reads, `stats.py`, `ranked_state.py` for rank state and the `lp_history` table, `match_stats.py` for `player_match_stats`).
- `src/report_logic.py`: Pure ranking/time-window/queue-bucket helpers.
- `src/rank_logic.py`: Rank normalization/comparison and rank-change message text.
- `src/recap_lp.py`: LP gain/loss for ranked recaps, built from the rank worker's polls (no extra Riot requests) and edited into posted recaps; also builds `lp_history` rows.
- `src/arena_static.py`: Arena augment/item display-name loader (CommunityDragon/Data Dragon, cached per process).
- `src/champion_icons.py`: Recap champion display mode (`!champdisplay name|icon|both`) and Data Dragon portraits uploaded as Discord application emojis.
- `src/puuid_remap.py`: Background rewrite of old PUUIDs in cached match payloads after a Riot API key change (PUUIDs are encrypted per key project), matched by Riot ID.
- `src/match_stats.py`: Per-player match stats (`player_match_stats`, one row per tracked player per cached match) extracted from cached payloads by a background sync, so analysis is a SQL query instead of a payload scan.
- `src/player_analysis.py`: Pure `!analyze` rules (guarded strengths/tips per queue) and the compact card; the handler is `src/commands/analysis_handlers.py`.
- `src/wrapped.py`: Poro Wrapped: pure year-in-review logic (awards, banter, group and personal cards) and the background task that posts the group cards in `EVENTS_CHANNEL_ID` on 31 December; the `!wrapped` handler is `src/commands/wrapped_handlers.py`.
- `src/belts.py`: Weekly title belts: pure awards/defended-streak logic and the background task that posts the card in `EVENTS_CHANNEL_ID` once per closed report week.
- `src/records.py`: Pure Hall of Fame logic (ranked-only group records, personal bests, streaks) for `!records` and the "New group record" and "Personal best" recap lines; the handler is `src/commands/records_handlers.py`.
- `tests/`: Unit tests for pure logic, command routing/handlers, workers, and runtime helpers.
- `README.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, `SELF_HOSTING.md`: Runtime, design, runbook, and deployment documentation. `IMPROVEMENT_BACKLOG.md`: planned features and recently shipped work.
- `.github/workflows/test.yml`: GitHub Actions CI (pytest on the `.python-version` Python for pushes/PRs to `main`); `.github/dependabot.yml`: weekly pip and GitHub Actions update PRs.
- `.railway/railway.ts`: Railway Infrastructure as Code (start command, restart policy); applied with `railway config apply`, not on deploy. `Procfile`: start command for Procfile-based hosts (`python -m src.app`).
- `.vs/`, `__pycache__/`, `build/`, `env/`, `.venv/`, `node_modules/`: Editor/build/venv artifacts; do not treat as source.

Keep Discord event handlers in `src/discord_bot.py` thin and push logic into service/client/helper modules.

## Build, Test, and Development Commands
Use Python 3.14 (pinned in `.python-version`, used by Railway and CI).

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m src.app
```

- `python -m venv .venv`: Creates a local virtual environment.
- `pip install -r requirements-dev.txt`: Installs runtime dependencies (`requirements.txt`, exact pins, what Railway installs) plus test tooling.
- `python -m src.app`: Runs the bot locally.

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indentation.
- Use `snake_case` for functions/variables, `UPPER_CASE` for constants (`TOKEN`, `RIOT_API_KEY`), and clear async function names (`fetch_matches`).
- Keep command parsing/routing in `src/commands/` (channel policy in `src/commands/routing.py`); keep `src/discord_bot.py` limited to wiring. Move API and formatting logic into helper/service modules.
- Prefer type hints for new code.

If you add tooling, use `black` + `ruff` and run them before opening a PR.

## Testing Guidelines
Automated tests are present. Add new `pytest` coverage under `tests/`:

- File pattern: `tests/test_<module>.py`
- Test names: `test_<behavior>`
- Focus on pure logic first (result parsing, win/loss aggregation, window calculations, ranking helpers).

Run tests with:

```powershell
pytest -q
```

Tests run without Discord, Riot, or Postgres access: `tests/conftest.py` sets dummy values for required environment variables. Keep new tests offline (stub Riot/Discord/DB calls).

## Commit & Pull Request Guidelines
Use this convention (matches the existing git history):

- Commit format: `type(scope): summary` (example: `feat(report): add ranked queue filter`)
- Keep commits focused and small.
- PRs should include: purpose, key changes, test evidence (command output), and any config/secrets impact.
- Link related issues and include screenshots/log snippets when behavior changes are visible.

Workflow:
- `main` is protected: changes go through a branch and a PR, the `test` check must pass, and force pushes are blocked. Use `gh` for PRs and merges.
- Squash-merge only when the maintainer asks, then delete the branch and update local `main`.
- Railway deploys every push to `main`. After a merge, check the deploy logs: startup login, each worker's `Cycle complete` line, `Role baselines built`, and memory (`railway metrics`).
- `.railway/railway.ts` changes are applied manually (`railway config plan` / `apply`), not on deploy.
- When reusing a contributor's work, credit them with a `Co-authored-by:` trailer. When a contributor PR is superseded, close it with a thank-you comment.
- Production data analysis uses read-only database sessions and reports aggregates only. Never print credentials or connection strings.

## Security & Configuration Tips
Never hardcode secrets. Load all credentials/config from environment variables.

Required runtime variables:
- `DISCORD_TOKEN`
- `RIOT_API_KEY`
- `DATABASE_URL`
- `DAILY_REPORT_CHANNEL_ID`
- `WEEKLY_REPORT_CHANNEL_ID`
- `MATCH_RECAP_CHANNEL_ID`
- `EVENTS_CHANNEL_ID`

Common optional variables:
- `REPORT_TIMEZONE`
- `REPORT_DAY_START_HOUR`
- `REPORT_CACHE_SECONDS`
- `DAILY_REFRESH_SECONDS`
- `MATCH_RECAP_POLL_SECONDS`
- `MAX_TODAY_MATCH_DETAILS`
- `MAX_MATCH_IDS_SCAN`
- `MAX_IN_MEMORY_MATCH_CACHE`
- `MATCH_CACHE_RETENTION_DAYS`
- `DB_POOL_SIZE`
- `LOG_RIOT_REQUESTS`
- `LOG_JSON`
- `RIOT_PLATFORM_ROUTING`
- `RIOT_REGIONAL_ROUTING`
- `BACKFILL_STALE_THRESHOLD_SECONDS`
- `ADMIN_ROLE_ID`
- `MANUAL_COMMANDS_CHANNEL_ID`

Low-resource hosting variables (optional):
- `STARTUP_PREFER_SNAPSHOT`
- `BASELINE_MATCH_LIMIT`
- `RIOT_REQUEST_YIELD_SECONDS`
- `WORKER_STAGGER_SECONDS`
- `ASYNCIO_THREAD_POOL_SIZE`

Esports news variables (optional; disabled unless `NEWS_CHANNEL_ID` is set):
- `NEWS_CHANNEL_ID`
- `ESPORTS_LEAGUES`
- `ESPORTS_POLL_SECONDS`
- `ESPORTS_SPOILER_TAGS`
- `ESPORTS_API_KEY`

Patch notes variables (optional; enabled by default, posts in `EVENTS_CHANNEL_ID`):
- `PATCH_NOTES_ENABLED`
- `PATCH_NOTES_POLL_SECONDS`
- `PATCH_NOTES_LOCALE`
