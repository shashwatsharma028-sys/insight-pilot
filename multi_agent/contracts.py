"""
Typed handoff contracts between the Supervisor and the agents.

Every message is an envelope (a plain, JSON-serialisable dict, so LangGraph
checkpoints can store it):
    {"seq": int, "reply_to": int | None, "sender": str, "recipient": str,
     "kind": str, "task_id": str | None, "attempt": int, "payload": dict}

- ROUTES lists every allowed (sender, recipient, kind). Anything else is rejected.
- Each kind has a payload validator. A validator raises HandoffRejected with
  the list of problems; it never repairs a payload.
- Agents only talk to the Supervisor, never to each other directly, so every
  handoff passes through validation.
"""

import json
import re

from multi_agent import claims as claim_vocab

SUPERVISOR = "supervisor"
PLANNING = "planning_agent"
APPROVAL = "approval_gate"
ANALYST = "data_analyst_agent"
VERIFIER = "verification_agent"
INSIGHT = "business_insight_agent"
AGENTS = (PLANNING, APPROVAL, ANALYST, VERIFIER, INSIGHT)

MAX_TASKS = 8
MAX_CLAIMS_PER_TASK = 12
MAX_STDOUT_CHARS = 20_000
MAX_CODE_CHARS = 40_000
MAX_INSIGHTS = 8
TASK_ID = re.compile(r"^[a-z0-9_]{1,40}$")
ANALYSIS_TYPES = ("descriptive", "comparative", "correlation", "outlier", "trend", "forecast", "custom")

ROUTES = {
    (SUPERVISOR, PLANNING, "plan_request"), (PLANNING, SUPERVISOR, "plan"),
    (SUPERVISOR, APPROVAL, "approval_request"), (APPROVAL, SUPERVISOR, "approval_decision"),
    (SUPERVISOR, ANALYST, "analysis_request"), (ANALYST, SUPERVISOR, "analysis_result"),
    (SUPERVISOR, VERIFIER, "verification_request"), (VERIFIER, SUPERVISOR, "verification_report"),
    (SUPERVISOR, INSIGHT, "insight_request"), (INSIGHT, SUPERVISOR, "insight_report"),
    (SUPERVISOR, VERIFIER, "insight_check_request"), (VERIFIER, SUPERVISOR, "insight_check_report"),
} | {(a, SUPERVISOR, "agent_error") for a in AGENTS} | {
    # P5.1A: an infrastructure or policy failure of the execution port; terminates the run.
    (ANALYST, SUPERVISOR, "execution_terminated"),
}

REPLY_KIND = {"plan_request": "plan", "approval_request": "approval_decision",
              "analysis_request": "analysis_result", "verification_request": "verification_report",
              "insight_request": "insight_report", "insight_check_request": "insight_check_report"}


class HandoffRejected(ValueError):
    def __init__(self, problems):
        self.problems = list(problems) if isinstance(problems, (list, tuple)) else [str(problems)]
        super().__init__("; ".join(self.problems))


# ── helpers ──────────────────────────────────────────────────────────────────

def _require(problems, cond, msg):
    if not cond:
        problems.append(msg)
    return cond


def _fields(problems, obj, name, required, optional=()):
    if not _require(problems, isinstance(obj, dict), f"{name} must be an object"):
        return False
    missing = [f for f in required if f not in obj]
    extra = sorted(set(obj) - set(required) - set(optional))
    _require(problems, not missing, f"{name} is missing {missing}")
    _require(problems, not extra, f"{name} has unknown fields {extra}")
    return not missing


def _str(problems, v, name, max_len=10_000, empty=False):
    return _require(problems, isinstance(v, str) and (empty or v.strip()) and len(v) <= max_len,
                    f"{name} must be a {'' if empty else 'non-empty '}string of at most {max_len} characters")


def _json_value(v) -> bool:
    try:
        json.dumps(v, allow_nan=True)
        return True
    except (TypeError, ValueError):
        return False


def json_size(obj) -> int:
    return len(json.dumps(obj, sort_keys=True, default=str))


# ── payload validators ───────────────────────────────────────────────────────

def validate_profile(p):
    problems = []
    if _fields(problems, p, "profile", ("file_name", "dataset_sha256", "row_count", "columns")):
        _require(problems, isinstance(p["row_count"], int) and p["row_count"] >= 0, "row_count must be >= 0")
        _require(problems, isinstance(p["columns"], list) and p["columns"], "columns must be a non-empty list")
        for c in p["columns"] if isinstance(p["columns"], list) else []:
            _fields(problems, c, "column", ("name", "kind", "missing"))
    return problems


def validate_plan(payload, profile):
    """A plan must be fully checkable before a human is asked to approve it."""
    problems = []
    if not _fields(problems, payload, "plan", ("tasks",)):
        return problems
    tasks = payload["tasks"]
    if not _require(problems, isinstance(tasks, list) and 1 <= len(tasks) <= MAX_TASKS,
                    f"plan must have 1 to {MAX_TASKS} tasks"):
        return problems
    ids, keys = set(), set()
    for i, t in enumerate(tasks):
        if not _fields(problems, t, f"task {i}", ("id", "title", "description", "analysis_type", "claims")):
            continue
        _require(problems, isinstance(t["id"], str) and TASK_ID.match(t["id"]), f"task {i}: invalid id {t['id']!r}")
        _require(problems, t["id"] not in ids, f"task {i}: duplicate id {t['id']!r}")
        ids.add(t["id"])
        _str(problems, t["title"], f"task {i} title", 120)
        _str(problems, t["description"], f"task {i} description", 1000)
        _require(problems, t["analysis_type"] in ANALYSIS_TYPES, f"task {i}: unknown analysis_type {t['analysis_type']!r}")
        cl = t["claims"]
        if not _require(problems, isinstance(cl, list) and 1 <= len(cl) <= MAX_CLAIMS_PER_TASK,
                        f"task {i}: needs 1 to {MAX_CLAIMS_PER_TASK} claims"):
            continue
        for c in cl:
            problems.extend(f"task {i}: {p}" for p in claim_vocab.validate_claim(c, profile))
            key = c.get("key") if isinstance(c, dict) else None
            _require(problems, key not in keys, f"task {i}: duplicate claim key {key!r}")
            keys.add(key)
    return problems


def triage_plan(payload, profile, kept=(), allowed_ids=None, required_keys=None):
    """
    P6 selective task recovery (opt-in): split a plan into tasks that are valid and tasks that are not.

    Returns (structural_problems, accepted_tasks, rejected) where rejected is [(index, task, problems)].
    - The plan's structure (a single "tasks" list of 1..MAX_TASKS) is checked exactly as validate_plan does; a
      structural problem rejects the whole reply (structural_problems non-empty, nothing accepted).
    - Each task is checked with validate_plan itself (strict claim validation, unchanged), then validate_plan is
      run on kept + accepted + [task], so duplicate ids, duplicate claim keys and the task limit still apply to the
      merged plan. The first valid occurrence wins; a later conflicting task is rejected with the exact problems.
    - allowed_ids (repair replies): a task whose id is not one of the tasks being repaired is rejected.
    - required_keys (repair replies): {task id: claim keys of the original task}. A repaired task must keep every
      one of them, so a repair can never silently drop a required question and still count as complete.
    Nothing is repaired here; problems are reported exactly as validate_plan words them.
    """
    problems = []
    if not _fields(problems, payload, "plan", ("tasks",)):
        return problems, [], []
    tasks = payload["tasks"]
    if not _require(problems, isinstance(tasks, list) and 1 <= len(tasks) <= MAX_TASKS,
                    f"plan must have 1 to {MAX_TASKS} tasks"):
        return problems, [], []
    accepted, rejected = [], []
    for i, t in enumerate(tasks):
        if allowed_ids is not None and not (isinstance(t, dict) and t.get("id") in allowed_ids):
            rejected.append((i, t, [f"task {i}: id {t.get('id') if isinstance(t, dict) else None!r} is not one of "
                                    f"the tasks to repair {sorted(allowed_ids)}"]))
            continue
        own = [p.replace("task 0", f"task {i}", 1) for p in validate_plan({"tasks": [t]}, profile)]
        if not own and required_keys is not None:
            have = {cl.get("key") for cl in t["claims"] if isinstance(cl, dict)}
            lost = sorted(set(required_keys.get(t["id"], ())) - have)
            if lost:
                own = [f"task {i}: repair must keep the original claim keys; missing {lost}"]
        if not own:
            own = [f"task {i} conflicts with the accepted tasks: {p}"
                   for p in validate_plan({"tasks": list(kept) + accepted + [t]}, profile)]
        if own:
            rejected.append((i, t, own))
        else:
            accepted.append(t)
    return [], accepted, rejected


def validate_approval(payload, plan):
    problems = []
    if not _fields(problems, payload, "approval", ("decision",), ("keep_task_ids", "reviewer_note")):
        return problems
    d = payload["decision"]
    _require(problems, d in ("approve", "reject", "edit"), f"unknown decision {d!r}")
    if d == "edit":
        keep = payload.get("keep_task_ids")
        ids = [t["id"] for t in plan["tasks"]]
        if _require(problems, isinstance(keep, list) and keep, "edit needs a non-empty keep_task_ids list"):
            _require(problems, all(k in ids for k in keep), "edit may only keep tasks from the proposed plan")
            _require(problems, len(set(keep)) == len(keep), "keep_task_ids has duplicates")
    elif "keep_task_ids" in payload:
        problems.append("keep_task_ids is only allowed with decision 'edit'")
    if "reviewer_note" in payload:
        _str(problems, payload["reviewer_note"], "reviewer_note", 2000, empty=True)
    return problems


def validate_analysis_result(payload, task):
    problems = []
    if not _fields(problems, payload, "analysis_result",
                   ("task_id", "attempt", "code", "execution", "claims", "claim_errors", "unplanned")):
        return problems
    _require(problems, payload["task_id"] == task["id"], "analysis_result is for another task")
    _str(problems, payload["code"], "code", MAX_CODE_CHARS, empty=True)
    ex = payload["execution"]
    if _fields(problems, ex, "execution", ("ok", "stdout", "error", "error_type", "chart_path", "backend",
                                           "isolation", "seconds", "blocked_by_safety_scan")):
        _require(problems, isinstance(ex["ok"], bool), "execution.ok must be a boolean")
        _require(problems, isinstance(ex["stdout"], str) and len(ex["stdout"]) <= MAX_STDOUT_CHARS,
                 f"execution.stdout must be a string of at most {MAX_STDOUT_CHARS} characters")
    planned = {c["key"] for c in task["claims"]}
    if _require(problems, isinstance(payload["claims"], dict), "claims must be an object"):
        _require(problems, all(_json_value(v) for v in payload["claims"].values()), "claim values must be JSON")
        _require(problems, set(payload["claims"]) <= planned, "claims may only contain planned keys")
    if _require(problems, isinstance(payload["claim_errors"], dict), "claim_errors must be an object"):
        _require(problems, all(isinstance(v, str) for v in payload["claim_errors"].values()),
                 "claim_errors values must be strings")
    _require(problems, isinstance(payload["unplanned"], list) and all(isinstance(k, str) for k in payload["unplanned"]),
             "unplanned must be a list of keys")
    return problems


_STATUSES = ("verified", "refuted", "missing", "malformed", "unverifiable")


def validate_verification_report(payload, task):
    problems = []
    if not _fields(problems, payload, "verification_report",
                   ("task_id", "attempt", "verdict", "claims", "unplanned", "dataset_integrity", "feedback")):
        return problems
    _require(problems, payload["task_id"] == task["id"], "verification_report is for another task")
    _require(problems, payload["verdict"] in ("accepted", "rejected"), "unknown verdict")
    planned = {c["key"] for c in task["claims"]}
    per = payload["claims"]
    if _require(problems, isinstance(per, dict) and set(per) == planned,
                "verification_report must cover exactly the planned claims"):
        _require(problems, all(isinstance(e, dict) and e.get("status") in _STATUSES for e in per.values()),
                 "every claim needs a known status")
        all_ok = all(e.get("status") == "verified" for e in per.values())
        _require(problems, (payload["verdict"] == "accepted") == all_ok,
                 "verdict is inconsistent with the per-claim statuses")
    return problems


def validate_insight_report(payload, task):
    problems = []
    if not _fields(problems, payload, "insight_report", ("task_id", "insights")):
        return problems
    _require(problems, payload["task_id"] == task["id"], "insight_report is for another task")
    ins = payload["insights"]
    if _require(problems, isinstance(ins, list) and len(ins) <= MAX_INSIGHTS, f"insights must be a list of at most {MAX_INSIGHTS}"):
        for i, x in enumerate(ins):
            if _fields(problems, x, f"insight {i}", ("text", "cites")):
                _str(problems, x["text"], f"insight {i} text", 600)
                _require(problems, isinstance(x["cites"], list) and all(isinstance(c, str) for c in x["cites"]),
                         f"insight {i}: cites must be a list of claim keys")
    return problems


def validate_insight_check_report(payload, task):
    problems = []
    if _fields(problems, payload, "insight_check_report", ("task_id", "verdict", "problems")):
        _require(problems, payload["task_id"] == task["id"], "insight_check_report is for another task")
        _require(problems, payload["verdict"] in ("accepted", "rejected"), "unknown verdict")
        _require(problems, (payload["verdict"] == "accepted") == (not payload["problems"]),
                 "verdict is inconsistent with the problems list")
    return problems


def validate_execution_terminated(payload):
    """Built by the trusted node wrapper from the exception CLASS, never from model output."""
    from multi_agent.execution import (EXECUTION_REFUSED, INFRASTRUCTURE_CLASSES, ISOLATION_UNAVAILABLE,
                                       POLICY_CLASSES)
    problems = []
    if not _fields(problems, payload, "execution_terminated", ("category", "error_class", "message")):
        return problems
    allowed = {ISOLATION_UNAVAILABLE: INFRASTRUCTURE_CLASSES, EXECUTION_REFUSED: POLICY_CLASSES}
    if _require(problems, payload["category"] in allowed, f"unknown category {payload['category']!r}"):
        _require(problems, payload["error_class"] in allowed[payload["category"]],
                 f"error_class {payload['error_class']!r} does not belong to {payload['category']!r}")
    _str(problems, payload["message"], "message", 500, empty=True)
    return problems


def validate_agent_error(payload):
    problems = []
    if _fields(problems, payload, "agent_error", ("error_type", "message")):
        _str(problems, payload["error_type"], "error_type", 100)
        _str(problems, payload["message"], "message", 2000, empty=True)
    return problems


# ── envelopes ────────────────────────────────────────────────────────────────

ENVELOPE_FIELDS = ("seq", "reply_to", "sender", "recipient", "kind", "task_id", "attempt", "payload")


def envelope(seq, sender, recipient, kind, payload, task_id=None, attempt=0, reply_to=None):
    env = {"seq": seq, "reply_to": reply_to, "sender": sender, "recipient": recipient, "kind": kind,
           "task_id": task_id, "attempt": attempt, "payload": payload}
    check_envelope(env)
    return env


def check_envelope(env, recipient=None, kinds=None, reply_to=None):
    """Structural and routing checks shared by every handoff; raises HandoffRejected."""
    problems = []
    if not _fields(problems, env, "envelope", ENVELOPE_FIELDS):
        raise HandoffRejected(problems)
    route = (env["sender"], env["recipient"], env["kind"])
    _require(problems, route in ROUTES, f"route not allowed: {route}")
    _require(problems, type(env["seq"]) is int and env["seq"] >= 0, "seq must be a non-negative integer")
    _require(problems, type(env["attempt"]) is int and env["attempt"] >= 0, "attempt must be a non-negative integer")
    _require(problems, isinstance(env["payload"], dict), "payload must be an object")
    if recipient is not None:
        _require(problems, env["recipient"] == recipient, f"addressed to {env['recipient']!r}, not {recipient!r}")
    if kinds is not None:
        _require(problems, env["kind"] in kinds, f"unexpected kind {env['kind']!r}, expected one of {sorted(kinds)}")
    if reply_to is not None:
        _require(problems, env["reply_to"] == reply_to, f"reply_to {env['reply_to']!r} does not match request {reply_to}")
    if problems:
        raise HandoffRejected(problems)

