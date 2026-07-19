# Testing Rules

Scope: the test suite and testing discipline for all project changes.

## Discipline (MUST)

1. The FULL test suite MUST be run after every code change. Green tests
   are the definition of done; a red suite blocks commit and push.
2. Existing tests MUST NOT be deleted or weakened to make a change pass.
   If a test fails, either the change is wrong or the test found a real
   behavior change that needs explicit acknowledgment.
3. Every bug fix SHOULD ship with a regression test reproducing the bug.
4. Refactors claiming "no behavior change" MUST assert it where feasible
   (e.g. byte-identity tests for extracted prompts).

## Test design (MUST / SHOULD)

5. Tests MUST NOT consume real LLM API quota: all LLM calls in tests are
   mocked. A test that needs the network is a defect.
6. Tests MUST NOT depend on each other or on execution order; fixtures
   create isolated inputs (tmp_path CSVs, fresh states).
7. Each suite SHOULD map to one module/responsibility:
   sandbox, llm utils, ingestion, agent logic, reports, rules engine,
   skills loader. New modules get new suites.
8. Safety-critical paths MUST have adversarial tests (forbidden imports
   blocked, timeouts kill hangs, malformed LLM output survives parsing).

## Coverage expectations (SHOULD)

9. Deterministic logic (parsing, routing, classification, enforcement)
   SHOULD be tested exhaustively — it's cheap and it's where bugs hide.
10. LLM-quality concerns (is the plan smart? is the insight good?) are
    NOT unit-testable and SHOULD NOT be faked with brittle assertions;
    they are validated by demo runs on the reference datasets.
