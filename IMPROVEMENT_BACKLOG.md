# Improvement Backlog

This backlog tracks work not yet implemented as of 2026-02-25.

## Source docs reviewed

- `SESSION_NOTES.md`
- `README.md`
- `ARCHITECTURE.md`
- `OPERATIONS.md`

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

- Runtime + DB + command module split completed.
- Streak callouts moved to separate messages and TTS toggle added.
- `!tts` routing allowed in events + recap channels.
- `!profile Name#Tag` command implemented.
- Command routing matrix tests added.
- Recap/streak separation integration test coverage added.
- GitLab test pipeline tightened for push + MR.
- Runtime reliability hardening:
  - previous-day message text corruption fixed
  - Riot 401 alert mark-on-success behavior
  - refresh cleanup state decoupled from message state
  - worker latency metrics export in `!health`
  - stalled-worker watchdog with deduped alert/recovery messages
- Scoring system adjustments shipped (no Scoring V2 rewrite):
  - stricter Wilson confidence default
  - weighted performance percentile
  - performance weight ramp by game count
- Ops/docs refresh:
  - added `OPERATIONS.md` runbook
  - synced `README.md` and `SESSION_NOTES.md` to current behavior
