"""
P6: live-LLM benchmark, single-agent (V1-style) versus multi-agent (V2 Supervisor), in TRUSTED-OPS mode.

Mode (until independent security validation): models never write executable code. They answer fixed analytical
questions by choosing claims from the validated op vocabulary (multi_agent.claims); multi_agent.op_engine computes
them in-process with reviewed pandas code. No generated code runs, no container is needed, and the V1 executors
are tripwired for the whole run (a hit aborts it).

Fair comparison (same model, prompts' shared parts, datasets, questions, op engine and machine):
- single_agent: per task, one claims call (retried with error feedback, like V1's retry-on-error, max 2 retries),
  trusted computation, then one insight call that sees the computed values (unverified). No Supervisor, no
  independent verification, no plan validation.
- multi_agent: the P5 Supervisor (Planning LLM -> plan validation -> approval -> TrustedOpsAnalyst ->
  stdlib Verification Agent -> Business Insight LLM with grounding checks and bounded retries).
Both receive the identical dataset profile, op catalogue and question wording. Golden answers are never in a
prompt. The dataset profile contains the row count, so the 4 row_count questions are reported separately; the
primary accuracy uses the other 24 questions, whose answers no prompt contains (tests/v2/p6 checks this).

Usage:
    python -m evaluation.live --provider fake --mode pilot --out DIR                (offline dry run, $0)
    GOOGLE_API_KEY=... python -m evaluation.live --provider gemini --mode pilot \\
        --budget-usd 0.25 --prices-confirmed --i-understand-this-makes-live-calls --out DIR
"""

import argparse
import contextlib
import json
import os
import random
import sys
import time

from evaluation import metrics, paired
from multi_agent import contracts as c
from multi_agent import dataset as dsmod
from multi_agent import live_llm, op_engine, prompts
from multi_agent.agents import LLMOutputError, _json_reply
from multi_agent.claims import describe_ops
from multi_agent.llm import MOCK, PROVIDER

HARNESS_VERSION = "p6.0-live-trusted-ops"
ARCHS = ("single_agent", "multi_agent")
DATASETS = ("sales", "attendance", "student", "ecommerce")
PILOT = {"datasets": ("student",), "reps": 1}
MAX_SINGLE_RETRIES = 2                     # V1's max_retries

# The 28 golden questions: what to establish and in which unit, never the value.
QUESTIONS = {
    "sales": {
        "row_count": "How many sales records (rows) does the dataset contain?",
        "total_sales_amount": "What is the total sales amount across all records?",
        "total_returns": "What is the total number of returns across all records?",
        "mean_customer_satisfaction": "What is the average customer satisfaction score?",
        "sales_by_region": "What is the total sales amount for each region?",
        "top_region": "Which region has the highest total sales amount?",
        "target_attainment_pct": "Overall, what percentage of the total sales target was achieved "
                                 "(total sales amount as a percentage of total sales target)?",
    },
    "attendance": {
        "row_count": "How many attendance records does the dataset contain?",
        "status_counts": "How many records are there for each attendance status?",
        "present_rate": "What fraction (between 0 and 1) of all records have the status Present?",
        "mean_hours_worked": "What is the average number of hours worked per record?",
        "mean_hours_by_department": "What is the average number of hours worked in each department?",
        "top_department_by_mean_hours": "Which department has the highest average hours worked?",
        "gps_mismatch_count": "How many records failed the GPS location match?",
    },
    "student": {
        "row_count": "How many student records does the dataset contain?",
        "pass_rate": "What fraction (between 0 and 1) of students passed?",
        "grade_letter_counts": "How many records are there for each grade letter?",
        "mean_score": "What is the average score?",
        "mean_score_by_subject": "What is the average score in each subject?",
        "top_subject_by_mean_score": "Which subject has the highest average score?",
        "corr_study_hours_score": "What is the Pearson correlation between daily study hours and score?",
    },
    "ecommerce": {
        "row_count": "How many daily records does the dataset contain?",
        "total_revenue": "What is the total revenue across all records?",
        "total_orders": "What is the total number of orders across all records?",
        "mean_revenue_weekend": "What is the average daily revenue on weekend days?",
        "mean_revenue_weekday": "What is the average daily revenue on weekdays (not weekend)?",
        "top_month_by_revenue": "Which calendar month (formatted YYYY-MM) has the highest total revenue?",
        "corr_visitors_revenue": "What is the Pearson correlation between website visitors and revenue?",
    },
}
# Labels used in every report: the baseline is NOT the original V1 pipeline (which generates and runs pandas code).
ARCH_LABELS = {
    "single_agent": "V1-style single agent, trusted-ops (not the original unrestricted V1 code-generation pipeline)",
    "multi_agent": "V2 multi-agent Supervisor, trusted-ops (planner, trusted analyst, stdlib verifier, insight agent)",
}


def profile_numbers(profile) -> set:
    """Every number the shared schema profile shows a model: the row count and per-column missing counts."""
    return {profile["row_count"]} | {c["missing"] for c in profile["columns"]}


def leaked_keys(dataset) -> set:
    """
    Questions whose golden answer (or any value inside it) equals a number in the profile metadata, e.g. row_count
    and attendance's status_counts['Absent'] (= the 1268 missing punch_in values). Excluded from primary accuracy.
    """
    nums = profile_numbers(dsmod.profile(dsmod.load(dataset_path(dataset))))
    out = set()
    for k, v in golden(dataset).items():
        vals = list(v.values()) if isinstance(v, dict) else [v]
        if any(isinstance(x, (int, float)) and not isinstance(x, bool) and x in nums for x in vals):
            out.add(k)
    return out

SINGLE_SYSTEM = (
    "You are a data analyst. You see only the dataset's schema, never its rows. For the task, answer every "
    "required question by stating exactly one claim per question, using the given key and only the allowed ops. "
    "A trusted engine computes the claims. Reply with JSON: {\"claims\": [{\"key\", \"op\", \"args\"}]}."
)


def tasks_for(dataset):
    """The fixed analytical tasks: the golden scenario tasks that carry golden questions (sales_t4 has none)."""
    from tests.v2.golden import scenarios as sc
    from tests.v2.p5 import scripted
    out = []
    for t in sc.SCENARIOS[dataset]["tasks"]:
        keys = [k for k in scripted.task_claim_keys(dataset, t) if k in QUESTIONS[dataset]]
        if keys:
            out.append({"id": t["id"], "title": t["title"], "description": t["description"],
                        "analysis_type": t["analysis_type"], "questions": {k: QUESTIONS[dataset][k] for k in keys}})
    return out


def dataset_path(dataset):
    from tests.v2.golden.reference import DATA_DIR, DATASETS as FILES
    return os.path.join(DATA_DIR, FILES[dataset])


def golden(dataset):
    from tests.v2.golden.reference import load_expected
    return load_expected(dataset)["answers"]


# ── prompts (shared parts are identical for both architectures) ───────────────

def single_claims_prompt(profile, task, attempt, feedback):
    return (f"DATASET PROFILE JSON:\n{json.dumps(profile, sort_keys=True)}\n"
            f"CLAIM OPS JSON:\n{json.dumps(describe_ops(), sort_keys=True)}\n"
            f"TASK ID: {task['id']}\nTASK: {task['title']}\nDESCRIPTION: {task['description']}\n"
            f"REQUIRED QUESTIONS JSON (key -> question):\n{json.dumps(task['questions'], sort_keys=True)}\n"
            f"ATTEMPT: {attempt}\nPROBLEMS WITH THE PREVIOUS ATTEMPT:\n"
            + ("\n".join(f"- {f}" for f in feedback) if feedback else "- none") + "\n")


def multi_goal(tasks):
    spec = [{"id": t["id"], "title": t["title"], "description": t["description"],
             "analysis_type": t["analysis_type"], "questions": t["questions"]} for t in tasks]
    return ("Plan exactly these tasks, keeping each id, title, description and analysis_type. For each task, "
            "state exactly one claim per question, using the given key and only the allowed ops. "
            f"TASKS JSON: {json.dumps(spec, sort_keys=True)}")


# ── runs ──────────────────────────────────────────────────────────────────────

def run_single_agent(dataset, port, clock=time.perf_counter):
    path, tasks = dataset_path(dataset), tasks_for(dataset)
    profile = dsmod.profile(dsmod.load(path))
    out = {"claims": {}, "tasks": {}, "retries": 0, "execution_failures": 0, "llm_errors": 0}
    for t in tasks:
        feedback, values, errors, attempts = [], {}, {}, 0
        for attempt in range(1, MAX_SINGLE_RETRIES + 2):
            attempts = attempt
            try:
                reply = port.complete("single_agent_claims", SINGLE_SYSTEM,
                                      single_claims_prompt(profile, t, attempt, feedback))
                body = _json_reply(reply.content)
                specs = body.get("claims") if isinstance(body, dict) else None
                if not isinstance(specs, list):
                    raise LLMOutputError("reply must be an object with a 'claims' list")
            except (LLMOutputError, live_llm.ProviderError) as e:
                out["llm_errors"] += 1
                feedback = [f"{type(e).__name__}: {e}"[:300]]
                continue
            specs = [s for s in specs if isinstance(s, dict) and s.get("key") in t["questions"]]
            values, errors = op_engine.compute_claims(specs, path, profile)
            missing = sorted(set(t["questions"]) - set(values))
            if not missing:
                break
            out["execution_failures"] += len(errors)
            feedback = [f"{k}: {errors.get(k, 'no claim given')}" for k in missing][:12]
        out["retries"] += attempts - 1
        insight_ok = False
        if values:
            try:
                req = {"task_id": t["id"], "title": t["title"], "description": t["description"], "attempt": 1,
                       "verified_claims": values, "feedback": []}  # unverified in V1: no independent verifier
                ins = _json_reply(port.complete("single_agent_insight", prompts.INSIGHT_SYSTEM,
                                                prompts.insight(req)).content)
                insight_ok = isinstance(ins, dict) and isinstance(ins.get("insights"), list)
            except (LLMOutputError, live_llm.ProviderError):
                out["llm_errors"] += 1
        out["claims"].update(values)
        out["tasks"][t["id"]] = {"attempts": attempts, "completed": set(t["questions"]) <= set(values) and insight_ok,
                                 "claim_errors": errors}
    return out


def run_multi_agent(dataset, port, clock=time.perf_counter, selective_recovery=False):
    """selective_recovery=True turns on the Supervisor's opt-in selective task recovery (P6); nothing else changes."""
    from multi_agent.team import build_team
    from tests.v2.p5 import scripted
    path, tasks = dataset_path(dataset), tasks_for(dataset)
    sup, _ = build_team(port, None, scripted.approve_all, clock=clock, analyst=op_engine.TrustedOpsAnalyst(clock))
    res = sup.run(path, goal=multi_goal(tasks),
                  limits={"selective_plan_recovery": True} if selective_recovery else None)
    f = res["final"]
    claims = f["accepted_claims"] or f["verified_claims_for_review"]
    tstat = f.get("tasks") or {}
    return {"claims": claims, "status": f["status"], "run_complete": f["run_complete"],
            "tasks": {tid: {"attempts": v.get("attempts"), "insight_attempts": v.get("insight_attempts"),
                            "completed": v.get("status") == "completed"} for tid, v in tstat.items()},
            "retries": sum(max(0, (v.get("attempts") or 1) - 1) + max(0, (v.get("insight_attempts") or 1) - 1)
                           for v in tstat.values()),
            "agent_errors": f.get("agent_errors", 0), "handoffs_rejected": f.get("handoffs_rejected", 0),
            # Diagnostics only (added after baseline attempt 3; not used for grading): the exact validation
            # problems the Supervisor fed back, so planner and task failures can be root-caused.
            "diagnostics": diagnostics(res),
            **({"plan_recovery": f.get("plan_recovery")} if selective_recovery else {})}


def diagnostics(res, limit=40):
    """Exact plan/handoff/task problems from the final graph state (validation text only, truncated)."""
    def clip(xs):
        return [str(x)[:300] for x in (xs or [])][:limit]
    lessons = res.get("lessons") or []
    events = res.get("events") or []
    tasks = (res.get("final") or {}).get("tasks") or {}
    return {
        "plan_problems": [{"attempt": l.get("attempt"), "problems": clip(l.get("problems"))}
                          for l in lessons if l.get("stage") == "planning"][:limit],
        "handoff_problems": [{"kind": e.get("kind"), "problems": clip(e.get("problems"))}
                             for e in events if e.get("type") in ("handoff_rejected", "agent_error")][:limit],
        "task_failures": {tid: str(v.get("failure"))[:300] for tid, v in tasks.items() if v.get("failure")},
        # the planner's own rejected replies (model output: task specs only), for replay and root-causing
        "rejected_plans": _rejected_plans(res, events, limit),
    }


def _rejected_plans(res, events, limit):
    bad = {e.get("seq") for e in events if e.get("type") == "handoff_rejected" and e.get("kind") == "plan"}
    return [{"attempt": m.get("attempt"), "plan_json": json.dumps(m.get("payload"), sort_keys=True, default=str)[:6000]}
            for m in (res.get("messages") or []) if m.get("kind") == "plan" and m.get("seq") in bad][:limit]


def required_task_completion(dataset, per_key):
    """(completed, required) for one run: a required task (tasks_for) is complete when every one of its questions
    was answered (reported, correct or not). Denominator: ALL required tasks of the run, whatever happened to the
    plan, so plan_invalid runs and abandoned tasks count as incomplete. Same definition for every architecture."""
    tasks = tasks_for(dataset)
    done = sum(1 for t in tasks if all(per_key.get(k, "missing") != "missing" for k in t["questions"]))
    return done, len(tasks)


RUNNERS = {"single_agent": run_single_agent, "multi_agent": run_multi_agent}


def grade(dataset, claims):
    from tests.v2.golden.compare import values_match
    gold = golden(dataset)
    per = {}
    for k in QUESTIONS[dataset]:
        if k not in claims:
            per[k] = "missing"
        else:
            try:
                per[k] = "correct" if values_match(claims[k], gold[k]) else "incorrect"
            except Exception:
                per[k] = "incorrect"
    primary = [k for k in per if k not in leaked_keys(dataset)]
    return {"per_key": per, "primary_keys": primary,
            "correct_hidden": sum(per[k] == "correct" for k in primary), "total_hidden": len(primary),
            "correct_all": sum(v == "correct" for v in per.values()), "total_all": len(per),
            "incorrect_reported": sum(v == "incorrect" for v in per.values()),
            "reported": sum(v != "missing" for v in per.values())}


@contextlib.contextmanager
def v1_executor_tripwire():
    """For the whole run, any call to a V1 executor raises and is counted (there is no fallback)."""
    import sandbox.executor as v1
    hits = {"n": 0}
    mods = [v1]
    try:
        import sandbox_v2.executor as v2x
        mods.append(v2x)
    except ImportError:
        pass
    saved = [(m, m.execute_code) for m in mods]

    def trip(*a, **k):
        hits["n"] += 1
        raise RuntimeError("V1 executor reached during a trusted-ops live run")
    for m in mods:
        m.execute_code = trip
    try:
        yield hits
    finally:
        for m, fn in saved:
            m.execute_code = fn


def schedule(datasets, reps, seed):
    jobs = [(a, d, r) for d in datasets for r in range(reps) for a in ARCHS]
    random.Random(seed).shuffle(jobs)
    return jobs


def run_benchmark(datasets, reps, make_port, seed=20261124, clock=time.perf_counter, log=print, incremental=None):
    """
    make_port(run_label) -> a BudgetedPort. Stops scheduling new runs as soon as the budget is exhausted or a
    quota/billing stop occurs. `incremental`: a JSON-lines path; every completed run is appended at once.
    """
    results, stopped = [], None
    with v1_executor_tripwire() as trip:
        for i, (arch, ds, rep) in enumerate(schedule(datasets, reps, seed)):
            label = f"{arch}/{ds}/r{rep}"
            port = make_port(label)
            t0 = clock()
            try:
                out = RUNNERS[arch](ds, port, clock)
                error = None
            except (live_llm.BudgetExceeded, live_llm.QuotaOrBillingStop) as e:
                stopped = f"{label}: {e}"
                log(f"stop: {stopped}")
                break
            except Exception as e:                               # recorded, never silently dropped
                out, error = {"claims": {}, "tasks": {}}, f"{type(e).__name__}: {str(e)[:300]}"
            wall = clock() - t0
            if port.exhausted:
                stopped = f"{label}: {getattr(port, 'stop_reason', None) or 'budget or limit reached'} (run excluded)"
                log(f"stop: {stopped}")
                break
            wait = getattr(port, "wait_s", 0.0)
            g = grade(ds, out["claims"])
            rec = {"order": i, "arch": arch, "dataset": ds, "rep": rep, "wall_s": wall, "error": error,
                   "wait_s": wait, "wall_excl_wait_s": max(0.0, wall - wait),
                   "grade": g, "run": {k: v for k, v in out.items() if k != "claims"},
                   "calls": port.calls, "llm_records": port.records, "spent_usd": port.spent_usd,
                   "rate_limited": getattr(port, "rate_limited", 0),
                   "call_source": port.call_source}
            results.append(rec)
            if incremental:
                with open(incremental, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            log(f"{i:3d} {label:28s} {g['correct_hidden']}/{g['total_hidden']} hidden correct, "
                f"{port.calls} calls, ${port.spent_usd:.5f}, {wall:.1f}s{'  ERROR ' + error if error else ''}")
        tripwire_hits = trip["n"]
    return {"harness_version": HARNESS_VERSION, "arch_labels": ARCH_LABELS, "seed": seed, "datasets": list(datasets),
            "reps": reps, "primary_questions": {d: sorted(set(QUESTIONS[d]) - leaked_keys(d)) for d in datasets},
            "results": results, "stopped": stopped, "v1_executor_tripwire_hits": tripwire_hits,
            "summary": summarize(results)}


def _required_rate(rs):
    pairs = [required_task_completion(r["dataset"], r["grade"]["per_key"]) for r in rs]
    total = sum(t for _, t in pairs)
    return round(sum(d for d, _ in pairs) / total, 4) if total else None


def summarize(results):
    out = {}
    for arch in ARCHS:
        rs = [r for r in results if r["arch"] == arch]
        if not rs:
            continue
        recs = [x for r in rs for x in r["llm_records"]]
        tasks = [t for r in rs for t in r["run"].get("tasks", {}).values()]
        ch, th = sum(r["grade"]["correct_hidden"] for r in rs), sum(r["grade"]["total_hidden"] for r in rs)
        rep_n = sum(r["grade"]["reported"] for r in rs)
        correct_all = sum(r["grade"]["correct_all"] for r in rs)
        out[arch] = {
            "runs": len(rs), "run_errors": sum(1 for r in rs if r["error"]),
            "accuracy_hidden": round(ch / th, 4) if th else None, "correct_hidden": ch, "total_hidden": th,
            "accuracy_all": round(correct_all / sum(r["grade"]["total_all"] for r in rs), 4),
            "incorrect_claims_reported": sum(r["grade"]["incorrect_reported"] for r in rs),
            "claim_precision": round(correct_all / rep_n, 4) if rep_n else None,
            "claim_recall": round(correct_all / sum(r["grade"]["total_all"] for r in rs), 4),
            "latency_s": metrics.latency_stats([r["wall_s"] for r in rs]),
            "latency_excl_wait_s": metrics.latency_stats([r.get("wall_excl_wait_s", r["wall_s"]) for r in rs]),
            "wait_s_total": round(sum(r.get("wait_s", 0.0) for r in rs), 2),
            "llm_calls": sum(r["calls"] for r in rs),
            "input_tokens": sum(x["input_tokens"] for x in recs), "output_tokens": sum(x["output_tokens"] for x in recs),
            "total_tokens": sum(x["input_tokens"] + x["output_tokens"] for x in recs),
            "cost_usd": round(sum(r["spent_usd"] for r in rs), 6),
            "calls_charged_worst_case": sum(1 for x in recs if x["charged_worst_case"]),
            "failed_provider_attempts": sum(1 for x in recs if x.get("failed")),
            "provisional_unbilled_429": sum(1 for x in recs if x.get("billing_status") == "provisional_unbilled_429"),
            "failed_http_status": {str(k): sum(1 for x in recs if x.get("failed") and x.get("http_status") == k)
                                   for k in sorted({x.get("http_status") for x in recs if x.get("failed")}, key=str)},
            "provider_retries_429": sum(r.get("rate_limited", 0) for r in rs),
            "agent_retries": sum(r["run"].get("retries", 0) for r in rs),
            "execution_failures": sum(r["run"].get("execution_failures", 0) for r in rs),
            "task_completion_rate": round(sum(1 for t in tasks if t.get("completed")) / len(tasks), 4) if tasks else None,
            "required_task_completion_rate": _required_rate(rs),
            "call_source": sorted({r["call_source"] for r in rs}),
        }
    pairs = {}
    for r in results:
        pairs.setdefault((r["dataset"], r["rep"]), {})[r["arch"]] = r
    both = [p for p in pairs.values() if set(p) == set(ARCHS) and not any(x["error"] for x in p.values())]
    out["paired_latency_single_vs_multi"] = paired.paired_latency(
        [(p["single_agent"]["wall_s"], p["multi_agent"]["wall_s"]) for p in both])
    out["paired_latency_excl_wait_single_vs_multi"] = paired.paired_latency(
        [(p["single_agent"].get("wall_excl_wait_s", p["single_agent"]["wall_s"]),
          p["multi_agent"].get("wall_excl_wait_s", p["multi_agent"]["wall_s"])) for p in both])
    return out


# ── P6 optimisation experiment: selective task recovery vs a contemporaneous control ─────────────────────

VARIANTS = ("selective_recovery", "control")
RUN_RESERVE_USD = 0.010          # conservative reservation per multi-agent run when deciding whether to add a control


def run_recovery_experiment(make_port, seed=20261124, clock=time.perf_counter, log=print, incremental=None,
                            include_control=True, reserve_per_run=RUN_RESERVE_USD):
    """
    The baseline's 20 multi-agent (dataset, rep) slots in the baseline's seeded order. Each slot runs the optimised
    variant (selective task recovery ON) and, budget permitting, a matched control (OFF: the baseline code path),
    adjacent and in a seeded random order, so provider load affects both alike. A control runs only if the
    remaining ledger budget still reserves `reserve_per_run` for every remaining optimised run.
    """
    slots = [(ds, rep) for arch, ds, rep in schedule(DATASETS, 5, seed) if arch == "multi_agent"]
    rng = random.Random(seed + 1)
    results, stopped, skipped_controls, order = [], None, [], 0
    with v1_executor_tripwire() as trip:
        for si, (ds, rep) in enumerate(slots):
            variants = list(VARIANTS) if include_control else ["selective_recovery"]
            rng.shuffle(variants)
            for variant in variants:
                label = f"{variant}/{ds}/r{rep}"
                port = make_port(label)
                if variant == "control":
                    remaining = port.budget_usd - port.ledger.spent_usd()
                    optimised_pending_here = "selective_recovery" in variants[variants.index(variant) + 1:]
                    left = (len(slots) - si - 1) + (1 if optimised_pending_here else 0)
                    if remaining - reserve_per_run < left * reserve_per_run:
                        skipped_controls.append(label)
                        log(f"skip: {label} (budget reserved for the optimised runs)")
                        continue
                t0, error = clock(), None
                try:
                    out = run_multi_agent(ds, port, clock, selective_recovery=(variant == "selective_recovery"))
                except (live_llm.BudgetExceeded, live_llm.QuotaOrBillingStop) as e:
                    stopped = f"{label}: {e}"
                    log(f"stop: {stopped}")
                    break
                except Exception as e:                                       # recorded, never hidden
                    out, error = {"claims": {}, "tasks": {}}, f"{type(e).__name__}: {str(e)[:300]}"
                wall = clock() - t0
                if port.exhausted:
                    stopped = f"{label}: {getattr(port, 'stop_reason', None) or 'budget or limit reached'} (run excluded)"
                    log(f"stop: {stopped}")
                    break
                wait = getattr(port, "wait_s", 0.0)
                g = grade(ds, out["claims"])
                rec = {"order": order, "slot": si, "variant": variant, "arch": "multi_agent", "dataset": ds,
                       "rep": rep, "wall_s": wall, "error": error, "wait_s": wait,
                       "wall_excl_wait_s": max(0.0, wall - wait), "grade": g,
                       "run": {k: v for k, v in out.items() if k != "claims"}, "calls": port.calls,
                       "llm_records": port.records, "spent_usd": port.spent_usd,
                       "rate_limited": getattr(port, "rate_limited", 0), "call_source": port.call_source}
                order += 1
                results.append(rec)
                if incremental:
                    with open(incremental, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                pr = (out.get("plan_recovery") or {})
                log(f"{rec['order']:3d} {label:36s} {str(out.get('status')):24s} {g['correct_hidden']}/{g['total_hidden']} "
                    f"hidden, {port.calls} calls, ${port.spent_usd:.5f}, {wall:.1f}s"
                    + (f", recovered {pr.get('recovered_task_ids')}, abandoned "
                       f"{[a['id'] for a in pr.get('abandoned_tasks') or []]}" if pr else ""))
            if stopped:
                break
        hits = trip["n"]
    return {"harness_version": HARNESS_VERSION, "experiment": "selective_task_recovery", "seed": seed,
            "slots": [list(x) for x in slots], "results": results, "stopped": stopped,
            "skipped_controls": skipped_controls, "v1_executor_tripwire_hits": hits,
            "summary": summarize_variants(results)}


def _provider_problem(problems):
    return any("ProviderError" in str(p) for p in problems)


def variant_metrics(rs):
    """The P6 optimisation metrics for one group of multi-agent runs (baseline, control or optimised)."""
    if not rs:
        return None
    recs = [x for r in rs for x in r["llm_records"]]
    ok = [x for x in recs if not x.get("failed")]
    failed = [x for x in recs if x.get("failed")]
    ch, th = sum(r["grade"]["correct_hidden"] for r in rs), sum(r["grade"]["total_hidden"] for r in rs)
    req = [required_task_completion(r["dataset"], r["grade"]["per_key"]) for r in rs]
    hp = [h for r in rs for h in (r["run"].get("diagnostics") or {}).get("handoff_problems", [])]
    rec = [r["run"].get("plan_recovery") or {} for r in rs]
    return {
        "runs": len(rs), "run_errors": sum(1 for r in rs if r["error"]),
        "accuracy_hidden": round(ch / th, 4) if th else None, "correct_hidden": ch, "total_hidden": th,
        "complete_planner_failures": sum(1 for r in rs if r["run"].get("status") == "plan_invalid"),
        "complete_planner_failure_rate": round(sum(1 for r in rs if r["run"].get("status") == "plan_invalid")
                                               / len(rs), 4),
        "required_tasks_completed": sum(d for d, _ in req), "required_tasks": sum(t for _, t in req),
        "required_task_completion_rate": round(sum(d for d, _ in req) / sum(t for _, t in req), 4),
        "incorrect_claims_reported": sum(r["grade"]["incorrect_reported"] for r in rs),
        "recovered_tasks": sum(len(x.get("recovered_task_ids") or []) for x in rec),
        "abandoned_tasks": sum(len(x.get("abandoned_tasks") or []) for x in rec),
        "runs_with_recovery_triggered": sum(1 for x in rec if x.get("recovered_task_ids") or x.get("abandoned_tasks")),
        "agent_retries_task": sum(r["run"].get("retries", 0) for r in rs),
        "planner_calls": sum(1 for x in recs if x["skill"] == "planner"),
        "provider_failures": len(failed),
        "provider_failures_by_status": {str(k): sum(1 for x in failed if x.get("http_status") == k)
                                        for k in sorted({x.get("http_status") for x in failed}, key=str)},
        "agent_validation_rejections": sum(1 for h in hp if h.get("kind") is not None and not _provider_problem(h["problems"])),
        "agent_output_errors": sum(1 for h in hp if h.get("kind") is None and not _provider_problem(h["problems"])),
        "successful_calls": len(ok),
        "provider_tokens_in": sum(x["input_tokens"] for x in ok), "provider_tokens_out": sum(x["output_tokens"] for x in ok),
        "provider_tokens_total": sum(x["input_tokens"] + x["output_tokens"] for x in ok),
        "cost_usd_total_estimate": round(sum(x["cost_usd"] for x in recs), 6),
        "cost_usd_provider_reported": round(sum(x["cost_usd"] for x in ok), 6),
        "cost_usd_worst_case_failed_attempts": round(sum(x["cost_usd"] for x in failed), 6),
        "latency_s": metrics.latency_stats([r["wall_s"] for r in rs]),
        "latency_excl_wait_s": metrics.latency_stats([r["wall_excl_wait_s"] for r in rs]),
        "call_source": sorted({r["call_source"] for r in rs}),
    }


def summarize_variants(results):
    out = {v: variant_metrics([r for r in results if r.get("variant") == v]) for v in VARIANTS}
    by = {}
    for r in results:
        by.setdefault(r["slot"], {})[r["variant"]] = r
    both = [p for p in by.values() if set(p) == set(VARIANTS) and not any(x["error"] for x in p.values())]
    out["matched_pairs"] = len(both)
    out["paired_latency_control_vs_selective"] = paired.paired_latency(
        [(p["control"]["wall_s"], p["selective_recovery"]["wall_s"]) for p in both])
    out["paired_latency_excl_wait_control_vs_selective"] = paired.paired_latency(
        [(p["control"]["wall_excl_wait_s"], p["selective_recovery"]["wall_excl_wait_s"]) for p in both])
    out["paired_hidden_correct"] = {"control": sum(p["control"]["grade"]["correct_hidden"] for p in both),
                                    "selective_recovery": sum(p["selective_recovery"]["grade"]["correct_hidden"]
                                                              for p in both),
                                    "of": sum(p["control"]["grade"]["total_hidden"] for p in both)}
    return out


def markdown(bench) -> str:
    s = bench["summary"]
    src = {a: s[a]["call_source"] for a in ARCHS if a in s}
    live = all(v == [PROVIDER] for v in src.values())
    lines = [f"# Live benchmark ({'PROVIDER calls' if live else 'OFFLINE / MOCK provider: not live results'})", "",
             f"- single_agent = {ARCH_LABELS['single_agent']}", f"- multi_agent = {ARCH_LABELS['multi_agent']}",
             "- accuracy_hidden: questions whose answers appear in no prompt (profile-metadata coincidences excluded); "
             "accuracy_all: all 28", "- cost_usd: provider-reported tokens x list prices (an ESTIMATE, not billing)",
             f"- harness {bench['harness_version']}; seed {bench['seed']}; datasets {bench['datasets']}; reps {bench['reps']}",
             f"- stopped early: {bench['stopped']}", f"- V1 executor tripwire hits: {bench['v1_executor_tripwire_hits']}", "",
             "| metric | single_agent | multi_agent |", "|---|---|---|"]
    keys = ["runs", "run_errors", "failed_provider_attempts", "failed_http_status", "provider_retries_429",
            "provisional_unbilled_429", "agent_retries", "wait_s_total",
            "accuracy_hidden", "accuracy_all", "incorrect_claims_reported", "claim_precision",
            "claim_recall", "llm_calls", "input_tokens", "output_tokens", "total_tokens", "cost_usd",
            "execution_failures", "task_completion_rate", "required_task_completion_rate"]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(s.get(a, {}).get(k)) for a in ARCHS) + " |")
    for a in ARCHS:
        if a in s:
            lines.append(f"| latency p50/p95 ms ({a}) | {s[a]['latency_s']['median_ms']} / {s[a]['latency_s']['p95_ms']} | |")
            lines.append(f"| latency excl. pacing/rate-limit waits p50/p95 ms ({a}) | "
                         f"{s[a]['latency_excl_wait_s']['median_ms']} / {s[a]['latency_excl_wait_s']['p95_ms']} | |")
    lines += ["", f"Paired latency (single -> multi): {s.get('paired_latency_single_vs_multi')}",
              f"Paired latency excl. waits (single -> multi): {s.get('paired_latency_excl_wait_single_vs_multi')}"]
    return "\n".join(lines) + "\n"


# ── offline fake answers (oracle from the reviewed golden specs; for dry runs and tests only) ─────

def oracle_answer(dataset):
    from tests.v2.p5 import scripted
    specs = scripted.CLAIM_SPECS[dataset]

    def spec(k):
        op, a = specs[k]
        return {"key": k, "op": op, "args": dict(a)}

    def answer(skill, system, user):
        if skill == "single_agent_claims":
            qs = json.loads(user.split("REQUIRED QUESTIONS JSON (key -> question):\n", 1)[1].split("\n", 1)[0])
            return json.dumps({"claims": [spec(k) for k in qs]})
        if skill == "planner":
            tasks = json.loads(user.split("TASKS JSON: ", 1)[1].split("\n", 1)[0])
            return json.dumps({"tasks": [{"id": t["id"], "title": t["title"], "description": t["description"],
                                          "analysis_type": t["analysis_type"],
                                          "claims": [spec(k) for k in t["questions"]]} for t in tasks]})
        keys = list(json.loads(user.split("VERIFIED CLAIMS JSON: ", 1)[1].split("\n", 1)[0]))
        return json.dumps({"insights": [{"text": "The verified figures summarise this task.", "cites": keys[:1]}]})
    return answer


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=("fake", "gemini"), required=True)
    ap.add_argument("--mode", choices=("pilot", "full"), required=True)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20261124)
    ap.add_argument("--model", default=live_llm.DEFAULT_MODEL)
    ap.add_argument("--budget-usd", type=float, default=0.25)
    ap.add_argument("--max-calls", type=int, default=400)
    ap.add_argument("--max-total-tokens", type=int, default=3_000_000)
    ap.add_argument("--max-output-tokens", type=int, default=live_llm.DEFAULT_MAX_OUTPUT_TOKENS)
    ap.add_argument("--ledger", default=live_llm.DEFAULT_LEDGER)
    ap.add_argument("--rate-limit-retries", type=int, default=0, help="explicit, recorded retries on HTTP 429")
    ap.add_argument("--charge-429", choices=("worst_case", "zero"), default="worst_case")
    ap.add_argument("--min-interval-s", type=float, default=0.0, help="proactive pacing between call starts")
    ap.add_argument("--transport", choices=("openai_compat", "native"), default="openai_compat",
                    help="openai_compat: Bearer key injected by the session proxy; native: x-goog-api-key SDK")
    ap.add_argument("--prices-confirmed", action="store_true")
    ap.add_argument("--i-understand-this-makes-live-calls", action="store_true")
    ap.add_argument("--out", required=True)
    ap.add_argument("--experiment", choices=("none", "selective_recovery"), default="none",
                    help="selective_recovery: the 20 baseline multi-agent slots with recovery ON, each paired with a "
                         "contemporaneous control (OFF) when the budget allows")
    ap.add_argument("--no-control", action="store_true", help="with --experiment: run the optimised variant only")
    a = ap.parse_args(argv)
    if a.budget_usd > 5.0:
        raise SystemExit("the authorized budget is at most $5.00")
    datasets, reps = (PILOT["datasets"], PILOT["reps"]) if a.mode == "pilot" else (DATASETS, a.reps)
    os.makedirs(a.out, exist_ok=True)
    if a.provider == "gemini":
        if not (a.prices_confirmed and a.i_understand_this_makes_live_calls):
            raise SystemExit("live calls need --prices-confirmed and --i-understand-this-makes-live-calls; "
                             f"prices used: {live_llm.PRICES_USD_PER_MTOK[a.model]} USD per 1M tokens (in, out)")
        ledger = live_llm.Ledger(a.ledger)
        if ledger.spent_usd() >= a.budget_usd:
            raise SystemExit(f"ledger already records ${ledger.spent_usd():.4f} of the ${a.budget_usd:.2f} budget")
        pacer = live_llm.Pacer(a.min_interval_s)
        inner = (live_llm.GeminiOpenAICompatPort(a.model, a.max_output_tokens) if a.transport == "openai_compat"
                 else live_llm.GeminiPort(a.model, a.max_output_tokens))
        if a.transport == "openai_compat":
            available = inner.list_models()
            if not any(m.split("/")[-1] == a.model for m in available):
                raise SystemExit(f"model {a.model!r} is not available to this key; available: {available[:40]}")
        # A listed model may still refuse generation (pilot 1: gemini-2.5-flash-lite "no longer available to new
        # users"). One tiny budgeted generation probe proves availability before any benchmark run.
        probe = live_llm.BudgetedPort(inner, model=a.model, budget_usd=a.budget_usd, ledger=ledger, max_calls=1,
                                      max_total_tokens=a.max_total_tokens, max_output_tokens=a.max_output_tokens,
                                      run_label="preflight_probe")
        try:
            probe.complete("preflight", "Reply with the single word OK.", "OK?")
        except (live_llm.ProviderError, live_llm.QuotaOrBillingStop) as e:
            raise SystemExit(f"model {a.model!r} cannot generate for this key: {e}")
        print(f"model {a.model} generated a reply (preflight probe ${probe.spent_usd:.6f})")
        make = lambda label: live_llm.BudgetedPort(inner, model=a.model, budget_usd=a.budget_usd, ledger=ledger,  # noqa: E731
                                                   max_calls=a.max_calls, max_total_tokens=a.max_total_tokens,
                                                   max_output_tokens=a.max_output_tokens, run_label=label,
                                                   rate_limit_retries=a.rate_limit_retries, charge_429=a.charge_429,
                                                   pacer=pacer)
    else:
        ledger = live_llm.Ledger(os.path.join(a.out, "dry_run_ledger.jsonl"))       # never the live ledger
        make = lambda label: live_llm.BudgetedPort(  # noqa: E731
            live_llm.FakeProvider(oracle_answer(label.split("/")[1])), model=a.model, budget_usd=a.budget_usd,
            ledger=ledger, max_calls=a.max_calls, max_total_tokens=a.max_total_tokens,
            max_output_tokens=a.max_output_tokens, run_label=label)
    if a.experiment == "selective_recovery":
        exp = run_recovery_experiment(make, seed=a.seed, incremental=os.path.join(a.out, "runs.jsonl"),
                                      include_control=not a.no_control)
        exp.update(provider=a.provider, model=a.model, budget_usd=a.budget_usd,
                   prices_usd_per_mtok=list(live_llm.PRICES_USD_PER_MTOK[a.model]))
        with open(os.path.join(a.out, "recovery_experiment.json"), "w", encoding="utf-8") as f:
            json.dump(exp, f, indent=1, sort_keys=True, default=str)
        print(json.dumps(exp["summary"], indent=1, sort_keys=True, default=str))
        print(f"stopped early: {exp['stopped']}; skipped controls: {len(exp['skipped_controls'])}; "
              f"V1 executor tripwire hits: {exp['v1_executor_tripwire_hits']}")
        return 0 if not exp["stopped"] and exp["v1_executor_tripwire_hits"] == 0 else 1
    bench = run_benchmark(datasets, reps, make, seed=a.seed, incremental=os.path.join(a.out, "runs.jsonl"))
    bench.update(provider=a.provider, model=a.model, mode=a.mode, budget_usd=a.budget_usd,
                 prices_usd_per_mtok=list(live_llm.PRICES_USD_PER_MTOK[a.model]))
    with open(os.path.join(a.out, "live_benchmark.json"), "w", encoding="utf-8") as f:
        json.dump(bench, f, indent=1, sort_keys=True, default=str)
    with open(os.path.join(a.out, "live_benchmark.md"), "w", encoding="utf-8") as f:
        f.write(markdown(bench))
    print(markdown(bench))
    return 0 if not bench["stopped"] and bench["v1_executor_tripwire_hits"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
