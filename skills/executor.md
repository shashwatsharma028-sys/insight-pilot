---
skill: executor
type: code-implemented
used_by: agent/nodes/executor.py, sandbox/executor.py
purpose: Documentation of the execution responsibility (no LLM prompt)
governed_by: rules/sandbox_safety.rules.md, rules/retry_and_failure.rules.md
---
# Executor Skill (code-implemented)

This responsibility has NO LLM prompt — it is deterministic code.

Responsibility: safely execute generated analysis code and route outcomes.

1. Pre-execution: AST safety scan (agent/rules_engine.py) blocks forbidden
   imports/calls before anything runs. Fail closed.
2. Execution: isolated subprocess, 30s timeout, Agg matplotlib backend,
   df pre-loaded, CHART_PATH injected, stdout/stderr captured.
3. Post-execution routing:
   - success -> interpret
   - failure within retry budget -> classify error, clear code, route back
     to code_generator with error context
   - budget exhausted -> mark FAILED, preserve last error, continue run
