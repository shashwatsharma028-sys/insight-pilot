# CLAUDE.md — InsightPilot V2 Development Safety Contract

## Primary non-negotiable goal
Preserve the fully working InsightPilot project for the college summer-internship external examination. The presentation-ready code is represented by GitHub commit `fd5783c6202e3ea506a69e607160617a23eed807`, copied to branch `exam-backup-2026-11-17`. Treat it as a **read-only preservation snapshot**. It has not been independently smoke-tested by the reviewer; do not imply otherwise.

## Branch restrictions
- All new feature work MUST happen on `feature/insightpilot-v2` or feature branches created FROM that branch.
- NEVER make commits, pushes, merges, rebases, resets, deletes, history rewrites, or force pushes to `main` or `exam-backup-2026-11-17`.
- NEVER change the GitHub default branch.
- NEVER merge the V2 branch into `main` automatically.
- Before ANY edit, run `git status --short --branch` and `git branch --show-current`. If currently on `main` or `exam-backup-2026-11-17`, STOP editing and switch to a separate development worktree/branch.
- Before changes, ensure the working tree is clean or obtain explicit approval to handle local uncommitted changes; never discard user work.
- Never run `git clean -fdx`, `git reset --hard`, force checkout, bulk deletion, or destructive migration against the presentation workspace.

## Compatibility and safe delivery
- Inspect existing code before proposing edits; preserve original user-facing behavior by default.
- Work incrementally and commit small, reversible changes with clear messages.
- Prefer feature flags for new multi-agent orchestration, checkpointing, additional approvals, sandbox changes, or LLM/provider updates; existing workflow must remain available.
- If a new dependency, environment variable, database, API, or container is required, make it optional for the exam version and document setup.
- Keep existing Streamlit launch command functional: `streamlit run ui/app.py`.
- Preserve dataset upload, quality analysis, initial human approval, plan generation, code generation, execution, retries, insights, reports, and follow-up chat.
- Do not modify or remove exam/demo datasets, screenshots, final reports, or internship material without explicit approval.
- Keep credentials and real company/client data out of code, commits, prompts, traces, artifacts, and tests.

## Tests and quality gates
- First run and record the existing baseline tests, including the exact command, pass/fail count, environment, and observed failures. Historical documentation reports 200 tests, but verify rather than assume.
- For each change, run targeted tests plus the full available regression suite. Add a Streamlit smoke test and a representative end-to-end demo test if absent.
- Any change that introduces failures, breaks standard launch instructions, alters expected outputs unexpectedly, or worsens safety must be reverted or isolated on its development branch.
- Never silently skip failing tests or weaken checks to make them pass.
- Use mock LLM responses for default automated tests; request approval before incurring external API cost.
- Compare token metrics only on comparable workloads, with correctness and security tracked alongside cost.

## Human review gates
Pause and provide a summary BEFORE:
1. Replacing LangGraph orchestration or introducing multiple interacting agents.
2. Changing sandbox permissions, subprocess isolation, or persistent storage.
3. Introducing migrations or removing/renaming public modules.
4. Changing dependencies or deployment requirements in incompatible ways.
5. Creating a PR into `main` or modifying the presentation environment.

At each checkpoint provide: branch, base SHA, modified files, tests, reproduction commands, performance/security impact, failures, and rollback instructions.

## Required handoff after each phase
```
Branch:
Commit SHA:
Changed files:
Existing features verified:
Tests executed and outcome:
Demo smoke test result:
Security impact:
Token/cost evaluation:
Known issues:
Rollback steps:
Reviewer approval required:
```

**Hard stop condition:** If you cannot prove the current working code stays intact, make no risky edits. Preserve the examination snapshot first, then propose a safe alternative.
