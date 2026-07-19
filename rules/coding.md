# Coding Rules

Scope: the project's own source code (distinct from
code_generation.rules.md, which governs LLM-generated analysis code).

## Standards (MUST)

1. Every module MUST open with a docstring stating its responsibility.
2. State mutations MUST be immutable-style: nodes return new state dicts
   ({**state, "key": value}), never mutate the incoming state in place.
3. Every node MUST append to execution_log — silent actions are forbidden.
4. Errors MUST be handled at node boundaries: a node catches its own
   exceptions and routes via next_action; exceptions MUST NOT escape a
   node and crash the graph.
5. All LLM responses MUST pass through get_text() normalization.
   Direct response.content access outside utils/llm.py is forbidden.
6. New code MUST NOT introduce globals holding mutable run state —
   AgentState is the only run state carrier.

## Style (SHOULD)

7. Functions SHOULD stay under ~50 lines; nodes under ~120. Longer means
   the responsibility should split.
8. Names SHOULD say what things are for, not how they work
   (rate-limited wrapper: RateLimitedLLM, not RetryLoop3x).
9. Comments SHOULD explain WHY, not WHAT. Section-divider comments
   (── like this ──) are the house style for long files.
10. Type hints SHOULD be used on public function signatures; TypedDict
    for structured state.

## Change discipline (MUST)

11. The full test suite MUST be run after every code change, before commit.
    A change is not done until tests are green.
12. Bug fixes SHOULD add a test reproducing the bug (see the planner
    None-handling fix for the pattern).
13. Deprecation warnings MUST be treated as scheduled failures — fix them
    when they appear, not when they break.
