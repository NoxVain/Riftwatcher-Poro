# Task Board

Use this file as the single source of truth for active AI-agent work.

## Status Legend
- `todo`
- `in_progress`
- `review`
- `done`
- `blocked`

## Active Tasks

| ID | Title | Owner Agent | Status | Branch | Scope | Notes |
|---|---|---|---|---|---|---|
| T-001 | Team playbook + task board bootstrap | Codex | done | `main` | `TEAM_PLAYBOOK.md`, `TASKS.md` | Completed in commit `353f362` |
| T-002 | Multi-agent workflow prep pack | Codex | done | `main` | `TASKS.md`, `AGENT_PROMPTS.md`, `.gitlab/merge_request_templates/AI_TASK.md` | Adds execution templates and MR guardrails |
| T-003 | First pilot task (fill before starting) | Codex | todo | `task/T-003-<slug>` | `<define scope>` | Create this from template below before running agents |

## Suggested Next Task (Ready To Fill)

### T-003 - <your first real task>
- Owner: Codex
- Status: todo
- Branch: task/T-003-<slug>
- Goal:
  - <one sentence outcome>
- In scope:
  - <specific files/modules>
- Out of scope:
  - <areas to avoid>
- Acceptance criteria:
  - [ ] implementation complete
  - [ ] tests added/updated
  - [ ] `pytest -q` passes
  - [ ] Claude review completed with no unresolved High findings
  - [ ] docs updated if behavior changed
- Handoff notes:
  - Pending task definition.

## Task Template

Copy this block for each new task:

```md
### T-XXX - <short title>
- Owner: Codex | Claude
- Status: todo | in_progress | review | done | blocked
- Branch: task/T-XXX-<slug>
- Goal:
  - <what done looks like>
- In scope:
  - <allowed files/modules>
- Out of scope:
  - <forbidden areas>
- Acceptance criteria:
  - [ ] behavior criteria 1
  - [ ] tests added/updated
  - [ ] `pytest -q` passes
  - [ ] docs updated if behavior changed
- Handoff notes:
  - <latest summary>
```
