# Agent Prompts (Copy/Paste)

Use these prompts to run Codex and Claude as a coordinated team.

## 1) Codex - Task Execution

```text
Implement TASK <ID> from TASKS.md.

Rules:
- Stay strictly within "In scope".
- Do not edit "Out of scope" files.
- Keep changes minimal and production-safe.
- Add/update tests for changed behavior.
- Run pytest -q and report the result.
- Prepare a handoff using TEAM_PLAYBOOK.md format.

Deliverables:
1) Changed files
2) Test results
3) Risks / open questions
4) Suggested PR title using type(scope): summary
```

## 2) Claude - Findings-First Review

```text
Review TASK <ID> changes using TEAM_PLAYBOOK.md review template.

Requirements:
- Findings first, ordered High -> Medium -> Low.
- Include file:line references for each finding.
- Focus on regressions, correctness, reliability, and missing tests.
- Keep summary brief after findings.

Output format:
1) Findings
2) Open Questions
3) Residual Risk
4) Required fixes before merge
```

## 3) Codex - Apply Review Fixes

```text
Apply reviewer feedback for TASK <ID>.

Rules:
- Address all High findings before merge.
- Update/add tests where needed.
- Run pytest -q and report outcome.
- Return a short "fixes applied" changelog.
```

## 4) Claude - Re-Review After Fixes

```text
Re-review TASK <ID> after fixes.

Decide:
- Approve for merge, or
- Block with remaining findings (with severity and file:line).
```

## 5) Merge Decision Checklist

Only merge when all are true:
- `pytest -q` green
- no unresolved High findings
- scope constraints respected
- docs updated if behavior changed
