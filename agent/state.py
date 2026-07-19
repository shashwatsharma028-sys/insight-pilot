"""
Agent State — the single source of truth passed between every node in the graph.
Using TypedDict so LangGraph can serialize/deserialize cleanly.
"""

from typing import TypedDict, List, Dict, Any, Optional
from enum import Enum


class AnalysisStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ErrorType(str, Enum):
    SYNTAX = "syntax_error"
    RUNTIME = "runtime_error"
    LOGIC = "logic_error"
    DATA = "data_error"
    UNKNOWN = "unknown_error"


class AnalysisTask(TypedDict):
    id: str
    title: str
    description: str
    analysis_type: str          # descriptive / correlation / outlier / trend / forecast / custom
    priority: int               # 1 = highest
    status: AnalysisStatus
    generated_code: Optional[str]
    execution_result: Optional[str]
    error_message: Optional[str]
    error_type: Optional[ErrorType]
    retry_count: int
    chart_path: Optional[str]
    interpretation: Optional[str]
    confidence_score: Optional[float]
    assumptions: Optional[List[str]]


class DataQualityReport(TypedDict):
    total_rows: int
    total_columns: int
    duplicate_rows: int
    missing_values: Dict[str, int]        # column → count
    outlier_columns: List[str]
    invalid_type_columns: List[str]
    quality_score: float                   # 0-100
    recommendations: List[str]


class ConversationTurn(TypedDict):
    role: str                              # "user" or "agent"
    message: str
    timestamp: str
    related_task_id: Optional[str]


class AgentState(TypedDict):
    # ── Dataset info ──────────────────────────────────────────────
    csv_path: Optional[str]
    dataset_name: str
    df_summary: Optional[str]             # String summary passed to LLM (not the df itself)
    df_dtypes: Optional[Dict[str, str]]
    df_shape: Optional[tuple]
    df_sample: Optional[str]              # First 5 rows as string

    # ── Data quality ──────────────────────────────────────────────
    data_quality_report: Optional[DataQualityReport]

    # ── Analysis plan ─────────────────────────────────────────────
    analysis_plan: List[AnalysisTask]
    plan_approved: bool
    user_edits_to_plan: Optional[str]     # Free-text edits from human-in-the-loop

    # ── Execution ─────────────────────────────────────────────────
    current_task_index: int
    completed_tasks: List[AnalysisTask]
    failed_tasks: List[AnalysisTask]
    max_retries: int

    # ── Outputs ───────────────────────────────────────────────────
    chart_paths: List[str]
    execution_log: List[Dict[str, Any]]   # Timeline of every action
    final_report_md: Optional[str]
    final_report_pdf: Optional[str]
    notebook_path: Optional[str]
    business_insights: List[str]

    # ── Conversation memory ────────────────────────────────────────
    conversation_history: List[ConversationTurn]
    context_summary: Optional[str]        # Compressed older conversation
    context_warning: Optional[bool]       # True when >80% and compression ran
    current_user_message: Optional[str]
    agent_response: Optional[str]

    # ── Control flags ─────────────────────────────────────────────
    safe_mode: bool                        # If True, require approval before each execution
    is_followup_query: bool
    error_message: Optional[str]
    next_action: Optional[str]            # Routing hint for conditional edges
