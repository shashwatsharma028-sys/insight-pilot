# P6 optimisation 2: bounded transient-provider retries (implementation and live plan)

**Status:** implemented and validated offline (commit `56c2b9c6`). **No live request has been made.** The free-tier
daily quota resets at 08:00 UTC on 2026-11-25 (midnight Pacific). No other account or key will be used.

## 1. Why this is next (measured)

In the selective-recovery experiment (`SELECTIVE_RECOVERY.md`):
- **Remaining complete failures:** every remaining complete planner failure in the optimised arm was a **double
  HTTP 503**: the planner never received a plan, which cost 12 of 104 answers.
- **Shared 503 rate:** HTTP 503s hit 7–8 calls per 18 runs in **both** arms.
- **The mechanism:** each 503 used up one of the agent's 2 planning attempts.

## 2. What changed (opt-in; off by default)

- **Where:** `BudgetedPort` retries **only** HTTP 500, 502, 503 and 504 and client timeouts. It sits **below the
  agents**, so a temporary failure no longer costs a planning or claims attempt.
- **Same for both architectures:** the policy applies identically to both, because they share the same port.
- **Limits:**
  - at most `--transient-retries` retries per call;
  - exponential backoff × U(0.8, 1.2) jitter, honouring Retry-After but capping every wait at
    `--transient-max-wait-s`;
  - a per-call elapsed limit, `--transient-max-elapsed-s`;
  - every retry passes the existing pre-call budget check.
- **Never retried:** 429 (it keeps its own unchanged policy), the daily-quota or billing stop, 501/505, other 4xx,
  and non-timeout network errors.
- **Money:** every failed attempt is still charged the conservative worst case.
- **Recorded separately:**
  - per attempt: `provider_attempt` and `retry_reason`;
  - per run: transient retries, given-up calls, and pacing, 429 and transient waits;
  - per arm: provider attempts, successful provider-reported tokens and cost, and worst-case failure charges.
- **Offline validation:**
  - 24 tests (eventual success, repeated failures, exhausted budget, elapsed limit, non-retryable codes, timeouts vs
    other network errors, quota stop, 429 separation, no agent-attempt consumption in either architecture, and the
    offline CLI experiment for both architectures);
  - 10 of 10 mutations caught;
  - full offline regression green.

**Separate review fix (`d2c089bc`):** a repaired task must keep every claim key of the original task. Otherwise the
repair is rejected and, if it can't be fixed in time, the task is abandoned and reported. An audit of the 11 tasks
recovered in the live experiment found none that dropped a key, so the published results are unaffected.

## 3. Live evaluation plan (compact, matched; waiting for approval to start after the quota reset)

| Item | Plan |
|---|---|
| Workload | The baseline's 20 multi-agent slots, in seeded order (seed 20261124). Each slot runs `provider_retry` and an adjacent `control`, in a seeded random order. |
| Isolated variable | Optimised arm `--transient-retries 2`; control 0. Everything else is identical in both arms: selective recovery ON, model gemini-3.5-flash-lite, temperature 0, 2048 output tokens, prompts, 7 s pacing, 429 policy (6 retries, charged $0), daily-quota stop |
| Retry parameters | `--transient-backoff-s 2 --transient-max-wait-s 30 --transient-max-elapsed-s 120` |
| Requests | About 4.5 per run × 40 runs, plus retries and preflight: **about 190 requests**, under the 500-per-day quota with about 60% headroom |
| Budget | New ledger `live_llm_ledger_p6_provider.jsonl`, **cap $0.30**. Expected about $0.20 estimated, at about $0.005 per run including worst-case failure charges. The budget reserve skips controls before the optimised arm runs short. Overall P6 spend so far is $0.9936 of $5.00 |
| Single agent | The policy is validated offline for the single agent. A live single-agent pair (20 slots, about 330 requests) would need a second quota day, and is **optional**, not required for the decision |
| Primary metrics | Complete planner failures, split into provider-caused vs validation; hidden-answer accuracy; required-task completion (every required task counts); provider attempts, transient retries and transient wait; latency p50/p95 with and without waits; provider-reported tokens and cost per correct answer; worst-case failure charges |
| Statistics | Paired by slot: mean difference in correct answers with a 95% bootstrap CI and a sign test; paired latency |
| Decision rule (set in advance) | **Retain** if: no loss of hidden accuracy or required-task completion beyond noise (CI lower bound > −1 answer per run); fewer provider-caused complete failures; and p95 latency excluding waits not worse by more than 30 s. Report as **inconclusive** if no transient failure occurs on the day, because the policy then never activates |
| Stop conditions | Daily-quota or billing stop (unchanged); the budget cap; no further run starts after a stop |

**Command (after the reset and your approval):**

```bash
python -m evaluation.live --provider gemini --mode full --reps 5 --seed 20261124 --model gemini-3.5-flash-lite --budget-usd 0.30 --max-calls 600 --ledger ~/.local/state/insightpilot/live_llm_ledger_p6_provider.jsonl --rate-limit-retries 6 --charge-429 zero --min-interval-s 7 --transient-retries 2 --transient-backoff-s 2 --transient-max-wait-s 30 --transient-max-elapsed-s 120 --experiment provider_resilience --arch multi_agent --prices-confirmed --i-understand-this-makes-live-calls --out runs/p6_provider
```

## 4. Plain-language explanation (for interviews)

The AI provider sometimes answers "too busy, try again" (HTTP 503). Before, that counted as the planner's own
mistake and used up one of its two chances, so two busy signals in a row meant the whole analysis failed. Now the
connection layer waits a moment and tries again, a strictly limited number of times, with a little randomness so
retries don't pile up, and within the money and time limits. The agents only see real mistakes. Busy signals are
counted separately, so we can show exactly how often they happened and what retrying cost.
