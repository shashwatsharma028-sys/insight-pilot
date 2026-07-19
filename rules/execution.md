# Execution Rules

Scope: how agent runs and tasks execute, end to end.

## Preflight (MUST)

1. ALL rules MUST be loaded before every agent task executes. The rules
   engine preflight (load_all_rules) runs at graph entry and before each
   task's code generation. A rules load failure downgrades gracefully
   (embedded defaults) but MUST be logged.
2. Skills MUST comply with the rules that govern them: every prompt-bearing
   skill declares its governing rule file(s) in frontmatter (governed_by),
   and the governing rules are injected alongside the skill's prompt at
   call time. A skill without a governed_by declaration is a defect.
3. The dataset MUST be profiled and quality-checked before planning;
   planning MUST complete before any code generation; generated code MUST
   pass the AST safety gate before the sandbox runs it. This ordering is
   the pipeline contract and MUST NOT be bypassed.

## Task execution (MUST)

4. In safe mode (default), the plan MUST receive human approval before
   any task executes.
5. Tasks execute strictly in plan order; a failed task MUST NOT block
   subsequent tasks (see retry_and_failure.rules.md for budgets).
6. Every attempt MUST be visible in the execution timeline: generation,
   execution, retries, interpretation, failures — timestamped.
7. A run MUST always terminate in a report, even a partial one. There is
   no code path where the user gets nothing.

## Resource discipline (MUST / SHOULD)

8. Execution MUST respect the sandbox timeout (30s/task) and the LLM call
   budget (~20/run; see llm_usage.rules.md).
9. Long-running steps SHOULD surface progress to the UI (status messages,
   progress bar) — silent multi-minute waits are a defect.
