"""
Supervisor Agent: a LangGraph StateGraph that coordinates the specialist agents.

    START -> supervisor -> {planning_agent | approval_gate | data_analyst_agent |
                            verification_agent | business_insight_agent} -> supervisor -> ... -> finalize -> END

The Supervisor is the only node that decides what happens next. Each step:
1. absorb the reply envelope: check its route, reply_to, task and attempt, then
   validate its payload against the contract. A rejected handoff is logged as
   an event and counts as a failed attempt; it is never repaired;
2. decide: delegate the next request (one envelope) or finalize.

Policies:
- the plan is validated against the dataset profile BEFORE the human sees it;
- human approval gate (approve / reject / edit = keep a subset of tasks). With a
  checkpointer the graph pauses with langgraph.types.interrupt and resumes with
  Command(resume=decision); otherwise an approval_port callable decides. With
  neither, the run fails closed (status approval_failed);
- analysis attempts per task: 1 + max_retries (V1 semantics). Execution failures
  and verification rejections both consume an attempt; feedback to the analyst
  carries the reasons (never the verifier's recomputed values);
- insight attempts per task: max_insight_attempts;
- only verified claims are accepted, and only they reach the Business Insight Agent;
- a global step budget (max_steps dispatches) guarantees termination;
- reflection: every rejection is recorded as a lesson in the run state and fed
  back to the next attempt. Nothing is written to the repository's lessons/.

Termination statuses: completed, completed_with_failures, rejected_by_human,
plan_invalid, approval_failed, budget_exhausted, profile_failed.

This graph is separate from agent/graph.py (the V1 graph), which is unchanged.
"""

import copy
import operator
import time
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from multi_agent import contracts as c
from multi_agent import dataset as dsmod

DEFAULT_GOAL = "Analyse this dataset and report the most important verified findings."
DEFAULT_LIMITS = {"max_retries": 2, "max_plan_attempts": 2, "max_insight_attempts": 2, "max_steps": 200,
                  "max_feedback_items": 12}
TERMINAL_STATUSES = ("completed", "completed_with_failures", "rejected_by_human", "plan_invalid",
                     "approval_failed", "budget_exhausted", "profile_failed")
FINALIZE = "finalize"


class SupervisorState(TypedDict, total=False):
    dataset_path: str
    goal: str
    limits: dict
    profile: Optional[dict]
    plan: Optional[dict]
    plan_attempts: int
    plan_feedback: list
    approved_task_ids: Optional[list]
    approval_note: Optional[str]
    task_index: int
    tasks: dict
    pending: Optional[dict]
    awaiting: Optional[dict]
    seq: int
    steps: int
    status: Optional[str]
    route: str
    final: Optional[dict]
    messages: Annotated[list, operator.add]
    events: Annotated[list, operator.add]
    lessons: Annotated[list, operator.add]
    timings: Annotated[list, operator.add]


_SCALARS = ("profile", "plan", "plan_attempts", "plan_feedback", "approved_task_ids", "approval_note",
            "task_index", "tasks", "awaiting", "seq", "steps", "status")
_DEFAULTS = {"plan_attempts": 0, "plan_feedback": [], "task_index": 0, "tasks": {}, "seq": 0, "steps": 0}


def _new_task():
    return {"status": "pending", "stage": "needs_analysis", "attempts": 0, "insight_attempts": 0,
            "feedback": [], "insight_feedback": [], "analysis": None, "verification": None,
            "verified_claims": None, "candidate_insights": None, "insights": None, "failure": None,
            "history": []}


class Supervisor:
    def __init__(self, planning, analyst, verifier, insight, approval_port=None,
                 clock=time.perf_counter, ds_loader=dsmod.load):
        self.planning, self.analyst, self.verifier, self.insight = planning, analyst, verifier, insight
        self.approval_port = approval_port
        self.clock, self.ds_loader = clock, ds_loader

    # ── graph ────────────────────────────────────────────────────────────────

    def build(self, checkpointer=None):
        interactive = checkpointer is not None
        g = StateGraph(SupervisorState)
        g.add_node(c.SUPERVISOR, self._timed(c.SUPERVISOR, self.supervise))
        g.add_node(c.PLANNING, self._timed(c.PLANNING, self._agent_node(
            c.PLANNING, {"plan_request"}, lambda env: self.planning.run(env["payload"]))))
        g.add_node(c.APPROVAL, self._timed(c.APPROVAL, lambda s: self.approval_node(s, interactive)))
        g.add_node(c.ANALYST, self._timed(c.ANALYST, self._agent_node(
            c.ANALYST, {"analysis_request"}, lambda env: self.analyst.run(env["payload"]))))
        g.add_node(c.VERIFIER, self._timed(c.VERIFIER, self._agent_node(
            c.VERIFIER, {"verification_request", "insight_check_request"},
            lambda env: self.verifier.run(env["kind"], env["payload"]))))
        g.add_node(c.INSIGHT, self._timed(c.INSIGHT, self._agent_node(
            c.INSIGHT, {"insight_request"}, lambda env: self.insight.run(env["payload"]))))
        g.add_node(FINALIZE, self._timed(FINALIZE, self.finalize))
        g.add_edge(START, c.SUPERVISOR)
        g.add_conditional_edges(c.SUPERVISOR, lambda s: s["route"], {**{a: a for a in c.AGENTS}, FINALIZE: FINALIZE})
        for a in c.AGENTS:
            g.add_edge(a, c.SUPERVISOR)
        g.add_edge(FINALIZE, END)
        return g.compile(checkpointer=checkpointer)

    @staticmethod
    def initial_state(dataset_path, goal=DEFAULT_GOAL, limits=None):
        return {"dataset_path": dataset_path, "goal": goal, "limits": {**DEFAULT_LIMITS, **(limits or {})}}

    @staticmethod
    def run_config(limits=None, thread_id=None):
        lim = {**DEFAULT_LIMITS, **(limits or {})}
        cfg = {"recursion_limit": 2 * lim["max_steps"] + 10}
        if thread_id is not None:
            cfg["configurable"] = {"thread_id": thread_id}
        return cfg

    def run(self, dataset_path, goal=DEFAULT_GOAL, limits=None):
        """Non-interactive run; approvals come from approval_port (fail closed without one)."""
        return self.build().invoke(self.initial_state(dataset_path, goal, limits), self.run_config(limits))

    def start_interactive(self, dataset_path, thread_id, goal=DEFAULT_GOAL, limits=None):
        """Run until the human approval gate pauses. Returns (graph, config, state)."""
        from langgraph.checkpoint.memory import InMemorySaver
        graph = self.build(checkpointer=InMemorySaver())
        cfg = self.run_config(limits, thread_id)
        return graph, cfg, graph.invoke(self.initial_state(dataset_path, goal, limits), cfg)

    @staticmethod
    def resume(graph, cfg, decision):
        return graph.invoke(Command(resume=decision), cfg)

    def _timed(self, name, fn):
        def node(state):
            t0 = self.clock()
            out = dict(fn(state))
            out["timings"] = [{"node": name, "seconds": self.clock() - t0}]
            return out
        return node

    # ── agent nodes ──────────────────────────────────────────────────────────

    @staticmethod
    def _reply(state, env, sender, kind, payload):
        reply = c.envelope(state["seq"] + 1, sender, c.SUPERVISOR, kind, payload, env["task_id"], env["attempt"],
                           reply_to=env["seq"])
        return {"pending": reply, "seq": reply["seq"], "messages": [reply]}

    def _agent_node(self, me, kinds, call):
        def node(state):
            env = state.get("pending")
            c.check_envelope(env, recipient=me, kinds=kinds)        # a misrouted request is a supervisor bug: raise
            try:
                payload, kind = call(env), c.REPLY_KIND[env["kind"]]
            except Exception as e:                                   # agent failure -> typed error handoff
                payload, kind = {"error_type": type(e).__name__, "message": str(e)[:2000]}, "agent_error"
            return self._reply(state, env, me, kind, payload)
        return node

    def approval_node(self, state, interactive):
        env = state.get("pending")
        c.check_envelope(env, recipient=c.APPROVAL, kinds={"approval_request"})
        if self.approval_port is not None:
            decision = self.approval_port(copy.deepcopy(env["payload"]))
        elif interactive:
            decision = interrupt({"kind": "plan_approval", "plan": env["payload"]["plan"]})
        else:
            return self._reply(state, env, c.APPROVAL, "agent_error",
                               {"error_type": "NoApprover", "message": "no human approver configured; failing closed"})
        if not isinstance(decision, dict):
            decision = {"decision": decision}
        return self._reply(state, env, c.APPROVAL, "approval_decision", decision)

    # ── supervisor ───────────────────────────────────────────────────────────

    def supervise(self, state):
        lim = {**DEFAULT_LIMITS, **(state.get("limits") or {})}
        s = {k: copy.deepcopy(state[k]) if state.get(k) is not None else copy.deepcopy(_DEFAULTS.get(k))
             for k in _SCALARS}
        s["dataset_path"], s["goal"] = state["dataset_path"], state.get("goal", DEFAULT_GOAL)
        events, lessons = [], []
        env = state.get("pending")
        if env is not None and env.get("recipient") == c.SUPERVISOR:
            self._absorb(s, env, lim, events, lessons)
        request = self._decide(s, lim, events)
        out = {k: s[k] for k in _SCALARS}
        out.update(events=events, lessons=lessons, pending=request,
                   route=request["recipient"] if request else FINALIZE)
        if request:
            out["messages"] = [request]
        return out

    def _dispatch(self, s, recipient, kind, payload, task_id=None, attempt=0):
        s["seq"] += 1
        s["steps"] += 1
        env = c.envelope(s["seq"], c.SUPERVISOR, recipient, kind, payload, task_id, attempt)
        s["awaiting"] = {"seq": env["seq"], "recipient": recipient, "kind": kind, "task_id": task_id,
                         "attempt": attempt}
        return env

    def _absorb(self, s, env, lim, events, lessons):
        aw = s["awaiting"]
        s["awaiting"] = None
        try:
            if aw is None:
                raise c.HandoffRejected(["reply without an outstanding request"])
            c.check_envelope(env, recipient=c.SUPERVISOR, kinds={c.REPLY_KIND[aw["kind"]], "agent_error"},
                             reply_to=aw["seq"])
            problems = []
            if env["sender"] != aw["recipient"]:
                problems.append(f"reply from {env['sender']!r}, expected {aw['recipient']!r}")
            if env["task_id"] != aw["task_id"] or env["attempt"] != aw["attempt"]:
                problems.append("reply task or attempt does not match the request")
            if problems:
                raise c.HandoffRejected(problems)
        except c.HandoffRejected as e:
            events.append({"type": "handoff_rejected", "seq": env.get("seq") if isinstance(env, dict) else None,
                           "problems": e.problems})
            if aw is not None:
                self._step_failed(s, aw, ["invalid handoff: " + p for p in e.problems], lim, lessons)
            return
        payload, kind = env["payload"], env["kind"]
        if kind == "agent_error":
            problems = c.validate_agent_error(payload) or [f"{payload['error_type']}: {payload['message']}"]
            events.append({"type": "agent_error", "agent": env["sender"], "seq": env["seq"], "problems": problems})
            self._step_failed(s, aw, problems, lim, lessons)
            return
        task = self._current_spec(s) if aw["task_id"] else None
        validator = {
            "plan": lambda: c.validate_plan(payload, s["profile"]),
            "approval_decision": lambda: c.validate_approval(payload, s["plan"]),
            "analysis_result": lambda: c.validate_analysis_result(payload, task),
            "verification_report": lambda: c.validate_verification_report(payload, task),
            "insight_report": lambda: c.validate_insight_report(payload, task),
            "insight_check_report": lambda: c.validate_insight_check_report(payload, task),
        }[kind]
        problems = validator()
        if not problems and kind == "analysis_result" and payload["attempt"] != aw["attempt"]:
            problems = ["analysis_result attempt does not match the request"]
        if problems:
            events.append({"type": "handoff_rejected", "seq": env["seq"], "kind": kind, "problems": problems})
            self._step_failed(s, aw, ["invalid handoff: " + p for p in problems], lim, lessons)
            return
        events.append({"type": "handoff_accepted", "seq": env["seq"], "kind": kind})
        getattr(self, "_on_" + kind)(s, payload, lim, lessons)

    # handlers for valid replies

    def _on_plan(self, s, payload, lim, lessons):
        s["plan"] = payload

    def _on_approval_decision(self, s, payload, lim, lessons):
        s["approval_note"] = payload.get("reviewer_note")
        if payload["decision"] == "reject":
            s["status"] = "rejected_by_human"
            return
        ids = [t["id"] for t in s["plan"]["tasks"]]
        if payload["decision"] == "edit":
            keep = set(payload["keep_task_ids"])
            ids = [i for i in ids if i in keep]
        s["approved_task_ids"] = ids
        s["tasks"] = {i: _new_task() for i in ids}

    def _on_analysis_result(self, s, payload, lim, lessons):
        t = s["tasks"][self._current_id(s)]
        ex = payload["execution"]
        if not ex["ok"]:
            kind = "blocked by the safety scan" if ex["blocked_by_safety_scan"] else f"failed ({ex['error_type'] or 'error'})"
            lines = [ln for ln in ex["error"].strip().splitlines() if ln.strip()]
            self._fail_analysis(s, lim, lessons, [f"execution {kind}: {lines[-1][:300] if lines else ''}"])
            return
        t["analysis"] = payload
        t["stage"] = "awaiting_verification"

    def _on_verification_report(self, s, payload, lim, lessons):
        t = s["tasks"][self._current_id(s)]
        t["verification"] = payload
        if payload["verdict"] == "accepted":
            t["verified_claims"] = {k: t["analysis"]["claims"][k] for k in sorted(payload["claims"])}
            t["stage"] = "awaiting_insight"
        else:
            self._fail_analysis(s, lim, lessons, payload["feedback"] or ["verification rejected the analysis"])

    def _on_insight_report(self, s, payload, lim, lessons):
        t = s["tasks"][self._current_id(s)]
        t["candidate_insights"] = payload["insights"]
        t["stage"] = "awaiting_insight_check"

    def _on_insight_check_report(self, s, payload, lim, lessons):
        t = s["tasks"][self._current_id(s)]
        if payload["verdict"] == "accepted":
            t["insights"] = t["candidate_insights"]
            t["status"], t["stage"] = "completed", "done"
            t["history"].append({"stage": "insight", "attempt": t["insight_attempts"], "outcome": "accepted"})
            s["task_index"] += 1
        else:
            self._fail_insight(s, lim, lessons, payload["problems"])

    # failure handling (bounded)

    def _step_failed(self, s, aw, problems, lim, lessons):
        kind = aw["kind"]
        if kind == "plan_request":
            s["plan_feedback"] = (s["plan_feedback"] + problems)[-lim["max_feedback_items"]:]
            lessons.append({"stage": "planning", "attempt": aw["attempt"], "problems": problems})
            if s["plan_attempts"] >= lim["max_plan_attempts"]:
                s["status"] = "plan_invalid"
        elif kind == "approval_request":
            s["status"] = "approval_failed"
        elif kind == "analysis_request":
            self._fail_analysis(s, lim, lessons, problems)
        elif kind == "verification_request":
            # The verifier could not produce a valid report: fail closed, accept nothing for this task.
            t = s["tasks"][self._current_id(s)]
            t["status"], t["stage"], t["failure"] = "failed", "done", "verification unavailable: " + "; ".join(problems)
            t["history"].append({"stage": "verification", "attempt": t["attempts"], "outcome": "unavailable"})
            s["task_index"] += 1
        elif kind in ("insight_request", "insight_check_request"):
            self._fail_insight(s, lim, lessons, problems)

    def _fail_analysis(self, s, lim, lessons, reasons):
        tid = self._current_id(s)
        t = s["tasks"][tid]
        t["analysis"], t["verification"] = None, None
        t["feedback"] = (t["feedback"] + [f"attempt {t['attempts']}: {r}" for r in reasons])[-lim["max_feedback_items"]:]
        t["history"].append({"stage": "analysis", "attempt": t["attempts"], "outcome": "rejected", "reasons": reasons})
        lessons.append({"stage": "analysis", "task_id": tid, "attempt": t["attempts"], "problems": reasons})
        if t["attempts"] >= 1 + lim["max_retries"]:
            t["status"], t["stage"] = "failed", "done"
            t["failure"] = f"no accepted analysis after {t['attempts']} attempt(s)"
            s["task_index"] += 1
        else:
            t["stage"] = "needs_analysis"

    def _fail_insight(self, s, lim, lessons, reasons):
        tid = self._current_id(s)
        t = s["tasks"][tid]
        t["candidate_insights"] = None
        t["insight_feedback"] = (t["insight_feedback"] + [f"attempt {t['insight_attempts']}: {r}" for r in reasons]
                                 )[-lim["max_feedback_items"]:]
        t["history"].append({"stage": "insight", "attempt": t["insight_attempts"], "outcome": "rejected",
                             "reasons": reasons})
        lessons.append({"stage": "insight", "task_id": tid, "attempt": t["insight_attempts"], "problems": reasons})
        if t["insight_attempts"] >= lim["max_insight_attempts"]:
            t["status"], t["stage"] = "insight_rejected", "done"
            t["failure"] = f"no accepted insight after {t['insight_attempts']} attempt(s); verified claims kept"
            s["task_index"] += 1
        else:
            t["stage"] = "awaiting_insight"

    # decisions

    @staticmethod
    def _current_id(s):
        return s["approved_task_ids"][s["task_index"]]

    def _current_spec(self, s):
        tid = self._current_id(s)
        return next(t for t in s["plan"]["tasks"] if t["id"] == tid)

    def _decide(self, s, lim, events):
        if s["status"]:
            return None
        if s["profile"] is None:
            try:
                s["profile"] = dsmod.profile(self.ds_loader(s["dataset_path"]))
            except dsmod.DatasetError as e:
                events.append({"type": "profile_failed", "problems": [str(e)]})
                s["status"] = "profile_failed"
                return None
        if s["steps"] >= lim["max_steps"]:
            events.append({"type": "budget_exhausted", "steps": s["steps"]})
            s["status"] = "budget_exhausted"
            return None
        if s["plan"] is None:
            if s["plan_attempts"] >= lim["max_plan_attempts"]:
                s["status"] = "plan_invalid"
                return None
            s["plan_attempts"] += 1
            return self._dispatch(s, c.PLANNING, "plan_request", {
                "goal": s["goal"], "profile": s["profile"], "attempt": s["plan_attempts"],
                "feedback": s["plan_feedback"], "max_tasks": c.MAX_TASKS}, attempt=s["plan_attempts"])
        if s["approved_task_ids"] is None:
            return self._dispatch(s, c.APPROVAL, "approval_request", {
                "plan": s["plan"], "dataset": {"file_name": s["profile"]["file_name"],
                                               "row_count": s["profile"]["row_count"]}})
        if s["task_index"] >= len(s["approved_task_ids"]):
            return None
        tid = self._current_id(s)
        t, spec = s["tasks"][tid], self._current_spec(s)
        if t["stage"] == "needs_analysis":
            t["attempts"] += 1
            t["status"] = "running"
            return self._dispatch(s, c.ANALYST, "analysis_request", {
                "task": spec, "profile": s["profile"], "dataset_path": s["dataset_path"],
                "attempt": t["attempts"], "feedback": t["feedback"]}, tid, t["attempts"])
        if t["stage"] == "awaiting_verification":
            a = t["analysis"]
            return self._dispatch(s, c.VERIFIER, "verification_request", {
                "task_id": tid, "attempt": t["attempts"], "claims_spec": spec["claims"], "submitted": a["claims"],
                "claim_errors": a["claim_errors"], "unplanned": a["unplanned"], "dataset_path": s["dataset_path"],
                "dataset_sha256": s["profile"]["dataset_sha256"]}, tid, t["attempts"])
        if t["stage"] == "awaiting_insight":
            t["insight_attempts"] += 1
            return self._dispatch(s, c.INSIGHT, "insight_request", {
                "task_id": tid, "title": spec["title"], "description": spec["description"],
                "attempt": t["insight_attempts"], "verified_claims": t["verified_claims"],
                "feedback": t["insight_feedback"]}, tid, t["insight_attempts"])
        if t["stage"] == "awaiting_insight_check":
            return self._dispatch(s, c.VERIFIER, "insight_check_request", {
                "task_id": tid, "verified_claims": t["verified_claims"], "insights": t["candidate_insights"]},
                tid, t["insight_attempts"])
        raise AssertionError(f"task {tid} in unexpected stage {t['stage']!r}")   # pragma: no cover

    # ── finalize ─────────────────────────────────────────────────────────────

    def finalize(self, state):
        tasks = state.get("tasks") or {}
        ids = state.get("approved_task_ids") or []
        status = state.get("status")
        if not status:
            status = "completed" if ids and all(tasks[i]["status"] == "completed" for i in ids) else \
                "completed_with_failures"
        accepted = {}
        for i in ids:
            accepted.update(tasks[i].get("verified_claims") or {})
        events = state.get("events") or []
        final = {
            "status": status,
            "accepted_claims": dict(sorted(accepted.items())),
            "planned_task_ids": [t["id"] for t in (state.get("plan") or {}).get("tasks", [])],
            "approved_task_ids": ids,
            "tasks": {i: {k: tasks[i][k] for k in ("status", "attempts", "insight_attempts", "failure",
                                                    "verified_claims", "insights", "history")} for i in ids},
            "insights": [dict(x, task_id=i) for i in ids for x in (tasks[i].get("insights") or [])],
            "steps": state.get("steps", 0),
            "messages": len(state.get("messages") or []),
            "handoffs_rejected": sum(1 for e in events if e["type"] == "handoff_rejected"),
            "agent_errors": sum(1 for e in events if e["type"] == "agent_error"),
            "lessons": len(state.get("lessons") or []),
            "approval_note": state.get("approval_note"),
        }
        return {"status": status, "final": final, "route": END, "pending": None}
