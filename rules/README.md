# InsightPilot — Agent Rules

This folder contains the governance rules for the InsightPilot agent. Each file
defines the constraints and standards for one part of the system.

**Status:** Defined but not yet integrated. Integration plan: rule files will be
loaded at runtime and injected into the relevant node's system prompt
(`code_generation.rules.md` → code generator node, etc.), with hard rules
additionally enforced in code where possible.

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

## Rule levels

- **MUST / MUST NOT** — hard rules. Violations are bugs. Enforced in code wherever possible.
- **SHOULD / SHOULD NOT** — strong defaults. Deviations need a stated reason.
- **MAY** — explicitly permitted behavior.
