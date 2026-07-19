# Sandbox Safety Rules

Scope: all LLM-generated code executed by the sandbox (`sandbox/executor.py`).
These are the most critical rules in the system — generated code is untrusted input.

## Hard rules (MUST / MUST NOT)

1. Generated code MUST run in an isolated subprocess, never in the main process
   (`exec()` / `eval()` in-process is forbidden).
2. Every execution MUST have a wall-clock timeout (current: 30s). A hung process
   MUST be killed, not waited on.
3. Generated code MUST NOT perform network access. No `requests`, `urllib`,
   `socket`, `http.client`, or any outbound call.
4. Generated code MUST NOT read or write files outside its designated paths:
   - READ: only the provided dataset CSV
   - WRITE: only the designated chart path (CHART_PATH)
5. Generated code MUST NOT spawn further subprocesses (`os.system`, `subprocess`,
   `multiprocessing`), install packages, or modify the environment.
6. Generated code MUST NOT import: `os` (beyond what the wrapper provides),
   `sys`, `shutil`, `pathlib` for traversal, `subprocess`, `socket`, `ctypes`,
   `importlib`, `pickle` (deserialization risk).
7. The sandbox MUST use a non-interactive matplotlib backend (`Agg`).
   `plt.show()` MUST never be called (it hangs headless processes).
8. stderr and stdout MUST both be captured. Errors MUST be classified
   (syntax / data / runtime / logic / unknown) before any retry decision.
9. Resource limits SHOULD be applied where the platform allows:
   memory cap (suggested: 1 GB) and CPU niceness.

## Allowed imports (whitelist)

pandas, numpy, matplotlib, seaborn, scipy, sklearn, math, statistics,
datetime, json, re, collections, itertools, functools, warnings

## Escalation

If generated code attempts a forbidden operation, the execution MUST fail
closed (block and report), never fail open (allow and hope). The violation
SHOULD be logged to the execution timeline with the offending snippet.

## Known gaps (tracked for future hardening)

- Subprocess isolation is process-level, not container-level. Docker/gVisor
  isolation is the planned upgrade path for untrusted multi-tenant use.
- Import restrictions are currently prompt-enforced, not AST-enforced.
  Planned: static AST scan of generated code before execution, rejecting
  forbidden imports/calls pre-run.
