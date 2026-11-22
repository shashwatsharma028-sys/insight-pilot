"""
P5.0: evaluation of the multi-agent prototype (multi_agent/) with scripted agents.

Each case runs the Supervisor graph on one demo dataset, offline, with the
scripted agent model from tests/v2/p5/scripted.py, optionally with injected
faults, and measures:

- handoffs: requests dispatched, replies accepted or rejected by contract, agent errors;
- verification accuracy against labelled ground truth: a reported claim is
  truly correct only if it equals the committed golden answer (tests/v2/golden,
  computed by a separate implementation); detection rate, false rejections and
  incorrect claims accepted;
- insight checks against the injected insight faults;
- retries and recovery per task;
- agent calls and context usage: requests and request-payload characters per
  agent, LLM calls and prompt characters;
- latency: whole pipeline, per graph node and per sandbox execution;
- token provenance (evaluation.provenance): mock scripted calls are labelled
  character estimates, never provider tokens, cost or savings;
- final accepted claims against the 28 golden answers.

These are properties of the coordination logic on scripted workloads. They are
not evidence about real-model accuracy, tokens or cost.
"""

import json
import os
import socket
import time
from contextlib import contextmanager

from evaluation import metrics, provenance
from evaluation.isolation import OfflineViolation, Patcher, _is_loopback

HARNESS_VERSION = "p5.0-multi-agent"


def _plan_unknown_column(plan):
    plan["tasks"][0]["claims"][-1]["args"] = {"column": "no_such_column"}
    return plan


def _plan_unknown_op(plan):
    plan["tasks"][0]["claims"][0]["op"] = "median_of_everything"
    return plan


# name -> dataset, faults, limits, approval, expected outcome
CASES = {
    "clean-sales": {"dataset": "sales", "expect": {"status": "completed_with_failures",
                                                   "tasks": {"sales_t4": "failed"}}},
    "clean-attendance": {"dataset": "attendance", "expect": {"status": "completed"}},
    "clean-student": {"dataset": "student", "expect": {"status": "completed"}},
    "clean-ecommerce": {"dataset": "ecommerce", "expect": {"status": "completed"}},
    "wrong-value-recovered": {
        "dataset": "sales", "faults": {"analysis": {"sales_t3": {1: {"target_attainment_pct": ["scale", 1.01]}}}},
        "expect": {"tasks": {"sales_t3": "completed"}, "attempts": {"sales_t3": 2}}},
    "wrong-group-and-label-recovered": {
        "dataset": "sales", "faults": {"analysis": {"sales_t2": {1: {"sales_by_region": ["dict_scale", "East", 1.5],
                                                                     "top_region": ["replace", "South"]}}}},
        "expect": {"tasks": {"sales_t2": "completed"}, "attempts": {"sales_t2": 2}}},
    "wrong-value-persistent": {
        "dataset": "attendance",
        "faults": {"analysis": {"att_t3": {a: {"gps_mismatch_count": ["add", 1]} for a in (1, 2, 3)}}},
        "expect": {"status": "completed_with_failures", "tasks": {"att_t3": "failed"}, "attempts": {"att_t3": 3},
                   "not_accepted": ["gps_mismatch_count"]}},
    "missing-and-malformed": {
        "dataset": "student", "faults": {"analysis": {"stu_t1": {1: {"pass_rate": ["drop"],
                                                                     "mean_score": ["raw", "{not json"]}}}},
        "expect": {"status": "completed", "attempts": {"stu_t1": 2}}},
    "non-finite": {
        "dataset": "ecommerce", "faults": {"analysis": {"eco_t3": {1: {"corr_visitors_revenue": ["nan"]}}}},
        "expect": {"status": "completed", "attempts": {"eco_t3": 2}}},
    "duplicate-claim": {
        "dataset": "attendance", "faults": {"analysis": {"att_t1": {1: {"present_rate": ["duplicate", 0.99]}}}},
        "expect": {"status": "completed", "attempts": {"att_t1": 2}}},
    "type-confusion": {
        "dataset": "attendance", "faults": {"analysis": {"att_t1": {1: {"row_count": ["replace", "500"]}}}},
        "expect": {"status": "completed", "attempts": {"att_t1": 2}}},
    "within-tolerance": {
        "dataset": "student", "faults": {"analysis": {"stu_t3": {1: {"corr_study_hours_score": ["scale", 1 + 1e-12]}}}},
        "expect": {"status": "completed", "attempts": {"stu_t3": 1}}},
    "outside-tolerance": {
        "dataset": "ecommerce", "faults": {"analysis": {"eco_t1": {1: {"total_revenue": ["add", 1.0]}}}},
        "expect": {"status": "completed", "attempts": {"eco_t1": 2}}},
    "unplanned-claim": {
        "dataset": "sales", "faults": {"analysis": {"sales_t3": {1: {"target_attainment_pct": ["extra_key",
                                                                                               "bonus_metric", 1]}}}},
        "expect": {"tasks": {"sales_t3": "completed"}, "attempts": {"sales_t3": 1}, "not_accepted": ["bonus_metric"]}},
    "insight-faults": {
        "dataset": "ecommerce", "faults": {"insight": {"eco_t1": {1: "cite_unknown"}, "eco_t2": {1: "ungrounded_number"},
                                                       "eco_t3": {1: "bad_json", 2: "cite_other_task"}}},
        "expect": {"status": "completed_with_failures",
                   "tasks": {"eco_t1": "completed", "eco_t2": "completed", "eco_t3": "insight_rejected"}}},
    "invalid-plan-recovered": {
        "dataset": "student", "faults": {"plan": {1: _plan_unknown_column}}, "expect": {"status": "completed"}},
    "invalid-plan-persistent": {
        "dataset": "attendance", "faults": {"plan": {1: _plan_unknown_op, 2: _plan_unknown_op}},
        "expect": {"status": "plan_invalid", "analyst_requests": 0}},
    "human-reject": {"dataset": "sales", "approval": "reject",
                     "expect": {"status": "rejected_by_human", "analyst_requests": 0}},
    "human-edit-drops-task": {"dataset": "sales", "approval": "drop_last",
                              "expect": {"status": "completed", "approved": ["sales_t1", "sales_t2", "sales_t3"]}},
    "step-budget": {"dataset": "student", "limits": {"max_steps": 5}, "expect": {"status": "budget_exhausted"}},
}


@contextmanager
def offline_env(workdir):
    """No API key, no real LLM client, loopback-only sockets, charts inside workdir; restored on exit."""
    import sandbox.executor
    import utils.llm
    charts = os.path.join(workdir, "charts")
    os.makedirs(charts, exist_ok=True)
    p = Patcher()
    try:
        p.delenv("GOOGLE_API_KEY")

        class _Blocked:
            def __init__(self, *a, **k):
                raise OfflineViolation("real LLM client constructed during an offline evaluation")
        p.setattr(utils.llm, "ChatGoogleGenerativeAI", _Blocked)
        real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

        def _allowed(sock, address):
            return sock.family == getattr(socket, "AF_UNIX", None) or _is_loopback(address)

        def connect(sock, address):
            if not _allowed(sock, address):
                raise OfflineViolation(f"network connection blocked: {address!r}")
            return real_connect(sock, address)

        def connect_ex(sock, address):
            if not _allowed(sock, address):
                raise OfflineViolation(f"network connection blocked: {address!r}")
            return real_connect_ex(sock, address)
        p.setattr(socket.socket, "connect", connect)
        p.setattr(socket.socket, "connect_ex", connect_ex)
        p.setattr(sandbox.executor, "CHARTS_DIR", charts)
        yield {"charts": charts}
    finally:
        p.undo()


def _approval(name):
    from tests.v2.p5 import scripted
    return {"approve": scripted.approve_all, "reject": scripted.reject_all, "drop_last": scripted.drop_last_task}[name]


def run_case(name, workdir, strategy="v1", mode="subprocess", clock=time.perf_counter, case=None) -> dict:
    from multi_agent.execution import CodeTrust, resolve_port
    from multi_agent.team import build_team
    from tests.v2.golden.reference import DATA_DIR, DATASETS
    from tests.v2.p5 import scripted
    case = case or CASES[name]
    ds = case["dataset"]
    with offline_env(workdir):
        llm = scripted.ScriptedAgentLLM(ds, case.get("faults"), mode=mode)
        port = scripted.LiteralPrintExecution() if mode == "literal" else resolve_port(CodeTrust.SCRIPTED_FIXTURES, strategy)
        sup, calls = build_team(llm, port, _approval(case.get("approval", "approve")), clock=clock)
        t0 = clock()
        state = sup.run(os.path.join(DATA_DIR, DATASETS[ds]), limits=case.get("limits"))
        pipeline_s = clock() - t0
    return measure(name, case, state, calls, pipeline_s, strategy if mode == "subprocess" else "literal")


# ── measurement ──────────────────────────────────────────────────────────────

def _token_records(calls):
    out = []
    for c in calls:
        holder = type("R", (), {"usage_metadata": c["usage_metadata"]})()
        source, i, o, anomaly = provenance.classify_usage(holder)
        if source == provenance.ESTIMATED:
            i = (c["system_chars"] + c["user_chars"]) // provenance.CHARS_PER_TOKEN_ESTIMATE
            o = c["response_chars"] // provenance.CHARS_PER_TOKEN_ESTIMATE
        out.append({"skill": c["skill"], "agent": c["agent"], "call_source": c["call_source"],
                    "token_source": source, "input_tokens": i, "output_tokens": o, "anomaly": anomaly})
    return out


def _verification_confusion(messages, golden):
    from tests.v2.golden.compare import values_match as golden_match
    cm = {"tp": 0, "tn": 0, "fp": 0, "fn": 0, "unlabelled": 0}
    for m in messages:
        if m["kind"] != "verification_report":
            continue
        for key, e in m["payload"]["claims"].items():
            if key not in golden:
                cm["unlabelled"] += 1
                continue
            truly_correct = e["status"] not in ("missing", "malformed") and _safe_match(golden_match, e["reported"],
                                                                                      golden[key])
            rejected = e["status"] != "verified"
            cm[("fp" if rejected else "tn") if truly_correct else ("tp" if rejected else "fn")] += 1
    labelled = cm["tp"] + cm["tn"] + cm["fp"] + cm["fn"]
    positives, negatives = cm["tp"] + cm["fn"], cm["tn"] + cm["fp"]
    cm.update(claims_checked=labelled,
              accuracy=round((cm["tp"] + cm["tn"]) / labelled, 6) if labelled else None,
              detection_rate=round(cm["tp"] / positives, 6) if positives else None,
              false_rejection_rate=round(cm["fp"] / negatives, 6) if negatives else None)
    return cm


def _safe_match(fn, a, b):
    try:
        return bool(fn(a, b))
    except (TypeError, ValueError):
        return False


def _insight_confusion(messages, faults):
    injected = (faults or {}).get("insight", {})
    cm = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
    checked = {(m["task_id"], m["attempt"]): m["payload"]["verdict"] for m in messages if m["kind"] == "insight_check_report"}
    errors = {(m["task_id"], m["attempt"]) for m in messages
              if m["kind"] == "agent_error" and m["sender"] == "business_insight_agent"}
    for (tid, att), verdict in checked.items():
        bad = att in injected.get(tid, {})
        cm[("tp" if verdict == "rejected" else "fn") if bad else ("fp" if verdict == "rejected" else "tn")] += 1
    cm["unparseable_replies"] = len(errors)
    return cm


def measure(name, case, state, calls, pipeline_s, strategy) -> dict:
    from tests.v2.golden.compare import values_match as golden_match
    from tests.v2.p5 import scripted
    final, messages, events = state["final"], state.get("messages") or [], state.get("events") or []
    golden = scripted.golden_answers(case["dataset"])
    requests = [m for m in messages if m["sender"] == "supervisor"]
    replies = [m for m in messages if m["recipient"] == "supervisor"]
    agents = {}
    for m in requests:
        a = agents.setdefault(m["recipient"], {"requests": 0, "request_payload_chars": 0, "llm_calls": 0,
                                               "prompt_chars": 0, "response_chars": 0})
        a["requests"] += 1
        a["request_payload_chars"] += len(json.dumps(m["payload"], sort_keys=True))
    for c in calls:
        a = agents.setdefault(c["agent"], {"requests": 0, "request_payload_chars": 0, "llm_calls": 0,
                                           "prompt_chars": 0, "response_chars": 0})
        a["llm_calls"] += 1
        a["prompt_chars"] += c["system_chars"] + c["user_chars"]
        a["response_chars"] += c["response_chars"]
    per_node = {}
    for t in state.get("timings") or []:
        per_node.setdefault(t["node"], []).append(t["seconds"])
    execs = [m["payload"]["execution"] for m in replies if m["kind"] == "analysis_result"]
    tasks = final["tasks"]
    accepted = final["accepted_claims"]
    answers = metrics.score_answers(accepted, golden) if final["approved_task_ids"] else None
    incorrect_accepted = sorted(k for k, v in accepted.items() if k in golden and not _safe_match(golden_match, v, golden[k]))
    contract_ok = sum(1 for e in events if e["type"] == "handoff_accepted")
    result = {
        "case": name, "dataset": case["dataset"], "strategy": strategy, "status": final["status"],
        "handoffs": {"requests": len(requests), "replies": len(replies), "accepted_by_contract": contract_ok,
                     "rejected_by_contract": final["handoffs_rejected"], "agent_errors": final["agent_errors"],
                     "contract_acceptance_rate": round(contract_ok / len(replies), 6) if replies else None},
        "verification": _verification_confusion(messages, golden),
        "insight_checks": _insight_confusion(messages, case.get("faults")),
        "plan": {"attempts": state.get("plan_attempts", 0), "accepted": state.get("plan") is not None},
        "tasks": {tid: {"status": t["status"], "analysis_attempts": t["attempts"], "insight_attempts": t["insight_attempts"],
                        "recovered": t["status"] in ("completed", "insight_rejected") and any(
                            h["stage"] == "analysis" and h["outcome"] == "rejected" for h in t["history"])}
                  for tid, t in tasks.items()},
        "retries": {"analysis_retries": sum(max(0, t["attempts"] - 1) for t in tasks.values()),
                    "insight_retries": sum(max(0, t["insight_attempts"] - 1) for t in tasks.values()),
                    "recovered_tasks": sum(1 for t in tasks.values() if t["status"] in ("completed", "insight_rejected")
                                           and any(h["stage"] == "analysis" and h["outcome"] == "rejected"
                                                   for h in t["history"])),
                    "failed_tasks": sum(1 for t in tasks.values() if t["status"] == "failed")},
        "agents": dict(sorted(agents.items())),
        "latency": {"pipeline_ms": round(pipeline_s * 1000, 1),
                    "per_node": {n: metrics.latency_stats(v) for n, v in sorted(per_node.items())},
                    "sandbox_executions": metrics.latency_stats([e["seconds"] for e in execs if not e["blocked_by_safety_scan"]]),
                    "executions": len(execs), "blocked_by_safety_scan": sum(e["blocked_by_safety_scan"] for e in execs)},
        "tokens": provenance.aggregate(_token_records(calls)),
        "final_answers": {"accepted": len(accepted), "incorrect_accepted": incorrect_accepted,
                          "golden": answers and {k: answers[k] for k in ("expected", "correct", "incorrect", "missing")}},
        "steps": final["steps"], "lessons": final["lessons"],
        "trace": trace(messages),
    }
    result["expectations"] = check_expectations(case.get("expect", {}), result, final)
    return result


def _summary(m) -> str:
    p, k = m["payload"], m["kind"]
    if k == "plan_request":
        return f"planning attempt {p['attempt']}; {len(p['feedback'])} earlier problem(s)"
    if k == "plan":
        tasks = p.get("tasks") if isinstance(p.get("tasks"), list) else []
        return f"{len(tasks)} task(s), {sum(len(t.get('claims') or []) for t in tasks if isinstance(t, dict))} claim(s)"
    if k == "approval_request":
        return "tasks " + ", ".join(t["id"] for t in p["plan"]["tasks"])
    if k == "approval_decision":
        return f"decision {p.get('decision')!r}" + (f" keep {p['keep_task_ids']}" if "keep_task_ids" in p else "")
    if k == "analysis_request":
        return f"{len(p['task']['claims'])} claim spec(s); {len(p['feedback'])} feedback item(s)"
    if k == "analysis_result":
        ex = p.get("execution") or {}
        run = "blocked by safety scan" if ex.get("blocked_by_safety_scan") else "ran ok" if ex.get("ok") else "failed"
        return f"{run}; {len(p.get('claims') or {})} claim(s), {len(p.get('claim_errors') or {})} parse error(s)"
    if k == "verification_request":
        return f"{len(p['submitted'])} reported value(s) to recompute"
    if k == "verification_report":
        counts = {}
        for e in p["claims"].values():
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        return f"{p['verdict']}: " + ", ".join(f"{n} {st}" for st, n in sorted(counts.items()))
    if k == "insight_request":
        return f"{len(p['verified_claims'])} verified claim(s) only"
    if k == "insight_report":
        return f"{len(p.get('insights') or [])} insight(s)"
    if k == "insight_check_request":
        return f"{len(p['insights'])} insight(s) to ground"
    if k == "insight_check_report":
        return p["verdict"] + (f": {p['problems'][0]}" if p["problems"] else "")
    if k == "agent_error":
        return f"{p.get('error_type')}: {str(p.get('message'))[:80]}"
    if k == "execution_terminated":
        return f"{p.get('category')}: {p.get('error_class')}"
    return ""


def trace(messages) -> list:
    """One line per handoff: seq, route, kind, task/attempt and a payload summary (no values)."""
    return [f"{m['seq']:>3} {m['sender']} -> {m['recipient']}: {m['kind']}"
            + (f" [{m['task_id']} #{m['attempt']}]" if m["task_id"] else "") + f" | {_summary(m)}" for m in messages]


def check_expectations(expect, result, final) -> dict:
    problems = []
    if "status" in expect and result["status"] != expect["status"]:
        problems.append(f"status {result['status']} != {expect['status']}")
    for tid, st in expect.get("tasks", {}).items():
        got = result["tasks"].get(tid, {}).get("status")
        if got != st:
            problems.append(f"task {tid} status {got} != {st}")
    for tid, n in expect.get("attempts", {}).items():
        got = result["tasks"].get(tid, {}).get("analysis_attempts")
        if got != n:
            problems.append(f"task {tid} attempts {got} != {n}")
    for k in expect.get("not_accepted", []):
        if k in final["accepted_claims"]:
            problems.append(f"claim {k} was accepted")
    if "analyst_requests" in expect:
        got = result["agents"].get("data_analyst_agent", {}).get("requests", 0)
        if got != expect["analyst_requests"]:
            problems.append(f"analyst requests {got} != {expect['analyst_requests']}")
    if "approved" in expect and final["approved_task_ids"] != expect["approved"]:
        problems.append(f"approved {final['approved_task_ids']} != {expect['approved']}")
    if result["final_answers"]["incorrect_accepted"]:
        problems.append(f"incorrect claims accepted: {result['final_answers']['incorrect_accepted']}")
    if result["verification"]["fn"]:
        problems.append(f"{result['verification']['fn']} incorrect claim(s) verified")
    return {"met": not problems, "problems": problems}


def run_suite(out_dir, cases=None, strategy="v1", mode="subprocess", clock=time.perf_counter) -> dict:
    names = cases or list(CASES)
    results = [run_case(n, os.path.join(out_dir, "runs", n), strategy, mode, clock) for n in names]
    return {"harness_version": HARNESS_VERSION, "strategy": strategy, "mode": mode, "cases": results,
            "summary": summarize(results)}


def summarize(results) -> dict:
    def tot(path):
        s = 0
        for r in results:
            v = r
            for p in path:
                v = v[p]
            s += v or 0
        return s
    v = {k: tot(("verification", k)) for k in ("tp", "tn", "fp", "fn")}
    labelled = sum(v.values())
    ins = {k: tot(("insight_checks", k)) for k in ("tp", "tn", "fp", "fn")}
    clean = [r for r in results if r["case"].startswith("clean-")]
    return {
        "cases": len(results), "expectations_met": sum(r["expectations"]["met"] for r in results),
        "handoffs": {"replies": tot(("handoffs", "replies")), "accepted_by_contract": tot(("handoffs", "accepted_by_contract")),
                     "rejected_by_contract": tot(("handoffs", "rejected_by_contract")),
                     "agent_errors": tot(("handoffs", "agent_errors"))},
        "verification": {**v, "claims_checked": labelled,
                         "accuracy": round((v["tp"] + v["tn"]) / labelled, 6) if labelled else None,
                         "detection_rate": round(v["tp"] / (v["tp"] + v["fn"]), 6) if v["tp"] + v["fn"] else None,
                         "false_rejection_rate": round(v["fp"] / (v["fp"] + v["tn"]), 6) if v["fp"] + v["tn"] else None},
        "insight_checks": ins,
        "incorrect_claims_accepted": sum(len(r["final_answers"]["incorrect_accepted"]) for r in results),
        "clean_runs_golden_correct": sum(r["final_answers"]["golden"]["correct"] for r in clean if r["final_answers"]["golden"]),
        "clean_runs_golden_expected": sum(r["final_answers"]["golden"]["expected"] for r in clean if r["final_answers"]["golden"]),
        "analysis_retries": tot(("retries", "analysis_retries")), "recovered_tasks": tot(("retries", "recovered_tasks")),
        "failed_tasks": tot(("retries", "failed_tasks")),
        "llm_calls": tot(("tokens", "total_calls")), "provider_calls": tot(("tokens", "provider_calls")),
    }


def markdown(suite) -> str:
    s = suite["summary"]
    v = s["verification"]
    lines = [f"# P5.0 multi-agent evaluation ({suite['mode']}, strategy {suite['strategy']})", "",
             "Scripted agents; injected faults. Not evidence about real-model accuracy, tokens or cost.", "",
             f"- Cases: {s['cases']}; expectations met: {s['expectations_met']}/{s['cases']}",
             f"- Verification: {v['claims_checked']} labelled claim checks; TP {v['tp']}, TN {v['tn']}, FP {v['fp']}, "
             f"FN {v['fn']}; accuracy {v['accuracy']}; detection rate {v['detection_rate']}; "
             f"false-rejection rate {v['false_rejection_rate']}",
             f"- Incorrect claims accepted: {s['incorrect_claims_accepted']}",
             f"- Insight checks: TP {s['insight_checks']['tp']}, TN {s['insight_checks']['tn']}, "
             f"FP {s['insight_checks']['fp']}, FN {s['insight_checks']['fn']}",
             f"- Clean runs: {s['clean_runs_golden_correct']}/{s['clean_runs_golden_expected']} golden answers accepted and correct",
             f"- Handoff replies: {s['handoffs']['replies']}; accepted by contract {s['handoffs']['accepted_by_contract']}; "
             f"rejected {s['handoffs']['rejected_by_contract']}; agent errors {s['handoffs']['agent_errors']}",
             f"- Analysis retries: {s['analysis_retries']}; recovered tasks: {s['recovered_tasks']}; failed tasks: {s['failed_tasks']}",
             f"- LLM calls: {s['llm_calls']} (provider calls: {s['provider_calls']})", "",
             "| Case | Status | Expected | Requests | Retries | Recovered | TP/TN/FP/FN | Pipeline ms |",
             "|---|---|---|---|---|---|---|---|"]
    for r in suite["cases"]:
        vv = r["verification"]
        lines.append(f"| {r['case']} | {r['status']} | {'met' if r['expectations']['met'] else 'NOT MET: ' + '; '.join(r['expectations']['problems'])} "
                     f"| {r['handoffs']['requests']} | {r['retries']['analysis_retries']} | {r['retries']['recovered_tasks']} "
                     f"| {vv['tp']}/{vv['tn']}/{vv['fp']}/{vv['fn']} | {r['latency']['pipeline_ms']} |")
    return "\n".join(lines) + "\n"
