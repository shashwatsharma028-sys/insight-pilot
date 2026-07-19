"""
Node 2: Analysis Planner
- Decides what analyses to run based on the dataset profile
- Selects appropriate statistical tests automatically
- Detects time-series data and adds forecasting if relevant
- Handles both fresh plans and follow-up/replanning requests
"""

import json
import uuid
from datetime import datetime
from langchain_core.messages import HumanMessage, SystemMessage
from agent.state import AgentState, AnalysisTask, AnalysisStatus
from utils.llm import get_llm, get_text
from agent.rules_engine import get_rules_for
from agent.skills_loader import load_skill
from agent.reflection_engine import (
    load_relevant_lessons, format_lessons_for_prompt,
    generate_lesson_for_user_correction, save_lessons,
)


_DEFAULT_PLANNER_SYSTEM_PROMPT = """You are an expert data analyst AI. Your job is to create a structured analysis plan for a dataset.

Given a dataset summary, generate a JSON array of analysis tasks. Each task must follow this exact schema:
{
  "id": "unique_string",
  "title": "Short task title",
  "description": "What this analysis will reveal and why it's valuable",
  "analysis_type": "one of: descriptive | correlation | outlier | trend | forecast | distribution | comparative | custom",
  "priority": integer 1-5 (1=highest),
  "status": "pending",
  "generated_code": null,
  "execution_result": null,
  "error_message": null,
  "error_type": null,
  "retry_count": 0,
  "chart_path": null,
  "interpretation": null,
  "confidence_score": null,
  "assumptions": null
}

Rules:
- Generate EXACTLY 5 tasks maximum. Quality over quantity — pick the 5 most valuable analyses.
- ALWAYS start with a descriptive statistics task (priority 1).
- If date/time columns exist, include a trend analysis AND a forecast task.
- If multiple numeric columns exist, include a correlation task.
- If the data has categorical groupings, include a comparative/group-by analysis.
- Include an outlier detection task if numeric columns exist.
- Make task descriptions specific to this dataset — not generic.
- Return ONLY the JSON array. No markdown. No explanation. No backticks.
"""

# Loaded from skills/planner.md — falls back to embedded default if missing
PLANNER_SYSTEM_PROMPT = load_skill("planner", fallback=_DEFAULT_PLANNER_SYSTEM_PROMPT)



def _parse_plan(raw: str) -> list:
    """Parse and validate LLM plan output."""
    # Strip any accidental markdown
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip().rstrip("```")

    tasks = json.loads(clean)

    # Ensure all required fields exist with defaults
    validated = []
    for t in tasks:
        task: AnalysisTask = {
            "id": t.get("id", str(uuid.uuid4())[:8]),
            "title": t.get("title", "Untitled Analysis"),
            "description": t.get("description", ""),
            "analysis_type": t.get("analysis_type", "descriptive"),
            "priority": t.get("priority", 3),
            "status": AnalysisStatus.PENDING,
            "generated_code": None,
            "execution_result": None,
            "error_message": None,
            "error_type": None,
            "retry_count": 0,
            "chart_path": None,
            "interpretation": None,
            "confidence_score": None,
            "assumptions": None
        }
        validated.append(task)

    return sorted(validated, key=lambda x: x["priority"])


def planner_node(state: AgentState) -> AgentState:
    """Generate or replan the analysis based on dataset and user edits."""

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "node": "planner",
        "action": "Generating analysis plan"
    }

    llm = get_llm(temperature=0.2, skill="planner")

    # Build the planning prompt
    user_edits = state.get("user_edits_to_plan", "")

    # ── Reflection: a user correction is a lesson in itself ──
    if user_edits:
        save_lessons([generate_lesson_for_user_correction(
            user_edits, state.get("dataset_name", "")
        )])

    # ── Reflection: load relevant stored lessons to improve this plan ──
    lessons_block = format_lessons_for_prompt(
        load_relevant_lessons(dataset_name=state.get("dataset_name", ""))
    )
    current_message = state.get("current_user_message", "")

    if state.get("is_followup_query") and current_message:
        # Replanning from a follow-up: user wants to modify or add analyses
        prompt = f"""
The dataset has already been profiled. The user wants to modify or add to the analysis plan.

DATASET SUMMARY:
{state['df_summary']}

USER REQUEST: {current_message}

EXISTING PLAN (tasks already completed should be kept as completed):
{json.dumps([{"id": t["id"], "title": t["title"], "status": t["status"]} for t in state.get("analysis_plan", [])], indent=2)}

Generate a REVISED JSON array that:
1. Keeps completed tasks as-is
2. Adds/modifies tasks per the user's request
3. Follows the same schema as before

Return ONLY the JSON array.
"""
    elif user_edits:
        # Human-in-the-loop: user edited the plan
        prompt = f"""
The user has reviewed and edited the analysis plan. Apply their edits.

DATASET SUMMARY:
{state['df_summary']}

ORIGINAL PLAN:
{json.dumps(state.get("analysis_plan", []), indent=2)}

USER EDITS/INSTRUCTIONS:
{user_edits}

Return a REVISED JSON array applying the user's changes. Return ONLY the JSON array.
"""
    else:
        # Fresh plan
        prompt = f"""
Generate a comprehensive analysis plan for this dataset.

DATASET SUMMARY:
{state['df_summary']}

DATA QUALITY NOTES:
{json.dumps((state.get("data_quality_report") or {}).get("recommendations", []), indent=2)}

Return ONLY the JSON array of analysis tasks.
{lessons_block}"""

    try:
        messages = [
            SystemMessage(content=PLANNER_SYSTEM_PROMPT + get_rules_for("planning")),
            HumanMessage(content=prompt)
        ]
        response = llm.invoke(messages)
        plan = _parse_plan(get_text(response))

        log_entry["status"] = "success"
        log_entry["detail"] = f"Generated {len(plan)} analysis tasks"

        return {
            **state,
            "analysis_plan": plan,
            "plan_approved": False,          # Always requires approval
            "user_edits_to_plan": None,      # Clear after applying
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "await_approval" if state.get("safe_mode", True) else "execute"
        }

    except Exception as e:
        log_entry["status"] = "error"
        log_entry["detail"] = str(e)
        return {
            **state,
            "error_message": f"Planning failed: {str(e)}",
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "error"
        }
