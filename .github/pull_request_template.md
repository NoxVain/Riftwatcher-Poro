## Purpose
<!-- The problem this solves, in a sentence or two. One change per PR (see CONTRIBUTING.md). -->

## Changes
<!-- What changed, file by file where it helps. -->

## Before / after
<!-- For anything players see: the approved mockup and the issue where it was agreed. Delete if nothing is visible. -->

## Test evidence
```
pytest -q
```

## Config / secrets impact
<!-- New environment variables, schema changes, stats rebuilds, or "None". -->

## Checklist
- [ ] One change in this PR; visible changes were approved with a mockup
- [ ] Offline tests added or updated (no dependence on today's date)
- [ ] Log line for the new behaviour
- [ ] Docs updated (README / ARCHITECTURE / OPERATIONS / IMPROVEMENT_BACKLOG)
- [ ] No new Riot requests; large reads are paged
- [ ] No secrets or production data in the diff or this description
