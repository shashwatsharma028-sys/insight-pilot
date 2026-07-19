# InsightPilot — Agent Rules

This folder contains the governance rules for the InsightPilot agent. Each file
defines the constraints and standards for one part of the system.

**Status:** INTEGRATED. All rules load before every agent task
(preflight at graph entry + before each task's code generation). Runtime
rules inject into node prompts; safety-critical rules are additionally
enforced in code (AST gate in the sandbox). Skills declare their governing
rules in frontmatter (`governed_by:`) and receive them at call time.

## Files

| File | Governs | Enforced by (planned) |
|---|---|---|
| `sandbox_safety.rules.md` | What generated code may/may not do | Sandbox executor (code-level) |
| `code_generation.rules.md` | Standards for LLM-generated analysis code | Code generator prompt |
| `planning.rules.md` | How analysis plans are constructed | Planner prompt |
| `interpretation.rules.md` | Honesty & quality bar for insights | Interpreter prompt |
| `data_handling.rules.md` | Privacy and data hygiene | Ingestion node + prompts |
| `retry_and_failure.rules.md` | Retry budgets and failure behavior | Executor node (code-level) |
| `llm_usage.rules.md` | API call budgets and model policy | LLM client wrapper |

### Tier 2 — Governance rules (govern the project itself)

| File | Governs | Enforced by |
|---|---|---|
| `architecture.md` | Module boundaries, change control, feature preservation | Development process + user approval gate |
| `coding.md` | Source-code standards for the project itself | Development process |
| `execution.md` | Pipeline ordering, rules preflight, run guarantees | `load_all_rules()` preflight + graph contract |
| `safety.md` | Top-level safety policy | Delegates to sandbox/data rules + AST gate |
| `testing.md` | Test discipline: run after every change, never weaken | Development process |

**Key process rules:** architecture changes require user approval before
implementation; existing features are never removed; the full test suite
runs after every code change.

## Rule levels

- **MUST / MUST NOT** — hard rules. Violations are bugs. Enforced in code wherever possible.
- **SHOULD / SHOULD NOT** — strong defaults. Deviations need a stated reason.
- **MAY** — explicitly permitted behavior.
