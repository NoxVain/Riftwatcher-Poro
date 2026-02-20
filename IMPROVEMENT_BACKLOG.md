# Improvement Backlog

This backlog is derived from current project docs and only includes items that are not implemented yet.

## Source docs reviewed

- `SESSION_NOTES.md`
- `README.md`
- `ARCHITECTURE.md`

## Operational Improvements

1. Add per-worker success/error counters to health output
- Source: `SESSION_NOTES.md` ("Next Improvement Candidates")
- Current gap: `!health` reports uptime/player/db/cache status, but no per-worker counters.

2. Add log sampling or structured metrics export for cycle latency trends
- Source: `SESSION_NOTES.md` ("Next Improvement Candidates")
- Current gap: cycle timing is logged, but no sampled metrics stream/export.

## Scoring V2 (Draft)

3. Implement Gamer Score v2 composite model
- Source: `SESSION_NOTES.md` ("Scoring V2 Draft (Recovered)")
- Draft components:
  - Bayesian smoothing: `p_bayes = (wins + a) / (games + a + b)` with `a=b=3`
  - Confidence penalty: `conf = sqrt(games / (games + k))` with `k=8`
  - Role-aware normalized performance component (`perf_norm`)
  - Recency weighting component (`recency_norm`)
  - Composite:
    - `GamerScore = 100 * (0.65 * p_bayes * conf + 0.25 * perf_norm + 0.10 * recency_norm)`
- Current gap: production scoring is still Wilson-based gamer score.

## Feature Backlog

4. Daily MVP and Clutch/Int awards
- Source: `SESSION_NOTES.md` ("Future Fun Features Backlog")

5. Head-to-head command (`!vs Name1 Name2`) for today/week
- Source: `SESSION_NOTES.md`

6. Weekly title belts (example: Damage King, Objective Goblin, Vision Dad)
- Source: `SESSION_NOTES.md`

7. Prediction game for day record guesses and points
- Source: `SESSION_NOTES.md`

8. Profile command (`!profile Name#Tag`) with trend and best queue
- Source: `SESSION_NOTES.md`

9. Party synergy stats for duo/trio combinations
- Source: `SESSION_NOTES.md`

10. Seasonal month-long points race and winner announcement
- Source: `SESSION_NOTES.md`

11. Clip or quote of the day attachment in recap flow
- Source: `SESSION_NOTES.md`

12. Weekly boss challenge mode with success/fail announcement
- Source: `SESSION_NOTES.md`

## Excluded (already implemented)

- Ranked streak callouts in recap flow (with dedupe state).
