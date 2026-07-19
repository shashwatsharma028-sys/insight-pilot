"""
Reflection Engine
After every task, reviews what happened and distills lessons into /lessons
as JSON. Before planning, relevant lessons are loaded and injected into the
planner's context to improve decisions.

Honesty contract (per task requirements):
- This is NOT learning in the ML sense: no retraining, no weight updates,
  no claims of persistent model improvement. The LLM itself never changes.
- All "memory" is stored lessons (JSON on disk) injected as context.
- Lesson generation is deterministic rule-based analysis of execution
  facts (failures, retries, confidence, user corrections) — not an LLM
  call. This keeps reflection free, fast, and testable.
"""

import os
import json
from datetime import datetime
from typing import Optional

from agent.state import AnalysisStatus, ErrorType


def get_lessons_dir() -> str:
    """Lessons directory — overridable via env for test isolation."""
    override = os.environ.get("LESSONS_DIR")
    if override:
        return override
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lessons"
    )


def _lessons_path() -> str:
    return os.path.join(get_lessons_dir(), "lessons.json")


# ── Lesson generation (deterministic) ────────────────────────────────────────

_FIX_BY_ERROR_TYPE = {
    ErrorType.SYNTAX: "Tighten code-generation prompting toward simpler constructs; prefer multiple short statements over dense one-liners.",
    ErrorType.DATA: "Re-verify exact column names and dtypes from the dataset profile before generating code; add dropna/type-coercion guards for the involved columns.",
    ErrorType.RUNTIME: "Constrain generated code to whitelisted imports and defensive patterns (try/except around conversions, empty-frame checks after filtering).",
    ErrorType.LOGIC: "Add printed sanity checks (row counts, value ranges) so logical errors surface in output instead of silently passing.",
    ErrorType.UNKNOWN: "Simplify the analytical approach for this task type; fewer transformations per attempt.",
}


def generate_lessons_for_task(task: dict, dataset_name: str) -> list:
    """
    Deterministically derive lessons from a finished task's execution facts.
    Returns a list of lesson dicts (possibly empty — smooth runs teach little).
    """
    lessons = []
    now = datetime.now().isoformat()
    base = {
        "timestamp": now,
        "dataset": dataset_name,
        "task_title": task.get("title", ""),
        "analysis_type": task.get("analysis_type", ""),
    }

    status = task.get("status")
    retries = task.get("retry_count", 0)
    err_type = task.get("error_type")
    confidence = task.get("confidence_score")

    # 1. Hard failure — strongest signal
    if status == AnalysisStatus.FAILED:
        lessons.append({
            **base,
            "issue": f"Task '{task.get('title')}' failed after {retries} attempt(s).",
            "cause": f"Persistent {err_type or 'unknown'} — last error: {str(task.get('error_message', ''))[:200]}",
            "fix": _FIX_BY_ERROR_TYPE.get(err_type, _FIX_BY_ERROR_TYPE[ErrorType.UNKNOWN]),
            "confidence": 0.9,
            "kind": "failure",
        })

    # 2. Succeeded but needed retries — the approach was fragile
    elif status == AnalysisStatus.COMPLETED and retries > 0:
        lessons.append({
            **base,
            "issue": f"Task '{task.get('title')}' needed {retries} retry(ies) before succeeding.",
            "cause": f"Initial generated code hit a {err_type or 'transient'} error before self-correction recovered.",
            "fix": _FIX_BY_ERROR_TYPE.get(err_type, "Start with the simpler approach that ultimately succeeded for this analysis type."),
            "confidence": 0.7,
            "kind": "retry",
        })

    # 3. Completed but weak output — interpretation wasn't confident
    if status == AnalysisStatus.COMPLETED and confidence is not None and confidence < 0.5:
        lessons.append({
            **base,
            "issue": f"Task '{task.get('title')}' produced a low-confidence interpretation ({confidence:.0%}).",
            "cause": "Execution output was sparse, ambiguous, or partially unusable for interpretation.",
            "fix": "For this analysis type, generate code that prints richer labeled findings (explicit counts, named extremes, comparison baselines).",
            "confidence": 0.6,
            "kind": "weak_output",
        })

    return lessons


def generate_lesson_for_user_correction(user_edits: str, dataset_name: str) -> dict:
    """A user editing the plan is direct feedback the planner missed intent."""
    return {
        "timestamp": datetime.now().isoformat(),
        "dataset": dataset_name,
        "task_title": "(plan)",
        "analysis_type": "planning",
        "issue": "User corrected the proposed analysis plan before approving.",
        "cause": f"Planner output did not fully match user intent. Correction given: {user_edits[:200]}",
        "fix": "When planning for this dataset, account for this correction pattern in task selection and emphasis.",
        "confidence": 0.8,
        "kind": "user_correction",
    }


# ── Storage ──────────────────────────────────────────────────────────────────

def save_lessons(lessons: list) -> int:
    """Append lessons to the JSON store. Returns total stored count."""
    if not lessons:
        existing = load_all_lessons()
        return len(existing)

    os.makedirs(get_lessons_dir(), exist_ok=True)
    existing = load_all_lessons()
    existing.extend(lessons)

    with open(_lessons_path(), "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, default=str)
    return len(existing)


def load_all_lessons() -> list:
    """Load every stored lesson. Empty list if none / corrupted."""
    path = _lessons_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def load_relevant_lessons(dataset_name: str = "", analysis_types: Optional[list] = None,
                          limit: int = 5) -> list:
    """
    Load the lessons most relevant to an upcoming planning decision.
    Relevance: same dataset first, then same analysis types, newest first.
    Capped to `limit` to respect prompt token budgets (llm_usage rules).
    """
    lessons = load_all_lessons()
    if not lessons:
        return []

    def score(lesson):
        s = 0
        if dataset_name and lesson.get("dataset") == dataset_name:
            s += 2
        if analysis_types and lesson.get("analysis_type") in analysis_types:
            s += 1
        return s

    ranked = sorted(lessons, key=lambda l: (score(l), l.get("timestamp", "")), reverse=True)
    return ranked[:limit]


def format_lessons_for_prompt(lessons: list) -> str:
    """Render lessons as a prompt block for the planner. Empty string if none."""
    if not lessons:
        return ""
    lines = ["\nLESSONS FROM PAST RUNS (stored reflections — use them to improve this plan):"]
    for l in lessons:
        lines.append(
            f"- [{l.get('kind', 'lesson')}] {l.get('issue', '')} "
            f"Cause: {l.get('cause', '')} Fix: {l.get('fix', '')} "
            f"(confidence {l.get('confidence', 0):.0%})"
        )
    return "\n".join(lines) + "\n"
