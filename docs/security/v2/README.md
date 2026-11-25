# InsightPilot V2 AI security: engineering record

**Branch:** `feature/v2-ai-security`, from the reviewed V2 base `b42cb611`.
- **Not merged anywhere.** `main`, `exam-backup-2026-11-17` and the V1 application are untouched.
- **Offline only:** no live API call, paid service or infrastructure.

| Document | Contents |
|---|---|
| [THREAT_MODEL.md](THREAT_MODEL.md) | Trust boundaries; V1 history vs existing V2 controls vs gaps vs out of scope; OWASP Agentic mapping (analyst judgement, no compliance claim) |
| [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md) | Architecture diagram, agent capability matrix, policy specification P1–P15, audit events, deployment assumptions |
| [BENCHMARK_METHODOLOGY.md](BENCHMARK_METHODOLOGY.md) | Frozen methodology, success criteria, dev and held-out families, targets (committed before any control) |
| [RESULTS.md](RESULTS.md) | Baseline vs secured metrics, residual cases, corpus defects, overhead, regression and mutation evidence, reproduction |
| [LIMITATIONS.md](LIMITATIONS.md) | Residual risks, assumptions, intended behaviour changes, the proposed live evaluation (not run) |
| `results/` | Per-case JSONL for both trees, `summary.json`, `overhead.json` |

**Code:**
- **Controls:** `multi_agent/security/` (`policy`, `outbound`, `detect`, `audit`), wired into `supervisor`, `team`, `llm`, `op_engine`, `prompts`, `live_llm`, `v2/app_service.py` and `v2/app_page.py` (Security tab).
- **Benchmark:** `security_bench/`.
- **Tests:** `tests/v2/sec/`.
