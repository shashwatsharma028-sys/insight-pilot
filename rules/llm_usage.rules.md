# LLM Usage Rules

Scope: every call made to the LLM API. These rules exist because agents make
many calls, quotas are finite, and costs scale with carelessness.

## Call budget (MUST)

1. A full analysis run MUST stay within ~20 LLM calls:
   1 (plan) + 5 tasks × (1 gen + ≤2 retry gens + 1 interpret) worst-case ≈ 21.
   The 5-task plan cap exists to protect this budget.
2. Follow-up chat turns cost 1 call each and MUST reuse stored findings —
   a follow-up MUST NOT trigger re-analysis unless explicitly routed
   through replanning.
3. No node may call the LLM in a loop without a bounded iteration count.

## Model policy (MUST / SHOULD)

4. The model MUST be configurable via environment (`LLM_MODEL` in .env),
   never hardcoded in node logic.
5. Default model SHOULD be the cheapest tier that passes the test suite
   (currently a flash-lite tier). Agents need high-volume cheap inference;
   pro-tier models are the wrong shape for this workload.
6. Preview/experimental models SHOULD NOT be defaults — they get retired
   and quota-zeroed without notice (lesson learned twice in this project).

## Temperature policy (SHOULD)

| Purpose | Temperature |
|---|---|
| Code generation | 0.0 – 0.1 (determinism) |
| Planning | 0.2 (light variety, stable JSON) |
| Interpretation / chat | 0.3 (fluency without drift) |

## Prompt hygiene (MUST / SHOULD)

7. Prompts MUST NOT include the full dataset — profile only
   (see data_handling.rules.md rule 2).
8. Structured-output prompts MUST demand raw JSON and the parser MUST
   still defensively strip markdown fences (models violate format
   instructions routinely).
9. System prompts SHOULD state rules positively and concretely
   ("save charts to CHART_PATH") rather than only negatively.
10. Every response consumed programmatically MUST go through the
    normalization layer (get_text) — raw response.content access is
    forbidden outside utils/llm.py.

## Resilience (MUST)

11. All LLM calls MUST go through the rate-limited wrapper
    (RateLimitedLLM). Direct client calls from nodes are forbidden.
12. Any new LLM-calling code MUST have tests with mocked responses.
    Tests MUST NOT consume real API quota.
