"""
Node 3: Code Generator
- Generates pandas/matplotlib/seaborn code for each analysis task
- Context-aware: knows the dataset schema, previous errors, and error type
- Different prompting strategy per error type for intelligent retries
"""

from datetime import datetime
from langchain_core.messages import HumanMessage, SystemMessage
from agent.state import AgentState, AnalysisTask, AnalysisStatus, ErrorType
from utils.llm import get_llm, get_text
from agent.rules_engine import get_rules_for, load_all_rules
from agent.skills_loader import load_skill


_DEFAULT_CODE_GEN_SYSTEM_PROMPT = """You are an expert Python data analyst. Generate clean, working Python code for data analysis tasks.

CRITICAL RULES:
1. The DataFrame is ALREADY LOADED as `df`. Do NOT use pd.read_csv() or load any file.
2. Save charts using: plt.savefig(CHART_PATH, bbox_inches='tight') — CHART_PATH is pre-defined.
3. Always call plt.close('all') after saving.
4. Print key findings to stdout — the agent captures stdout as results.
5. Use try/except for risky operations (type conversions, date parsing).
6. For time series: convert date columns with pd.to_datetime(df['col'], errors='coerce').
7. Never use plt.show() — it will hang. Use savefig() only.
8. Print clear, labeled summaries of findings (e.g., "Top region by sales: North ($2.1M)").
9. RETURN ONLY THE PYTHON CODE. No explanation. No markdown fences. No comments outside the code.
"""

# Loaded from skills/code_generator.md — falls back to embedded default if missing
CODE_GEN_SYSTEM_PROMPT = load_skill("code_generator", fallback=_DEFAULT_CODE_GEN_SYSTEM_PROMPT)



def _get_retry_instructions(error_type: ErrorType, error_message: str, prev_code: str) -> str:
    """Generate targeted fix instructions based on error classification."""

    if error_type == ErrorType.SYNTAX:
        return f"""
The previous code had a SYNTAX ERROR:
{error_message}

Fix the syntax. Common issues:
- Missing colons after if/for/def
- Mismatched parentheses/brackets
- Wrong indentation
- Invalid f-string syntax

Previous (broken) code:
{prev_code}

Write corrected code:
"""

    elif error_type == ErrorType.DATA:
        return f"""
The previous code had a DATA ERROR:
{error_message}

This usually means:
- Wrong column name (check exact spelling/case)
- Column has wrong type (e.g., trying to do math on a string column)
- NaN values causing issues

The available columns are in the dataset summary above. Use exact column names.
Add: df = df.dropna(subset=['col']) before operating on columns with missing values.

Previous (broken) code:
{prev_code}

Write corrected code:
"""

    elif error_type == ErrorType.RUNTIME:
        return f"""
The previous code had a RUNTIME ERROR:
{error_message}

Fix this by:
- Checking imports (all standard libs are available: pandas, numpy, matplotlib, seaborn, scipy, sklearn)
- Wrapping type conversions in try/except
- Handling empty DataFrames after filtering

Previous (broken) code:
{prev_code}

Write corrected code:
"""

    else:
        return f"""
The previous code failed with:
{error_message}

Rewrite it from scratch taking a different, simpler approach.
Previous (broken) code:
{prev_code}

Write corrected code:
"""


def code_generator_node(state: AgentState) -> AgentState:
    """Generate or regenerate code for the current analysis task."""

    idx = state.get("current_task_index", 0)
    plan = state.get("analysis_plan", [])

    if idx >= len(plan):
        return {**state, "next_action": "report"}

    task: AnalysisTask = plan[idx]
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "node": "code_generator",
        "action": f"Generating code for: {task['title']}",
        "task_id": task["id"],
        "retry": task["retry_count"]
    }

    load_all_rules()  # Preflight before every task (cached — execution.md rule 1)
    llm = get_llm(temperature=0.05)  # Very low temp for code

    # Build prompt
    is_retry = task["retry_count"] > 0

    if is_retry:
        retry_instructions = _get_retry_instructions(
            task.get("error_type", ErrorType.UNKNOWN),
            task.get("error_message", "Unknown error"),
            task.get("generated_code", "")
        )
        user_prompt = f"""
DATASET SUMMARY:
{state['df_summary']}

TASK: {task['title']}
DESCRIPTION: {task['description']}
ANALYSIS TYPE: {task['analysis_type']}

RETRY #{task['retry_count']}:
{retry_instructions}
"""
    else:
        user_prompt = f"""
DATASET SUMMARY:
{state['df_summary']}

TASK: {task['title']}
DESCRIPTION: {task['description']}
ANALYSIS TYPE: {task['analysis_type']}

Generate Python code to perform this analysis. Remember:
- df is already loaded
- Save chart to CHART_PATH
- Print key numeric findings to stdout
- Handle NaN values gracefully
"""

    try:
        messages = [
            SystemMessage(content=CODE_GEN_SYSTEM_PROMPT + get_rules_for("code_generation")),
            HumanMessage(content=user_prompt)
        ]
        response = llm.invoke(messages)

        # Clean response — strip any accidental markdown
        code = get_text(response).strip()
        if code.startswith("```"):
            lines = code.split("\n")
            code = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        code = code.strip()

        # Update task with generated code
        updated_task = {**task, "generated_code": code, "status": AnalysisStatus.RUNNING}
        updated_plan = plan[:idx] + [updated_task] + plan[idx+1:]

        log_entry["status"] = "success"
        log_entry["detail"] = f"Generated {len(code.splitlines())} lines of code"

        return {
            **state,
            "analysis_plan": updated_plan,
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "execute"
        }

    except Exception as e:
        log_entry["status"] = "error"
        log_entry["detail"] = str(e)

        updated_task = {**task, "status": AnalysisStatus.FAILED,
                        "error_message": f"Code generation failed: {str(e)}"}
        updated_plan = plan[:idx] + [updated_task] + plan[idx+1:]

        return {
            **state,
            "analysis_plan": updated_plan,
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "next_task"
        }
