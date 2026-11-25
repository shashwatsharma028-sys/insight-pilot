"""
The policy engine (RuntimeAuthority): deterministic, deny-by-default, and held only by the trusted runtime.

Identity: an agent's identity is the name the trusted runtime gave its port when the team was built
(team.build_team); nothing a model writes can change it.

Integrity: the authority holds a random key in process memory only. After every graph node it tags the
integrity-relevant part of the run state (the plan, approvals, grant, tasks, the pending envelope, ...), and every
node verifies the tag before it acts. A checkpoint that was altered outside the runtime (a forged message, a
changed plan, a planted approval) fails verification and the run stops (IntegrityViolation). The key never enters
the state, so the tag cannot be recomputed by anyone who can only write the checkpoint. A process restart loses the
key: an interrupted run must be started again (durable approvals across restarts are NOT provided).

Approval: the question shown to the human carries a binding {session, run, plan SHA-256, dataset SHA-256, policy
version, one-time nonce}. A decision is accepted only if it quotes that binding exactly, and each nonce is consumed
once (under a lock), so a captured decision cannot approve another run, a changed plan, or the same plan twice.
An accepted decision becomes a GRANT: the SHA-256 of every approved task's canonical JSON.

Operation authorization: immediately before an analysis runs, the runtime checks the task against the grant (same
id, same canonical content), the dataset file against the approved SHA-256, and the policy version; it then issues a
one-task authorization token that TrustedOpsAnalyst verifies again at the computation boundary.
"""

import copy
import hashlib
import hmac
import json
import secrets
import threading
import time

from multi_agent import contracts as c
from multi_agent.security import audit
from multi_agent.security.audit import POLICY_VERSION

CAPABILITIES = {
    c.PLANNING: frozenset({"llm:planner"}),
    c.ANALYST: frozenset({"llm:data_analyst", "ops:compute"}),
    c.INSIGHT: frozenset({"llm:business_insight"}),
    c.VERIFIER: frozenset({"dataset:read"}),
    c.APPROVAL: frozenset({"approval:decide"}),
    c.SUPERVISOR: frozenset({"approval:bind", "grant:issue", "ops:authorize"}),
}

# State keys whose integrity is tagged after every node. Reducer channels (messages, events, lessons, timings,
# security_events) are append-only logs that no decision reads.
COVERED = ("dataset_path", "goal", "limits", "run_id", "profile", "plan", "plan_attempts", "plan_feedback",
           "plan_recovery", "approved_task_ids", "approval_note", "approval_binding", "grant", "task_index", "tasks",
           "pending", "awaiting", "seq", "steps", "status", "route", "tag_seq")
INITIAL = ("dataset_path", "goal", "limits", "run_id")
BINDING_FIELDS = ("session_id", "run_id", "plan_sha256", "dataset_sha256", "policy_version", "nonce")


class SecurityViolation(PermissionError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


class IntegrityViolation(SecurityViolation):
    """The run state or a message was not produced by this runtime. The run stops; nothing is retried."""


class PolicyRefused(SecurityViolation):
    """A policy refused an action (capability, outbound information, authorization)."""


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=True,
                      default=str).encode("utf-8")


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def file_sha256(path) -> tuple:
    """(bytes, sha256) of a file, read ONCE so the hash and the parsed content are the same bytes."""
    with open(path, "rb") as f:
        data = f.read()
    return data, hashlib.sha256(data).hexdigest()


class RuntimeAuthority:
    def __init__(self, session_id="local", clock=time.perf_counter, sink=None):
        self.session_id = str(session_id)
        self.clock = clock
        self.sink = sink                         # optional callable(event) for the application's audit view
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._consumed = set()
        self._spent = set()                      # state tags already consumed by a completed node (single use)
        self._buffer = []
        self.events = []
        self.decisions = 0                       # every decision point counts; each one emits exactly one event
        self.current_run = None                  # set by the Supervisor's node wrapper (event correlation only)

    # ── events ────────────────────────────────────────────────────────────────────────────────────────────

    def emit(self, decision, reason, category, t0=None, run_id=None, **kw):
        ev = audit.event(decision, reason, category=category, session_id=self.session_id,
                         run_id=run_id or self.current_run,
                         check_ms=None if t0 is None else (self.clock() - t0) * 1000.0, **kw)
        with self._lock:
            self.decisions += decision in ("allow", "deny", "redact")
            self._buffer.append(ev)
            self.events.append(ev)
        if self.sink is not None:
            try:
                self.sink(ev)
            except Exception:  # noqa: BLE001 - auditing must never change a decision
                pass
        return ev

    def drain(self) -> list:
        with self._lock:
            out, self._buffer = self._buffer, []
        return out

    # ── integrity ─────────────────────────────────────────────────────────────────────────────────────────

    def _mac(self, obj) -> str:
        return hmac.new(self._key, canonical(obj), hashlib.sha256).hexdigest()

    def state_tag(self, state) -> str:
        return self._mac({k: state.get(k) for k in COVERED})

    def verify_state(self, state, node, run_id=None):
        t0 = self.clock()
        tag = state.get("state_tag")
        if tag is None:
            extra = [k for k in COVERED if k not in INITIAL and state.get(k) not in (None, [], {}, 0)]
            if extra:
                self.emit("deny", "state_untagged", "integrity", t0, agent=node, run_id=state.get("run_id"),
                          location="state")
                raise IntegrityViolation("state_untagged", "run state was not produced by this runtime")
            return
        if not hmac.compare_digest(tag, self.state_tag(state)):
            self.emit("deny", "state_tag_mismatch", "integrity", t0, agent=node, run_id=state.get("run_id"),
                      location="state")
            raise IntegrityViolation("state_tag_mismatch", "run state was changed outside the runtime")
        with self._lock:                          # single use: a second driver of the same state is refused
            replay = tag in self._spent
            self._spent.add(tag)
        if replay:
            self.emit("deny", "state_replayed", "integrity", t0, agent=node, run_id=state.get("run_id"),
                      location="state")
            raise IntegrityViolation("state_replayed", "this run state is already being executed")
        return tag

    def release(self, tag):
        """A node that did not complete (e.g. the approval interrupt) leaves its state usable again."""
        if tag is not None:
            with self._lock:
                self._spent.discard(tag)

    # ── capabilities (deny by default) ────────────────────────────────────────────────────────────────────

    def capability(self, agent, cap, run_id=None):
        t0 = self.clock()
        if cap in CAPABILITIES.get(agent, frozenset()):
            self.emit("allow", "capability_granted", "capability", t0, agent=agent, run_id=run_id)
            return
        self.emit("deny", "capability_denied", "capability", t0, agent=agent, run_id=run_id)
        raise PolicyRefused("capability_denied", f"{agent} does not hold {cap}")

    # ── approval binding ──────────────────────────────────────────────────────────────────────────────────

    def new_binding(self, run_id, plan, dataset_sha256) -> dict:
        b = {"session_id": self.session_id, "run_id": str(run_id), "plan_sha256": digest(plan),
             "dataset_sha256": dataset_sha256, "policy_version": POLICY_VERSION, "nonce": secrets.token_hex(16)}
        self.emit("allow", "approval_binding_issued", "approval", agent=c.SUPERVISOR, run_id=run_id,
                  location="approval")
        return b

    def accept_decision(self, expected, quoted, plan, run_id):
        """Returns None if the quoted binding is exactly the expected one (and consumes its nonce), else a reason.
        Raises IntegrityViolation for a replay of an already-consumed nonce."""
        t0 = self.clock()
        reason = None
        if not isinstance(expected, dict):
            reason = "no_binding_issued"
        elif not isinstance(quoted, dict) or set(quoted) != set(BINDING_FIELDS):
            reason = "binding_missing_or_malformed"
        else:
            for f in BINDING_FIELDS:
                if not isinstance(quoted[f], str) or not hmac.compare_digest(quoted[f], str(expected[f])):
                    reason = f"binding_{f}_mismatch"
                    break
            if reason is None and digest(plan) != expected["plan_sha256"]:
                reason = "plan_changed_since_question"
        if reason is not None:
            self.emit("deny", reason, "approval", t0, agent=c.APPROVAL, run_id=run_id, location="approval")
            return reason
        with self._lock:
            replay = expected["nonce"] in self._consumed
            self._consumed.add(expected["nonce"])
        if replay:
            self.emit("deny", "approval_replayed", "approval", t0, agent=c.APPROVAL, run_id=run_id, location="approval")
            raise IntegrityViolation("approval_replayed", "this approval has already been used")
        self.emit("allow", "approval_bound", "approval", t0, agent=c.APPROVAL, run_id=run_id, location="approval")
        return None

    def grant(self, binding, plan, ids) -> dict:
        tasks = {t["id"]: digest(t) for t in plan["tasks"] if t["id"] in set(ids)}
        g = {k: binding[k] for k in ("session_id", "run_id", "dataset_sha256", "policy_version", "nonce")}
        g["tasks"] = tasks
        self.emit("allow", "grant_issued", "authorization", agent=c.SUPERVISOR, run_id=binding["run_id"],
                  count=len(tasks))
        return g

    # ── operation authorization ───────────────────────────────────────────────────────────────────────────

    def authorize_analysis(self, state, env) -> dict:
        """Check the analysis request against the grant and the dataset bytes; return a one-task token."""
        t0 = self.clock()
        run_id = state.get("run_id")
        g, p = state.get("grant"), env.get("payload") or {}
        task = p.get("task")
        tid = env.get("task_id")

        def deny(code):
            self.emit("deny", code, "authorization", t0, agent=c.ANALYST, run_id=run_id, task_id=tid,
                      location="operation")
            raise PolicyRefused(code, f"analysis not authorized: {code}")
        if not isinstance(g, dict) or not isinstance(task, dict):
            deny("no_grant")
        if g.get("policy_version") != POLICY_VERSION:
            deny("policy_version_changed")
        if g.get("session_id") != self.session_id or g.get("run_id") != run_id:
            deny("grant_scope_mismatch")
        if tid not in g["tasks"] or task.get("id") != tid or digest(task) != g["tasks"][tid]:
            deny("task_not_approved")
        try:
            _, sha = file_sha256(p.get("dataset_path"))
        except (OSError, TypeError):
            deny("dataset_unreadable")
        if sha != g["dataset_sha256"]:
            deny("dataset_changed_since_approval")
        token = {"session_id": self.session_id, "run_id": run_id, "task_id": tid, "task_sha256": g["tasks"][tid],
                 "dataset_sha256": sha, "policy_version": POLICY_VERSION, "attempt": env.get("attempt")}
        token["mac"] = self._mac(token)
        self.emit("allow", "analysis_authorized", "authorization", t0, agent=c.ANALYST, run_id=run_id, task_id=tid,
                  location="operation")
        return token

    def verify_token(self, token, task, dataset_sha256) -> None:
        """Re-checked by TrustedOpsAnalyst at the computation boundary (dataset bytes already read once)."""
        t0 = self.clock()
        ok = isinstance(token, dict) and isinstance(token.get("mac"), str)
        if ok:
            body = {k: v for k, v in token.items() if k != "mac"}
            ok = (hmac.compare_digest(token["mac"], self._mac(body)) and token.get("task_id") == task.get("id")
                  and token.get("task_sha256") == digest(task) and token.get("dataset_sha256") == dataset_sha256
                  and token.get("policy_version") == POLICY_VERSION and token.get("session_id") == self.session_id)
        tid = task.get("id") if isinstance(task, dict) else None
        if not ok:
            self.emit("deny", "operation_token_invalid", "authorization", t0, agent=c.ANALYST,
                      run_id=(token or {}).get("run_id") if isinstance(token, dict) else None, task_id=tid,
                      location="operation")
            raise PolicyRefused("operation_token_invalid", "operation is not authorized for this task and dataset")
        self.emit("allow", "operation_token_valid", "authorization", t0, agent=c.ANALYST, run_id=token.get("run_id"),
                  task_id=tid, location="operation")


def without_binding(decision):
    d = copy.deepcopy(decision) if isinstance(decision, dict) else {"decision": decision}
    quoted = d.pop("binding", None)
    return d, quoted
