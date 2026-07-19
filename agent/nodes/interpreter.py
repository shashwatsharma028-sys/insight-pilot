"""
Node 5: Interpreter
- Reads execution output and charts to generate plain-English insights
- Produces business-level recommendations (not just observations)
- Assigns confidence scores and states assumptions
- Moves to next task after interpretation
"""

import json
from datetime import datetime
from langchain_core.messages import HumanMessage, SystemMessage
from agent.state import AgentState, AnalysisTask, AnalysisStatus
from utils.llm import get_llm, get_text
from agent.rules_engine import get_rules_for
from agent.skills_loader import load_skill


_DEFAULT_INTERPRETER_SYSTEM_PROMPT = """You are a senior business data analyst. Your job is to interpret technical analysis results into clear, actionable business insights.

Given the analysis output, produce a JSON response with exactly these fields:
{
  "interpretation": "2-4 sentences explaining what this analysis found in plain English. Be specific with numbers.",
  "business_insight": "1-2 sentences on what this means for the business and what action it implies.",
  "confidence_score": float between 0.0 and 1.0 (how confident you are in this interpretation),
  "assumptions": ["list", "of", "key", "assumptions", "made"]
}

Rules:
- Be specific: say "sales dropped 45% in March" not "sales decreased"
- Confidence: 0.9+ if results are clear and unambiguous; 0.5-0.8 if there's some uncertainty; <0.5 if data is sparse
- Always state at least one assumption
- Return ONLY the JSON object. No markdown. No explanation.
"""

# Loaded from skills/business_insights.md — falls back to embedded default if missing
INTERPRETER_SYSTEM_PROMPT = load_skill("business_insights", fallback=_DEFAULT_INTERPRETER_SYSTEM_PROMPT)



def interpreter_node(state: AgentState) -> AgentState:
    """Interpret the current task's results and generate business insights."""

    idx = state.get("current_task_index", 0)
    plan = state.get("analysis_plan", [])

    if idx >= len(plan):
        return {**state, "next_action": "next_task"}

    task: AnalysisTask = plan[idx]

    if task["status"] != AnalysisStatus.COMPLETED:
        return {**state, "next_action": "next_task"}

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "node": "interpreter",
        "action": f"Interpreting results: {task['title']}",
        "task_id": task["id"]
    }

    llm = get_llm(temperature=0.3)  # Slightly higher for natural language

    prompt = f"""
DATASET: {state['dataset_name']}
DATASET SUMMARY: {state['df_summary'][:1000]}

ANALYSIS PERFORMED: {task['title']}
ANALYSIS TYPE: {task['analysis_type']}
DESCRIPTION: {task['description']}

EXECUTION OUTPUT:
{task.get('execution_result', 'No output captured')}

A chart was {"generated and saved" if task.get('chart_path') else "NOT generated"} for this analysis.

Interpret these results and provide business insights in the required JSON format.
"""

    try:
        messages = [
            SystemMessage(content=INTERPRETER_SYSTEM_PROMPT + get_rules_for("interpretation")),
            HumanMessage(content=prompt)
        ]
        response = llm.invoke(messages)

        # Parse response
        raw = get_text(response).strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        result = json.loads(raw.strip())

        updated_task = {
            **task,
            "interpretation": result.get("interpretation", ""),
            "confidence_score": result.get("confidence_score", 0.7),
            "assumptions": result.get("assumptions", [])
        }
        updated_plan = plan[:idx] + [updated_task] + plan[idx+1:]

        # Collect business insights
        business_insights = state.get("business_insights", [])
        if result.get("business_insight"):
            business_insights = business_insights + [
                f"**{task['title']}:** {result['business_insight']}"
            ]

        completed_tasks = state.get("completed_tasks", []) + [updated_task]

        log_entry["status"] = "success"
        log_entry["detail"] = f"Confidence: {result.get('confidence_score', 0):.0%}"

        return {
            **state,
            "analysis_plan": updated_plan,
            "completed_tasks": completed_tasks,
            "business_insights": business_insights,
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "next_task"
        }

    except Exception as e:
        # Interpretation failure is non-fatal — just move on
        updated_task = {
            **task,
            "interpretation": f"Interpretation failed: {str(e)}. Raw output: {task.get('execution_result', '')[:300]}",
            "confidence_score": 0.3,
            "assumptions": ["Interpretation was generated from raw output due to LLM error"]
        }
        updated_plan = plan[:idx] + [updated_task] + plan[idx+1:]
        completed_tasks = state.get("completed_tasks", []) + [updated_task]

        log_entry["status"] = "warning"
        log_entry["detail"] = f"Interpretation failed: {str(e)}"

        return {
            **state,
            "analysis_plan": updated_plan,
            "completed_tasks": completed_tasks,
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "next_task"
        }


def advance_task_node(state: AgentState) -> AgentState:
    """Move to the next task in the plan."""
    return {
        **state,
        "current_task_index": state.get("current_task_index", 0) + 1,
        "next_action": "check_tasks"
    }
