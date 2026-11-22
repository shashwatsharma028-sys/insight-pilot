"""
LangGraph compatibility for the V2 multi-agent prototype (P5.1A).

The examination build keeps requirements.txt unchanged (langgraph>=0.2.0).
That floor is too old for multi_agent: LangGraph 0.2.0 has no
langgraph.types.interrupt/Command, and checkpoint packages before 2.1.2 have
no InMemorySaver (docs/v2/P51_READINESS_REVIEW.md §4).

The V2 environment therefore pins one exact, tested pair in
requirements-v2.txt (TESTED_PAIR below; identical to environment B,
docs/v2/ENV_B_PINS.txt, in which the whole offline suite runs). This module
checks the FEATURES multi_agent needs, so a wrong environment fails with a
clear message instead of an obscure ImportError.
"""

TESTED_PAIR = {"langgraph": "1.2.9", "langgraph-checkpoint": "4.1.1", "langchain-core": "1.4.9"}


class MultiAgentEnvironmentError(RuntimeError):
    """The installed LangGraph lacks a feature multi_agent needs; use requirements-v2.txt."""


def installed_versions() -> dict:
    import importlib.metadata as md
    out = {}
    for dist in TESTED_PAIR:
        try:
            out[dist] = md.version(dist)
        except md.PackageNotFoundError:
            out[dist] = None
    return out


def missing_features() -> list:
    problems = []
    try:
        from langgraph.types import Command, interrupt  # noqa: F401
    except ImportError:
        problems.append("langgraph.types.interrupt / Command")
    try:
        from langgraph.checkpoint.memory import InMemorySaver  # noqa: F401
    except ImportError:
        problems.append("langgraph.checkpoint.memory.InMemorySaver")
    return problems


def in_memory_saver():
    """The in-memory checkpointer for the human approval gate (tests and offline runs only)."""
    problems = missing_features()
    if problems:
        raise MultiAgentEnvironmentError(
            "the installed LangGraph lacks " + ", ".join(problems) + f" (installed {installed_versions()}); "
            f"install the V2 environment from requirements-v2.txt (tested pair {TESTED_PAIR})")
    from langgraph.checkpoint.memory import InMemorySaver
    return InMemorySaver()
