"""
InsightPilot V2 application service (UI-1): a UI-agnostic adapter between a future Streamlit page and the existing
V2 multi-agent Supervisor. Not wired to any entry point yet (no launcher admission change in this phase).

It reuses, unchanged: multi_agent.team.build_team (real Supervisor, Planning, Verification and Business Insight
agents), the interactive LangGraph approval gate (Supervisor.start_interactive / Supervisor.resume), the trusted
operation engine (op_engine.TrustedOpsAnalyst: no generated code, no execution port), the dataset loader, and
multi_agent.live_llm.BudgetedPort for budgets, limits and per-call usage records.

The LLM provider is INJECTED (an object with complete(skill, system, user), call_source and code_trust). Nothing
here builds a live provider, reads credentials, imports the V1 application, the V1 executor, tests or evaluation
harnesses, or uses scripted approvals or golden answers.

State machine (one analysis at a time per service; one service per browser session):

    idle --start--> planning --> awaiting_approval --decide(any valid decision)--> running --> completed
                        |               |                                                 --> completed_with_failures
                        +--> failed     +--(invalid/stale decision: refused, state unchanged) --> rejected
                                                                                          --> failed
    any state except planning/running --reset--> idle;   terminal states --start--> planning

Limitations (documented, by design for this phase): checkpoints are in memory (lost when the process ends);
an analysis that raises during planning or execution ends in `failed` and is NOT continued: a new run is required.
No exactly-once guarantee is claimed across process failure. Per-session limits are not per-user limits, and this
module does not claim comprehensive multi-user security.

Data handling (UI-2 review): analytical data (claim operations and arguments, result values, column names) is
passed through EXACTLY and must be escaped by the renderer; it is never clipped or redacted, because it is what is
approved and computed. Large results are truncated only EXPLICITLY (ValueView.truncated / total_entries) and keep
their type. Model prose (titles, descriptions, insights) has control characters removed and credential-like
tokens redacted. Internal diagnostics (errors, task failures, validation problems) are sanitised with safe_text.

Session cleanup: close() deletes the session directory; a weakref finaliser deletes it when the service object is
garbage-collected; cleanup_stale_sessions() removes abandoned session directories (heartbeat older than a bound).
A browser session that disappears without close() keeps its files until one of these runs or the process exits.
"""

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import weakref
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from multi_agent import dataset as dsmod
from multi_agent import live_llm, op_engine
from multi_agent.security import POLICY_VERSION, RuntimeAuthority, SecurityViolation
from multi_agent.supervisor import DEFAULT_LIMITS, Supervisor
from multi_agent.team import build_team

SCHEMA_VERSION = "ui2.0"
SESSION_PREFIX = "ip-v2-session-"
SESSION_MARKER = ".ip-v2-session"
HEARTBEAT = ".heartbeat"
MAX_RESULT_ENTRIES = 500                 # a mapping result longer than this is truncated EXPLICITLY
MAX_TEXT_VALUE_CHARS = 10_000
VERIFICATION_SCOPE = ("Verified means each value was recomputed independently from your dataset and matched. "
                      "It does not prove that the chosen calculation answers your question as you intended.")
COST_LABEL = "ESTIMATE from provider-reported tokens x configured list prices; not billing"
USAGE_LABELS = {
    "mock": ("MOCK provider: tokens are synthetic (about characters / 4), not provider-reported; "
             "the cost figures are illustrative only."),
    "provider": "Provider-reported tokens. " + COST_LABEL + ".",
    "mixed": "Mixed mock and provider calls: treat every figure as illustrative.",
    "none": "No provider calls recorded.",
}
SECURITY_MESSAGE = ("A security check stopped the analysis: the run's state, approval or data no longer matched what "
                    "was approved. Nothing further was run; start a new analysis.")
CONSERVATIVE_LABEL = ("Conservative charges: each failed attempt is counted at its worst case (full prompt + "
                      "maximum output) because a failed call may still be billed; not billing.")


# ── configuration ────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Limits:
    max_upload_bytes: int = 10 * 1024 * 1024
    max_rows: int = 200_000
    max_columns: int = 60
    max_column_name_chars: int = 64
    min_goal_chars: int = 3
    max_goal_chars: int = 1000
    max_runs_per_session: int = 20
    session_budget_usd: float = 0.25            # cumulative estimated spend for the whole session
    max_session_calls: int = 200                # cumulative provider attempts for the whole session
    max_session_tokens: int = 1_000_000         # cumulative tokens for the whole session
    max_output_tokens: int = 2048


@dataclass(frozen=True)
class ServiceConfig:
    """Every optimisation is OFF by default and independently switchable."""
    model: str = live_llm.DEFAULT_MODEL
    prices_usd_per_mtok: Optional[tuple] = None  # None: live_llm.PRICES_USD_PER_MTOK[model]
    selective_recovery: bool = False
    schema_guidance: bool = False
    semantic_guidance: bool = False
    provider_retries: int = 0                    # transient HTTP 5xx / timeout retries (0 = off)
    max_steps: int = DEFAULT_LIMITS["max_steps"]
    limits: Limits = field(default_factory=Limits)
    # SEC: columns whose values must never leave the application (computed and shown locally only). The email and
    # credential heuristics supplement this list; they are not comprehensive PII detection.
    sensitive_columns: tuple = ()

    def supervisor_limits(self) -> dict:
        lim = {"max_steps": int(self.max_steps)}
        if self.sensitive_columns:
            lim["sensitive_columns"] = [str(x) for x in self.sensitive_columns]
        if self.selective_recovery:
            lim["selective_plan_recovery"] = True
        if self.schema_guidance:
            lim["schema_aware_planner"] = True
        if self.semantic_guidance:
            lim["semantic_planner"] = True
        return lim


# ── errors (messages are safe to show to a user) ─────────────────────────────────────────────────────────

class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class InvalidInput(ServiceError):
    """An upload or goal was rejected; nothing was stored or run."""


class TransitionRefused(ServiceError):
    """The action is not allowed in the current state (duplicate, stale, concurrent or out of order)."""


# ── state machine ────────────────────────────────────────────────────────────────────────────────────────

class Phase(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL = frozenset({Phase.COMPLETED, Phase.COMPLETED_WITH_FAILURES, Phase.REJECTED, Phase.FAILED})
BUSY = frozenset({Phase.PLANNING, Phase.RUNNING})
TRANSITIONS = {
    Phase.IDLE: {Phase.PLANNING},
    Phase.PLANNING: {Phase.AWAITING_APPROVAL, Phase.FAILED},
    Phase.AWAITING_APPROVAL: {Phase.RUNNING, Phase.REJECTED, Phase.FAILED},
    Phase.RUNNING: {Phase.COMPLETED, Phase.COMPLETED_WITH_FAILURES, Phase.REJECTED, Phase.FAILED},
    **{p: {Phase.PLANNING} for p in TERMINAL},
}

STATUS_MESSAGES = {      # Supervisor terminal status -> (phase, safe message)
    "completed": (Phase.COMPLETED, "Analysis completed."),
    "completed_with_failures": (Phase.COMPLETED_WITH_FAILURES,
                                "Analysis completed, but some tasks failed or were abandoned."),
    "rejected_by_human": (Phase.REJECTED, "The plan was rejected; nothing was analysed."),
    "plan_invalid": (Phase.FAILED, "The planner could not produce a valid plan for this goal and dataset."),
    "approval_failed": (Phase.FAILED, "The approval could not be processed; nothing was analysed."),
    "budget_exhausted": (Phase.FAILED, "The analysis reached its step limit before finishing."),
    "profile_failed": (Phase.FAILED, "The dataset could not be read."),
    "isolation_unavailable": (Phase.FAILED, "Execution was refused for safety reasons."),
    "execution_refused": (Phase.FAILED, "Execution was refused for safety reasons."),
    "policy_refused": (Phase.FAILED, "The data-protection policy refused to send this request to the AI provider "
                                     "(it would include an email address, a credential-like string or too much data)."),
}


# ── view models (JSON-serialisable; no raw graph state, paths, tracebacks or provider responses) ──────────

@dataclass(frozen=True)
class ColumnView:
    name: str
    kind: str
    missing: int


@dataclass(frozen=True)
class DatasetView:
    display_name: str
    rows: int
    columns: tuple


@dataclass(frozen=True)
class ClaimSpecView:
    """EXACT claim specification as validated and approved (never clipped or redacted)."""
    key: str
    op: str
    args: dict


@dataclass(frozen=True)
class ValueView:
    """A result value with its type kept. `truncated` is True only when entries or characters were dropped."""
    kind: str                       # number | integer | boolean | text | mapping | null | other
    value: object
    truncated: bool = False
    total_entries: Optional[int] = None
    shown_entries: Optional[int] = None


@dataclass(frozen=True)
class TaskPlanView:
    task_id: str
    title: str
    description: str
    analysis_type: str
    claims: tuple


@dataclass(frozen=True)
class AbandonedTaskView:
    task_id: str
    title: Optional[str]
    claim_keys: tuple
    problems: tuple


@dataclass(frozen=True)
class SecuritySignalView:
    """A detection SIGNAL (defence in depth; it never blocks or allows anything by itself)."""
    location: str
    signals: tuple


@dataclass(frozen=True)
class SecurityView:
    """Measured security events of one analysis: counts and reason codes only (no payloads)."""
    policy_version: str
    events: int
    allowed: int
    denied: int
    redacted: int
    signals: int
    refusals: tuple          # ((reason_code, count), ...)
    signal_locations: tuple  # ((location, count), ...)
    median_check_ms: Optional[float] = None


@dataclass(frozen=True)
class ApprovalView:
    run_id: str
    plan_id: str            # fingerprint of the exact plan shown; a decision must quote it
    goal: str
    dataset: DatasetView
    tasks: tuple
    abandoned_tasks: tuple  # tasks dropped by selective recovery before approval (if enabled)
    security_signals: tuple = ()   # SEC: injection or sensitive-pattern signals seen so far (for the reviewer)


@dataclass(frozen=True)
class ClaimResultView:
    key: str
    task_id: Optional[str]
    value: object                   # == result.value (kept for compatibility); see `result` for type/truncation
    spec: Optional[ClaimSpecView]
    result: Optional[ValueView] = None


@dataclass(frozen=True)
class InsightView:
    task_id: Optional[str]
    text: str
    cites: tuple


@dataclass(frozen=True)
class TaskResultView:
    task_id: str
    title: str
    status: str
    attempts: int
    failure: Optional[str]


@dataclass(frozen=True)
class ActivityView:
    kind: str
    detail: str


@dataclass(frozen=True)
class UsageView:
    provider_calls: int
    failed_provider_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    estimated_cost_failed_attempts_usd: float
    cost_label: str
    calls_by_skill: dict
    seconds_by_agent: dict
    seconds_by_node: dict
    usage_source: str = "none"      # mock | provider | mixed | none
    usage_label: str = ""
    conservative_label: str = CONSERVATIVE_LABEL


@dataclass(frozen=True)
class ResultView:
    run_id: str
    phase: str
    message: str
    complete: bool                 # True only for completed / completed_with_failures
    verification_scope: str
    verified_claims: tuple         # accepted results (complete runs only)
    review_only_claims: tuple      # verified but from an incomplete run: NOT accepted results
    insights: tuple
    review_only_insights: tuple
    tasks: tuple
    recovered_task_ids: tuple
    abandoned_tasks: tuple
    activity: tuple
    usage: UsageView
    error_code: Optional[str] = None
    security: Optional[SecurityView] = None


@dataclass(frozen=True)
class SessionUsageView:
    runs_started: int
    max_runs: int
    provider_calls: int
    max_calls: int
    tokens: int
    max_tokens: int
    estimated_spend_usd: float
    budget_usd: float
    limit_reached: bool
    cost_label: str = COST_LABEL
    usage_source: str = "none"
    usage_label: str = ""


def to_dict(view) -> dict:
    """A view model as plain JSON-serialisable data."""
    return json.loads(json.dumps(asdict(view), default=str))


# ── sanitising ───────────────────────────────────────────────────────────────────────────────────────────

_PATH = re.compile(r"(?:[A-Za-z]:\\|/)(?:[^\s'\"<>|]+[/\\])+[^\s'\"<>|]*")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET = re.compile(r"(?i)(bearer\s+|api[_-]?key\s*[=:]\s*|key=)[A-Za-z0-9._\-]{6,}")


def safe_text(value, limit=300) -> str:
    """User-facing text: no control characters, filesystem paths or credential-looking tokens; clipped."""
    s = _CONTROL.sub("", str(value if value is not None else ""))
    s = _SECRET.sub(r"\1[redacted]", s)
    s = _PATH.sub("[path]", s)
    return s if len(s) <= limit else s[:limit - 3] + "..."


def prose(value, limit=4000) -> str:
    """Model-written analytical prose: control characters removed, credential-like tokens redacted."""
    s = _CONTROL.sub("", str(value if value is not None else ""))
    s = _SECRET.sub(r"\1[redacted]", s)
    return s if len(s) <= limit else s[:limit - 3] + "..."


class MalformedPlan(ValueError):
    """The approval payload is not a well-formed plan; it is never shown or approved."""


def _json_exact(v):
    """A JSON round trip that must not change the value (finite numbers, str keys, plain containers)."""
    try:
        text = json.dumps(v, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise MalformedPlan("not plain JSON") from None
    return json.loads(text)


def value_view(v) -> ValueView:
    """Typed, explicitly truncated result value. Mappings stay mappings; nothing becomes a string silently."""
    if v is None:
        return ValueView("null", None)
    if isinstance(v, bool):
        return ValueView("boolean", v)
    if isinstance(v, int):
        return ValueView("integer", v)
    if isinstance(v, float):
        return ValueView("number", v) if math.isfinite(v) else ValueView("other", str(v))
    if isinstance(v, str):
        if len(v) > MAX_TEXT_VALUE_CHARS:
            return ValueView("text", v[:MAX_TEXT_VALUE_CHARS], truncated=True)
        return ValueView("text", v)
    if isinstance(v, dict):
        items = sorted(v.items(), key=lambda kv: str(kv[0]))
        shown = items[:MAX_RESULT_ENTRIES]
        body = {str(k): (x if isinstance(x, (int, float, str, bool)) or x is None else str(x)) for k, x in shown}
        return ValueView("mapping", body, truncated=len(items) > len(shown), total_entries=len(items),
                         shown_entries=len(shown))
    return ValueView("other", str(v)[:MAX_TEXT_VALUE_CHARS], truncated=len(str(v)) > MAX_TEXT_VALUE_CHARS)


def cleanup_stale_sessions(base_dir=None, max_idle_s=12 * 3600, now=None) -> int:
    """Remove abandoned session directories (marker present, heartbeat older than max_idle_s). Returns the count.
    Only directories this module created (prefix + marker) are considered."""
    base = base_dir or tempfile.gettempdir()
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        d = os.path.join(base, name)
        if not name.startswith(SESSION_PREFIX) or not os.path.isfile(os.path.join(d, SESSION_MARKER)):
            continue
        hb = os.path.join(d, HEARTBEAT)
        try:
            last = os.path.getmtime(hb if os.path.exists(hb) else os.path.join(d, SESSION_MARKER))
        except OSError:
            continue
        if now - last > max_idle_s:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


# ── the service ──────────────────────────────────────────────────────────────────────────────────────────

class AnalysisService:
    """One per browser session. Thread-safe: every transition is taken under a lock."""

    def __init__(self, llm_port, config: Optional[ServiceConfig] = None, base_dir: Optional[str] = None,
                 clock=time.perf_counter):
        self.config = config or ServiceConfig()
        self.limits = self.config.limits
        self.clock = clock
        self._lock = threading.Lock()
        self._dir = tempfile.mkdtemp(prefix=SESSION_PREFIX, dir=base_dir)        # 0700, random name
        self._finalizer = weakref.finalize(self, shutil.rmtree, self._dir, True)  # GC of the service deletes files
        try:
            self._data_dir = os.path.join(self._dir, "data")
            os.mkdir(self._data_dir, 0o700)
            open(os.path.join(self._dir, SESSION_MARKER), "w").close()
            prices = self.config.prices_usd_per_mtok
            # ONE budgeted port per session: spend, calls and tokens are cumulative across analyses and resets.
            self._port = live_llm.BudgetedPort(
                llm_port, model=self.config.model, budget_usd=self.limits.session_budget_usd,
                ledger=live_llm.Ledger(os.path.join(self._dir, "ledger.jsonl")),
                max_calls=self.limits.max_session_calls, max_total_tokens=self.limits.max_session_tokens,
                max_output_tokens=self.limits.max_output_tokens, prices=tuple(prices) if prices else None,
                run_label="v2-app", clock=time.time, transient_retries=self.config.provider_retries)
        except Exception:
            self._finalizer()                                                     # no leaked directory
            raise
        self._phase = Phase.IDLE
        self._dataset_path = None
        self._dataset_view = None
        self._runs_started = 0
        self._closed = False
        self._session_id = uuid.uuid4().hex             # SEC: approvals and grants are bound to this session
        self._security_log = []                         # SEC: this session's audit events (codes only), bounded
        self._clear_analysis()

    # ── public API ───────────────────────────────────────────────────────────────────────────────────

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def dataset(self) -> Optional[DatasetView]:
        return self._dataset_view

    @property
    def approval(self) -> Optional[ApprovalView]:
        return self._approval if self._phase == Phase.AWAITING_APPROVAL else None

    @property
    def result(self) -> Optional[ResultView]:
        return self._result

    def upload(self, filename: str, data: bytes) -> DatasetView:
        """Validate and store a CSV under a random internal name. Replaces any previous dataset."""
        with self._lock:
            self._require_open()
            if self._phase in BUSY or self._phase == Phase.AWAITING_APPROVAL:
                raise TransitionRefused("busy", "Finish or reset the current analysis before uploading.")
            name = safe_text(os.path.basename(str(filename or "").replace("\\", "/")), 80)
            if not name.lower().endswith(".csv"):
                raise InvalidInput("not_csv", "Only .csv files are accepted.")
            if not isinstance(data, (bytes, bytearray)):
                raise InvalidInput("bad_upload", "The upload could not be read.")
            if len(data) == 0:
                raise InvalidInput("empty", "The file is empty.")
            if len(data) > self.limits.max_upload_bytes:                          # checked BEFORE writing
                raise InvalidInput("too_large", f"The file is larger than {self.limits.max_upload_bytes} bytes.")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise InvalidInput("not_utf8", "The file must be UTF-8 encoded text.") from None
            path = os.path.join(self._data_dir, f"dataset-{uuid.uuid4().hex}.csv")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                view = self._validate_dataset(path, name)
            except InvalidInput:
                _remove_quietly(path)
                raise
            except Exception:                                    # disk full, permissions, ...: nothing is kept
                _remove_quietly(path)
                raise InvalidInput("upload_failed", "The file could not be stored.") from None
            self._touch()
            self._remove_dataset()
            self._dataset_path, self._dataset_view = path, view
            self._clear_analysis()
            self._phase = Phase.IDLE
            return view

    def start(self, goal: str):
        """Validate the goal and run planning until the human approval gate. Returns an ApprovalView, or the
        ResultView of a run that ended before approval (for example an invalid plan)."""
        with self._lock:
            self._require_open()
            if self._phase not in (Phase.IDLE,) + tuple(TERMINAL):
                raise TransitionRefused("busy", "An analysis is already in progress.")
            if self._dataset_path is None:
                raise TransitionRefused("no_dataset", "Upload a dataset first.")
            goal = self._validate_goal(goal)
            if self._runs_started >= self.limits.max_runs_per_session:
                raise TransitionRefused("usage_limit", "The session's analysis limit has been reached.")
            if self._limit_reached():
                raise TransitionRefused("usage_limit", "The session's usage limit has been reached.")
            self._clear_analysis()
            self._transition(Phase.PLANNING)
            self._runs_started += 1
            self._touch()
            run_id = uuid.uuid4().hex
            self._run_id, self._goal, self._record_start = run_id, goal, len(self._port.records)
        try:
            self._security_start = len(self._security_log)
            authority = RuntimeAuthority(session_id=self._session_id, sink=self._record_security)
            sup, self._agent_calls = build_team(self._port, None, approval_port=None, clock=self.clock,
                                                analyst=op_engine.TrustedOpsAnalyst(self.clock), authority=authority)
            thread_id = f"{run_id}-{uuid.uuid4().hex}"
            graph, cfg, state = sup.start_interactive(self._dataset_path, thread_id, goal=goal,
                                                      limits=self.config.supervisor_limits())
            with self._lock:
                interrupts = state.get("__interrupt__") or []
                if interrupts:
                    payload = interrupts[0].value
                    approval = self._approval_view(run_id, goal, payload)        # raises MalformedPlan
                    self._graph, self._cfg, self._pending, self._approval = graph, cfg, payload, approval
                    self._transition(Phase.AWAITING_APPROVAL)
                    return self._approval
                self._result = self._result_view(run_id, state)    # ended before approval (e.g. plan invalid)
                self._transition(Phase(self._result.phase))
                return self._result
        except MalformedPlan:
            return self._fail(run_id, "invalid_plan_payload", None,
                              "The planner returned a plan that cannot be shown for approval; nothing was analysed.")
        except SecurityViolation:
            return self._fail(run_id, "security_refused", None, SECURITY_MESSAGE)
        except Exception as e:                                       # unexpected: fail closed, no continuation
            return self._fail(run_id, "internal_error", e)

    def decide(self, run_id: str, plan_id: str, decision: str, keep_task_ids=None, note: str = "") -> ResultView:
        """Apply a human decision to the exact plan shown (run_id + plan_id). Refused, with no effect, if it is
        duplicate, stale, concurrent, malformed or names tasks outside the plan."""
        with self._lock:
            self._require_open()
            self._touch()
            if self._phase != Phase.AWAITING_APPROVAL or self._approval is None:
                raise TransitionRefused("not_awaiting", "There is no plan waiting for a decision.")
            if run_id != self._approval.run_id or plan_id != self._approval.plan_id:
                raise TransitionRefused("stale", "This decision refers to a different or outdated plan.")
            payload = self._decision_payload(decision, keep_task_ids, note)
            if isinstance(self._pending, dict) and isinstance(self._pending.get("binding"), dict):
                payload["binding"] = dict(self._pending["binding"])     # SEC: the exact question that was shown
            graph, cfg = self._graph, self._cfg
            self._transition(Phase.RUNNING)                  # under the lock: a second decision is now refused
        try:
            state = Supervisor.resume(graph, cfg, payload)
            with self._lock:
                self._graph = self._cfg = self._pending = None
                self._result = self._result_view(run_id, state)
                self._transition(Phase(self._result.phase))
                return self._result
        except SecurityViolation:
            return self._fail(run_id, "security_refused", None, SECURITY_MESSAGE)
        except Exception as e:
            return self._fail(run_id, "internal_error", e)

    def security_events(self, limit=200) -> tuple:
        """This session's most recent security events (ids, codes, timings only; never payloads)."""
        return tuple(dict(e) for e in self._security_log[-limit:])

    def _record_security(self, ev):
        if len(self._security_log) >= 5000:
            del self._security_log[:1000]
            self._security_start = max(0, getattr(self, "_security_start", 0) - 1000)
        self._security_log.append(ev)

    def _security_view(self) -> SecurityView:
        evs = self._security_log[getattr(self, "_security_start", 0):]
        refusals, locations, ms = {}, {}, []
        for e in evs:
            if e["decision"] == "deny":
                refusals[e["reason_code"]] = refusals.get(e["reason_code"], 0) + 1
            if e["decision"] == "signal":
                locations[e["location"]] = locations.get(e["location"], 0) + 1
            if e.get("check_ms") is not None:
                ms.append(e["check_ms"])
        ms.sort()
        return SecurityView(policy_version=POLICY_VERSION, events=len(evs),
                            allowed=sum(e["decision"] == "allow" for e in evs),
                            denied=sum(e["decision"] == "deny" for e in evs),
                            redacted=sum(e["decision"] == "redact" for e in evs),
                            signals=sum(e["decision"] == "signal" for e in evs),
                            refusals=tuple(sorted(refusals.items())), signal_locations=tuple(sorted(locations.items())),
                            median_check_ms=round(ms[len(ms) // 2], 4) if ms else None)

    def _signal_views(self) -> tuple:
        evs = self._security_log[getattr(self, "_security_start", 0):]
        return tuple(SecuritySignalView(safe_text(e["location"], 40), tuple(e.get("signals") or ()))
                     for e in evs if e["decision"] == "signal")[:20]

    def reset(self, clear_dataset: bool = False) -> None:
        """Discard the current analysis (graph, checkpoint, results). Refused while planning or running.
        Cumulative session usage is NOT reset."""
        with self._lock:
            self._require_open()
            if self._phase in BUSY:
                raise TransitionRefused("busy", "An analysis is running; wait for it to finish.")
            self._clear_analysis()
            if clear_dataset:
                self._remove_dataset()
            self._phase = Phase.IDLE

    def close(self) -> None:
        """Delete every file of this session. Refused while planning or running."""
        with self._lock:
            if self._closed:
                return
            if self._phase in BUSY:
                raise TransitionRefused("busy", "An analysis is running; wait for it to finish.")
            self._clear_analysis()
            self._finalizer()                                    # deletes the directory (idempotent)
            self._closed = True

    def session_usage(self) -> SessionUsageView:
        recs = self._port.records
        return SessionUsageView(
            runs_started=self._runs_started, max_runs=self.limits.max_runs_per_session,
            provider_calls=self._port.calls, max_calls=self.limits.max_session_calls,
            tokens=self._port.tokens, max_tokens=self.limits.max_session_tokens,
            estimated_spend_usd=round(sum(r["cost_usd"] for r in recs), 6),
            budget_usd=self.limits.session_budget_usd,
            limit_reached=self._limit_reached() or self._runs_started >= self.limits.max_runs_per_session,
            usage_source=_source(recs), usage_label=USAGE_LABELS[_source(recs)])

    # ── internals ────────────────────────────────────────────────────────────────────────────────────

    def _touch(self):
        """Heartbeat for cleanup_stale_sessions (best effort)."""
        try:
            with open(os.path.join(self._dir, HEARTBEAT), "w"):
                pass
        except OSError:
            pass

    def _require_open(self):
        if self._closed:
            raise TransitionRefused("closed", "This session has ended.")

    def _transition(self, new: Phase):
        if new not in TRANSITIONS.get(self._phase, set()):
            raise TransitionRefused("bad_transition", f"Cannot go from {self._phase.value} to {new.value}.")
        self._phase = new

    def _clear_analysis(self):
        self._graph = self._cfg = self._pending = None
        self._approval = self._result = None
        self._run_id, self._goal = None, None
        self._agent_calls, self._record_start = [], len(self._port.records) if hasattr(self, "_port") else 0

    def _remove_dataset(self):
        if self._dataset_path and os.path.exists(self._dataset_path):
            os.remove(self._dataset_path)
        self._dataset_path = self._dataset_view = None

    def _limit_reached(self) -> bool:
        p, lim = self._port, self.limits
        return bool(p.exhausted or getattr(p, "stop_reason", None) or p.calls >= lim.max_session_calls
                    or p.ledger.spent_usd() >= lim.session_budget_usd or p.tokens >= lim.max_session_tokens)

    def _validate_dataset(self, path, display_name) -> DatasetView:
        lim = self.limits
        try:
            ds = dsmod.load(path, max_bytes=lim.max_upload_bytes)
            prof = dsmod.profile(ds)
        except (dsmod.DatasetError, UnicodeDecodeError, OSError) as e:
            raise InvalidInput("invalid_csv", "The CSV could not be read: " + safe_text(e, 160)) from None
        except Exception:                                            # csv.Error and similar
            raise InvalidInput("invalid_csv", "The CSV could not be read.") from None
        if not ds.rows:
            raise InvalidInput("no_rows", "The CSV has a header but no data rows.")
        if prof["row_count"] > lim.max_rows:
            raise InvalidInput("too_many_rows", f"The CSV has more than {lim.max_rows} rows.")
        if len(prof["columns"]) > lim.max_columns:
            raise InvalidInput("too_many_columns", f"The CSV has more than {lim.max_columns} columns.")
        for c in prof["columns"]:
            n = c["name"]
            if not n.strip() or len(n) > lim.max_column_name_chars or _CONTROL.search(n):
                raise InvalidInput("bad_column_name",
                                   f"Column names must be 1 to {lim.max_column_name_chars} printable characters.")
        cols = tuple(ColumnView(c["name"], c["kind"], int(c["missing"])) for c in prof["columns"])
        return DatasetView(display_name=display_name, rows=prof["row_count"], columns=cols)

    def _validate_goal(self, goal) -> str:
        if not isinstance(goal, str):
            raise InvalidInput("bad_goal", "The goal must be text.")
        g = goal.strip()
        if _CONTROL.search(g):
            raise InvalidInput("bad_goal", "The goal contains control characters.")
        if not (self.limits.min_goal_chars <= len(g) <= self.limits.max_goal_chars):
            raise InvalidInput("bad_goal", f"The goal must be {self.limits.min_goal_chars} to "
                                           f"{self.limits.max_goal_chars} characters.")
        return g

    def _decision_payload(self, decision, keep_task_ids, note) -> dict:
        if not isinstance(decision, str) or decision not in ("approve", "reject", "edit"):
            raise TransitionRefused("bad_decision", "The decision must be approve, reject or edit.")
        payload = {"decision": decision}
        if decision == "edit":
            ids = [t.task_id for t in self._approval.tasks]
            if (not isinstance(keep_task_ids, (list, tuple)) or not keep_task_ids
                    or not all(isinstance(k, str) for k in keep_task_ids)
                    or len(set(keep_task_ids)) != len(keep_task_ids) or not set(keep_task_ids) <= set(ids)):
                raise TransitionRefused("bad_decision", "Keep a non-empty set of distinct tasks from the plan.")
            payload["keep_task_ids"] = [i for i in ids if i in set(keep_task_ids)]
        elif keep_task_ids:
            raise TransitionRefused("bad_decision", "Tasks can only be selected with the edit decision.")
        if note:
            payload["reviewer_note"] = safe_text(note, 500)
        return payload

    def _fail(self, run_id, code, exc, message=None) -> ResultView:
        with self._lock:
            self._graph = self._cfg = self._pending = None
            self._approval = None
            msg = message or ("A provider or usage limit stopped the analysis." if isinstance(
                exc, (live_llm.BudgetExceeded, live_llm.QuotaOrBillingStop)) else
                "The analysis stopped unexpectedly. Start a new analysis.")
            try:
                self._result = self._empty_result(run_id, Phase.FAILED, msg, code)
            except Exception:                                    # never stay busy, even if usage mapping fails
                self._result = None
            self._phase = Phase.FAILED
            return self._result

    # views

    def _approval_view(self, run_id, goal, payload) -> ApprovalView:
        """The EXACT plan in the interrupt payload. Anything malformed raises MalformedPlan (fail closed)."""
        if not isinstance(payload, dict) or not isinstance(payload.get("plan"), dict):
            raise MalformedPlan("no plan")
        plan = _json_exact(payload["plan"])
        if plan != payload["plan"] or not isinstance(plan.get("tasks"), list) or not plan["tasks"]:
            raise MalformedPlan("plan is not exact JSON or has no tasks")
        plan_id = hashlib.sha256(json.dumps(plan, sort_keys=True, allow_nan=False).encode()).hexdigest()
        tasks, seen = [], set()
        for t in plan["tasks"]:
            if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not t["id"] or t["id"] in seen:
                raise MalformedPlan("task without a unique string id")
            seen.add(t["id"])
            claims = []
            for c in t.get("claims") or []:
                if not (isinstance(c, dict) and isinstance(c.get("key"), str) and isinstance(c.get("op"), str)
                        and isinstance(c.get("args", {}), dict)):
                    raise MalformedPlan("malformed claim")
                claims.append(ClaimSpecView(c["key"], c["op"], c.get("args", {})))
            tasks.append(TaskPlanView(task_id=t["id"], title=prose(t.get("title"), 200),
                                      description=prose(t.get("description"), 2000),
                                      analysis_type=prose(t.get("analysis_type"), 40), claims=tuple(claims)))
        return ApprovalView(run_id=run_id, plan_id=plan_id, goal=goal, dataset=self._dataset_view, tasks=tuple(tasks),
                            abandoned_tasks=_abandoned(payload.get("abandoned_tasks")),
                            security_signals=self._signal_views())

    def _result_view(self, run_id, state) -> ResultView:
        final = state.get("final") or {}
        status = final.get("status") or "approval_failed"
        phase, msg = STATUS_MESSAGES.get(status, (Phase.FAILED, "The analysis stopped."))
        code = None if phase in (Phase.COMPLETED, Phase.COMPLETED_WITH_FAILURES, Phase.REJECTED) else status
        if self._port.exhausted or getattr(self._port, "stop_reason", None):
            code, msg = "usage_limit", msg + " A session usage or provider limit was reached."
        elif phase == Phase.FAILED and self._provider_failed():
            code, msg = "provider_unavailable", msg + " The AI provider did not respond successfully."
        complete = bool(final.get("run_complete"))
        plan = state.get("plan") or {}
        specs, titles = {}, {}
        for t in plan.get("tasks", []) if isinstance(plan, dict) else []:
            if not isinstance(t, dict):
                continue
            titles[t.get("id")] = t.get("title")
            for c in t.get("claims") or []:
                if isinstance(c, dict) and isinstance(c.get("key"), str):
                    specs[c["key"]] = (t.get("id"), ClaimSpecView(c["key"], str(c.get("op")),
                                                                  c.get("args") if isinstance(c.get("args"), dict) else {}))

        def claims_of(d):
            out = []
            for k, v in sorted((d or {}).items()):
                vv = value_view(v)
                out.append(ClaimResultView(str(k), specs.get(k, (None,))[0], vv.value, specs.get(k, (None, None))[1], vv))
            return tuple(out)

        def insights_of(xs):
            return tuple(InsightView(x.get("task_id"), prose(x.get("text"), 4000),
                                     tuple(str(c) for c in x.get("cites", []))) for x in xs or [])
        tasks = tuple(TaskResultView(str(tid), prose(titles.get(tid), 200), safe_text(v.get("status"), 32),
                                     int(v.get("attempts") or 0), safe_text(v.get("failure"), 300) if v.get("failure") else None)
                      for tid, v in (final.get("tasks") or {}).items())
        rec = final.get("plan_recovery") or {}
        return ResultView(
            run_id=run_id, phase=phase.value, message=msg, complete=complete, verification_scope=VERIFICATION_SCOPE,
            verified_claims=claims_of(final.get("accepted_claims")) if complete else (),
            review_only_claims=claims_of(final.get("verified_claims_for_review")),
            insights=insights_of(final.get("insights")) if complete else (),
            review_only_insights=insights_of(final.get("insights_for_review")),
            tasks=tasks, recovered_task_ids=tuple(safe_text(x, 64) for x in rec.get("recovered_task_ids") or []),
            abandoned_tasks=_abandoned(rec.get("abandoned_tasks")), activity=self._activity(state),
            usage=self._usage(state), error_code=code, security=self._security_view())

    def _empty_result(self, run_id, phase, msg, code) -> ResultView:
        return ResultView(run_id=run_id, phase=phase.value, message=msg, complete=False,
                          verification_scope=VERIFICATION_SCOPE, verified_claims=(), review_only_claims=(),
                          insights=(), review_only_insights=(), tasks=(), recovered_task_ids=(), abandoned_tasks=(),
                          activity=(), usage=self._usage({}), error_code=code, security=self._security_view())

    def _provider_failed(self) -> bool:
        recs = self._port.records[self._record_start:]
        return bool(recs) and all(r.get("failed") for r in recs if r["skill"] == "planner")

    @staticmethod
    def _activity(state, limit=60) -> tuple:
        out = []
        for e in (state.get("events") or [])[:limit]:
            kind = safe_text(e.get("type"), 40)
            what = e.get("kind") or e.get("agent") or e.get("category") or ""
            probs = "; ".join(safe_text(p, 160) for p in (e.get("problems") or [])[:3])
            out.append(ActivityView(kind, safe_text(f"{what} {probs}".strip(), 400)))
        return tuple(out)

    def _usage(self, state) -> UsageView:
        recs = self._port.records[self._record_start:]
        ok = [r for r in recs if not r.get("failed")]
        by_skill, by_agent, by_node = {}, {}, {}
        for r in recs:
            by_skill[r["skill"]] = by_skill.get(r["skill"], 0) + 1
        for c in self._agent_calls:
            by_agent[c["agent"]] = round(by_agent.get(c["agent"], 0.0) + float(c.get("seconds") or 0.0), 4)
        for t in state.get("timings") or []:
            by_node[t["node"]] = round(by_node.get(t["node"], 0.0) + float(t.get("seconds") or 0.0), 4)
        src = _source(recs)
        return UsageView(
            usage_source=src, usage_label=USAGE_LABELS[src],
            provider_calls=len(recs), failed_provider_calls=len(recs) - len(ok),
            input_tokens=sum(r["input_tokens"] for r in ok), output_tokens=sum(r["output_tokens"] for r in ok),
            estimated_cost_usd=round(sum(r["cost_usd"] for r in ok), 6),
            estimated_cost_failed_attempts_usd=round(sum(r["cost_usd"] for r in recs if r.get("failed")), 6),
            cost_label=COST_LABEL, calls_by_skill=by_skill, seconds_by_agent=by_agent, seconds_by_node=by_node)


def _source(records) -> str:
    kinds = {r.get("call_source") for r in records}
    if not kinds:
        return "none"
    if kinds == {live_llm.MOCK}:
        return "mock"
    if kinds == {live_llm.PROVIDER}:
        return "provider"
    return "mixed"


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _abandoned(items) -> tuple:
    return tuple(AbandonedTaskView(safe_text(a.get("id"), 64), safe_text(a.get("title"), 120) if a.get("title") else None,
                                   tuple(safe_text(k, 64) for k in a.get("claim_keys", [])),
                                   tuple(safe_text(p, 200) for p in a.get("problems", [])[:5]))
                 for a in items or [] if isinstance(a, dict))
