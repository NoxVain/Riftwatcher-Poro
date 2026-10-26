# Improvement Backlog

This backlog tracks work not yet implemented as of 2026-10-26.

## Source docs reviewed

- `README.md`
- `ARCHITECTURE.md`
- `OPERATIONS.md`
- `SELF_HOSTING.md`
- git history through PR #18 (2026-10-26)

## Feature Backlog

1. Head-to-head command (`!vs Name1 Name2`) for day/week
2. Daily MVP + Clutch/Int awards
3. Weekly title belts (Damage King, Objective Goblin, Vision Dad)
4. Prediction game for day record guesses
5. Party synergy stats for duo/trio combinations
6. Seasonal month-long points race
7. Clip/quote of the day in recap flow
8. Weekly boss challenge mode with success/fail announcement

## Recently Completed (Removed From Backlog)

### 2026-10-25 to 2026-10-26

- Match recaps:
  - LP gain/loss on Solo/Duo and Flex lines from the rank worker's existing polls (no extra Riot requests); edited into posted recaps; before-and-after LP and both ranks on promotions/demotions.
  - Components V2 cards: one card per match with a result-coloured edge, compact header (queue, result, game length) and bold stat lines; plain-text fallback if Discord rejects a card; LP edits target the player's card component under an edit lock.
  - Champion portraits as application emojis named after the champion (`!champdisplay name|icon|both`, default portraits, name fallback); per-player result only in mixed-result games.
- Memory and reliability:
  - Baseline/backfill payloads read in pages (bot memory ~75-140 MB, down from 1.2-4.8 GB); freed memory returned to the OS; `MALLOC_ARENA_MAX=2`.
  - Baselines never block scoreboards or recaps (background rebuild, 120 s limit); one build at startup.
  - DB connect timeout, keepalives and pool wait limit; stall alerts log stack dumps and also catch workers stuck in their first cycle.
- Platform:
  - Python 3.14 (Railway and CI via `.python-version`), exact dependency pins with `requirements-dev.txt`, Dependabot for pip and GitHub Actions, actions v7.
  - Railway settings moved from `railway.json` to Infrastructure as Code (`.railway/railway.ts`).
  - `main` protected by a ruleset (PR + passing `test` check; admin bypass).
- Data:
  - `lp_history` table records every observed LP change for later analysis.
  - Old PUUIDs (from before the Feb 2026 Riot key change) in cached matches are rewritten to current ones by Riot ID, so all cached games are found by PUUID again.

### 2026-06 to 2026-10

- League patch-notes announcements in `EVENTS_CHANNEL_ID` (leagueoflegends.com page data; 6h default poll, restart-aware schedule, silent first baseline).
- Riot key alert: generic text (no hardcoded mention/host), `403` detection with status-v4 confirmation, automatic flag reset + recovery message.
- Arena static names: failed loads are no longer cached; retried after a cooldown.
- Recap labels for common non-ranked queues (normals, Swiftplay/Quickplay, ARAM, ARAM Mayhem, Clash, rotating modes, Co-op vs AI).
- Leaderboard badges reworked: per-minute rates with shrinkage toward the group rate, minimum games (2 daily / 3 weekly), and a Grinder badge.
- Extra streak callout tiers: 10+ wins (FINAL BOSS UNLOCKED), 10-12 losses (UNINSTALL PROTOCOL), 13+ losses (BIOHAZARD IN THE LOBBY).
- Esports news (optional `NEWS_CHANNEL_ID`):
  - daily schedule, live/game/series announcements with spoiler tags
  - weekly schedule edited in place as TBD matchups resolve
  - stale-response and duplicate-post hardening
  - `ESPORTS_API_KEY` from env with auth-failure alert and recovery notice
- Security hardening:
  - mutating commands require Manage Server/Administrator or `ADMIN_ROLE_ID`
  - all outgoing mentions suppressed bot-wide
  - raw exception text no longer echoed to Discord (safe reason + log ref)
- `/health` slash command and admin-only `!sync`; executor no longer leaks on reconnect.
- Ranked 5s (`710`) counted in the Flex bucket.
- Arena recap: damage dealt + team damage share; updated augment data source.
- Large recap batches split at player boundaries instead of truncating.
- Low-resource (NAS/IoT) hosting options (`STARTUP_PREFER_SNAPSHOT`, `BASELINE_MATCH_LIMIT`, `RIOT_REQUEST_YIELD_SECONDS`, `WORKER_STAGGER_SECONDS`, `ASYNCIO_THREAD_POOL_SIZE`).
- Backfill stall threshold raised and made configurable (`BACKFILL_STALE_THRESHOLD_SECONDS`).
- Repository renamed to Riftwatcher Poro; CI moved to GitHub Actions; LF line endings enforced via `.gitattributes`.

### Earlier

- Runtime + DB + command module split completed.
- Streak callouts moved to separate messages and TTS toggle added.
- `!tts` routing allowed in events + recap channels.
- `!profile Name#Tag` command implemented.
- `!remove Name#Tag` command implemented.
- `!backfill YYYY-MM-DD YYYY-MM-DD` command implemented (cache-only historical rebuild, up to 366 days).
- `player_daily_stats` expanded with additional performance fields:
  - assists/gold/warding/objective takedowns
  - multi-kill counters
  - kill participation numerator/denominator
- Command routing matrix tests added.
- Recap/streak separation integration test coverage added.
- Runtime reliability hardening:
  - previous-day message text corruption fixed
  - Riot 401 alert mark-on-success behavior
  - refresh cleanup state decoupled from message state
  - worker latency metrics export in `!health`
  - stalled-worker watchdog with deduped alert/recovery messages
  - transient Discord fetch failures no longer reset tracked daily/weekly message IDs
- Scoring system adjustments shipped (no Scoring V2 rewrite):
  - stricter Wilson confidence default
  - weighted performance percentile
  - performance weight ramp by game count
- Ops/docs refresh: added `OPERATIONS.md` runbook.
- Arena recap support:
  - queue `1750` formatted as Arena 3x6
  - placement/team ordering
  - named augments/items and skillshots hit/dodged
  - recap worker diagnostics for missing/skipped recaps
