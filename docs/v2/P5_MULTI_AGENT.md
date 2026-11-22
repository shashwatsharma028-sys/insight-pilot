# InsightPilot V2 P5.0: multi-agent architecture and orchestration foundation

**Status:** P5.0 IMPLEMENTED on `feature/v2-p5-multi-agent`, branched from the P4.1 commit `d321785c`. It is an opt-in prototype. The V1 pipeline (`agent/graph.py`, `streamlit run ui/app.py`) is unchanged and remains the default. P5.1 is not started.

## Why this is a multi-agent system, not renamed nodes

The V1 graph is one pipeline over one shared `AgentState`: every node reads and writes every field, and there is no check between steps. P5.0 differs in five testable ways.

1. **Separate responsibilities and tools.** Each agent has its own `AgentSpec` (`multi_agent/agents.py`), and holds only the tools it lists. The Verification Agent has no LLM at all, and it cannot be constructed with one.
2. **Context boundaries.** An agent receives only its typed request, built by the Supervisor, and its prompt is built only from that request. Tests check what each prompt and payload can contain:
   - the planner's prompt contains no cell value from any dataset;
   - the analyst's prompt contains only values the plan itself names, and never the dataset path or the verifier's recomputed values;
   - the insight agent sees verified claims only, never refuted values, code or paths;
   - the verifier never receives the analyst's code or stdout.
3. **Typed handoffs.** Every message is an envelope (`multi_agent/contracts.py`) on an allowed route. Agents talk only to the Supervisor. The Supervisor checks the route, `reply_to`, task, attempt and the payload schema of every reply. It rejects a bad reply and never repairs it, and a rejected reply consumes the attempt.
4. **Independent verification.** A claim is accepted only if the Verification Agent recomputes it from the CSV and gets the same value. It does this with its own standard-library implementation, and is never asked whether the analyst's answer "looks right".
5. **A coordinator that decides.** The Supervisor is the only node with outgoing choices. It delegates, validates, retries within bounds, gates on human approval and terminates. Every agent edge returns to it (a graph-structure test checks this).

## Agents

| Agent | Responsibility | Input (its whole context) | Output | Tools |
|---|---|---|---|---|
| **Supervisor** (`supervisor.py`) | delegation, handoff validation, bounded retries, approval gate, termination | the run state and message log | requests; the final result | dataset profiler |
| **Planning Agent** | goal → tasks, each with declarative claims | dataset profile (column names, kinds, missing counts, row count, SHA-256), goal, earlier plan problems | `plan` | LLM |
| **Data Analyst Agent** | write and run code for one task; report its claims | one task and its claim specs, column names and kinds, feedback from earlier attempts | `analysis_result` (code, execution outcome, parsed claims) | LLM, ExecutionPort |
| **Verification Agent** | recompute every claim from the data; check that insights are grounded | claim specs, reported values, dataset path and expected SHA-256; or insights plus verified claims | `verification_report`, `insight_check_report` | dataset reader (no LLM) |
| **Business Insight Agent** | turn verified claims into cited insights | task title and description, verified claims only, feedback | `insight_report` | LLM |
| Approval gate (human) | approve, reject, or edit (keep a subset of tasks) | the validated plan | `approval_decision` | human (LangGraph `interrupt`) or an approval port |

## Flow and policies

```
START → supervisor ⇄ {planning_agent | approval_gate | data_analyst_agent | verification_agent | business_insight_agent}
        supervisor → finalize → END
```

1. The Supervisor profiles the dataset, which is a deterministic tool.
2. It asks for a plan, then validates it against the profile: known ops, existing columns of the right kind, unique ids and keys, and limits. An invalid plan is retried with its problems as feedback, up to `max_plan_attempts` (2); after that the status is `plan_invalid`.
3. **Human approval of the validated plan.**
   - With a checkpointer, the graph pauses (`interrupt`) before any code is written. It resumes with `Command(resume=decision)`.
   - Without one, an `approval_port` decides.
   - With neither, or with an invalid decision, the run fails closed: status `approval_failed`.
   - An edit may only keep tasks from the plan, never add tasks or change claims. The approval port receives a copy of the plan, so it cannot modify it.
4. **Per task, analysis and verification.** The analyst writes and runs code. If execution fails, the Supervisor retries with the error as feedback. If execution succeeds, the verifier recomputes the claims.
   - The analysis is accepted only if **every planned claim is verified**.
   - Otherwise the Supervisor retries with the verifier's reasons. These name the claim and the status, never the recomputed value, so the analyst cannot copy the answer.
   - There are `1 + max_retries` attempts (default 3), the same budget as V1. After that the task is `failed` and none of its claims are accepted.
5. **Per task, insights.** The insight agent receives only the verified claims. The verifier then checks two things:
   - every insight cites verified keys only;
   - every number in an insight is traceable to a cited value.

   An insight is retried up to `max_insight_attempts` (2); after that the task is `insight_rejected`. The verified claims are kept, but the run reports failures.
6. **Termination.** A global `max_steps` (200 dispatches) guarantees termination; when it is reached the status is `budget_exhausted`. The other statuses are:
   - `completed`;
   - `completed_with_failures`;
   - `rejected_by_human`;
   - `plan_invalid`;
   - `approval_failed`;
   - `profile_failed`.
7. **Reflection.** Every rejection is recorded as a lesson in the run state and fed back to the next attempt. Nothing is written to `lessons/`, and V1's reflection engine is untouched.

The envelope log, events, lessons and per-node timings are append-only LangGraph channels. All state is plain JSON, so it can be checkpointed.

## Claims and independent verification

A claim is declarative, for example `{"key": "total_sales_amount", "op": "sum", "args": {"column": "sales_amount"}}`. The analyst prints `CLAIM total_sales_amount=13573929.0` from sandboxed pandas code.

- **Recomputation:** `multi_agent/verification.py` recomputes the claim with `csv`, `math` and `statistics`, and with pandas' default NA tokens. It supports 13 ops: count, sum, mean, group sum and mean (optionally by a key prefix such as a month), argmax of each, ratio of sums, count, rate and mean where a column equals a value, value counts, and Pearson correlation.
- **Comparison:** the golden tolerance (relative 1e-9, absolute 1e-6). Booleans never equal numbers, and dict keys must match exactly.
- **Statuses:** `verified`, `refuted`, `missing`, `malformed` (unparseable, duplicated or not finite) and `unverifiable` (dataset changed since profiling, unreadable, zero variance, …).
- **Independence:**
  - the verifier imports neither pandas nor the golden reference (an AST test checks this);
  - it equals the separately written `tests/v2/golden/reference.py` on all 28 golden answers;
  - on synthetic data with NA tokens, it equals pandas for every op.

## Execution boundary (security)

`multi_agent/execution.py` defines `ExecutionPort`, the only way the analyst can run code.

| Port | What it is | Who may use it |
|---|---|---|
| `LegacyV1SubprocessExecution` | `sandbox.executor.execute_code`, unchanged: AST safety scan, temp-file subprocess, 30 s timeout. **Not an isolation boundary.** | reviewed scripted fixtures only (`CodeTrust.SCRIPTED_FIXTURES`) |
| `LazySeabornExecution` | the P4.1 `sandbox_v2` executor: same process model, lazy-seaborn prelude (an optional strategy) | the same |
| `HardenedContainerExecution` | the P3 integration point. In P5.0 it **fails closed**: it raises `IsolationUnavailable` and starts no process. | untrusted model output |

How untrusted code is kept off the legacy ports:
- Model code is `CodeTrust.UNTRUSTED`.
- The legacy ports refuse it when they are constructed.
- `resolve_port` gives untrusted code, or flags with `IP_FF_SANDBOX_HARDENED=1`, only the hardened port, with no fallback.
- The analyst checks its LLM's trust against the port both when it is constructed and on every run.
- A provider-labelled model on the hardened placeholder therefore produces typed agent errors, no accepted claims and no subprocess (tested with `subprocess.run` and `subprocess.Popen` patched to fail).

What P5.0 does not change or add:
- no change to subprocess permissions or isolation;
- no persistent agent processes and no fork server;
- no live API calls, credentials or paid services;
- `LangChainChatAdapter` exists for a later live phase, but nothing constructs a provider model.

## Opt-in and V1 preservation

- **Nothing in V1 imports `multi_agent`.** No module in `agent/`, `sandbox/`, `utils/`, `ui/`, `config/` or `sandbox_v2/` imports it (an AST test checks this).
- **Opt-in flag:** `multi_agent.is_enabled()` is true only with the existing flag `IP_FF_ORCHESTRATOR=v2`. It is false by default.
- **Protected paths are byte-identical to `d321785c`.** A test diffs these against the base: `agent/`, `sandbox/`, `utils/`, `ui/`, `rules/`, `skills/`, `data/`, `lessons/`, `config/`, `sandbox_v2/`, `tests/v2/golden/` and `requirements.txt`.
- **The V1 graph is intact:** it still has the same 11 nodes, and the V1 golden, snapshot and 13 strict-xfail security tests still pass and xfail as before.
- **No new dependency.** P5 uses `langgraph` and `langchain_core`, which are already required.
  - The interrupt-based approval path needs a LangGraph that provides `langgraph.types.interrupt`; this was tested with langgraph 1.2.9.
  - `requirements.txt` still says `langgraph>=0.2.0`. Raising that floor is a dependency change, so it is left for review.

## Usage

```bash
python -m evaluation multi-agent --out /tmp/p5 --mode literal
```
```bash
python -m evaluation multi-agent --out /tmp/p5_v1 --strategy v1
```
```bash
python -m evaluation multi-agent --out /tmp/p5_lazy --strategy lazy-seaborn
```

In Python:
- `multi_agent.team.build_team(llm, port, approval_port)` returns `(supervisor, llm_calls)`.
- For a non-interactive run, call `supervisor.run(csv_path)`.
- For the human gate, call `supervisor.start_interactive(csv_path, thread_id)` and then `Supervisor.resume(graph, cfg, decision)`.

## Evaluation (P4 harness extension: `evaluation/multi_agent.py`)

The harness has **20 cases**:
- 4 clean datasets;
- wrong scalar, group and label values, recovered;
- a persistent wrong value;
- missing and malformed claims;
- a non-finite claim;
- a duplicated claim;
- type confusion;
- within and just outside tolerance;
- an unplanned claim;
- insight faults (unknown citation, ungrounded number, unparseable reply, citation of another task's claim);
- an invalid plan, recovered and persistent;
- human reject;
- a human edit;
- a step budget.

Ground truth for verification accuracy is the committed golden answer, compared with the golden comparator, not the verifier's own.

Measured on one 4-CPU Linux container (CPython 3.11.18, langgraph 1.2.9) at commit `e18c15f8`, with a clean tree. All three modes gave **identical** correctness results:

| Metric | literal | real V1 subprocess | real lazy-seaborn subprocess |
|---|---|---|---|
| Cases whose expectations were met | 20/20 | 20/20 | 20/20 |
| Labelled claim checks; TP / TN / FP / FN | 143; 12 / 131 / 0 / 0 | the same | the same |
| Detection rate of injected wrong claims; false-rejection rate | 1.0; 0.0 | the same | the same |
| Incorrect claims accepted | 0 | 0 | 0 |
| Insight checks; TP / TN / FP / FN | 3 / 49 / 0 / 0 | the same | the same |
| Clean runs: golden answers accepted and correct | 28/28 | 28/28 | 28/28 |
| Handoff replies; rejected by contract; agent errors | 286; 3; 1 | the same | the same |
| Analysis retries; recovered tasks; failed tasks | 22; 12; 5 | the same | the same |
| LLM calls (all mock scripted); provider calls | 154; 0 | the same | the same |

Per-agent context on the clean sales run (request payload characters; LLM prompt characters):

| Agent | Requests | Payload characters | LLM calls | Prompt characters |
|---|---|---|---|---|
| Planning | 1 | 938 | 1 | 1,806 |
| Approval gate | 1 | 1,441 | 0 | 0 |
| Data Analyst | 7 | 9,535 | 7 | 8,193 |
| Verification | 6 | 2,634 | 0 | 0 |
| Business Insight | 3 | 811 | 3 | 1,642 |

**Latency** (one run per case, one machine; indicative only):
- Clean sales pipeline: 5,255 ms with the V1 port, 3,013 ms with the lazy-seaborn port.
- The sandbox dominates. The analyst node's median is 1,131 ms (V1) against 679 ms (lazy-seaborn).
- The Supervisor node's median is 0.2–0.3 ms; the verifier's is 0.4–1.8 ms.

**Tokens:** every call is `mock_scripted/estimated_chars_div_4` with cost `None`.

**Not comparable with the V1 pipeline:** P5.0 runs a different workload, with extra verification and insight steps and different prompts. **No accuracy, token, cost or latency improvement over V1 is claimed.**

Evidence:
- `docs/v2/p5/{literal,subprocess_v1,subprocess_lazy_seaborn}/`;
- `docs/v2/p5/handoff_demo.md`;
- `docs/v2/p5/mutation_checks.md`: 27/27 detected, 3 of them after tests were added.

## Tests

There are 174 P5 tests in `tests/v2/p5/`, all offline and all using the scripted model:

| File | Tests |
|---|---|
| `test_contracts_p50.py` | 35 |
| `test_verification_p50.py` | 53 |
| `test_execution_trust_p50.py` | 12 |
| `test_supervisor_p50.py` | 56 |
| `test_integration_p50.py` | 18 |

**Full offline suite** at `e18c15f8`: **551 passed, 13 xfailed** (before P5: 377 passed, 13 xfailed).
- The 13 strict xfails are the unchanged V1 security baseline.
- The one warning is the known Streamlit AppTest stale-widget restart in the V1 smoke test.

Command:

```bash
env -u GOOGLE_API_KEY python -m pytest tests/ -q -p no:cacheprovider -rxfE
```

## Limitations

- **Scripted agents.** The model is scripted and the analyst code is the hand-written golden pandas code. This proves the coordination, contracts and verification logic; it says nothing about real-model planning, coding or insight quality.
- **Claim vocabulary.** The verifier can only check claims expressible in its 13 ops. Unplanned claims are reported but never accepted. Questions outside the vocabulary need new ops; they cannot be accepted on trust.
- **Verification checks numbers, not choices.** The verifier checks that a reported number matches the data for the declared question. It does not judge whether the planner asked the right question; the human gate is there for that.
- **Conservative insight grounding.** Rescaled units ("13.6 million") and derived arithmetic are rejected rather than traced.
- **Sequential.** One task at a time; there is no parallel delegation yet.
- **No isolation boundary yet.** The real-subprocess runs use the V1 subprocess sandbox, which is not an isolation boundary; this is why only scripted fixtures may use it. The hardened port is a fail-closed placeholder until P3 is wired in.
- **In-memory checkpoints only.** The interactive checkpointer is in-memory (tests only), as `IP_HITL_CHECKPOINTER=memory` documents.
