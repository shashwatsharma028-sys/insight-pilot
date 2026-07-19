# Safety Rules

Scope: the system's top-level safety policy. Detailed mechanics live in
sandbox_safety.rules.md (execution isolation) and data_handling.rules.md
(privacy); this file states the policy those mechanics serve.

## Policy (MUST)

1. Untrusted code is the threat model: all LLM-generated code is treated
   as untrusted input. It MUST pass static analysis (AST gate) and MUST
   run only inside the isolated, time-limited sandbox. Fail closed:
   anything that cannot be verified safe does not run.
2. The human retains control: safe mode (plan approval before execution)
   is the default and MUST remain available. Removing or defaulting-off
   the approval gate is an architecture change requiring user approval
   (architecture.md rule 1).
3. User data stays local: raw datasets MUST NOT leave the machine; only
   profiles/aggregates reach the LLM (data_handling.rules.md rules 1-2).
4. The system MUST be honest about failure: failed tasks, low confidence,
   and quality caveats are surfaced, never hidden (interpretation rules,
   retry_and_failure rules).
5. Secrets MUST never enter the repository: API keys live in .env
   (gitignored) or deployment secret stores. A committed secret is a
   sev-1 defect requiring immediate key rotation.

## Escalation (MUST)

6. Any safety rule violation detected at runtime MUST block the unsafe
   action and log the violation with enough detail to reproduce.
7. Known gaps MUST be documented, not hidden (see sandbox_safety
   "Known gaps") — undocumented limitations are treated as defects.
