"""
Node 4: Executor
- Runs the generated code in the sandbox
- On failure: classifies error and routes back to code_generator for retry
- Respects max_retries limit per task
- Logs every attempt to the execution timeline
"""

from datetime import datetime
from agent.state import AgentState, AnalysisTask, AnalysisStatus
from sandbox.executor import execute_code


def executor_node(state: AgentState) -> AgentState:
    """Execute current task's generated code."""

    idx = state.get("current_task_index", 0)
    plan = state.get("analysis_plan", [])
    max_retries = state.get("max_retries", 3)

    if idx >= len(plan):
        return {**state, "next_action": "report"}

    task: AnalysisTask = plan[idx]

    if not task.get("generated_code"):
        return {**state, "next_action": "generate_code"}

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "node": "executor",
        "action": f"Executing: {task['title']} (attempt {task['retry_count'] + 1}/{max_retries + 1})",
        "task_id": task["id"]
    }

    success, output, chart_path, error_type = execute_code(
        code=task["generated_code"],
        csv_path=state["csv_path"],
        task_id=task["id"]
    )

    if success:
        updated_task = {
            **task,
            "status": AnalysisStatus.COMPLETED,
            "execution_result": output,
            "chart_path": chart_path,
            "error_message": None,
            "error_type": None
        }
        updated_plan = plan[:idx] + [updated_task] + plan[idx+1:]

        chart_paths = state.get("chart_paths", [])
        if chart_path:
            chart_paths = chart_paths + [chart_path]

        log_entry["status"] = "success"
        log_entry["detail"] = f"Execution successful. Chart: {chart_path or 'none'}"

        return {
            **state,
            "analysis_plan": updated_plan,
            "chart_paths": chart_paths,
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "interpret"
        }

    else:
        # Execution failed — check retries
        retry_count = task["retry_count"] + 1

        if retry_count > max_retries:
            # Give up on this task
            updated_task = {
                **task,
                "status": AnalysisStatus.FAILED,
                "error_message": output,
                "error_type": error_type,
                "retry_count": retry_count
            }
            updated_plan = plan[:idx] + [updated_task] + plan[idx+1:]
            failed_tasks = state.get("failed_tasks", []) + [updated_task]

            log_entry["status"] = "failed"
            log_entry["detail"] = f"Max retries ({max_retries}) exceeded. Error: {output[:200]}"

            return {
                **state,
                "analysis_plan": updated_plan,
                "failed_tasks": failed_tasks,
                "execution_log": state.get("execution_log", []) + [log_entry],
                "next_action": "next_task"
            }

        else:
            # Retry: update error info and route back to code generator
            updated_task = {
                **task,
                "status": AnalysisStatus.PENDING,
                "error_message": output,
                "error_type": error_type,
                "retry_count": retry_count,
                "generated_code": None  # Force regeneration
            }
            updated_plan = plan[:idx] + [updated_task] + plan[idx+1:]

            log_entry["status"] = "retry"
            log_entry["detail"] = f"Error [{error_type}]: {output[:200]}. Retrying..."

            return {
                **state,
                "analysis_plan": updated_plan,
                "execution_log": state.get("execution_log", []) + [log_entry],
                "next_action": "generate_code"
            }
