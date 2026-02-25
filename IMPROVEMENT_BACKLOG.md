# Improvement Backlog

This backlog tracks work not yet implemented as of 2026-02-25.

## Source docs reviewed

- `SESSION_NOTES.md`
- `README.md`
- `ARCHITECTURE.md`
- `OPERATIONS.md`

## Operational Improvements

1. Add lightweight metrics export for worker cycle latency and error rates
- Current gap: cycle timings are logged, but there is no persistent metrics sink/dashboard.

2. Add alerting for stalled workers
- Current gap: worker cycles/errors are visible via `!health`, but no proactive alert when a worker stops progressing.

## Feature Backlog

3. Profile command (`!profile Name#Tag`) with trend and best queue
4. Head-to-head command (`!vs Name1 Name2`) for day/week
5. Daily MVP + Clutch/Int awards
6. Weekly title belts (Damage King, Objective Goblin, Vision Dad)
7. Prediction game for day record guesses
8. Party synergy stats for duo/trio combinations
9. Seasonal month-long points race
10. Clip/quote of the day in recap flow
11. Weekly boss challenge mode with success/fail announcement

## Recently Completed (Removed From Backlog)

- Runtime + DB + command module split completed.
- Streak callouts moved to separate messages and TTS toggle added.
- `!tts` routing allowed in events + recap channels.
- Command routing matrix tests added.
- Recap/streak separation integration test coverage added.
- GitLab test pipeline tightened for push + MR.
- Runtime reliability hardening:
  - previous-day message text corruption fixed
  - Riot 401 alert mark-on-success behavior
  - refresh cleanup state decoupled from message state
- Scoring system adjustments shipped (no Scoring V2 rewrite):
  - stricter Wilson confidence default
  - weighted performance percentile
  - performance weight ramp by game count
- Ops/docs refresh:
  - added `OPERATIONS.md` runbook
  - synced `README.md` and `SESSION_NOTES.md` to current behavior
