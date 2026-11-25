# InsightPilot V2: security architecture and policy specification

**Policy version:** `v2-sec-1` (`multi_agent/security/audit.py: POLICY_VERSION`).

**Design rule:** every control is **deterministic** and enforced by trusted code.
- No control depends on a model following an instruction.
- No security property is claimed from prompt wording.
- Injection *detection* is a signal only. It never grants anything, and no allow decision depends on the absence of a signal.
- There is **no switch that disables the controls** (tested).

## 1. Architecture

```
            ┌───────────────────────────── trusted runtime (process memory) ─────────────────────────────┐
 user ─B1─▶ │ AnalysisService ──▶ Supervisor ──▶ node wrapper: verify state tag ▶ node ▶ re-tag ▶ events  │ ◀─B5─ checkpoint
            │   session_id          │  RuntimeAuthority (policy engine, HMAC key never in state)          │      (untrusted)
            │                       ├─ approval gate: question carries binding; decision must quote it    │
            │                       │      (session, run, plan SHA-256, dataset SHA-256, policy, nonce)   │
            │                       ├─ grant: SHA-256 of each approved task                               │
            │                       ├─ analyst node: authorize(task vs grant, dataset bytes vs SHA-256)   │
            │                       │      └▶ TrustedOpsAnalyst: verify token, hash-once, compute ≤1,000  │
            │                       ├─ Verification Agent (stdlib recomputation) + consistency re-check   │
            │                       ├─ insight view: local-only / pseudonymised labels / bounded size     │
            │                       └─ output policy on insights (links, markup, identifiers, controls)   │
            │  every agent port (RecordingLLM): capability llm:<skill> ▶ outbound guard ▶ provider        │─B2─▶ provider
            │                                   ◀ reply size bound (200,000 characters) ◀                 │
            └────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Agent capability matrix (deny by default; `multi_agent/security/policy.py: CAPABILITIES`)

| Agent (runtime-assigned identity) | Capabilities | Explicitly **not** held |
|---|---|---|
| `planning_agent` | `llm:planner` | any other model skill, operations, dataset rows |
| `data_analyst_agent` | `llm:data_analyst` (code path only; unreachable in trusted-ops mode), `ops:compute` (only with a valid operation token) | planner or insight skills; any task outside the grant |
| `verification_agent` | `dataset:read` | **any** model skill |
| `business_insight_agent` | `llm:business_insight` | planner skill, operations, dataset path, raw labels of sensitive columns |
| `approval_gate` | `approval:decide` (one bound decision per nonce) | |
| `supervisor` | `approval:bind`, `grant:issue`, `ops:authorize` | model skills |
| anything else | nothing | |

**Identity** is the name `team.build_team` gives each agent's port. Model output cannot change it, and envelope routes (`contracts.ROUTES`) are fixed.

## 3. Policy specification

| # | Rule | Enforced at | Refusal |
|---|---|---|---|
| P1 | A node acts only on state carrying a valid HMAC tag over the integrity-relevant keys (`COVERED`), written by this runtime after the previous node | `Supervisor._timed` (every node) | `IntegrityViolation`: the run stops, nothing is retried |
| P2 | The approval question carries a binding; a decision must quote it exactly. The plan must still hash to `plan_sha256` | `Supervisor.approval_node` | `approval_failed` (reason code recorded) |
| P3 | A binding's nonce is consumed once, under a lock | `RuntimeAuthority.accept_decision` | `IntegrityViolation` (`approval_replayed`) |
| P4 | An accepted decision becomes a grant: the canonical SHA-256 of each approved task. Edit decisions grant only the kept tasks | `_on_approval_decision` | |
| P5 | Immediately before an analysis: the task ID is in the grant, the task hashes to its granted digest, the dataset **bytes** hash to the approved SHA-256, and the policy version and scope match | the analyst node (`authorize_analysis`) | `execution_refused` (P5.1A terminal policy; never retried, no fallback) |
| P6 | TrustedOpsAnalyst computes only with a valid one-task token, on the same bytes it hashed (read once, parsed from memory) | `TrustedOpsAnalyst.run` | `PolicyRefused` |
| P7 | An agent's port serves only the skills in its capability set | `RecordingLLM.complete` | `policy_refused` |
| P8 | Outbound guard on the serialized prompt: more than 200,000 characters, an email address, or a credential-shaped string means the call is refused | `RecordingLLM.complete` | `policy_refused` |
| P9 | Insight requests: claims over a declared-sensitive **value** column are local-only. Labels from a declared-sensitive column, or email/credential-like labels (heuristic), or labels with injection/markup content, are pseudonymized (`label_n`). Mappings with more than 500 entries are local-only. A task whose every result is local-only gets no insight call | `outbound.insight_view` | recorded as `redact` |
| P10 | Model-written prose returned to the provider (titles, descriptions) has emails and credential-shaped strings replaced by `[redacted]`. The planner sees a neutral file name when the real one carries an identifier or instruction | `outbound.model_prose`, `planner_profile` | `redact` |
| P11 | Output policy: insights with links, Markdown or HTML markup, email addresses, credential-shaped strings or control/bidi characters are rejected (bounded insight retries) | `_on_insight_report` | the insight is rejected |
| P12 | An accepted verification report must show `recomputed` equal to `reported`, and `reported` equal to the analyst's value, for every claim | `_on_verification_report` | analysis rejected |
| P13 | Grouped or counted results with more than 1,000 entries are refused at computation; replies over 200,000 characters are refused before parsing | `op_engine.compute_claims`, `RecordingLLM` | claim error / reply refused |
| P14 | Budget accounting: worst-case reservation under a lock, so concurrent calls cannot overspend | `BudgetedPort._attempt` | `BudgetExceeded` |
| P15 | Untrusted text containing a line break (goal, model prose, feedback) is JSON-encoded in prompts, so it cannot start a forged section line. Single-line text is unchanged | `prompts._u` | structural |

**Local data is never altered.** Values used for computation, verification and the user's own view are exact. Only provider-bound copies are minimized, and normalization is applied to detection copies only (tested).

## 4. Security audit events (`multi_agent/security/audit.py`)

**Fields:** `event_id` (uuid4), `ts`, `session_id`, `run_id`, `task_id`, `agent` (runtime identity), `decision` (`allow` | `deny` | `signal` | `redact`), `reason_code`, `category`, `location`, `signals` (fixed codes), `count`, `policy_version` and `check_ms` (duration of the security check).

**Privacy:**
- Identifiers are character-filtered and every vocabulary is closed (asserted).
- No prompt text, dataset values, model output, file names or secrets are recorded. A test scans every event of an injection run for canaries and payload words.

**Where events go:**
- the run state (`security_events`) and the `final.security` counts;
- the per-session application log (`AnalysisService.security_events()`, bounded to 5,000);
- the V2 page's **Security** tab: counts of allowed, refused, withheld and signal events, the refusal reasons, the signal locations, and the median check time. No score is invented.

**Coverage:**
- **Event on every decision:** capability, outbound, reply size, binding issue/accept/deny, grant, operation authorization, token verification, insight view, output policy and consistency.
- **Event on refusal only:** the per-node integrity check, to keep the logs proportionate.
- **Tested:** every decision type emits its event in a representative run (`test_every_decision_type_emits_an_event`).

## 5. Deployment assumptions

- **Single process, one user per session.** The HMAC key and the consumed nonces live in process memory. A restart invalidates in-flight runs, and no durable approval is claimed.
- **No authentication.** `session_id` is a per-`AnalysisService` random identity, **not** user authentication. Per-session limits are not per-user limits.
- **The trusted computing base is not defended against itself.** An attacker with code execution in the process can bypass everything. The checkpoint store may be untrusted; the process may not.
- **Sensitive columns must be declared** (`ServiceConfig.sensitive_columns`). The heuristics cover emails and credential formats only. Undeclared identifiers and private numbers are **not** detected (residual cases SD-09 and SD-10).
- **The provider still receives** the goal, the schema (column names and kinds), model-written prose, and the non-sensitive verified values. The first planning call happens before any human approval, by design.
