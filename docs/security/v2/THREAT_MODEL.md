# InsightPilot V2: threat model and trust boundaries

**Scope:**
- the V2 multi-agent path: `multi_agent/*`, `v2/app_service.py` and `v2/app_page.py`, in trusted-ops mode;
- in trusted-ops mode, models choose declarative claims and reviewed code computes them, so **no model-written code runs**.

**Not in scope:**
- the V1 application (`agent/`, `sandbox/`, `ui/`), which is unchanged and covered by `docs/security/threat_model.md` (P1);
- the P3 container path, which is unreachable in this build.

**Status of this record:**
- **Baseline evidence:** `docs/security/v2/results/baseline_all.jsonl` (unmodified V2 at `b42cb611`).
- **Framework mapping:** the OWASP mapping is analyst judgement. **No formal compliance is claimed.**

## 1. Components and trust boundaries

```
 user (browser) ──B1──▶ AnalysisService (session) ──▶ Supervisor (LangGraph)  ◀──B5── checkpoint store
                         │ upload, goal, decision        │ envelopes (seq, route, reply_to)
                         │                               ├──▶ Planning Agent ──B2──▶ provider (LLM)
 dataset file ──B3──────▶│ profile (schema only)         ├──▶ TrustedOpsAnalyst (reviewed pandas ops)
                         │                               ├──▶ Verification Agent (stdlib recomputation; no LLM)
                         ▼                               └──▶ Business Insight Agent ──B2──▶ provider (LLM)
                     view models (escaped) ──B4──▶ page
```

| Boundary | What crosses it | Trust |
|---|---|---|
| **B1** user → service | file bytes, file name, goal, approval decisions | Untrusted input. The human is the **only** authority who can approve work |
| **B2** runtime ↔ provider | prompts out, model replies in | Outbound: only permitted information. Inbound: untrusted and possibly hostile text |
| **B3** dataset → runtime | headers, cell values, file name | Untrusted data. It can carry indirect injection and sensitive values |
| **B4** runtime → page | results, model prose, diagnostics | Rendered escaped (UI-2) |
| **B5** runtime ↔ checkpoint store | the whole run state, including the pending envelope, plan and approvals | **Integrity-critical.** In memory today; a persistent store is a planned option |

**Trusted computing base:**
- `Supervisor`, `contracts`, `claims`, `op_engine`, `verification`, `dataset`;
- the `RuntimeAuthority` (new): the policy engine and the holder of the in-memory integrity key.

The models, the dataset, the user's text and the checkpoint contents are **outside** the trusted computing base.

## 2. History and current state

| Area | Historical V1 vulnerability (P1 register) | Already in V2 before this phase (baseline protects) | Genuine remaining gap (baseline evidence) | Out of scope |
|---|---|---|---|---|
| Code execution | R-03/04/05/06: model code with user privileges | Trusted-ops mode runs no generated code; legacy executor refuses untrusted code (UO-13); no executor fallback (UO-14) | none in trusted-ops mode | Container isolation (P3) |
| Prompt injection | R-10: CSV values, headers, file name into prompts | Planner sees schema only; claims are validated data, not code; unknown ops and arguments rejected (UO-01..08) | Injected instructions reach models unflagged (24/24 delivered, 0 detected). The goal can forge prompt sections with raw line breaks (PS-01..06). Data labels and planner titles reach the insight model as instructions (PC, PT) | Model compliance itself |
| Disclosure | R-14: tracebacks, logs | Profile has no cell values | **Verified group labels and private aggregates are sent to the provider** in insight requests (SD-01..10, 14..16). Credential-shaped headers and goals, and identifiers in file names, are sent (SD-11..13) | Provider-side retention |
| Approval | R-08/09: UI-only approval | Graph-level interrupt; the service binds `run_id` and `plan_id`, and refuses stale, cross-session, concurrent and replayed decisions (AP-12..15); strict decision fields (AP-16, AP-17) | **Graph-level decisions bound to nothing**: a captured decision approves any run (AP-06), a plan altered after the question is executed (AP-01..03), two concurrent resumes execute twice (AP-07); there is no policy version or dataset binding (AP-04, AP-05, AP-09..11) | Durable approvals across restarts |
| Authorization | (F-02 future) | Envelope routes and contract validation | **The operation boundary has no authorization**: `TrustedOpsAnalyst.run` computes any task (UO-09); a forged analysis request is executed (UO-10); any agent's port serves any skill (UO-11, UO-12) | Multi-user identity, authentication |
| Inter-agent integrity | (F-01, F-08 future) | Route, reply_to, task, attempt and kind checks; no duplicates (FM-01..08) | **Checkpoint-written messages are accepted**: forged approvals, plans, terminations, insights, analysis results and verifier reports (FM-09..13, FR-12, FR-13, CS-02, CS-09) | Process-memory attacker |
| Output integrity | R-13: unescaped HTML (fixed in UI-2) | Independent recomputation (FR-01, FR-02); numeric grounding and citations (FR-03..05); the page escapes everything | **Links, emails, bidi controls and credential solicitations are accepted as insights** (FR-06, FR-07, FR-10, FR-14, FR-15, PC-01..05); words-based fabrication (FR-11) | The quality of business interpretation |
| Resources | R-16: unbounded consumption | Plan, claim, insight, retry and step bounds (RX-01, RX-02, RX-12, RX-13); CSV validation (RX-14) | **Oversized replies are parsed into state** (RX-03); results with more than 1,000 groups are sent to the provider (RX-05, RX-06); concurrent calls overspend the budget (RX-07); unbounded goal and profile prompts (RX-10, RX-11) | Provider-side rate limiting |
| Persistence and memory | R-11: lessons.json poisoning | V2 writes no lessons; no cross-run context (CS-03, CS-04, CS-10); service isolation (CS-06..08); cleanup marker (CS-11) | none observed | |

## 3. OWASP Top 10 for Agentic Applications 2026 (provisional analyst mapping; not a compliance claim)

| ID | Title | V2 risks (baseline cases) | Control in this phase |
|---|---|---|---|
| ASI01 | Agent Goal Hijack | PI, PS, PH, PC, PF, PT | Injection signals (detection only); structural prompt encoding; label quarantine; outbound and output policies contain the *effects* |
| ASI02 | Tool Misuse & Exploitation | UO-01..10 | Deny-by-default op allowlist; authorization of each operation against the approved grant |
| ASI03 | Identity & Privilege Abuse | UO-11, UO-12 | A capability matrix enforced on each agent's port, by runtime-assigned identity |
| ASI05 | Unexpected Code Execution | UO-13, UO-14 | Existing (no generated code; no fallback) |
| ASI06 | Memory & Context Poisoning | CS-03, CS-04, CS-10 | Existing; signals on model output |
| ASI07 | Insecure Inter-Agent Communication | FM-*, FR-12, FR-13, CS-02, CS-09 | Runtime-keyed integrity tag over the run state and the pending envelope |
| ASI08 | Cascading Failures | RX-* | Reply size, cardinality and prompt bounds; atomic budget reservation |
| ASI09 | Human-Agent Trust Exploitation | AP-*, PC, FR-06..15 | Approval bound to session, run, plan, dataset and policy; one-time nonce; output policy |
| ASI10 | Rogue Agents | FR-01, FR-02, LyingAnalyst | Existing independent verification; consistency re-check of the verification report |
| ASI04 | Agentic Supply Chain | not tested | Out of scope (dependencies unchanged) |
