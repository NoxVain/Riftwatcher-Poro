# Riftwatcher Poro Architecture

## Purpose

Riftwatcher Poro is a Discord worker service that tracks ranked League results for tracked Riot IDs and maintains:

- two daily scoreboard messages in the daily channel:
  - previous day snapshot message (top)
  - current day live scoreboard message (second)
- one live weekly scoreboard message (Monday `REPORT_DAY_START_HOUR` -> next Monday aggregate)
- match recap messages and ranked streak callouts
- rank change event messages
- optional esports news (schedules, live/game/series results)
- League patch-notes announcements

## Design Principles

These are the priorities behind most decisions in this repo. New changes should follow them; when one has to give, the order below is the order of importance.

1. **Core posts never get lost or stuck.** Recaps and scoreboards are the product. Everything optional around them must degrade, never block:
   - the recap cursor is saved *before* sending, so a bad message can't cause endless retries;
   - a rejected card falls back to plain text, a missing portrait to the champion name, missing baselines to pure Wilson scoring, missing LP to no LP;
   - optional work (LP, portraits, baselines, LP history) runs with timeouts and logs its failures instead of raising.

   The 2026-10-26 incident (OPERATIONS.md) is the cautionary example: one stalled optional read froze scoreboards and recaps.
2. **The Riot rate limit is fixed and shared.** A personal key allows 20 requests/1 s and 100/120 s for *all* workers combined. New features reuse data the bot already fetches; LP, for example, comes from the rank worker's existing poll. Backfill is lowest priority and backs off first.
3. **Memory is money.** On Railway, resident memory is ~97% of the bill. Never hold many match payloads at once: page through DB reads, keep only the numbers needed, and return freed memory to the OS (`src/runtime/memory.py`). Check with `railway metrics --memory` after deploys.
4. **Pure logic, thin wiring, offline tests.** Parsing, formatting, scoring and LP math live in plain modules (`*_logic.py`, `discord_text.py`, `recap_cards.py`, `recap_lp.py`) that are unit-tested without Discord, Riot or Postgres. Workers receive their dependencies (`db_get_state`, `riot_client`, …) as parameters so tests can pass fakes. `discord_bot.py` only wires things together.
5. **Observable by default.** Every worker logs a cycle line; important events log one line each (recap posted, LP added, portrait uploaded, card rejected). The stall watchdog alerts in Discord and logs a stack dump of every thread and task. Every deploy is checked in the Railway logs, and a deploy isn't "done" until the workers cycle cleanly.
6. **What users see is the maintainer's call.** Defaults and visible layout (champion display, recap format, what a recap shows) change only when the maintainer agrees, ideally after an issue or a mockup. Contributor PRs that change them get reworked in a separate PR rather than merged as-is (see `CONTRIBUTING.md`). Old messages and stored state keep working after a format change (e.g. pending LP edits on older recaps).
7. **Safe by construction.** No secrets in the repo (environment variables only, Railway variables stay unmanaged by IaC). The client sends no pings (`AllowedMentions.none()`), user-facing text is escaped, mutating commands need admin rights, and data analysis against production uses read-only sessions.
8. **Small, reviewed, verified changes.** Branch, then a PR (purpose, changes, test evidence, config impact), then CI on the `.python-version` Python, then squash-merge (`main` is protected), then verify the Railway deploy. Docs change in the same PR as the behaviour. One change per PR. Contributors whose work is reused get `Co-authored-by` credit, and a thank-you when their PR is superseded. The contributor rules are in `CONTRIBUTING.md`.

## Runtime Layers

- `src/app.py`
  - process entrypoint (`python -m src.app`)
- `src/discord_bot.py`
  - dependency wiring
  - Discord client events
  - background worker scheduling
  - startup logging and scoreboard lifecycle
- `src/config.py`
  - environment loading/validation (required vars fail fast at import)
- `src/constants.py`
  - `!command` names
- `src/discord_command_handlers.py`
  - command entrypoint that delegates into modular handlers
- `src/commands/`
  - command context (`context.py`), channel policy (`routing.py`), author permissions (`permissions.py`), and split handlers (`ops`, `player`, `report`, `slash`)
  - `slash_handlers.py` handles `/health` and the admin-only `!sync` command
- `src/discord_text.py`
  - pure text helpers (request ids, safe user-facing errors, health report, recap/streak/queue formatting, report signatures)
- `src/poro_service.py`
  - service facade for report generation/cache policy
  - delegates refresh/report internals to `src/services/`
- `src/services/`
  - `report_builder.py` (scoreboard text, badges), `refresh.py` (daily stats refresh, cache backfill), `baselines.py` (role baselines for Gamer Score)
- `src/riot_api.py`
  - Riot API calls (account/summoner/league/matches)
  - client-side rate limiting, retry/backoff behavior
  - in-memory + DB match cache integration
- `src/discord_recap_worker.py`
  - new-match detection and recap posting
  - post-recap stats resync
- `src/discord_rank_worker.py`
  - rank-state diffing and alert posting
- `src/discord_backfill_worker.py`
  - low-priority older-match DB cache fill
- `src/discord_esports_worker.py`
  - pro play news cycle: daily schedule, live/game/series announcements
- `src/esports_api.py`
  - LoL Esports web API client (`getLeagues`, `getSchedule`), league slug -> ID resolution with built-in fallbacks
- `src/esports_logic.py`
  - pure event parsing, snapshot diffing, and message formatting
- `src/discord_patch_worker.py`
  - patch-notes cycle: silent first baseline, new-article detection, posting, seen-state persistence
- `src/patch_notes_api.py`
  - leagueoflegends.com client: patch-notes tag page (article list) and article page (best-effort change list)
- `src/patch_notes_logic.py`
  - pure parsing of the embedded Next.js page data (`__NEXT_DATA__`) and article `<h2>`/`<h3 class="change-title">` structure, message formatting, seen-state and scheduling helpers
- `src/runtime/`
  - shared runtime worker loops (`workers.py`), message-store helpers (`message_store.py`), alerts and the stall watchdog (`alerts.py`), returning freed memory to the OS (`memory.py`), and stack dumps for stall alerts (`diagnostics.py`)
- `src/db/`
  - connection pool (`pool.py`)
  - schema creation/migrations (`schema.py`)
  - persistence helpers split by concern (`state.py`, `players.py`, `cache.py`, `stats.py`, `ranked_state.py`)
- `src/report_logic.py`
  - pure queue/window/ranking helpers, Gamer Score and Wilson math, remake detection
- `src/rank_logic.py`
  - rank normalization/comparison and rank-message text
- `src/recap_lp.py`
  - LP events from rank-worker polls, LP math/formatting, and the pending-recap/event join state
- `src/arena_static.py`
  - Arena augment (CommunityDragon) and item (Data Dragon) display names; successful loads are cached per process, failed sources retry after 10 minutes; recaps fall back to raw IDs
- `src/recap_cards.py`
  - match recap cards (Components V2): accent by result, packing within Discord limits with LP reserve, per-player text components, LP edit by component ID
- `src/champion_icons.py`
  - recap champion display mode (`name`/`icon`/`both`) and portrait emojis: Data Dragon portraits uploaded once as application-owned emojis named after the champion (`:Ahri:`, `:KaiSa:`; punctuation/spaces dropped), mapped from the numeric champion ID via the Data Dragon catalog; emojis from the earlier `rw_champ_<championId>` naming are renamed in place, not re-uploaded; serialized lookups, 5-minute emoji cache and failure backoff, daily catalog cache, 10-second lookup timeout, bounded downloads
- `src/puuid_remap.py`
  - rewrites old PUUIDs in `match_info_cache` to the tracked players' current ones. Riot encrypts PUUIDs per API key project, so payloads cached before the Feb 2026 key change named players by PUUIDs that lookups no longer match. Old→current pairs come from participants whose Riot ID matches a tracked player; ambiguous cases are skipped. The rewrite runs in Postgres on the payload text (pages of 500 match IDs, `updated_at` untouched). Runs as a background task 5 minutes after startup, once per set of tracked players (`puuid_remap_fingerprint`); a new player or a future key change triggers it again
- `src/match_stats.py`
  - per-player match stats: one compact row per tracked player per cached match in `player_match_stats` (about 35 numbers: result, champion, role, KDA, kill participation, damage share, gold/CS diff against the lane opponent, vision, objectives, tracked teammates), so analysis queries a year of games in milliseconds instead of parsing payloads. Rows come only from cached payloads (no Riot requests). A background loop starts after the PUUID remap and runs every 10 minutes. The first pass and any change to the tracked players or `STATS_VERSION` trigger a full rebuild (paged, about 16k rows) that also drops rows of untracked PUUIDs; later passes read only matches cached since the watermark, minus a 5-minute overlap
- `src/player_analysis.py`
  - `!analyze Name#Tag [months] [solo|flex]` (handler in `src/commands/analysis_handlers.py`): one queue's ranked games from the last 3 calendar months (1-12 with `months`; a tracked name ending in a number stays a name) from `player_match_stats` (Solo/Duo unless `flex`; remakes excluded), compared with the player's own splits and with per-role totals of all tracked players. Rule-based findings: best/weakest champions, off-roles against the main role (with lane gold), KDA and deaths against the group's players in the main role, trend (the last 90 days against the rest of the window, or each half of a window shorter than 180 days), tilt (the game within 70 minutes of a loss), sessions (game 4+ of a report day), laning CS trend, best duo partner. Every finding needs a minimum sample (10 games per champion/role, 15 per partner, 20 on each side of a split, 50 group games per role baseline) and a clear gap; the card shows at most 3 strengths and 3 tips, strongest first (gap × √games). Only the best duo partner is ever named. Sent as a Components V2 card with a plain-text fallback (split at line breaks into 2,000-character messages, nothing dropped); no Riot requests
- `src/records.py`
  - Hall of Fame (handler in `src/commands/records_handlers.py`): `RecordBook` pages through ranked rows of `player_match_stats` (Solo/Duo, Flex and ranked 5v5 only; remakes excluded) and keeps each player's best per category, ranked win/loss streaks (across the three queues, other games ignored) and career totals. Categories: kills, kills in a row (Riot's `largestKillingSpree`: most kills without dying in one game), damage, team damage share (15+ min games), solo kills, CS, CS/min (20+ min), gold, vision, assists, enemy jungle minions, buffs stolen, dragons, barons (Jungle section, any role, group card only), deaths, damage taken, longest game. `!records` shows the group record per category (a tie keeps the earlier game, then name order for teammates in the same game; the header counts unique matches, so a game with several tracked players counts once) plus career objective steals (Jungle section), streaks, pentakills, deathless games (0 deaths) and most games; `!records Name` shows that player's bests with their place in the group (#1-#3): Best (kills, kills in a row, damage, solo kills, CS, CS/min, vision, assists, win streak, pentakills, deathless games) and Worst (deaths, longest game, loss streak); zero values are hidden. Sent as a Components V2 card with a plain-text fallback (split at line breaks into 2,000-character messages, nothing dropped); no Riot requests
  - recap record lines: before posting, the recap worker compares the batch's ranked games with every stored ranked game that ended earlier (`detect_record_breaks`); a value strictly above the group record adds one line per record under that player's stats (`🏆 New group record: 34 kills (was 29, JonastCvuHU on Akshan, Dec 2025)`). When several tracked players in one game pass a record, the best of them takes it; a later game in the same batch must beat it. No lines until 200 ranked games are stored. The check has a 10-second limit and never blocks the recap. The worker then upserts the batch's rows into `player_match_stats`, so the next recap compares against them before the stats sync's next pass
- `src/wrapped.py` (handler in `src/commands/wrapped_handlers.py`)
  - Poro Wrapped: the year in review from `player_match_stats` (all queues, remakes excluded, paged by `db_iter_wrapped_rows`) plus each player's current rank (`player_ranked_state`; there is no rank history before 2026-10-26, so no peak rank). Ranked games are the main event; ARAM and Arena get their own sections. A background task checks every 10 minutes (first check 15 minutes after startup) and posts the group cards once in `EVENTS_CHANNEL_ID` on 31 December at 18:00 (`REPORT_TIMEZONE`), covering 1 January until then: headline numbers, the Poro Awards (ranked unless marked all modes; 20+ games to qualify, duos 25+ ranked games together), ranked records of the year, ARAM and Arena, and everyone's current rank. The year is marked in `bot_state` (`poro_wrapped_posted_year`) before sending, so a failed send never reposts; a late start still posts until the end of January. `!wrapped Name` (personal card: overview, Ranked with champions, duo partners, highs and lows, then ARAM, Arena, a title and a banter line) works only once the group post is out and until the end of January; before that (and after) the command is ignored without a reply, before channel routing, so nothing hints at it, and it isn't in `!help`. Banter lines are fixed templates behind thresholds. Cards split into several messages near Discord's 4,000-character limit; a rejected card falls back to plain text in 2,000-character messages. No Riot requests
- `src/belts.py`
  - weekly title belts: a background task checks every 10 minutes (first check 15 minutes after startup) and, once a report week (Monday `REPORT_DAY_START_HOUR` to Monday, `REPORT_TIMEZONE`) has been closed for 20 minutes, posts one card for it in `EVENTS_CHANNEL_ID`. Eight belts from that week's ranked games in `player_match_stats` (Solo/Duo, Flex, 5v5; no remakes): Glory (Damage King damage/min, Vision Dad vision/min, Objective Goblin turrets+dragons+barons+heralds a game, Bodyguard ally saves a game, Outnumbered kills while outnumbered a game, Matrix skillshots dodged/min) and Shame (Backseat Gamer pings a game, Grey Screen Enjoyer share of game time dead). A player needs 3 ranked games that week; the best rate wins, ties share the belt, a belt with no positive value is left out. Holders are stored in `bot_state` (`weekly_belts_state`); a holder of the same belt the week before is marked `🔁 defended` (×N from the third week in a row). The first run stores the last closed week's holders without posting. The week is marked before sending, so a failed send never reposts; a rejected card falls back to plain text. No Riot requests

## Worker Model

All workers run continuously while connected, each with startup jitter and cycle heartbeat logs. Every cycle records ok/error counts and last/avg/max latency in `WORKER_STATS`, which `!health` and `/health` display. Postgres is required (`DATABASE_URL`, so `DB_ENABLED` is always true); workers are scheduled from `on_ready` and are not duplicated on reconnect.

- Refresh worker
  - interval: `max(30, DAILY_REFRESH_SECONDS)`
  - rebuilds daily stats for all tracked players
  - pushes snapshot scoreboard updates with throttling (changed content at most every 30s, forced every 120s and at cycle end)
  - refreshes the weekly scoreboard message
  - runs DB match cache cleanup at most once per `max(3600, DAILY_REFRESH_SECONDS)` seconds
- Rank worker
  - interval: `max(30, DAILY_REFRESH_SECONDS)`
  - compares current ranked entries with `player_ranked_state`
  - sends rank up/down messages to `EVENTS_CHANNEL_ID`
  - first observation is baseline only (no alert); queues that disappear from Riot's response are removed from the baseline
  - every observed change in a queue's rank, LP or win/loss counts is appended to `lp_history` (`build_lp_history_row`); a failed insert is logged and never blocks alerts or rank-state persistence
  - LP for recaps (`src/recap_lp.py`): when a Solo/Duo or Flex queue's counters moved by exactly one game with a consistent LP change (wins gain LP; losses lose LP or stay at 0), it builds an LP event (100 LP per division, shared Master+ ladder). It edits the matching pending recap in `MATCH_RECAP_CHANNEL_ID` (same queue and result, game ended up to 30 minutes before the poll, newest first) or stores the event for the recap worker. If the recap channel can't be resolved, the pending recap is kept and the event stored; each poll retries pairing stored events with pending recaps. No extra Riot requests; LP failures are logged and never block rank-state persistence
- Recap worker
  - interval: `max(30, MATCH_RECAP_POLL_SECONDS)`
  - finds new matches using persisted per-player recap key (`last_announced_match_id::{riot_id}`); a player's first observation is baseline only
  - posts recap messages in `MATCH_RECAP_CHANNEL_ID` (queue label, local end time, duration, per-player line items); remakes and matches without tracked players are skipped
  - labels ranked queues plus common non-ranked queues (normals, Swiftplay/Quickplay, ARAM, ARAM Mayhem, Clash, rotating modes, Co-op vs AI)
  - formats Arena (`1700`/`1710`/`1750`) recaps with placement/team, damage share, augments/items, and skillshot challenge stats
  - posts each match as a card (`src/recap_cards.py`, Discord Components V2): one container per match with the compact header (`-# queue · **result** · game length`, from `match_result`) and player lines (`format_recap_player_line`), result-coloured accent, one text component per player; packed within 40 components / 4000 characters per message (an LP reserve is kept per pending player; oversized text is shortened, never rejected), messages 3s apart
  - ranked games that set a group record get one `🏆 New group record` line per record under the player's stats, then one small `-# 📈 Personal best` line per Best category of `!records Name` the player beat (players with 50+ stored ranked games; not where the same game set the group record) (`src/records.py`)
  - if Discord rejects a card (`HTTPException`), the same batch is sent as plain text (2000-char messages split at player boundaries). The recap cursor is still saved before sending, so a failed send can't cause endless retries
  - pending LP stores the player's card component ID; the LP edit (`edit_recap_with_lp`) rebuilds the card with `LayoutView.from_message` and edits only that component. Plain-text recaps still match the player's heading line. Fetch-then-edit runs under `LP_EDIT_LOCK` so two players' edits on one message can't overwrite each other
  - posts ranked win/loss streak callouts in `MATCH_RECAP_CHANNEL_ID` as separate messages (wins: 3-4 Momentum, 5-7 Heater Alert, 8-9 LEGENDARY, 10+ FINAL BOSS UNLOCKED; losses: 3-4 Cold Streak, 5-7 Tilt Watch, 8-9 FULL TILT, 10-12 UNINSTALL PROTOCOL, 13+ BIOHAZARD IN THE LOBBY); deduped by `last_announced_streak::{riot_id}` token
  - streak callouts use Discord TTS by default; toggle with `!tts on|off|status`
  - champion display per `!champdisplay name|icon|both` (default `icon`); portrait modes fall back to the name, and a batch spends at most 15 seconds on new portrait lookups before using cached ones only. The LP edit matches the recap's first line as posted, so portraits don't affect it
  - lookback window: 20 matches
  - a player's recap key only advances when all of their new matches fetched successfully (failed fetches retry next cycle)
  - Solo/Duo and Flex lines take a stored LP event if the rank worker saw the game first; otherwise, after sending, the line is registered as pending (message ID + heading line) for the rank worker to edit. A recap is skipped if its heading appears more than once in the batch. Pending lines and events expire after 30 minutes; a shared `asyncio.Lock` serializes both workers' updates
  - refreshes affected players' daily stats and forces daily + weekly scoreboard update
- Backfill worker
  - interval: `max(120, DAILY_REFRESH_SECONDS * 2)`
  - runs every cycle; pages backwards through each player's match history using a persisted `backfill_offset::{riot_id}` cursor
  - caches up to 3 uncached older match payloads per player per cycle
  - stores only in DB cache for backfill fetches (`cache_in_memory=False`)
  - Riot requests use the `backfill` tier: capped at 80 of the 100-per-120s budget and paused for `max(120s, 3 x Retry-After)` after any `429`
- Stall watchdog
  - interval: 30s
  - alerts in `EVENTS_CHANNEL_ID` when a worker exceeds its stale threshold; deduped via `worker_stall_alert_sent::{worker}` in `bot_state`, with recovery messages
  - thresholds: refresh/rank `max(90, max(30, DAILY_REFRESH_SECONDS) * 3)`, recap `max(90, max(30, MATCH_RECAP_POLL_SECONDS) * 3)`, backfill `max(300, backfill interval * 6)` (overridable via `BACKFILL_STALE_THRESHOLD_SECONDS`), esports `max(300, max(30, ESPORTS_POLL_SECONDS) * 5)`, patch `PATCH_NOTES_POLL_SECONDS * 4`
- Esports worker (only when `NEWS_CHANNEL_ID` is set)
  - interval: `max(30, ESPORTS_POLL_SECONDS)`
  - fetches `getSchedule` per league in `ESPORTS_LEAGUES` and diffs against `esports_match_snapshot` in `bot_state`
  - first observation per league is baseline only (no alerts)
  - posts: live (`unstarted` -> `inProgress`), game win (`gameWins` increased, except the deciding game), series win (`completed`)
  - snapshot never regresses: responses with an earlier state or lower score than stored (stale API cache) are ignored
  - team order is pinned to the first-seen order per match (stored `codes`); the API swaps sides between games
  - a `completed` match whose score doesn't yet show a series winner is held (`pending_since`) for up to 15 minutes, then posted with the winner's tally settled
  - posts today's schedule once per report day (`esports_schedule_posted_cycle`), only from a fully successful fetch
  - on Monday's report day, first posts the week's schedule (Mon-Sun report days, split across messages if over 2000 chars); a missed Monday is skipped
  - the weekly post is tracked in `esports_weekly_schedule_posted_week` (JSON: week, message IDs, rendered text) and edited in place for the rest of the week when matchups or times change (e.g. TBD slots resolving). It has no LIVE/score markers, so results don't cause edits. If it grows, an extra message is appended; if it's deleted, tracking stops until next Monday
  - a resolved matchup reverting to TBD is treated as a stale response; schedules keep the stored team names (`names` in the snapshot)
  - stale threshold: `max(300, poll * 5)`; raises (counts as error) only when every league fetch fails
  - league slugs resolve via `getLeagues`, falling back to built-in IDs for `worlds`, `msi`, `first_stand`
  - a missing/rejected `ESPORTS_API_KEY` (`401`/`403`) posts one alert in `NEWS_CHANNEL_ID` (`esports_auth_alert_sent`) and a recovery notice after the first successful poll
  - snapshot entries no longer returned by the API are dropped 7 days after their start time
- Patch notes worker (unless `PATCH_NOTES_ENABLED=false`)
  - interval: `PATCH_NOTES_POLL_SECONDS` (default 6h, min 1h); after an error it retries after `min(poll, 3600)`
  - sized for cheap hosting: one gzip page request (~60 KB) per check, and the CDN ignores conditional requests, so the poll interval is the cost control. Riot publishes notes about every 2 weeks (Tuesdays ~18:00 UTC)
  - persists `patch_notes_last_checked_at`; on startup it waits out the remaining interval, so restarts/redeploys don't add requests
  - first observation (no `patch_notes_seen`) is baseline only
  - posts each unseen article published within the last 7 days in `EVENTS_CHANNEL_ID`, oldest first; the article page is fetched only then, for the changed champion/item names (falls back to title/blurb/link)
  - an article counts as seen by CMS id or URL, so a change to one of them doesn't cause a repost; seen-state keeps the latest 50 keys
  - listing fetch/parse failures and Discord send failures count as errors (stall watchdog alerts after 4 missed polls); seen-state is saved after each posted article

## Data Flow

1. Startup
   - load env/config
   - initialize DB schema
   - load tracked players
   - construct Riot client, `PoroService` report service, and (when enabled) the esports and patch-notes clients
   - on first `on_ready`: initialize daily + weekly scoreboard messages and schedule workers (reconnects reuse running workers)
2. Command flow (`!Daily`)
   - delete the invoking message (if permitted) and take the report lock (concurrent `!Daily`/`!Weekly` get a "please wait" reply)
   - show the stored snapshot immediately, then quick-refresh the last 20 matches per player and edit in the result
   - ensure both daily tracked messages exist
   - update current-day message
   - on day rollover, copy prior day final content into previous-day message (dated header), then continue updating current-day message
3. Command flow (`!Weekly`)
   - aggregate stored `player_daily_stats` rows from Monday day-start cutoff through next Monday cutoff
   - update persisted weekly scoreboard message
4. Refresh flow
   - pull ranked match info for current report day
   - upsert per-player daily stats
   - push daily snapshot scoreboard updates during/after cycle
   - refresh weekly scoreboard snapshot from DB aggregates
5. Recap flow
   - detect newly seen matches
   - post recap text
   - resync daily stats for affected players
   - refresh daily + weekly scoreboard messages
6. Rank flow
   - load persisted ranked baseline
   - fetch live ranked entries
   - post up/down messages if rank level changed
   - persist new baseline

## Gamer Score

Scoreboards sort by Gamer Score (0-100), then win rate, games played, and name.

- Win component: Wilson lower bound of ranked wins/losses with `z = 1.40`.
- Performance component: weighted percentile of per-minute stats (CS, champion damage, objective damage, healing, damage taken, kills, deaths inverted, vision) against same-role baselines.
- Weights: 65% win / 35% performance; the performance share ramps linearly over the first 8 ranked games.
- Baselines are built from cached ranked, non-remake matches (`BASELINE_MATCH_LIMIT`), need 20+ samples per role/stat, and are shown in `!health`. Payloads are read from Postgres in pages inside a worker thread: one small query lists the newest match IDs, then each page of 50 payloads is a short primary-key query (no server-side cursor or long transaction), keeping only per-minute numbers; afterwards freed memory is returned to the OS (`src/runtime/memory.py`). `!backfill` scans the cache the same way. Rebuilds run as one background task with a 120 s limit; callers never wait on it once baselines exist (before the first build they wait at most 30 s, then score with Wilson only).
- Without a baseline or primary role the score is pure Wilson x 100.

Badges (`report_builder.py`) are separate from the score: per-minute rates shrunk toward the group rate (prior of 2 games daily, 1 weekly), minimum 2 games daily / 3 weekly, plus a Grinder badge for most games.

## Riot API Client

- Proactive limiter sized for personal keys: 20 requests/1s and 100 requests/120s, shared across all workers.
- Up to 3 attempts per request: request errors and `408`/`425`/`5xx` use exponential backoff with jitter; `429` sleeps for `Retry-After`.
- `401` from any endpoint, or `403` from account-v1/status-v4, triggers the persisted one-time key alert in `EVENTS_CHANNEL_ID`. Other `403`s are confirmed with a `lol/status/v4` probe (throttled to once per 5 minutes) so endpoint restrictions don't raise false alarms.
- After an alert, the first successful response posts a recovery message and resets `riot_401_alert_sent` (`on_authorized` callback; in-memory fast path, one DB read per process).
- `RIOT_REQUEST_YIELD_SECONDS` adds a pause before each request on low-resource hosts.

## Persistence Model

- `tracked_players`
  - source of tracked Riot IDs and PUUID cache
- `player_daily_stats`
  - per cycle/day wins/losses and aggregate performance stats, keyed by `(day_date, riot_id)`
  - includes expanded fields: assists, gold, wards, objective takedowns, multi-kills, KP numerator/denominator, and `primary_role`
- `match_info_cache`
  - cached Riot match payloads, cleaned up `MATCH_CACHE_RETENTION_DAYS` after they were cached (`0` = never; production keeps everything because the cache holds history Riot no longer serves, see OPERATIONS.md)
- `bot_state`
  - key/value operational state:
    - scoreboard message ids (`last_report_*`, `last_weekly_report_*`, previous-day message + cycle keys)
    - per-player cursors: `last_seen_match_id::`, `last_announced_match_id::`, `last_announced_streak::`, `backfill_offset::`
    - per-player LP joins (JSON lists, 30-minute expiry): `recap_lp_pending::` (posted recap lines awaiting LP), `recap_lp_events::` (LP changes awaiting a recap)
    - `streak_tts_enabled`
    - `champion_display_mode` (`name` | `icon` | `both`; unset = `icon`)
    - `puuid_remap_fingerprint` (hash of tracked Riot IDs + PUUIDs the cache was last remapped for)
    - `player_match_stats_fingerprint` (tracked PUUIDs + `STATS_VERSION` of the last full stats rebuild), `player_match_stats_cached_through` (newest `match_info_cache.updated_at` processed), `player_match_stats_version` (`STATS_VERSION` of the last completed full pass; recap record checks wait until it matches, so a half-rebuilt column never announces a false record)
    - `weekly_belts_state` (start of the last report week with belts, and each belt's holders with their consecutive weeks held)
    - `poro_wrapped_posted_year` (the last year whose Wrapped group post went out)
    - alert dedupe flags: `riot_401_alert_sent`, `worker_stall_alert_sent::{worker}`, `esports_auth_alert_sent`
    - esports state: `esports_match_snapshot`, `esports_schedule_posted_cycle`, `esports_weekly_schedule_posted_week`
    - patch-notes state: `patch_notes_seen` (JSON list of posted article ids/URLs), `patch_notes_last_checked_at` (unix seconds)
- `player_ranked_state`
  - last known rank state per `(riot_id, queue_type)`
- `lp_history`
  - append-only log of every rank/LP change the rank worker observes (Riot keeps no LP history): player, queue, `observed_at`, `won` (only when exactly one game happened; NULL for dodge/decay, several games or resets), `wins_delta`/`losses_delta`, `lp_delta` (100 LP per division, shared Master+ ladder), and the rank before and after
- `player_match_stats`
  - one row per tracked player per cached match, keyed by `(puuid, match_id)`, indexed by `(puuid, game_end_at)`; derived from `match_info_cache` by `src/match_stats.py` and rebuildable from it at any time. Arena rows carry `arena_placement` (all Arena queues since `STATS_VERSION` 6; before that only queue 1700); remakes are flagged, not dropped. `penta_kills` was added with `STATS_VERSION` 2 and the belt columns (`herald_takedowns`, `objective_steals`, `ally_saves`, `outnumbered_kills`, `skillshots_dodged`, `pings`, `time_dead_seconds`; NULL where older payloads lack them) with `STATS_VERSION` 3, `killing_spree` with `STATS_VERSION` 4, and `enemy_jungle_camps` and `buffs_stolen` with `STATS_VERSION` 5 (each bump makes the next pass rebuild every row). The recap worker also upserts its ranked games here (see `src/records.py`)

Schema creation and additive migrations (`ADD COLUMN IF NOT EXISTS`) run on every startup in `src/db/schema.py`.

## Channels

- `DAILY_REPORT_CHANNEL_ID`: daily scoreboard + `!Daily` command
- `WEEKLY_REPORT_CHANNEL_ID`: weekly scoreboard + `!Weekly` command
- `EVENTS_CHANNEL_ID`: automatic posts: rank alerts, patch-notes posts, weekly title belts, Poro Wrapped, and Riot-key/stall alerts
- `MANUAL_COMMANDS_CHANNEL_ID` (optional; `EVENTS_CHANNEL_ID` when unset): the commands channel for `!help`, `!health`, `/health`, `!score`, `!test`, `!riottest`, `!Add`, `!remove`, `!DebugPlayer`, `!profile`, `!analyze`, `!records`/`!record`, `!wrapped`, `!backfill`, `!tts`, `!champdisplay`
- `MATCH_RECAP_CHANNEL_ID`: match recap posts, streak callouts, `!streak`, `!tts`, and `!champdisplay` commands
- `NEWS_CHANNEL_ID` (optional): esports schedule and live/game/series updates; feature disabled when unset

All required channels must be set. The only fallback is the optional commands channel, which falls back to the events channel. `!sync` (administrators only) works in any server channel.

Command authorization (`src/commands/permissions.py`) runs after channel routing: `!Add`, `!remove`, `!backfill`, `!tts on|off`, and `!champdisplay name|icon|both` require Manage Server/Administrator or the optional `ADMIN_ROLE_ID` role. The client is created with `AllowedMentions.none()`, so no outgoing message can ping. User-facing command errors go through `discord_text.format_user_error` (safe category + request ref); raw exception text is logged only.

## Key Operational Behaviors

- Snapshot throttling avoids unnecessary Discord edits in refresh cycles.
- Oldest-data-first player ordering is used in full refresh cycles.
- Daily and weekly tracked message references survive transient Discord fetch errors; message state is reset only on `NotFound`/`Forbidden`.
- Riot `401`/`403` key rejections trigger one persisted alert flag to prevent notification spam; the flag resets with a recovery message once requests succeed again.
- Riot `429` retries are handled in the Riot client.
- `ASYNCIO_THREAD_POOL_SIZE` installs a custom default executor once (not again on reconnect).
