# InsightPilot V2 feature flags

Defined in `config/feature_flags.py`. **As of P3.1, nothing in the application reads these flags.** The module exists so later phases share one strict, tested contract. With no `IP_*` variables set, the app behaves exactly like the examination build (`streamlit run ui/app.py`).

## Rules

- **Default off.** Every flag defaults to V1 behaviour.
- **Strict parsing.** An invalid value (`IP_FF_TRACING=maybe`) or an unknown variable with a reserved prefix (`IP_FF_`, `IP_SANDBOX_`, `IP_HITL_`, for example the typo `IP_FF_VERIFER`) raises `FeatureFlagError`. Nothing silently falls back to a default.
- **No unsafe fallback.** `IP_FF_SANDBOX_HARDENED=1` requires `IP_SANDBOX_BACKEND=container` in every profile; there is no hardened subprocess mode. Any other combination raises `InsecureConfigurationError`. If the container backend is unavailable, `resolve_sandbox_backend()` raises `InsecureConfigurationError`. It never returns the legacy executor.
- **Launch command.** `streamlit run ui/app.py` is pure V1 and ignores every `IP_*` variable, now and after P3. The planned V2 launcher (`v2/launch_app.py`, P3.2, not yet implemented) will be the only entry point that honours these flags. See `docs/v2/P3_SECURE_EXECUTION_DESIGN.md`.
- **Secure profile.** `IP_DEPLOYMENT_PROFILE=secure` only starts with OS/container isolation and enforced guardrails, and it rejects in-memory HITL checkpointing (which is for tests only).

## Flags

| Environment variable | Values (default **bold**) | Phase | Purpose |
|---|---|---|---|
| `IP_DEPLOYMENT_PROFILE` | **`exam`**, `secure` | all | `secure` enforces the isolation requirements below |
| `IP_FF_SANDBOX_HARDENED` | **`0`**, `1` | P3 | Use the rootless container executor; requires `IP_SANDBOX_BACKEND=container` |
| `IP_SANDBOX_BACKEND` | **`subprocess`**, `container` | P3 | `subprocess` = unchanged V1 executor (only with hardening off); `container` = rootless OS isolation (requires `IP_FF_SANDBOX_HARDENED=1`) |
| `IP_FF_GUARDRAILS` | **`off`**, `monitor`, `enforce` | P2 | Input/tool/output guardrails |
| `IP_FF_TRACING` | **`0`**, `1` | P5 | Per-session, per-run tracing |
| `IP_FF_VERIFIER` | **`off`**, `shadow`, `annotate`, `enforce` | P6 | Verification Agent |
| `IP_FF_HITL_V2` | **`0`**, `1` | P8 | Graph-enforced approvals |
| `IP_HITL_CODE_REVIEW` | **`off`**, `high_risk`, `all` | P8 | Generated-code approval (requires `IP_FF_HITL_V2=1`) |
| `IP_HITL_CHECKPOINTER` | **`none`**, `memory`, `sqlite` | P8 | Approval persistence; `memory` is for tests only (requires `IP_FF_HITL_V2=1`) |
| `IP_FF_ORCHESTRATOR` | **`v1`**, `v2` | P7 | Multi-agent orchestration |
| `IP_FF_MAX_PARALLEL_TASKS` | **`1`** … `8` | P7 | Values above 1 require `IP_FF_ORCHESTRATOR=v2` |
| `IP_LIVE_LLM_BUDGET_USD` | **`0`** … `1000` | evals | Budget for live LLM evaluations. `0` means no paid calls are permitted. |

### Secure profile requirements

`IP_DEPLOYMENT_PROFILE=secure` requires all of:

- `IP_FF_SANDBOX_HARDENED=1`
- `IP_SANDBOX_BACKEND=container`
- `IP_FF_GUARDRAILS=enforce`
- `IP_HITL_CHECKPOINTER` not set to `memory`

## Test-only variables (not flags)

| Variable | Used by | Effect |
|---|---|---|
| `IP_UPDATE_GOLDEN=1` | `tests/v2` | Rewrites the V1 golden snapshots. Use only after reviewer approval. |
