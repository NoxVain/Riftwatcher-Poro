# Contributing to Riftwatcher Poro

Thanks for helping out. This page is how we like changes to look, so your work can be merged as it is instead of being split up or reworked. It's short on purpose; the reasons behind each rule are in `ARCHITECTURE.md` → **Design Principles**.

## Before you write code

- **Read the Design Principles** in `ARCHITECTURE.md` and skim `IMPROVEMENT_BACKLOG.md` (what's planned, and what shipped recently and why).
- **Visible changes need the maintainer's OK first.** Anything a player sees in Discord is the maintainer's call: card layout and wording, new or removed sections, defaults (time windows, thresholds, which channel something posts in), new commands. Open an issue with a **mockup on real data** (a screenshot or the text the bot would send) and wait for a yes before opening the PR.
- **Don't undo recent decisions.** If something was added on purpose (check the backlog and `git log`), don't remove or rename it in passing. Raise it in an issue instead.
- **Bigger ideas start as an issue.** A short description and a mockup save everyone a rewrite.

## One change per PR

- A PR does **one thing**: one fix, one feature, or one refactor. If the description needs "and also", it's two PRs.
- Keep it small enough to review in one sitting. As a guide: one feature or fix, a few files, a few hundred lines of code plus its tests.
- Don't mix a bug fix into a feature PR. Send the fix on its own; it can be merged right away.
- Separate PRs can come from separate branches at the same time; say in the description if one depends on another.

## What every change needs

- **Offline tests** under `tests/` (`tests/test_<module>.py`, `test_<behaviour>`). Tests run without Discord, Riot or Postgres: pass fakes in. Never depend on today's date: give the code a clock it can be handed (`now=` or a module-level `_now()`), and freeze it in the test.
- **A log line** for every behaviour change, so we can see it working in the Railway logs.
- **Docs in the same PR:** `README.md` (features, commands, variables), `ARCHITECTURE.md` (how it works), `OPERATIONS.md` (logs to look for, what to do when it breaks), and a line under "Recently Completed" in `IMPROVEMENT_BACKLOG.md`. Keep each doc entry short and plain.
- **No new Riot requests** when an existing poll or the match cache already has the data. The personal key's 100 requests per 120 s are shared by every worker.
- **Mind memory:** page large database reads and never hold many match payloads at once.
- **Never block recaps or scoreboards.** Optional extras get a timeout and a fallback, and log their failures instead of raising.
- **Logic in pure modules**, wiring in `src/discord_bot.py`, command parsing and channel rules in `src/commands/`.
- **New settings are optional environment variables** with a safe default, listed in `README.md`, `SELF_HOSTING.md`, `.env.example` and `AGENTS.md`. Never commit secrets.

## Code style

- Match the code around you: naming, comment density, how errors and logs are written.
- PEP 8, 4-space indents, `snake_case`, `UPPER_CASE` constants, type hints on new code.
- Comments say **why**, not what. No dead code, no commented-out code, no stray debug prints.
- User-facing text is plain and short: sentence case, no exclamation marks, numbers with thousands separators.

## Commits and the PR description

- Commit messages: `type(scope): summary`, for example `fix(commands): split long plain-text fallbacks`. Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.
- The PR description (the template fills in the headings) covers:
  - **Purpose**: the problem, in a sentence or two.
  - **Changes**: what changed, file by file where it helps.
  - **Before / after** for anything visible, with the approved mockup.
  - **Test evidence**: the `pytest -q` output on the Python in `.python-version` (CI checks it too).
  - **Config / secrets impact**: new variables, schema changes, rebuilds; "None" if none.

## Data and privacy

- Use read-only database sessions for any production analysis, and share **aggregates only**.
- Don't paste production data (player stats, rank lists, real cards) into issues or PRs. Use made-up examples, or counts.
- Never print or commit credentials, tokens or connection strings.

## How review works

- CI (`pytest` on `.python-version`) must pass; `main` is protected.
- The maintainer squash-merges. A PR that bundles several changes, or changes something visible without approval, may be closed and the good parts taken over in separate PRs, with you credited as `Co-authored-by`.

## Quick checklist

- [ ] One change in this PR, and visible changes were approved with a mockup
- [ ] Tests added or updated, offline, no dependence on today's date
- [ ] `pytest -q` passes
- [ ] Log line for the new behaviour
- [ ] README / ARCHITECTURE / OPERATIONS / backlog updated
- [ ] No new Riot requests; large reads are paged
- [ ] No secrets or production data in the diff or description
