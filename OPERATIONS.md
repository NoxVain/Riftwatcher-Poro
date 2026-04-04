# MoodBot Operations Runbook

## Riot 401 Recovery

Symptom:
- Logs include `Riot API returned 401 Unauthorized`.
- Bot can no longer fetch Riot data.

Actions:
1. Update `RIOT_API_KEY` in Railway project variables.
2. Redeploy the service.
3. Run `!riottest` in `EVENTS_CHANNEL_ID`.
4. Confirm `!health` shows DB/workers healthy and recap/report activity resumes.

Notes:
- The 401 alert is deduped in `bot_state` (`riot_401_alert_sent`) to avoid spam.
- If needed for a fresh alert cycle, clear that state key manually in DB.

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
2. Check for custom/manual posting logic outside MoodBot.
3. Run tests: `python -m pytest -q tests/test_discord_recap_worker.py`.

## Daily Scoreboard Duplicate Message Guard

Symptom:
- A new daily leaderboard message appears instead of updating the tracked one.

Current behavior:
- Transient Discord API fetch failures (`discord.HTTPException`) no longer clear tracked message IDs.
- Tracked message state is cleared only when Discord returns `NotFound` or `Forbidden`.

Troubleshooting:
1. Confirm deployment includes commit `39b522c` or later.
2. Check logs for `NotFound`/`Forbidden` while fetching tracked daily/weekly messages.
3. Verify bot permissions in `DAILY_REPORT_CHANNEL_ID` still allow reading message history and editing messages.
4. If IDs were previously reset due to old behavior, run `!Daily` once to re-anchor tracked message IDs.

## GitLab Deploy Rules

Current pipeline behavior:
- `test` job runs on pushes and merge requests.
- `deploy_to_railway` runs when:
  - `FORCE_DEPLOY == "1"`, or
  - commit is on default branch and changed files include:
    - `src/**/*`
    - `requirements.txt`
    - `Procfile`
    - `railway.json`
    - `.gitlab-ci.yml`

Implication:
- Test-only commits normally do not deploy.

## Health Triage Checklist (`!health`)

Check in this order:
1. `DB: ok`
2. Workers line (`refresh`, `rank`, `recap`, `backfill`) shows cycles increasing and low errors.
3. `Backfill cursors active` and `Backfill max offset` are not growing unexpectedly for long periods.
4. `Match cache entries` and `Report cache active` are sensible for current activity.
5. Baselines line is present once score baselines are built.

Fast diagnosis patterns:
- `DB: down`: investigate DB connectivity/credentials first.
- Worker errors rising with no cycle growth: inspect logs for failing external API/channel access.
- Recap issues with healthy DB: verify `MATCH_RECAP_CHANNEL_ID` permissions and Riot API health.
