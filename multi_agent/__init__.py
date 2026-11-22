"""
InsightPilot V2 P5.0: multi-agent coordination prototype (opt-in; V1 is unchanged).

Agents (multi_agent.agents), each with its own input/output contract, context
boundary and permitted tools:

- Supervisor (multi_agent.supervisor): delegation, handoff validation, bounded
  retries, the human approval gate and termination. A LangGraph StateGraph
  separate from the V1 graph in agent/graph.py.
- PlanningAgent: turns a dataset *profile* (schema only, never raw rows) into a
  plan of tasks, each with declarative, machine-checkable claims.
- DataAnalystAgent: writes analysis code for one task and runs it behind an
  ExecutionPort (multi_agent.execution).
- VerificationAgent: no LLM. Recomputes every claim from the dataset with the
  standard library (multi_agent.verification) and checks insight grounding.
- BusinessInsightAgent: writes insights that may cite verified claims only.

Opt-in: nothing in agent/, sandbox/, utils/ or ui/ imports this package. An
application integration must check is_enabled() (IP_FF_ORCHESTRATOR=v2).
Security: the legacy subprocess ports are explicitly unhardened and refuse
model-generated code; see multi_agent.execution.
"""


def is_enabled(flags=None) -> bool:
    """True only when the V2 orchestrator flag is set (default: V1)."""
    from config.feature_flags import load_flags
    flags = load_flags() if flags is None else flags
    return flags.orchestrator == "v2"
