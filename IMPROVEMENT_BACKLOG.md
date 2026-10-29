# Improvement Backlog

This backlog tracks work not yet implemented as of 2026-10-27.

## Source docs reviewed

- `README.md`
- `ARCHITECTURE.md`
- `OPERATIONS.md`
- `SELF_HOSTING.md`
- git history through PR #25 (2026-10-27)

## Feature Backlog

### Next up (in this order, one at a time)

Each starts with a mockup on real data for the maintainer to approve, and needs no new Riot requests.

1. Poro Wrapped (December): per-player year in review.
   - Games and hours played, top champions, best duo, peak rank (`lp_history`), most-pinged teammate, worst day.
   - Build in November so it can post when the season ends.

### Later

No new Riot requests:
- Daily awards on the daily scoreboard: Clutch of the day (best KDA in a win) and Grey screen of the day (most time dead in one game). Mocked up 2026-10-27; left out for now. The daily MVP is already the scoreboard's ⭐.
- Head-to-head command (`!vs Name1 Name2`) for day/week.
- Party synergy stats for duo/trio combinations (self-join on `match_id`; `!analyze` already finds the best duo partner).
- Nemesis: the enemy champion each player loses to most (`opponent_champion_id` is already stored).
- Recap highlight lines from challenges (Baron/Dragon steal, outnumbered kill, ally saved, flawless ace, perfect game).
- Group ban stats ("the group's most hated champion").
- Win rate by keystone and summoner spells, possibly as an `!analyze` finding.
- Riot server maintenance and incident notices for the platform, from the status endpoint the API-key check already calls.

Few Riot requests (check the shared 100 per 120 s budget first):
- Champion mastery milestone callouts and `!mastery` (champion-mastery-v4, about one request per player per day).
- Weekly free champion rotation post (champion rotations endpoint, one request per week).
- Clash reminders before Clash weekends (clash-v1 tournaments, one request per day).
- Live game callouts ("NoxVain and Freshwhite just started Flex, Lux + Jinx") from spectator-v5. Costs about one request per player per poll, roughly a fifth of the budget at a 2-minute poll; only worth it at a slow poll for recently active players.

Bigger or less certain:
- Prediction game for day record guesses.
- Seasonal month-long points race.
- Clip/quote of the day in recap flow.
- Weekly boss challenge mode with success/fail announcement.

Considered and skipped:
- Match timelines (gold diff at 15, first death time): one extra request per match and large payloads, against the memory budget.

### Unused match data

Checked 2026-10-27 against the 12,426 cached matches. These fields are in the cached payloads but not yet extracted into `player_match_stats`:

| Field | Cached matches with it |
|---|---|
| Ping counts (`enemyMissingPings`, `allInPings`, `dangerPings`, `onMyWayPings`, ...) | 12,422 |
| `challenges` (~100 stats: `skillshotsDodged`, `epicMonsterSteals`, `outnumberedKills`, `saveAllyFromDeath`, `flawlessAces`, `perfectGame`, ...) | 12,120 |
| `totalTimeSpentDead` | 12,422 |
| Team `bans` | 12,422 |
| `perks` (runes) and summoner spells | 12,422 |
| Atakhan and other team objectives | 9,137 |

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
  - Cached matches are kept forever in production (`MATCH_CACHE_RETENTION_DAYS=0`): the cache holds history Riot no longer serves.
  - `player_match_stats`: per-player match numbers extracted from the cache in the background (no Riot requests), the data layer for player analysis.
  - Weekly title belts in the events channel when a report week closes: 6 Glory and 2 Shame belts by best rate among players with 3+ ranked games, ties shared, `🔁 defended` when held the week before. New `player_match_stats` columns for pings, time dead, ally saves, outnumbered kills, skillshots dodged, objective steals and herald takedowns.
  - `!records [Name#Tag]`: Hall of Fame from ranked games (Solo/Duo, Flex, ranked 5v5) with group records, streaks, pentakills and career totals, or one player's personal bests with their place in the group; recaps get one "New group record" line per record broken. Recaps also get small `📈 Personal best` lines (players with 50+ ranked games; about 6 a month). The personal card also shows CS/min and longest game; kills in a row (Riot's largest killing spree) and a deathless-games count (0 deaths) are on both cards. A Jungle section on the Hall of Fame (any role): enemy jungle camps, buffs stolen, dragons and barons in one game, and career objective steals.
  - `!analyze Name#Tag [solo|flex]`: compact card with up to 3 strengths and 3 tips from the last 12 months of one ranked queue, each with its record; guarded by minimum samples and gaps; only the best duo partner is named.

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
