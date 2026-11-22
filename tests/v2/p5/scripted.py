"""
Scripted agent replies for the P5.0 multi-agent prototype, with fault injection.

What this proves and what it does not:
- The scripted "LLM" answers each agent from its prompt only (it parses the
  prompt the agent built), so the Supervisor, contracts, execution ports and
  Verification Agent are exercised exactly as with a model.
- The analyst code is the hand-written golden pandas code (tests/v2/golden/
  scenarios.py) with GOLDEN lines renamed to CLAIM lines. It says nothing about
  real model quality, provider tokens or cost.
- Faults are injected deliberately (wrong values, missing or malformed claims,
  ungrounded insights, invalid plans, ...) so detection, rejection and bounded
  retry can be tested against labelled ground truth.

Two execution modes:
- "subprocess": the generated code runs through a real ExecutionPort (the V1
  subprocess or the P4.1 lazy-seaborn subprocess).
- "literal": fast mode for orchestration tests. The scripted code consists only
  of print("CLAIM ...") / fail("...") calls built from the committed golden
  answers, and LiteralPrintExecution interprets just those two statement forms
  (after V1's AST safety scan). Nothing is executed.
"""

import ast
import inspect
import json
import re

from multi_agent.execution import CodeTrust, ExecutionOutcome, ExecutionPort
from multi_agent.llm import MOCK
from tests.v2.golden import scenarios as sc
from tests.v2.golden.reference import load_expected

# ── the plan: golden tasks with declarative claims ───────────────────────────

CLAIM_SPECS = {
    "sales": {
        "row_count": ("count_rows", {}),
        "total_sales_amount": ("sum", {"column": "sales_amount"}),
        "total_returns": ("sum", {"column": "returns"}),
        "mean_customer_satisfaction": ("mean", {"column": "customer_satisfaction"}),
        "sales_by_region": ("group_sum", {"by": "region", "column": "sales_amount"}),
        "top_region": ("argmax_group_sum", {"by": "region", "column": "sales_amount"}),
        "target_attainment_pct": ("ratio_of_sums_pct", {"numerator": "sales_amount", "denominator": "sales_target"}),
    },
    "attendance": {
        "row_count": ("count_rows", {}),
        "status_counts": ("value_counts", {"column": "status"}),
        "present_rate": ("rate_where_equals", {"column": "status", "value": "Present"}),
        "mean_hours_worked": ("mean", {"column": "hours_worked"}),
        "mean_hours_by_department": ("group_mean", {"by": "department", "column": "hours_worked"}),
        "top_department_by_mean_hours": ("argmax_group_mean", {"by": "department", "column": "hours_worked"}),
        "gps_mismatch_count": ("count_where_equals", {"column": "gps_location_match", "value": "False"}),
    },
    "student": {
        "row_count": ("count_rows", {}),
        "pass_rate": ("rate_where_equals", {"column": "passed", "value": "True"}),
        "grade_letter_counts": ("value_counts", {"column": "grade_letter"}),
        "mean_score": ("mean", {"column": "score"}),
        "mean_score_by_subject": ("group_mean", {"by": "subject", "column": "score"}),
        "top_subject_by_mean_score": ("argmax_group_mean", {"by": "subject", "column": "score"}),
        "corr_study_hours_score": ("pearson", {"x": "study_hours_per_day", "y": "score"}),
    },
    "ecommerce": {
        "row_count": ("count_rows", {}),
        "total_revenue": ("sum", {"column": "revenue"}),
        "total_orders": ("sum", {"column": "orders"}),
        "mean_revenue_weekend": ("mean_where_equals", {"column": "revenue", "where_column": "is_weekend", "value": "True"}),
        "mean_revenue_weekday": ("mean_where_equals", {"column": "revenue", "where_column": "is_weekend", "value": "False"}),
        "top_month_by_revenue": ("argmax_group_sum", {"by": "date", "column": "revenue", "by_prefix": 7}),
        "corr_visitors_revenue": ("pearson", {"x": "website_visitors", "y": "revenue"}),
    },
}
# The adversarial sales task's code always tries `import os`; its claim is a placeholder it never reaches.
ADVERSARIAL = {"sales_t4": [{"key": "inventory_file_count", "op": "count_rows", "args": {}}]}
_EMITS = re.compile(r'emit\("(\w+)"')


def task_claim_keys(dataset, task):
    attempts = sc.SCENARIOS[dataset]["code"][task["title"]]
    keys = _EMITS.findall(attempts[-1])
    return [k for k in keys if k in CLAIM_SPECS[dataset]]


def plan_for(dataset) -> dict:
    tasks = []
    for t in sc.SCENARIOS[dataset]["tasks"]:
        claims = ADVERSARIAL.get(t["id"]) or [
            {"key": k, "op": CLAIM_SPECS[dataset][k][0], "args": dict(CLAIM_SPECS[dataset][k][1])}
            for k in task_claim_keys(dataset, t)]
        tasks.append({"id": t["id"], "title": t["title"], "description": t["description"],
                      "analysis_type": t["analysis_type"], "claims": claims})
    return {"tasks": tasks}


def golden_answers(dataset) -> dict:
    return load_expected(dataset)["answers"]


# ── fault rules: ONE implementation, used in-process (literal mode) and embedded in subprocess code ──

def fault_lines(key, value, rule):
    """CLAIM lines to print for one claim under a fault rule (self-contained; uses only json)."""
    kind = rule[0]
    line = lambda k, v: "CLAIM " + k + "=" + json.dumps(v)  # noqa: E731
    if kind == "scale":
        return [line(key, value * rule[1])]
    if kind == "add":
        return [line(key, value + rule[1])]
    if kind == "replace":
        return [line(key, rule[1])]
    if kind == "drop":
        return []
    if kind == "raw":
        return ["CLAIM " + key + "=" + rule[1]]
    if kind == "nan":
        return [line(key, float("nan"))]
    if kind == "duplicate":
        return [line(key, value), line(key, rule[1])]
    if kind == "dict_scale":
        d = dict(value)
        d[rule[1]] = d[rule[1]] * rule[2]
        return [line(key, d)]
    if kind == "dict_extra":
        d = dict(value)
        d[rule[1]] = rule[2]
        return [line(key, d)]
    if kind == "extra_key":
        return [line(key, value), line(rule[1], rule[2])]
    raise ValueError("unknown fault rule " + str(kind))


_FAULT_SRC = inspect.getsource(fault_lines)


def _subprocess_code(dataset, title, attempt, rules):
    attempts = sc.SCENARIOS[dataset]["code"][title]
    code = attempts[min(attempt - 1, len(attempts) - 1)].replace('"GOLDEN "', '"CLAIM "')
    if not rules:
        return code
    head, body = code.split("\n\n", 1)          # the emit preamble, then the task body
    inject = (_FAULT_SRC + f"\n_RULES = {rules!r}\n_emit_ok = emit\n\n"
              "def emit(key, value):\n"
              "    if key not in _RULES:\n"
              "        return _emit_ok(key, value)\n"
              "    for _line in fault_lines(key, value, _RULES[key]):\n"
              "        print(_line)\n")
    return head + "\n\n" + inject + "\n" + body


# golden code attempts that fail by design (sales_t1 attempt 1 uses a wrong column name)
LITERAL_FAILURES = {("sales", "Sales Overview", 1): "KeyError: 'Sales'"}


def _literal_code(dataset, task_id, title, attempt, keys, rules):
    if task_id in ADVERSARIAL:
        return sc.SCENARIOS[dataset]["code"][title][-1]          # real adversarial code: blocked by the scan
    fail = LITERAL_FAILURES.get((dataset, title, attempt))
    if fail:
        return f"fail({fail!r})\n"
    answers = golden_answers(dataset)
    lines = []
    for k in keys:
        lines += fault_lines(k, answers[k], rules[k]) if k in rules else ["CLAIM " + k + "=" + json.dumps(answers[k])]
    return "".join(f"print({ln!r})\n" for ln in lines)


# ── the scripted model ───────────────────────────────────────────────────────

class MockReply:
    def __init__(self, content):
        self.content = content


class ScriptedAgentLLM:
    """
    Faults (all optional):
      plan:     {attempt: callable(plan) -> plan or str (raw reply)}
      analysis: {task_id: {attempt: {claim_key: rule}}}       rule: see fault_lines
      insight:  {task_id: {attempt: mode}}  mode: cite_unknown | cite_other_task | ungrounded_number |
                                                  no_cites | bad_json | empty
    """
    call_source = MOCK
    code_trust = CodeTrust.SCRIPTED_FIXTURES

    def __init__(self, dataset, faults=None, mode="subprocess"):
        self.dataset, self.faults, self.mode = dataset, faults or {}, mode
        self.prompts = []

    def complete(self, skill, system, user):
        self.prompts.append((skill, user))
        return MockReply(getattr(self, "_" + skill)(user))

    @staticmethod
    def _field(prompt, name):
        m = re.search(rf"^{name}: (.*)$", prompt, re.M)
        if not m:
            raise LookupError(f"prompt has no {name} line")
        return m.group(1).strip()

    def _planner(self, prompt):
        attempt = int(self._field(prompt, "PLANNING ATTEMPT"))
        plan = plan_for(self.dataset)
        fault = self.faults.get("plan", {}).get(attempt)
        if fault:
            plan = fault(plan)
        return plan if isinstance(plan, str) else json.dumps(plan)

    def _data_analyst(self, prompt):
        tid, title = self._field(prompt, "TASK ID"), self._field(prompt, "TASK")
        attempt = int(self._field(prompt, "ATTEMPT"))
        keys = [c["key"] for c in json.loads(self._field(prompt, "CLAIMS JSON"))]
        rules = self.faults.get("analysis", {}).get(tid, {}).get(attempt, {})
        if self.mode == "literal":
            return _literal_code(self.dataset, tid, title, attempt, keys, rules)
        return _subprocess_code(self.dataset, title, attempt, rules)

    def _business_insight(self, prompt):
        tid = self._field(prompt, "TASK ID")
        attempt = int(self._field(prompt, "ATTEMPT"))
        verified = json.loads(self._field(prompt, "VERIFIED CLAIMS JSON"))
        mode = self.faults.get("insight", {}).get(tid, {}).get(attempt)
        if mode == "bad_json":
            return "Revenue looks great!"
        insights = [insight_for(k, v) for k, v in sorted(verified.items())][:3]
        if mode == "empty":
            insights = []
        elif mode == "cite_unknown":
            insights.append({"text": "Customer churn fell sharply.", "cites": ["churn_rate"]})
        elif mode == "cite_other_task":
            other = next(k for k in CLAIM_SPECS[self.dataset] if k not in verified)
            insights.append({"text": "This also holds across the business.", "cites": [other]})
        elif mode == "ungrounded_number":
            insights[0] = {"text": insights[0]["text"] + " Growth was 37.5% year on year.",
                           "cites": insights[0]["cites"]}
        elif mode == "no_cites":
            insights[0] = {"text": insights[0]["text"], "cites": []}
        return json.dumps({"insights": insights})


def _fmt(v):
    return f"{v:,}" if isinstance(v, int) else f"{v:,.2f}"


def insight_for(key, value):
    label = key.replace("_", " ")
    if isinstance(value, dict):
        top = max(sorted(value), key=lambda k: value[k])
        text = f"For {label}, the highest is {top} at {_fmt(value[top])}."
    elif isinstance(value, str):
        text = f"The {label} is {value}."
    else:
        text = f"The {label} is {_fmt(value)}."
    return {"text": text, "cites": [key]}


# ── test doubles ─────────────────────────────────────────────────────────────

class LiteralPrintExecution(ExecutionPort):
    """Test double: interprets only print("...") and fail("...") with string literals. Executes nothing."""
    name = "literal-print (test double; executes nothing)"
    isolation = "not applicable: no code is executed"
    hardened = False

    def __init__(self):
        self.calls = []

    def execute(self, code, dataset_path, task_id, dataset_sha256=None):
        from agent.rules_engine import scan_code_safety
        self.calls.append(task_id)
        out = dict(stdout="", error="", error_type="", chart_path="", backend=self.name,
                   isolation=self.isolation, seconds=0.0, blocked_by_safety_scan=False)
        violation = scan_code_safety(code)
        if violation:
            return ExecutionOutcome(ok=False, **{**out, "error": violation, "error_type": "runtime_error",
                                                 "blocked_by_safety_scan": True})
        printed = []
        for stmt in ast.parse(code).body:
            call = stmt.value if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) else None
            ok_form = (call is not None and isinstance(call.func, ast.Name) and call.func.id in ("print", "fail")
                       and len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
                       and isinstance(call.args[0].value, str) and not call.keywords)
            if not ok_form:
                return ExecutionOutcome(ok=False, **{**out, "error": "unsupported statement", "error_type": "logic_error"})
            if call.func.id == "fail":
                return ExecutionOutcome(ok=False, **{**out, "error": "Traceback (most recent call last):\n"
                                                     + call.args[0].value, "error_type": "data_error"})
            printed.append(call.args[0].value)
        return ExecutionOutcome(ok=True, **{**out, "stdout": "\n".join(printed)})


def approve_all(request):
    return {"decision": "approve"}


def reject_all(request):
    return {"decision": "reject", "reviewer_note": "not needed"}


def drop_last_task(request):
    """The V1 HITL 'edit' path drops the last task; the same edit here."""
    return {"decision": "edit", "keep_task_ids": [t["id"] for t in request["plan"]["tasks"][:-1]]}
