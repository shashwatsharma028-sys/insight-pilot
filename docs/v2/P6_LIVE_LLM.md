# InsightPilot V2 P6: real LLM integration and single-agent vs multi-agent benchmarking

**Status:** implementation, budget controls and pilot preparation are complete and tested offline. **No live call has
been made.** The first live call waits for secure key provisioning and approval of the execution environment (§6).
Branch: `feature/v2-p6-live-llm-benchmark`, from the frozen P3.6-4B milestone (`facc2bec`).

## 1. Safety model: trusted-ops mode

Until independent security validation (VE-2), no model-written code runs anywhere.
- **What models choose:** claims from the existing, validated op vocabulary (`multi_agent/claims.py`: an op name plus
  column and value arguments). These are data, not code. The claim contract rejects any other field, so a model cannot
  state a value.
- **Who computes:** `multi_agent/op_engine.py` computes the claims in-process with reviewed pandas code. There is no
  `exec`, no subprocess, no container and no execution port; an AST test enforces this for all three new modules.
- **Independent cross-check:** the stdlib Verification Agent still recomputes every claim. Offline, the engine, the
  verifier and the golden answers agree on all 28 golden claims.
- **No V1 fallback:** the V1 executors are replaced by a refusing tripwire for the whole benchmark; a hit is counted
  and aborts that run.
- **API key:** `GOOGLE_API_KEY` is read from the process environment by `GeminiPort` only. It never appears in a
  record, ledger, report or prompt, and exception text is redacted (tested). It cannot reach a container
  (`--env-host=false`, explicit env list; tested). CI is unchanged and has no secrets.

**What trusted-ops mode measures:** whether a model maps a business question onto the right operation, column and
value (semantic correctness), and how each architecture uses calls and tokens. It does not measure model-written pandas
code. That needs VE-2 first, then the validated container path.

## 2. Architectures (same model, datasets, questions, profile, op engine and machine)

| | single_agent (V1-style) | multi_agent (V2) |
|---|---|---|
| Structure | per task: a claims call → trusted computation → an insight call (sees unverified values) | Supervisor: one planning call for all tasks → plan validation → approval → TrustedOpsAnalyst → stdlib verifier → an insight call per task with grounding checks |
| Retries | claims call retried with error feedback, at most 2 retries (V1's `max_retries`) | bounded plan and insight retries (P5 limits) |
| Independent verification | none | every claim recomputed (stdlib); insights checked against verified claims |
| Calls per dataset (no retries) | 2 × tasks = 6 | 1 + tasks = 4 |

Both receive the identical dataset profile JSON, op catalogue and question wording (tested). The P5 multi-agent code
is reused unchanged. The only change is `build_team(..., analyst=...)`, which accepts a replacement analyst.

## 3. Questions and grading

- **Questions:** 4 datasets × 7 golden questions (`evaluation/live.py: QUESTIONS`). They name the quantity and its unit,
  never the value. Golden answers stay in `tests/v2/golden/expected/` and are only used for grading.
- **Primary accuracy:** **24 hidden-answer questions**. The 4 `row_count` answers are in the shared schema profile, so
  they are reported separately in "all 28".
- **Leak test:** no planning or claims prompt contains a hidden golden answer. Numbers that are schema metadata are
  excluded; for example, attendance's 1268 missing `punch_in` values coincide with the `Absent` count. Insight prompts
  receive engine-computed values by design.

## 4. Metrics (per architecture; `evaluation/live.py: summarize`)

| Metric | Definition |
|---|---|
| Numerical accuracy | correct / 24 hidden (primary); correct / 28 (secondary); `tests.v2.golden.compare.values_match` |
| Incorrect analytical claims | reported claims whose value ≠ golden |
| Claim precision / recall | correct / reported; correct / required. For multi_agent, "reported" means accepted after verification. In trusted-ops mode the verifier guards computation integrity, not semantic choice, so precision mostly reflects the planner's op choices |
| Latency | end-to-end wall time per dataset run: p50 / p95 (`metrics.latency_stats`), plus a paired single→multi comparison with bootstrap CI and sign test (`evaluation.paired.paired_latency`) |
| Tokens | provider-reported input, output (incl. reasoning) and total, per call |
| LLM calls, API cost | counted per call; cost = reported tokens × confirmed list prices |
| Retries, execution failures | claim/plan/insight retries; claims the engine could not compute |
| Task completion rate | tasks with all required claims computed (and, for multi_agent, accepted) and an insight produced |

## 5. Budget controls (`multi_agent/live_llm.py`)

- **Pre-call check:** a call is refused if `ledger spend + worst case > budget`. The worst case is the prompt's UTF-8
  bytes (an upper bound on input tokens) × the input price, plus `max_output_tokens` × the output price. Calls and
  total tokens also have hard limits.
- **Recording:** provider-reported usage is recorded after each call (output = max(output, total − input), so reasoning
  tokens count). A response without usage metadata is charged the worst case.
- **Ledger:** append-only, mode 0600, numbers only, at `~/.local/state/insightpilot/live_llm_ledger.jsonl` (outside the
  repository). It is shared by every run, so the cumulative spend can never pass the cap. Offline runs use a separate
  dry-run ledger.
- **CLI gates:** `--budget-usd` above $5.00 is refused. Live calls need both `--prices-confirmed` and
  `--i-understand-this-makes-live-calls`. When the budget is reached, no new run starts, and a run cut short is excluded.
- **Prices:** list prices to confirm before the first live call, in `PRICES_USD_PER_MTOK`, USD per 1M tokens (input,
  output). `gemini-2.5-flash-lite` (default, the existing app's default) is entered as 0.10 / 0.40 and
  `gemini-2.5-flash` as 0.30 / 2.50.

**Worst-case cost estimate** (flash-lite, about 3.5 KB prompts, 2048 output-token cap): about $0.0012 per call.
- **Pilot:** student dataset, 1 run per architecture, about 10 calls: at most about $0.02. **Proposed pilot cap: $0.05.**
- **Full baseline:** 4 datasets × 5 reps × 2 architectures = 40 runs, about 200 calls including retries: at most about
  $0.25. **Proposed cap: $0.50 cumulative.** Expected actual spend is far lower, since real outputs are far below the
  2048-token cap.
- **Remaining for optimisation arms:** up to $4.50 of the $5.00 authorisation.

## 6. Before the first live call (decision needed)

1. **Key provisioning,** pick one:
   - **(a) Your machine:** `export GOOGLE_API_KEY=...` in the shell only. Never in `.env` or a committed file, and the
     runner never reads `.env`. You run the command and share the output files.
   - **(b) This cloud session:** I request the credential through the platform's secure form for
     `generativelanguage.googleapis.com` (header `x-goog-api-key`). The proxy injects it, so the key never appears in
     the environment, the repository or the conversation; the SDK gets a dummy value.
2. **Price confirmation** for the chosen model.
3. **Pilot command:**

```bash
python -m evaluation.live --provider gemini --mode pilot --budget-usd 0.05 --prices-confirmed --i-understand-this-makes-live-calls --out runs/p6_pilot
```

**Offline dry run, no key, $0:**

```bash
python -m evaluation.live --provider fake --mode full --reps 5 --out runs/p6_dry
```

Its report header states "OFFLINE / MOCK provider: not live results".

## 7. Optimisation plan (only after the live baseline)

Each optimisation is evaluated alone against the same baseline: same seed schedule, ≥ 5 paired repetitions, and the
same metrics. Order is decided by the measured bottleneck: tokens, latency or accuracy.
- **Prompt and context trimming:** for example, send only the columns a task needs, or a compact op catalogue.
- **Dataset-schema caching:** the profile is identical across calls; batch or cache it.
- **Verifier-guided retries:** feed the specific failed-claim reasons back.
- **lazy-seaborn:** applies only to the code-execution path, so it is not in trusted-ops mode. It was observed in CI
  run #10 (68.4 s vs 101.5 s for the P5 suite, 20/20 cases faster, one repetition each in a fixed order) and needs its
  own matched, repeated, order-randomised benchmark on the container path.

## 8. Resume and interview evidence (template; filled ONLY from live, provider-reported results)

For each improvement:
- original bottleneck (a measured number);
- engineering solution and files;
- methodology: arms, n, seed, model, dates, run IDs;
- before and after;
- % change (formula shown);
- trade-offs;
- a resume bullet;
- a plain-language interview explanation.

No entry may use offline, mock or scripted numbers.
