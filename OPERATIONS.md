# Riftwatcher Poro Operations Runbook

## Riot API Key Rejected (401/403)

Symptom:
- `Riot API returned 401 Unauthorized` or `403 Forbidden` alert in `EVENTS_CHANNEL_ID`.
- Bot can no longer fetch Riot data.

Detection:
- `401` from any Riot endpoint, or `403` from account-v1/status-v4 (available to every key), counts as a rejected key.
- A `403` from other endpoints can be an endpoint restriction, so the bot confirms it with a `lol/status/v4` probe (at most once per 5 minutes; log: `Key probe after 403: key rejected|ok`).

Actions:
1. Update `RIOT_API_KEY` in your host's environment variables (e.g. Railway project variables).
2. Redeploy/restart the service.
3. Run `!riottest` in `EVENTS_CHANNEL_ID`.
4. Confirm the `✅ Riot API key is working again` message and that `!health` shows DB/workers healthy.

Notes:
- The alert is posted once and deduped in `bot_state` (`riot_401_alert_sent`, also used for 403); the flag is only set after the alert was actually sent.
- The first successful Riot request afterwards posts the recovery message and resets the flag to `0`, so the next expiry alerts again. The flag survives restarts, so recovery is also posted after a redeploy with a new key.
- If the recovery message can't be posted, it is retried at most once a minute.

## Streak TTS Behavior

Command:
- `!tts on|off|status`

Allowed channels:
- `EVENTS_CHANNEL_ID`
- `MATCH_RECAP_CHANNEL_ID`

Scope:
- Controls TTS only for streak callout messages.
- Applies to:
  - automatic streak callouts from recap worker
  - manual `!streak Name#Tag` callouts
- Does not affect recap messages themselves.
- `!tts on|off` requires Manage Server/Administrator or `ADMIN_ROLE_ID`; `!tts status` is open to everyone.
- Stored in `bot_state` as `streak_tts_enabled` (default ON when unset).

## Command Permission Denied

Symptom:
- `!Add`, `!remove`, `!backfill`, or `!tts on|off` replies with "requires the **Manage Server** permission".

Actions:
1. Give the member Manage Server (or Administrator), or set `ADMIN_ROLE_ID` to a role they hold and redeploy.
2. `!sync` is stricter: it requires Administrator.
3. Logs show `[auth] Denied <command> for author <id>`.

## Historical Stats Backfill

Command:
- `!backfill YYYY-MM-DD YYYY-MM-DD`

Allowed channel:
- `EVENTS_CHANNEL_ID`

Behavior:
- Rebuilds `player_daily_stats` rows from cached match payloads (`match_info_cache`) only.
- Does not call Riot APIs.
- Recomputes expanded stat fields (assists/gold/wards/objective takedowns/multi-kills/KP numerators+denominators).
- Maximum range is 366 days per command run.

Troubleshooting:
1. If output shows high `Tracked players missing PUUID`, verify `tracked_players.puuid` is populated.
2. If output shows low matches scanned, check cache retention settings and recent cache cleanup activity.
3. If no rows updated, confirm requested date range overlaps cached match end times in the report timezone/day-start window.

## Recap vs Streak Message Separation

Expected behavior:
- Match recap messages and streak callouts are posted as separate Discord messages.
- Recap messages are always non-TTS.
- Streak callouts use TTS based on `!tts` state.

If behavior looks merged:
1. Verify bot version is current deployment.
2. Check for custom/manual posting logic outside Riftwatcher Poro.
3. Run tests: `python -m pytest -q tests/test_discord_recap_worker.py`.

## Arena Recap Troubleshooting

Expected behavior:
- Queue `1750` posts as `Arena 3x6`; queues `1700`/`1710` post as `Arena`.
- Arena player lines use placement/team instead of lane role.
- Augments and items are shown by name when static data can be loaded; raw IDs are used as fallback.
- Skillshots hit/dodged are shown when Riot includes those challenge stats.

If a recent Arena match does not appear:
1. Check recap worker logs for `No new matches to post`.
   - `initialized_players > 0` means the worker established a baseline and will only post future matches.
   - `players_with_new_ids=0` means Riot's recent match list did not contain a newer match than the stored recap key.
2. Check `Match scan summary`.
   - `candidate_matches > 0` and `prepared_matches=0` means matches were fetched but skipped.
   - `skipped_remakes` indicates remake/short-duration filtering.
   - `skipped_no_tracked` indicates the fetched match did not include tracked participants.
3. If the daily refresh sees `matches=1 total=0W-0L solo=0W-0L flex=0W-0L`, that can be normal for Arena because daily scoreboard totals only include solo/flex ranked queues.
4. If augment/item names are missing, verify outbound access to CommunityDragon/Data Dragon from the host. The recap still works with raw IDs when static data is unavailable. Successful loads are cached for the process lifetime; a failed source is retried on the next Arena recap at least 10 minutes later.

## Esports News Troubleshooting

Expected behavior (only when `NEWS_CHANNEL_ID` is set):
- First cycle per league (`ESPORTS_LEAGUES`, default `worlds`) baselines silently: log `Baselined N events for worlds`.
- Daily schedule posts once per report day (first poll after `REPORT_DAY_START_HOUR`, or at startup if not yet posted that day). Days without matches post nothing.
- Weekly schedule posts once on Monday's report day, just before the daily one, then is edited in place during the week as TBD matchups are decided (log: `Updated weekly schedule`). If the bot is down all Monday, that week's post is skipped. Weeks without matches post nothing. Deleting the weekly message stops updates until next Monday.
- Live/game/match updates post within `ESPORTS_POLL_SECONDS` (default `60`) of lolesports.com updating.
- The deciding game has no separate "Game N" post; the series post covers it. A series post can lag up to 15 minutes if lolesports marks the match completed before the final score updates.
- Duplicate posts with identical text usually mean two bot instances share the database (e.g. an overlapping deploy or a local run against production). Stale API responses are already ignored.

If updates stop:
1. Check `!health` for the `esports` worker error count; the stall watchdog alerts in `EVENTS_CHANNEL_ID` after 5 missed polls.
2. Check `NEWS_CHANNEL_ID` for an `ESPORTS_API_KEY` alert, and logs for `[esports] Auth failure` / `[esports] Schedule fetch failed`.
   - `401`/`403` or key not set: Riot rotated the lolesports.com web key (or it was never configured). Get the current `x-api-key` header from any `esports-api.lolesports.com` request on lolesports.com (DevTools -> Network) and set `ESPORTS_API_KEY`. The alert is posted once (persisted as `esports_auth_alert_sent` in `bot_state`); a recovery message follows on the first successful poll.
   - Timeouts/5xx: transient API outage; the worker retries every cycle and keeps its snapshot, so no duplicate announcements are posted after recovery.
3. `Unknown league slug` means a typo in `ESPORTS_LEAGUES` (valid examples: `worlds`, `msi`, `first_stand`, `lec`, `lck`).
4. To force today's schedule to repost, delete the `esports_schedule_posted_cycle` row from `bot_state`. For the weekly schedule (reposts on the next Monday-report-day poll), delete `esports_weekly_schedule_posted_week`.

## Patch Notes Troubleshooting

Expected behavior (enabled unless `PATCH_NOTES_ENABLED=false`):
- Source: the leagueoflegends.com patch-notes page (`PATCH_NOTES_LOCALE`, default `en-us`). Riot has no official patch-notes API, so this reads the site's embedded page data.
- First check baselines silently: log `[patch] Baselined N patch articles without posting`. The current patch is never posted on first deploy.
- Checks every `PATCH_NOTES_POLL_SECONDS` (default `21600` = 6h, minimum `3600`); failed checks retry after `min(poll, 3600)` seconds. Riot publishes notes about every 2 weeks (Tuesdays ~18:00 UTC), so a post normally lands the same day.
- The last check time is persisted (`patch_notes_last_checked_at`), so restarts and redeploys resume the schedule instead of fetching again.
- Each new article is posted once in `EVENTS_CHANNEL_ID` (title, date, blurb, changed champions/items, link). Posted articles are tracked in `patch_notes_seen`. Articles older than 7 days are never posted (e.g. after long downtime).
- Mid-patch updates to an already-posted article are not re-posted.

If posts stop or look wrong:
1. Check `!health` for the `patch` worker error count; the stall watchdog alerts in `EVENTS_CHANNEL_ID` after 4 missed polls (24h by default).
2. Logs with `PatchNotesParseError` (`no __NEXT_DATA__ payload` / `contained no patch articles`) mean leagueoflegends.com changed its page structure; `src/patch_notes_logic.py` needs updating.
3. A post without the champion/item lines (log `[patch] Could not read article sections`) means the article page couldn't be read; the post still includes the link.
4. To re-post the latest patch, remove its id/URL from the `patch_notes_seen` JSON in `bot_state` and delete `patch_notes_last_checked_at` (the next check then runs right after restart). Deleting `patch_notes_seen` entirely re-baselines silently instead.

## Daily Scoreboard Duplicate Message Guard

Symptom:
- A new daily leaderboard message appears instead of updating the tracked one.

Current behavior:
- Transient Discord API fetch failures (`discord.HTTPException`) no longer clear tracked message IDs.
- Tracked message state is cleared only when Discord returns `NotFound` or `Forbidden`.

Troubleshooting:
1. Check logs for `NotFound`/`Forbidden` while fetching tracked daily/weekly messages.
2. Verify bot permissions in `DAILY_REPORT_CHANNEL_ID` still allow reading message history and editing messages.
3. If IDs were previously reset due to old behavior, run `!Daily` once to re-anchor tracked message IDs.

## Stalled Worker Alerts

Symptom:
- `Worker <name> appears stalled` in `EVENTS_CHANNEL_ID`.

Thresholds (no successful cycle for):
- `refresh`, `rank`: `max(90, max(30, DAILY_REFRESH_SECONDS) * 3)` seconds
- `recap`: `max(90, max(30, MATCH_RECAP_POLL_SECONDS) * 3)` seconds
- `backfill`: `BACKFILL_STALE_THRESHOLD_SECONDS`, or `max(300, max(120, DAILY_REFRESH_SECONDS * 2) * 6)` when unset/0
- `esports`: `max(300, max(30, ESPORTS_POLL_SECONDS) * 5)` seconds
- `patch`: `PATCH_NOTES_POLL_SECONDS * 4` seconds (24h by default)

A worker that never finished a cycle is measured from process start, so one stuck in its first cycle also alerts.

Actions:
1. Search the logs for `[diag]` lines right after `[health] Worker stall alert sent for <worker>`: every alert logs a stack dump of all threads and asyncio tasks, showing the line each one is waiting on (e.g. a DB call or a `wait_for`).
2. Check `!health` for that worker's error count and latency.
3. Search logs for `[<worker>] Unexpected` errors.
4. A recovery message posts automatically after the next successful cycle. Alert state is persisted as `worker_stall_alert_sent::<worker>`, so restarts don't repeat alerts.

Known incident (2026-10-26): the role-baseline rebuild's server-side cursor stalled mid-FETCH and, behind a rebuild lock, froze every scoreboard build and the recap worker's post-recap sync. Fixed in #17: paged reads, baselines never block callers (120 s rebuild limit), DB connect/pool timeouts.

## LP History Analysis

`lp_history` stores every rank/LP change the rank worker sees (one row per change; history starts with the deploy that added it). Single games have `won` set; rows with `won IS NULL` are dodges/decay (`wins_delta = losses_delta = 0`), several games between polls, or season resets. Example: average LP per win and loss by month for one player (run read-only):

```sql
SELECT date_trunc('month', observed_at) AS month, queue_type,
       count(*) FILTER (WHERE won) AS wins, round(avg(lp_delta) FILTER (WHERE won), 1) AS avg_lp_win,
       count(*) FILTER (WHERE won = false) AS losses, round(avg(lp_delta) FILTER (WHERE won = false), 1) AS avg_lp_loss
FROM lp_history
WHERE lower(riot_id) = lower('NoxVain#EUW')
GROUP BY 1, 2 ORDER BY 1, 2;
```

## PUUIDs After a Riot API Key Change

Riot encrypts PUUIDs per API key project. A new key project (as in Feb 2026) gives every player a new PUUID, while cached match payloads keep the old ones, so lookups by PUUID miss older games. `src/puuid_remap.py` fixes this in the background 5 minutes after startup:

- It scans the cache (paged), maps each tracked player's old PUUID to the current one by Riot ID, and rewrites the payload text in Postgres.
- It logs `[puuid] Remapped old PUUIDs: players=… rewritten=… scanned=…`, or `[puuid] Skipping <riot id>: …` for ambiguous players (more than one old PUUID, or an old PUUID shared by two tracked players). Those players are left as they are.
- Afterwards it stores `puuid_remap_fingerprint` in `bot_state` and doesn't run again until the tracked players or their PUUIDs change.
- On failure (`[puuid] PUUID remap failed; will retry on next startup`), it retries on the next restart. Re-running the rewrite is safe.
- To force a re-run, delete the `puuid_remap_fingerprint` row and restart.

The first run on 2026-10-26 data: 12,415 cached matches, one old PUUID for each of the 18 tracked players, no ambiguities. Rewriting about 12k payloads creates dead tuples in Postgres that autovacuum reclaims.

## Match Cache Retention

Production runs with `MATCH_CACHE_RETENTION_DAYS=0` (set 2026-10-27), so cached matches are never deleted. The cache is the bot's only complete match history:

- Riot's match list for a player covers only recent games (for Freshwhite#EUW, about 950 games back to Feb 2026). Older games can still be fetched by match ID, but nothing lists them any more.
- Riot deletes match data after about two years (a June 2024 game returns 404). The cache still has games back to March 2024.
- Retention counts from when a match was cached (`updated_at`), not when it was played. With the 730-day default, the first deletions would have started on 2028-02-17, removing the 2024 games for good.

Cost: the `match_info_cache` table is ~705 MB on disk and grows with every cached game. Disk is cheap on Railway, and memory barely changes: baselines read only the newest `BASELINE_MATCH_LIMIT` (5000) payloads, and Postgres keeps mostly recent data in memory. The refresh worker still logs `Match cache cleanup complete: deleted=0, retention_days=0` every hour. To turn deletion back on, set a positive number of days; the code default is still 730 for self-hosters.

## Memory & Hosting Cost

On Railway, memory is almost the whole bill (Sep-Oct 2026: memory ~97% of project cost; CPU and egress are cents). Since the fixes below (2026-10-26) the bot runs at ~75-140 MB (previously 1.2-4.8 GB); Postgres (~600-735 MB) is now the larger share.

- Role baselines (every 12h) and `!backfill` read cached match payloads in pages: the newest match IDs first, then `MATCH_PAYLOAD_BATCH_SIZE` (50) payloads per short primary-key query (`src/db/cache.py`), keeping only the per-minute numbers they need. (A server-side cursor did this until #17; it stalled mid-FETCH in production on 2026-10-26.) Loading all 5000 payloads at once used to peak at ~1.2 GB of Python objects (synthetic benchmark: 1247 MB -> 41 MB for 5000 matches, identical baselines).
- Both run in a worker thread, so the event loop (recaps, scoreboards, commands) isn't blocked.
- After each baseline rebuild or backfill, `src/runtime/memory.release_freed_memory()` runs `gc.collect()` and glibc `malloc_trim(0)` so freed memory goes back to the OS instead of staying resident (and billed).
- Railway variable `MALLOC_ARENA_MAX=2` limits glibc per-thread arenas (less fragmentation from `asyncio.to_thread` workers).
- Check usage with `railway usage projects --project "Riftwatcher Poro"` and `railway metrics --since 7d --memory`. A per-service memory limit (e.g. 512 MB, well above the ~140 MB steady state) is an option to cap surprises.

## CI & Deploy

Current behavior:
- GitHub Actions workflow `Python CI` (`.github/workflows/test.yml`) runs `python -m pytest -q` on the Python version in `.python-version` (same as Railway) for pushes and pull requests to `main`.
- Dependencies are pinned exactly in `requirements.txt` (runtime, installed on Railway) and `requirements-dev.txt` (adds pytest). Dependabot opens weekly grouped PRs for pip and GitHub Actions updates; merge them once CI is green. Python upgrades are manual: change `.python-version`.
- CI has no deploy job. Railway builds and deploys the service from the connected repository based on the Railway service settings.
- Railway service settings (start command `python -m src.app`, restart policy `ALWAYS`) are managed as Infrastructure as Code in `.railway/railway.ts`. Railway stops reading `railway.json` (Config as Code) on 2026-12-01; this service was migrated off it on 2026-10-25. Service variables are `unmanaged`: they live only in Railway, never in the repo. `Procfile` (`worker: python -m src.app`) covers Procfile-based hosts.
- Changing Railway settings: edit `.railway/railway.ts`, then run `railway config plan` and `railway config apply` from the repo root (Railway CLI 5.42.1+, linked with `railway link`). The TypeScript SDK must be installed locally first: `npm install railway --no-save` (`node_modules/` is git-ignored; don't commit a root `package.json`, which could make Railway's builder treat the repo as Node). Merging to `main` does not apply IaC changes by itself.
- Git Bash quirk: the SDK finds the CLI through the `_` environment variable, which Git Bash points at npm's wrapper script. Run `plan`/`apply` from PowerShell, or prefix with `env _="<path to railway.exe>"`.

Implication:
- Check that the CI run is green before merging to the branch Railway deploys from.
- Only one instance should run against a database at a time (see duplicate-post notes in the esports section).

## Health Triage Checklist (`!health`)

Check in this order:
1. `DB: ok`
2. `Workers` line shows `<worker>: <ok>ok/<err>err (<last>ms last/<avg>ms avg/<max>ms max)` for `refresh`, `rank`, `recap`, `backfill` (plus `esports` and `patch` when enabled), with ok counts increasing and low errors. `patch` only runs every few hours, so `0ok/0err` shortly after a restart is normal.
3. `Backfill cursors active` and `Backfill max offset` are not growing unexpectedly for long periods.
4. `Match cache entries` and `Report cache active` are sensible for current activity.
5. `Gamer Score baselines` shows roles/samples once built (`not yet built` right after startup is normal).

`/health` returns the same report but only answers in `EVENTS_CHANNEL_ID` (elsewhere it replies privately with a pointer). If `/health` is missing, an Administrator should run `!sync` once.

Fast diagnosis patterns:
- `DB: down`: investigate DB connectivity/credentials first.
- Worker errors rising with no cycle growth: inspect logs for failing external API/channel access.
- Recap issues with healthy DB: verify `MATCH_RECAP_CHANNEL_ID` permissions and Riot API health.
- Command errors show a short reason plus `ref <id>`; search the logs for that ref to get the full exception.

## Local Testing & Smoke Checks

For developers running a local instance for testing:

1. **Environment**: Ensure all required variables from `README.md` are in your `.env` (copy `.env.example`).
2. **Verify Tests**: `python -m pytest -q` (requires no active bot, DB, or real credentials; `tests/conftest.py` sets dummy values).
3. **Smoke Test Command List**:
   - `!health` (Check DB and worker cycles)
   - `!Daily` (Trigger scoreboard update)
   - `!Weekly` (Trigger weekly update)
   - `!tts status` (Verify TTS configuration)
   - `!streak Name#Tag` (Test streak routing)
   - `!profile Name#Tag` (Test player data fetch)
   - `!riottest` (Verify Riot API key and outbound connectivity)
