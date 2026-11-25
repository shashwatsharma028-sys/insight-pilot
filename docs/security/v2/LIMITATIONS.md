# V2 security: remaining limitations, residual risks and deployment assumptions

## Residual risks (measured, not hidden)

| Risk | Evidence | Why it remains |
|---|---|---|
| Undeclared identifiers (for example `ACCT-…`) and undeclared private numbers (for example salary) reach the provider in insight requests | SD-09, SD-10 still succeed (pre-registered) | The only robust control is the explicit sensitive-column policy (`ServiceConfig.sensitive_columns`). A generic identifier or number heuristic would pseudonymize ordinary business labels. **Operators must declare sensitive columns** |
| Fabricated quantities written in words ("tripled") | FR-11 (pre-registered) | Numeric grounding checks digits. Words-to-number interpretation, and whether an insight answers the business question, are **not verified**: numerical correctness ≠ correct interpretation |
| Credential or password solicitation without a link or token | FR-15, PC-05 | Detected as `credential_solicitation` (a signal only). Blocking it would require keyword blocking of benign prose |
| Detection coverage | Recall and precision measured on 27 offline injection cases | Signals are heuristic, normalized-text patterns. Novel phrasings will be missed. Detection is never relied on for containment |
| Live-model behaviour | None (offline only) | Scripted malicious models prove containment of *effects*; they do not measure how often a real model complies |

## Deployment assumptions

- **Single trusted process:**
  - the integrity key, the consumed nonces and the spent state tags exist only in process memory;
  - a restart invalidates in-flight analyses (they fail closed and must be restarted);
  - **durable approvals across restarts are not implemented**.
- **No user authentication:**
  - `session_id` is a random per-`AnalysisService` identity;
  - multi-user isolation relies on one service per browser session, and per-session limits are not per-user limits;
  - no comprehensive multi-user security is claimed.
- **The trusted computing base is trusted.** The Supervisor, RuntimeAuthority, verifier, op engine and dataset loader are not defended against in-process code execution, and a compromised verifier could approve fabricated values (P12 limits only *inconsistent* reports).
- **What the provider receives:**
  - the goal, the schema (column names and kinds), and model-written prose (with emails and credentials redacted);
  - non-sensitive verified values and pseudonymized labels.
  - The first planning call happens before any human approval, by design.
- **Model code:** code-generating analysis (the P3 container path) remains unreachable in this build. All results here are for trusted-ops mode.
- **Unchanged:** sandbox permissions, subprocess isolation, persistence, dependencies and deployment. No new dependency or service was added; every security module uses only the Python standard library.

## Behaviour changes (intended, documented)

| Change | Effect on existing callers |
|---|---|
| Interactive approval requires the binding. `Supervisor.resume` attaches the binding of the question pending in *that* thread | A raw `graph.invoke(Command(resume={"decision": ...}))` without the binding now ends in `approval_failed` |
| `TrustedOpsAnalyst.run` requires an authorization token from its team's authority | Calling it directly, outside `build_team`, is refused. `op_engine.compute_claims` (the library) is unchanged |
| New terminal status `policy_refused` (outbound or capability policy) | Shown as a failed analysis with a safe message |
| Insights with links, markup, emails, credentials or control characters are rejected | Before, they were accepted (prose redaction only); now they never become findings |
| Grouped or counted results with more than 1,000 entries are refused; mappings with more than 500 entries are local-only for insights | Golden workloads unaffected (at most 7 groups) |
| Declared-sensitive and heuristic-sensitive labels appear as `label_n` in **insight text** | Local results show the exact values; insights refer to pseudonyms |
| Untrusted multi-line text is JSON-encoded in prompts | Single-line prompts are byte-identical (existing tests); the effect of multi-line encoding on live accuracy is **unmeasured** |

## Live evaluation (not run; would need approval)

A live LLM security evaluation would add:
- the real-model compliance rate with injected instructions;
- the false-positive rate of the output policy on real insights;
- the effect of pseudonymization on insight quality.

**Proposed plan:**
- 20 injection runs (the PI, PH and PC families, 4 datasets) and 20 matched benign runs;
- both on secured V2 (containment is deterministic, so no baseline arm is needed for effects);
- the baseline arm only for the compliance rate.

**Requests and budget:**
- about 40 runs × 5 calls, plus about 10% retries: **about 220 requests** on the free-tier daily quota of 500;
- budget cap **$0.30** on a new ledger.
- It can run only after approval; the provider experiment's quota use on the same day must be taken into account.
