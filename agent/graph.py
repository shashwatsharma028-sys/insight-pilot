"""
Main LangGraph Pipeline
Wires all nodes into a directed graph with conditional routing.

Flow:
  ingest → plan → [await_approval] → execute_loop → report
                                           ↑
  execute_loop: generate_code → execute → interpret → next_task → (loop or report)
                                    ↓ (on error)
                               generate_code (retry)
"""

from langgraph.graph import StateGraph, END
from agent.state import AgentState
from agent.nodes.data_ingestion import data_ingestion_node
from agent.nodes.planner import planner_node
from agent.nodes.code_generator import code_generator_node
from agent.nodes.executor import executor_node
from agent.nodes.interpreter import interpreter_node, advance_task_node
from agent.nodes.report_generator import report_generator_node
from agent.memory.conversation import handle_followup_node


# ── Routing functions ──────────────────────────────────────────────────────────

def route_after_ingestion(state: AgentState) -> str:
    return state.get("next_action", "error")


def route_after_planning(state: AgentState) -> str:
    next_action = state.get("next_action", "error")
    # If safe_mode is on, always await approval
    if state.get("safe_mode", True) and next_action != "error":
        return "await_approval"
    return "execute" if next_action != "error" else "error"


def route_after_execution(state: AgentState) -> str:
    return state.get("next_action", "generate_code")


def route_after_interpretation(state: AgentState) -> str:
    return "next_task"


def route_after_advance(state: AgentState) -> str:
    """Check if more tasks remain or go to report."""
    idx = state.get("current_task_index", 0)
    plan = state.get("analysis_plan", [])
    pending = [t for t in plan[idx:] if t["status"] in ("pending", "running")]
    return "generate_code" if pending else "report"


def route_followup(state: AgentState) -> str:
    """Determine if this is a follow-up or a replan request."""
    message = state.get("current_user_message", "").lower()
    replan_keywords = ["add analysis", "skip", "analyze", "run", "check", "also look at", "compare"]
    if any(kw in message for kw in replan_keywords):
        return "replan"
    return "followup"


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """Build and compile the full agent graph."""

    graph = StateGraph(AgentState)

    # Register all nodes
    graph.add_node("ingest", data_ingestion_node)
    graph.add_node("plan", planner_node)
    graph.add_node("await_approval", lambda s: {**s})   # Pause point — UI handles approval
    graph.add_node("generate_code", code_generator_node)
    graph.add_node("execute", executor_node)
    graph.add_node("interpret", interpreter_node)
    graph.add_node("next_task", advance_task_node)
    graph.add_node("report", report_generator_node)
    graph.add_node("followup", handle_followup_node)
    graph.add_node("replan", planner_node)   # Reuses planner with is_followup_query=True
    graph.add_node("error_handler", lambda s: {**s, "next_action": "done"})

    # Set entry point
    graph.set_entry_point("ingest")

    # Edges
    graph.add_conditional_edges("ingest", route_after_ingestion, {
        "plan": "plan",
        "error": "error_handler"
    })

    graph.add_conditional_edges("plan", route_after_planning, {
        "await_approval": "await_approval",
        "execute": "generate_code",
        "error": "error_handler"
    })

    # await_approval is a pause — UI resumes with generate_code after user approves
    graph.add_edge("await_approval", "generate_code")

    graph.add_edge("generate_code", "execute")

    graph.add_conditional_edges("execute", route_after_execution, {
        "interpret": "interpret",
        "generate_code": "generate_code",   # Retry
        "next_task": "next_task",            # Max retries exceeded
        "report": "report"
    })

    graph.add_edge("interpret", "next_task")

    graph.add_conditional_edges("next_task", route_after_advance, {
        "generate_code": "generate_code",
        "report": "report"
    })

    graph.add_edge("report", END)
    graph.add_edge("error_handler", END)

    # Follow-up edges (used by UI after initial analysis is done)
    graph.add_conditional_edges("followup", lambda s: "done", {
        "done": END
    })
    graph.add_edge("replan", "await_approval")

    return graph.compile()


# Singleton — import this in the UI
analyst_graph = build_graph()
