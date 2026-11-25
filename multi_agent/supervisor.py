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
plan_invalid, approval_failed, budget_exhausted, profile_failed, and (P5.1A)
isolation_unavailable and execution_refused.

P5.1A execution-failure policy:
- an infrastructure failure (isolation unavailable, protocol violation, the
  V1 tripwire, an unexpected port error) or a policy refusal (dataset not
  authorised, untrusted code on a disallowed port) reaches the Supervisor as
  an execution_terminated handoff, built by the trusted node wrapper from the
  exception class. The run terminates at once: no retry, no further task, no
  insight call, no fallback executor;
- only a COMPLETE run (completed, completed_with_failures) promotes verified
  claims and insights to final findings. Any other termination keeps the
  claims already verified by independent recomputation under
  verified_claims_for_review, with accepted_claims empty and run_complete False.

P6 selective task recovery (opt-in: limits["selective_plan_recovery"] = True; off by default, and when off
nothing below runs and the run state has no plan_recovery key):
- a plan reply with a valid structure is triaged task by task (contracts.triage_plan: strict claim validation
  unchanged; duplicate ids/keys and the task limit checked against the merged plan). Valid tasks are kept;
  invalid tasks are sent back, alone, with their exact validation problems, through the same planning prompt
  and within the same max_plan_attempts;
- a task that cannot be repaired is reported as abandoned (final["plan_recovery"], the approval request and a
  completed_with_failures status); it is never silently dropped;
- the merged plan is re-validated as a whole before the human approval gate; verification is unchanged.

P6 schema-aware planner (opt-in: limits["schema_aware_planner"] = True; off by default, and when off the plan
request and planning prompt are byte-identical to before): every plan request, including selective-recovery repair
requests, asks the Planning Agent's prompt to include the claim-argument schema (prompts.op_schema_section). Plan
validation is unchanged: nothing is coerced.

P6 semantic planner guidance (opt-in, independent of the schema flag: limits["semantic_planner"] = True): every
plan request also asks for the op-result and grouping semantics (prompts.op_semantics_section). Off by default;
validation, execution and verification are unchanged.

This graph is separate from agent/graph.py (the V1 graph), which is unchanged.
"""

import copy
import json
import operator
import time
import uuid
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

try:
    from langgraph.types import Command, interrupt
except ImportError as exc:  # pragma: no cover - depends on the installed LangGraph
    raise ImportError("multi_agent needs the V2 environment (requirements-v2.txt): this LangGraph has no "
                      "langgraph.types.interrupt/Command") from exc

from multi_agent import contracts as c
from multi_agent import dataset as dsmod
from multi_agent.execution import EXECUTION_REFUSED, ISOLATION_UNAVAILABLE, classify_execution_failure
from multi_agent.security import outbound as sec_out
from multi_agent.security import policy as sec
from multi_agent.security.audit import POLICY_VERSION
from multi_agent.verification import values_match

DEFAULT_GOAL = "Analyse this dataset and report the most important verified findings."
DEFAULT_LIMITS = {"max_retries": 2, "max_plan_attempts": 2, "max_insight_attempts": 2, "max_steps": 200,
                  "max_feedback_items": 12}
COMPLETE_STATUSES = ("completed", "completed_with_failures")
TERMINAL_STATUSES = COMPLETE_STATUSES + ("rejected_by_human", "plan_invalid", "approval_failed",
                                         "budget_exhausted", "profile_failed",
                                         "isolation_unavailable", "execution_refused",
                                         "policy_refused")          # SEC: the outbound or capability policy refused
FINALIZE = "finalize"


class SupervisorState(TypedDict, total=False):
    dataset_path: str
    goal: str
    limits: dict
    profile: Optional[dict]
    plan: Optional[dict]
    plan_attempts: int
    plan_feedback: list
    plan_recovery: Optional[dict]
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
    run_id: str                                   # SEC: run identity (the thread id for interactive runs)
    approval_binding: Optional[dict]
    grant: Optional[dict]
    state_tag: Optional[str]
    tag_seq: int
    security_events: Annotated[list, operator.add]


_SCALARS = ("profile", "plan", "plan_attempts", "plan_feedback", "approved_task_ids", "approval_note",
            "task_index", "tasks", "awaiting", "seq", "steps", "status", "approval_binding", "grant")
_DEFAULTS = {"plan_attempts": 0, "plan_feedback": [], "task_index": 0, "tasks": {}, "seq": 0, "steps": 0}


def _new_task():
    return {"status": "pending", "stage": "needs_analysis", "attempts": 0, "insight_attempts": 0,
            "feedback": [], "insight_feedback": [], "analysis": None, "verification": None,
            "verified_claims": None, "candidate_insights": None, "insights": None, "failure": None,
            "history": [], "insight_view": None}


class Supervisor:
    def __init__(self, planning, analyst, verifier, insight, approval_port=None,
                 clock=time.perf_counter, ds_loader=dsmod.load, authority=None):
        self.planning, self.analyst, self.verifier, self.insight = planning, analyst, verifier, insight
        self.approval_port = approval_port
        self.clock, self.ds_loader = clock, ds_loader
        self.authority = authority or sec.RuntimeAuthority(clock=clock)     # SEC: the trusted runtime's policy engine

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
    def initial_state(dataset_path, goal=DEFAULT_GOAL, limits=None, run_id=None):
        return {"dataset_path": dataset_path, "goal": goal, "limits": {**DEFAULT_LIMITS, **(limits or {})},
                "run_id": str(run_id) if run_id is not None else uuid.uuid4().hex}

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
        from multi_agent.compat import in_memory_saver
        graph = self.build(checkpointer=in_memory_saver())
        cfg = self.run_config(limits, thread_id)
        return graph, cfg, graph.invoke(self.initial_state(dataset_path, goal, limits, run_id=thread_id), cfg)

    @staticmethod
    def pending_question(graph, cfg):
        """The approval question currently waiting in this thread (with its binding), or None."""
        for t in graph.get_state(cfg).tasks or ():
            for intr in getattr(t, "interrupts", ()) or ():
                return intr.value
        return None

    @staticmethod
    def resume(graph, cfg, decision):
        """Resume THIS thread with a human decision. A dict decision without a binding is bound to the question
        currently waiting in this thread (the caller names the thread, so it can only approve what it was shown
        there); a decision that quotes a binding must match it exactly. A raw Command(resume=...) without the
        binding is refused by the approval gate."""
        if isinstance(decision, dict) and "binding" not in decision:
            q = Supervisor.pending_question(graph, cfg)
            if isinstance(q, dict) and isinstance(q.get("binding"), dict):
                decision = dict(decision, binding=copy.deepcopy(q["binding"]))
        return graph.invoke(Command(resume=decision), cfg)

    def _timed(self, name, fn):
        def node(state):
            t0 = self.clock()
            self.authority.current_run = state.get("run_id")
            tag = self.authority.verify_state(state, name)      # SEC: refuse state not produced by this runtime
            try:
                out = dict(fn(state))
            except BaseException:                               # interrupted or failed: the state stays usable
                self.authority.release(tag)
                raise
            out["tag_seq"] = (state.get("tag_seq") or 0) + 1
            out["state_tag"] = self.authority.state_tag({**state, **out})
            ev = self.authority.drain()
            if ev:
                out["security_events"] = ev
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
            if me == c.ANALYST:                  # SEC: authorize the operation at the boundary, immediately before it
                try:
                    token = self.authority.authorize_analysis(state, env)
                except sec.PolicyRefused as e:   # a policy refusal ends the run (P5.1A), never retried or rerouted
                    return self._reply(state, env, me, "execution_terminated", {
                        "category": EXECUTION_REFUSED, "error_class": "PolicyRefused", "message": e.code})
                env = dict(env, payload=dict(env["payload"], authorization=token))
            try:
                payload, kind = call(env), c.REPLY_KIND[env["kind"]]
            except sec.IntegrityViolation:
                raise
            except sec.PolicyRefused as e:                           # outbound / capability policy: no retry
                payload, kind = {"error_type": "PolicyRefused", "message": e.code}, "agent_error"
            except Exception as e:                                   # agent failure -> typed error handoff
                terminal = classify_execution_failure(e) if me == c.ANALYST else None
                if terminal is not None:                             # infrastructure or policy: ends the run
                    payload, kind = {"category": terminal[0], "error_class": terminal[1],
                                     "message": str(e)[:500]}, "execution_terminated"
                else:
                    payload, kind = {"error_type": type(e).__name__, "message": str(e)[:2000]}, "agent_error"
            return self._reply(state, env, me, kind, payload)
        return node

    def approval_node(self, state, interactive):
        env = state.get("pending")
        c.check_envelope(env, recipient=c.APPROVAL, kinds={"approval_request"})
        expected = state.get("approval_binding")
        if self.approval_port is not None:
            decision = self.approval_port(copy.deepcopy(env["payload"]))
            if not isinstance(decision, dict):
                decision = {"decision": decision}
            if "binding" not in decision:          # an in-process approver answers exactly the question it was given
                decision = dict(decision, binding=copy.deepcopy(expected))
        elif interactive:
            question = {"kind": "plan_approval", "plan": env["payload"]["plan"]}
            if "abandoned_tasks" in env["payload"]:                          # P6 selective recovery only
                question["abandoned_tasks"] = env["payload"]["abandoned_tasks"]
            question["binding"] = copy.deepcopy(expected)                    # SEC: the decision must quote it
            decision = interrupt(question)
        else:
            return self._reply(state, env, c.APPROVAL, "agent_error",
                               {"error_type": "NoApprover", "message": "no human approver configured; failing closed"})
        if not isinstance(decision, dict):
            decision = {"decision": decision}
        decision, quoted = sec.without_binding(decision)
        reason = self.authority.accept_decision(expected, quoted, state.get("plan"), state.get("run_id"))
        if reason is not None:                   # wrong run, session, plan, dataset, policy or nonce: fail closed
            return self._reply(state, env, c.APPROVAL, "agent_error",
                               {"error_type": "ApprovalBindingRejected", "message": reason})
        return self._reply(state, env, c.APPROVAL, "approval_decision", decision)

    # ── supervisor ───────────────────────────────────────────────────────────

    def supervise(self, state):
        lim = {**DEFAULT_LIMITS, **(state.get("limits") or {})}
        s = {k: copy.deepcopy(state[k]) if state.get(k) is not None else copy.deepcopy(_DEFAULTS.get(k))
             for k in _SCALARS}
        s["dataset_path"], s["goal"] = state["dataset_path"], state.get("goal", DEFAULT_GOAL)
        s["run_id"] = state.get("run_id")
        s["plan_recovery"] = copy.deepcopy(state.get("plan_recovery"))          # P6, opt-in; None when unused
        events, lessons = [], []
        env = state.get("pending")
        if env is not None and env.get("recipient") == c.SUPERVISOR:
            self._absorb(s, env, lim, events, lessons)
        request = self._decide(s, lim, events)
        out = {k: s[k] for k in _SCALARS}
        if s["plan_recovery"] is not None:
            out["plan_recovery"] = s["plan_recovery"]
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
            kinds = {c.REPLY_KIND[aw["kind"]], "agent_error"}
            if aw["kind"] == "analysis_request":
                kinds.add("execution_terminated")
            c.check_envelope(env, recipient=c.SUPERVISOR, kinds=kinds, reply_to=aw["seq"])
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
        if kind == "plan":                               # SEC: signals on every planner reply, valid or not
            for t in payload.get("tasks") or [] if isinstance(payload.get("tasks"), list) else []:
                if isinstance(t, dict):
                    sec_out.scan(self.authority, f"{t.get('title', '')} {t.get('description', '')}", "model_output",
                                 task_id=t.get("id") if isinstance(t.get("id"), str) else None)
        if kind == "execution_terminated":
            problems = c.validate_execution_terminated(payload)
            category = payload.get("category") if not problems else ISOLATION_UNAVAILABLE   # malformed: fail closed
            events.append({"type": "execution_terminated", "seq": env["seq"], "category": category,
                           "error_class": payload.get("error_class") if not problems else None,
                           "problems": problems})
            self._terminate_execution(s, category, payload.get("error_class") if not problems else "malformed")
            return
        if kind == "agent_error":
            invalid = c.validate_agent_error(payload)
            problems = invalid or [f"{payload['error_type']}: {payload['message']}"]
            events.append({"type": "agent_error", "agent": env["sender"], "seq": env["seq"], "problems": problems})
            if not invalid and payload["error_type"] == "PolicyRefused":     # SEC: deterministic refusal, no retry
                s["status"] = "policy_refused"
                return
            self._step_failed(s, aw, problems, lim, lessons)
            return
        if kind == "plan" and lim.get("selective_plan_recovery"):
            self._absorb_plan_selective(s, env, payload, lim, events, lessons)
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
        s["grant"] = self.authority.grant(s["approval_binding"], s["plan"], ids)     # SEC: what may now run

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
        if payload["verdict"] == "accepted" and not self._report_consistent(payload, t["analysis"]):
            self.authority.emit("deny", "verification_inconsistent", "integrity", task_id=self._current_id(s))
            self._fail_analysis(s, lim, lessons, ["verification report is inconsistent with the analysis"])
            return
        if payload["verdict"] == "accepted":
            t["verified_claims"] = {k: t["analysis"]["claims"][k] for k in sorted(payload["claims"])}
            t["stage"] = "awaiting_insight"
        else:
            self._fail_analysis(s, lim, lessons, payload["feedback"] or ["verification rejected the analysis"])

    @staticmethod
    def _report_consistent(report, analysis):
        """SEC defence in depth: an accepted report must show, for every claim, a recomputed value that matches the
        reported one, and the reported value must be the analyst's own."""
        for k, e in report["claims"].items():
            if "recomputed" not in e or k not in (analysis or {}).get("claims", {}):
                return False
            if not (values_match(e["reported"], e["recomputed"]) and values_match(analysis["claims"][k], e["reported"])):
                return False
        return True

    def _on_insight_report(self, s, payload, lim, lessons):
        t = s["tasks"][self._current_id(s)]
        unsafe = sec_out.unsafe_output(self.authority, payload["insights"], t.get("insight_view") or {},
                                       task_id=self._current_id(s))
        if unsafe:                                       # SEC output policy: links, markup, identifiers, controls
            self._fail_insight(s, lim, lessons, unsafe)
            return
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

    # P6 selective task recovery (opt-in)

    @staticmethod
    def _slot_id(i, t, taken):
        tid = t.get("id") if isinstance(t, dict) else None
        if not (isinstance(tid, str) and c.TASK_ID.match(tid)) or tid in taken:
            tid = f"repair_task_{i}"
            n = 0
            while tid in taken:
                n += 1
                tid = f"repair_task_{i}_{n}"
        return tid

    def _absorb_plan_selective(self, s, env, payload, lim, events, lessons):
        rec = s["plan_recovery"]
        repairing = rec is not None and bool(rec["slots"])
        kept = rec["kept"] if repairing else []
        allowed = {sl["id"] for sl in rec["slots"]} if repairing else None
        required = {sl["id"]: self._claim_keys(sl["original"]) for sl in rec["slots"]} if repairing else None
        structural, accepted, rejected = c.triage_plan(payload, s["profile"], kept=kept, allowed_ids=allowed,
                                                       required_keys=required)
        if structural:                                   # the reply as a whole is unusable: original handling
            events.append({"type": "handoff_rejected", "seq": env["seq"], "kind": "plan", "problems": structural})
            self._step_failed(s, {"kind": "plan_request", "attempt": env["attempt"]},
                              ["invalid handoff: " + p for p in structural], lim, lessons)
            return
        task_problems = [p for _, _, ps in rejected for p in ps]
        if rejected:
            events.append({"type": "handoff_rejected", "seq": env["seq"], "kind": "plan", "problems": task_problems,
                           "partial": True, "accepted_task_ids": [t["id"] for t in accepted]})
            lessons.append({"stage": "planning", "attempt": env["attempt"],
                            "problems": ["invalid handoff: " + p for p in task_problems]})
        else:
            events.append({"type": "handoff_accepted", "seq": env["seq"], "kind": "plan"})
        if not repairing:
            if not rejected:                             # fully valid first plan: exactly the original outcome
                if rec is not None:
                    rec["kept"] = list(accepted)
                s["plan"] = payload
                return
            taken = {t["id"] for t in accepted}
            slots = []
            for i, t, ps in rejected:
                sid = self._slot_id(i, t, taken)
                taken.add(sid)
                slots.append({"id": sid, "original": t, "problems": ps})
            s["plan_recovery"] = rec = {"kept": list(accepted), "slots": slots, "recovered": [], "abandoned": [],
                                        "triaged_at_attempt": env["attempt"]}
        else:
            fixed = {t["id"] for t in accepted}
            rec["kept"] += accepted
            rec["recovered"] += [t["id"] for t in accepted]
            by_id = {}
            for i, t, ps in rejected:
                if isinstance(t, dict) and t.get("id") in allowed:
                    by_id[t["id"]] = ps
            remaining = []
            for sl in rec["slots"]:
                if sl["id"] in fixed:
                    continue
                sl["problems"] = by_id.get(sl["id"], [f"task {sl['id']!r} was not returned by repair attempt "
                                                      f"{env['attempt']}"])
                remaining.append(sl)
            rec["slots"] = remaining
        if not rec["slots"] or s["plan_attempts"] >= lim["max_plan_attempts"]:
            self._settle_recovery(s, lim)
        else:
            s["plan_feedback"] = self._repair_feedback(rec, lim)

    @staticmethod
    def _claim_keys(task):
        """String claim keys of a (possibly invalid) original task: the questions its repair must keep."""
        cl = task.get("claims") if isinstance(task, dict) else None
        return [x["key"] for x in cl if isinstance(x, dict) and isinstance(x.get("key"), str)] if isinstance(cl, list) else []

    @staticmethod
    def _repair_feedback(rec, lim):
        head = (f"REPAIR ONLY: tasks {[t['id'] for t in rec['kept']]} were valid and are kept unchanged. Reply with "
                f'{{"tasks": [...]}} containing ONLY corrected versions of the {len(rec["slots"])} invalid task(s) '
                f"below, each keeping the id shown.")
        items = [head] + [f"invalid task id {sl['id']!r}: {json.dumps(sl['original'], sort_keys=True, default=str)[:1500]}"
                          f" -- problems: {'; '.join(sl['problems'])[:1500]}" for sl in rec["slots"]]
        return items[:lim["max_feedback_items"]]

    def _settle_recovery(self, s, lim):
        """No repair attempts left (or nothing left to repair): proceed with the kept tasks, report the rest."""
        rec = s["plan_recovery"]
        for sl in rec["slots"]:
            o = sl["original"] if isinstance(sl["original"], dict) else {}
            cl = o.get("claims") if isinstance(o.get("claims"), list) else []
            rec["abandoned"].append({"id": sl["id"], "title": str(o.get("title"))[:120] if "title" in o else None,
                                     "claim_keys": [str(x.get("key"))[:64] for x in cl if isinstance(x, dict)],
                                     "problems": [str(p)[:300] for p in sl["problems"]][:lim["max_feedback_items"]]})
        rec["slots"] = []
        plan = {"tasks": list(rec["kept"])}
        if rec["kept"] and not c.validate_plan(plan, s["profile"]):          # whole-plan validation, again
            s["plan"] = plan
        else:
            s["status"] = "plan_invalid"

    # failure handling (bounded)

    def _terminate_execution(self, s, category, error_class):
        """Infrastructure or policy failure: stop the whole run now. Never retried."""
        assert category in (ISOLATION_UNAVAILABLE, EXECUTION_REFUSED)
        tid = self._current_id(s)
        t = s["tasks"][tid]
        t["analysis"], t["verification"], t["candidate_insights"] = None, None, None
        t["status"], t["stage"] = "aborted", "done"
        t["failure"] = f"run terminated: {category} ({error_class})"
        t["history"].append({"stage": "analysis", "attempt": t["attempts"], "outcome": category})
        s["status"] = category

    def _step_failed(self, s, aw, problems, lim, lessons):
        kind = aw["kind"]
        if kind == "plan_request":
            rec = s["plan_recovery"] if lim.get("selective_plan_recovery") else None
            if rec is not None and rec["slots"]:
                # a repair attempt failed as a whole (e.g. a provider error): the kept tasks stay kept
                lessons.append({"stage": "planning", "attempt": aw["attempt"], "problems": problems})
                for sl in rec["slots"]:
                    sl["problems"] = (sl["problems"] + [f"repair attempt {aw['attempt']} failed: {p}"
                                                        for p in problems])[-lim["max_feedback_items"]:]
                if s["plan_attempts"] >= lim["max_plan_attempts"]:
                    self._settle_recovery(s, lim)
                else:
                    s["plan_feedback"] = self._repair_feedback(rec, lim)
                return
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
            sec_out.scan_inputs(self.authority, s["goal"], s["profile"])          # SEC: signals only
        if s["steps"] >= lim["max_steps"]:
            events.append({"type": "budget_exhausted", "steps": s["steps"]})
            s["status"] = "budget_exhausted"
            return None
        if s["plan"] is None:
            if s["plan_attempts"] >= lim["max_plan_attempts"]:
                s["status"] = "plan_invalid"
                return None
            s["plan_attempts"] += 1
            rec = s["plan_recovery"] if lim.get("selective_plan_recovery") else None
            max_tasks = len(rec["slots"]) if rec is not None and rec["slots"] else c.MAX_TASKS
            for f in s["plan_feedback"]:
                sec_out.scan(self.authority, f, "feedback")
            request = {"goal": s["goal"], "profile": sec_out.planner_profile(self.authority, s["profile"]),
                       "attempt": s["plan_attempts"], "feedback": s["plan_feedback"], "max_tasks": max_tasks}
            if lim.get("schema_aware_planner"):          # P6 opt-in: argument-representation guidance in the prompt
                request["op_schema"] = True
            if lim.get("semantic_planner"):              # P6 opt-in, independent: op-result / grouping semantics
                request["op_semantics"] = True
            return self._dispatch(s, c.PLANNING, "plan_request", request, attempt=s["plan_attempts"])
        if s["approved_task_ids"] is None:
            if s["approval_binding"] is None:
                s["approval_binding"] = self.authority.new_binding(s["run_id"], s["plan"],
                                                                   s["profile"]["dataset_sha256"])
            payload = {"plan": s["plan"], "dataset": {"file_name": s["profile"]["file_name"],
                                                      "row_count": s["profile"]["row_count"]}}
            if s["plan_recovery"] is not None and s["plan_recovery"]["abandoned"]:
                payload["abandoned_tasks"] = s["plan_recovery"]["abandoned"]     # the human sees what was lost
            return self._dispatch(s, c.APPROVAL, "approval_request", payload)
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
            if t.get("insight_view") is None:           # SEC: what may leave the application (local data unchanged)
                t["insight_view"], _ = sec_out.insight_view(self.authority, t["verified_claims"], spec["claims"],
                                                            lim.get("sensitive_columns"), task_id=tid)
            if not t["insight_view"]:                   # every result is local-only: no provider call for this task
                t["insights"], t["status"], t["stage"] = [], "completed", "done"
                t["history"].append({"stage": "insight", "attempt": 0, "outcome": "local_only"})
                s["task_index"] += 1
                return self._decide(s, lim, events)
            t["insight_attempts"] += 1
            return self._dispatch(s, c.INSIGHT, "insight_request", {
                "task_id": tid, "title": sec_out.model_prose(self.authority, spec["title"], tid),
                "description": sec_out.model_prose(self.authority, spec["description"], tid),
                "attempt": t["insight_attempts"], "verified_claims": t["insight_view"],
                "feedback": t["insight_feedback"]}, tid, t["insight_attempts"])
        if t["stage"] == "awaiting_insight_check":
            return self._dispatch(s, c.VERIFIER, "insight_check_request", {
                "task_id": tid, "verified_claims": t.get("insight_view") or t["verified_claims"],
                "insights": t["candidate_insights"]}, tid, t["insight_attempts"])
        raise AssertionError(f"task {tid} in unexpected stage {t['stage']!r}")   # pragma: no cover

    # ── finalize ─────────────────────────────────────────────────────────────

    def finalize(self, state):
        tasks = state.get("tasks") or {}
        ids = state.get("approved_task_ids") or []
        status = state.get("status")
        rec = state.get("plan_recovery")
        abandoned = (rec or {}).get("abandoned") or []
        if not status:
            status = "completed" if ids and not abandoned and all(tasks[i]["status"] == "completed" for i in ids) \
                else "completed_with_failures"
        verified = {}
        for i in ids:
            if tasks[i]["status"] != "aborted":
                verified.update(tasks[i].get("verified_claims") or {})
        complete = status in COMPLETE_STATUSES
        insights = [dict(x, task_id=i) for i in ids for x in (tasks[i].get("insights") or [])]
        events = state.get("events") or []
        final = {
            "status": status,
            "run_complete": complete,
            # final findings only from a complete run; otherwise verified claims are kept for review only
            "accepted_claims": dict(sorted(verified.items())) if complete else {},
            "verified_claims_for_review": {} if complete else dict(sorted(verified.items())),
            "planned_task_ids": [t["id"] for t in (state.get("plan") or {}).get("tasks", [])],
            "approved_task_ids": ids,
            "tasks": {i: {k: tasks[i][k] for k in ("status", "attempts", "insight_attempts", "failure",
                                                    "verified_claims", "insights", "history")} for i in ids},
            "insights": insights if complete else [],
            "insights_for_review": [] if complete else insights,
            "steps": state.get("steps", 0),
            "messages": len(state.get("messages") or []),
            "handoffs_rejected": sum(1 for e in events if e["type"] == "handoff_rejected"),
            "agent_errors": sum(1 for e in events if e["type"] == "agent_error"),
            "execution_terminations": sum(1 for e in events if e["type"] == "execution_terminated"),
            "lessons": len(state.get("lessons") or []),
            "approval_note": state.get("approval_note"),
            "policy_version": POLICY_VERSION,
            "security": _security_summary(state.get("security_events") or []),
        }
        if rec is not None:                              # P6 selective recovery only: what was kept, fixed, lost
            final["plan_recovery"] = {"kept_task_ids": [t["id"] for t in rec.get("kept") or []],
                                      "recovered_task_ids": list(rec.get("recovered") or []),
                                      "abandoned_tasks": abandoned,
                                      "triaged_at_attempt": rec.get("triaged_at_attempt")}
        return {"status": status, "final": final, "route": END, "pending": None}


def _security_summary(events):
    """Counts only (no payloads): decisions, refusal reason codes and signal locations of this run."""
    out = {"events": len(events), "allow": 0, "deny": 0, "redact": 0, "signal": 0, "denied": {}, "signals": {}}
    for e in events:
        out[e["decision"]] = out.get(e["decision"], 0) + 1
        if e["decision"] == "deny":
            out["denied"][e["reason_code"]] = out["denied"].get(e["reason_code"], 0) + 1
        if e["decision"] == "signal":
            out["signals"][e["location"]] = out["signals"].get(e["location"], 0) + 1
    return out
