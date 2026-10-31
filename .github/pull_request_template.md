## Purpose
<!-- The problem this solves, in a sentence or two. One change per PR (see CONTRIBUTING.md). -->

## Changes
<!-- What changed, file by file where it helps. -->

## Before / after
<!-- For anything players see: the approved mockup (made-up data is fine) and the issue where it was agreed. Delete if nothing is visible. -->

## Test evidence
```
pytest -q
```

## Config / secrets impact
<!-- New environment variables, schema changes, stats rebuilds, or "None". -->

## Checklist
- [ ] One change in this PR; visible changes were approved with a mockup
- [ ] Nothing the maintainer declined before is back in this PR
- [ ] Offline tests added or updated (no dependence on today's date)
- [ ] Log line for the new behaviour, with values that vary
- [ ] Docs updated in the matching sections (README / ARCHITECTURE / OPERATIONS / IMPROVEMENT_BACKLOG)
- [ ] No new Riot requests; large reads are paged
- [ ] No secrets or production data in the diff or this description
