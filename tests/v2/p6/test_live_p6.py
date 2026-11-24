"""
P6 offline tests: trusted-ops engine, budget guard, key safety, hidden golden answers, the live benchmark runner.

No network, no API key, no container. FakeProvider responses are labelled MOCK and never reported as live.
"""

import ast
import json
import os
import re
from types import SimpleNamespace

import pytest

from evaluation import live
from multi_agent import dataset as dsmod
from multi_agent import live_llm, op_engine, verification
from tests.v2.golden.compare import values_match
from tests.v2.golden.reference import ROOT
from tests.v2.p5 import scripted

KEY = "AIzaFAKEKEY-for-tests-only-0123456789"


# ── trusted-ops engine ────────────────────────────────────────────────────────

@pytest.mark.parametrize("ds", live.DATASETS)
def test_engine_verifier_and_golden_agree_on_every_claim(ds):
    path = live.dataset_path(ds)
    d = dsmod.load(path)
    gold = scripted.golden_answers(ds)
    specs = [{"key": k, "op": op, "args": dict(a)} for k, (op, a) in scripted.CLAIM_SPECS[ds].items()]
    values, errors = op_engine.compute_claims(specs, path, dsmod.profile(d))
    assert errors == {}
    for s in specs:
        assert values_match(values[s["key"]], gold[s["key"]]), s
        assert values_match(verification.recompute(s, d)[0], gold[s["key"]]), s


def test_invalid_specs_become_errors_and_are_never_computed():
    path = live.dataset_path("sales")
    prof = dsmod.profile(dsmod.load(path))
    values, errors = op_engine.compute_claims([
        {"key": "a", "op": "sum", "args": {"column": "no_such_column"}},
        {"key": "b", "op": "sum", "args": {"column": "region"}},
        {"key": "c", "op": "drop_table", "args": {}},
        {"key": "d", "op": "count_where_equals", "args": {"column": "region", "value": "__import__('os')"}},
    ], path, prof)
    assert set(errors) == {"a", "b", "c"} and values == {"d": 0}      # a value is data: compared, never evaluated


@pytest.mark.parametrize("rel", ["multi_agent/op_engine.py", "multi_agent/live_llm.py", "evaluation/live.py"])
def test_no_code_execution_or_process_creation_in_the_live_path(rel):
    tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
        {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not names & {"exec", "eval", "compile", "__import__"}
    assert not attrs & {"system", "popen", "Popen", "spawn", "fork", "execv", "execve", "run_path"}
    assert not imported & {"subprocess", "multiprocessing", "ctypes", "runpy"}


def test_the_trusted_analyst_has_no_llm_and_no_execution_port():
    a = op_engine.TrustedOpsAnalyst()
    assert not hasattr(a, "llm") and not hasattr(a, "execution") and a.spec.tools == ("trusted_ops",)
    from multi_agent.team import build_team
    with pytest.raises(ValueError):
        build_team(live_llm.FakeProvider(lambda *x: "{}"), object(), analyst=a)


# ── budget guard and ledger ───────────────────────────────────────────────────

class _Inner:
    call_source, code_trust = "provider", "untrusted-model-output"

    def __init__(self, usage=None, text="{}"):
        self.n, self.usage, self.text = 0, usage, text

    def complete(self, skill, system, user):
        self.n += 1
        return SimpleNamespace(content=self.text, usage_metadata=self.usage)


def _port(tmp_path, inner, **kw):
    args = dict(model="gemini-2.5-flash-lite", budget_usd=1.0, ledger=live_llm.Ledger(str(tmp_path / "l.jsonl")),
                max_calls=10, max_total_tokens=10 ** 6, max_output_tokens=1000)
    args.update(kw)
    return live_llm.BudgetedPort(inner, **args)


def test_a_call_that_could_exceed_the_budget_is_refused_before_it_is_made(tmp_path):
    inner = _Inner({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    p = _port(tmp_path, inner, budget_usd=0.0001)              # worst case of 1000 output tokens = $0.0004
    with pytest.raises(live_llm.BudgetExceeded, match="budget"):
        p.complete("s", "sys", "user")
    assert inner.n == 0 and p.exhausted and p.records == []


def test_call_and_token_limits_are_hard(tmp_path):
    inner = _Inner({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    p = _port(tmp_path, inner, max_calls=2)
    p.complete("s", "a", "b"); p.complete("s", "a", "b")
    with pytest.raises(live_llm.BudgetExceeded, match="call limit"):
        p.complete("s", "a", "b")
    q = _port(tmp_path, _Inner(), max_total_tokens=500)
    with pytest.raises(live_llm.BudgetExceeded, match="token limit"):
        q.complete("s", "a", "b")


def test_provider_usage_is_recorded_and_reasoning_tokens_are_billed(tmp_path):
    p = _port(tmp_path, _Inner({"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1300}))
    p.complete("planner", "sys", "user")
    r = p.records[0]
    assert (r["input_tokens"], r["output_tokens"], r["charged_worst_case"]) == (1000, 300, False)
    assert r["cost_usd"] == pytest.approx(1000 / 1e6 * 0.10 + 300 / 1e6 * 0.40)


def test_a_failed_provider_attempt_is_charged_the_worst_case_and_counted(tmp_path):
    class Failing(_Inner):
        def complete(self, *a):
            self.n += 1
            raise live_llm.ProviderError("ReadTimeout: timed out")
    p = _port(tmp_path, Failing())
    with pytest.raises(live_llm.ProviderError):
        p.complete("s", "x" * 300, "y")
    r = p.records[0]
    assert r["failed"] == "ProviderError" and r["charged_worst_case"] and r["input_tokens"] == 301
    assert p.spent_usd == pytest.approx(301 / 1e6 * 0.10 + 1000 / 1e6 * 0.40) and p.calls == 1
    assert live_llm.Ledger(str(tmp_path / "l.jsonl")).spent_usd() == pytest.approx(p.spent_usd)


def test_the_gemini_client_makes_exactly_one_http_attempt_per_call():
    port = live_llm.GeminiPort(env={"GOOGLE_API_KEY": KEY})
    assert port._chat.max_retries == 1                       # -> HttpRetryOptions(attempts=1): no hidden retries


def test_missing_usage_is_charged_the_worst_case(tmp_path):
    p = _port(tmp_path, _Inner(None))
    p.complete("s", "x" * 400, "y" * 100)
    assert p.records[0]["charged_worst_case"] is True and p.records[0]["input_tokens"] == 500
    assert p.records[0]["output_tokens"] == 1000


def test_the_ledger_persists_across_runs_and_blocks_further_spend(tmp_path):
    ledger = live_llm.Ledger(str(tmp_path / "l.jsonl"))
    a = _port(tmp_path, _Inner({"input_tokens": 100000, "output_tokens": 100000, "total_tokens": 200000}),
              ledger=ledger, budget_usd=0.06)
    a.complete("s", "a", "b")                                   # costs $0.05
    b = _port(tmp_path, _Inner({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}), ledger=ledger,
              budget_usd=0.0503)
    with pytest.raises(live_llm.BudgetExceeded):
        b.complete("s", "a", "b")                               # $0.05 already spent + worst case $0.0004 > $0.0503
    assert ledger.spent_usd() == pytest.approx(0.05)
    lines = open(tmp_path / "l.jsonl", encoding="utf-8").read()
    assert '"a"' not in lines and "sys" not in lines and oct(os.stat(tmp_path / "l.jsonl").st_mode & 0o777) == "0o600"


# ── key safety ────────────────────────────────────────────────────────────────

def test_the_gemini_port_needs_the_environment_key_and_redacts_it(tmp_path):
    with pytest.raises(ValueError, match="GOOGLE_API_KEY"):
        live_llm.GeminiPort(env={})
    port = live_llm.GeminiPort(env={"GOOGLE_API_KEY": KEY})

    class Boom:
        def invoke(self, *_):
            raise RuntimeError(f"401 bad key {KEY} for request")
    port._chat = Boom()
    with pytest.raises(live_llm.ProviderError) as info:
        port.complete("s", "a", "b")
    assert KEY not in str(info.value) and "<redacted>" in str(info.value)
    bp = _port(tmp_path, _Inner({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}))
    bp.complete("s", "a", "b")
    assert KEY not in json.dumps(bp.records) and KEY not in open(tmp_path / "l.jsonl").read()


def test_the_key_never_reaches_a_container(monkeypatch):
    from v2.execution import podman_argv as pa
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)
    argv = pa.run_argv("/usr/bin/podman", "sha256:" + "a" * 64, instance_id="b" * 32, run_id="c" * 32,
                       inputs_dir="/var/lib/ip/runs/" + "c" * 32 + "/in")
    assert "--env-host=false" in argv and not any("GOOGLE" in a or KEY in a for a in argv)
    from v2.execution.container_runtime import ContainerRuntime
    rt = ContainerRuntime.__new__(ContainerRuntime)
    assert "GOOGLE_API_KEY" not in rt._env()


# ── the benchmark: hidden answers, fairness, tripwire, CLI gates ─────────────

def _hidden_needles(ds):
    out = set()
    for k, v in scripted.golden_answers(ds).items():
        if k in live.leaked_keys(ds):
            continue
        vals = list(v.values()) if isinstance(v, dict) else [v]
        for x in vals:
            if isinstance(x, float):
                out |= {f"{x:.4f}"[:-1], repr(x)}
            elif isinstance(x, str):
                out.add(x)
            elif isinstance(x, int) and x >= 1000:
                out.add(str(x))
    return out


PRE_COMPUTATION_SKILLS = ("planner", "single_agent_claims")


def _profile_numbers(ds):
    prof = dsmod.profile(dsmod.load(live.dataset_path(ds)))
    return {str(prof["row_count"])} | {str(c["missing"]) for c in prof["columns"]}


def test_no_pre_computation_prompt_contains_a_hidden_golden_answer(tmp_path):
    """
    Planning and claims prompts (the only ones before any value is computed) contain no hidden golden answer.
    Insight prompts receive the ENGINE-computed claims by design. Numbers that are schema metadata in the shared
    profile (row count, per-column missing counts; e.g. attendance's 1268 missing punch_in values equal the
    'Absent' count) are excluded: both architectures see the same profile, and a model never states values.
    """
    prompts_seen = []

    def make(label):
        ds = label.split("/")[1]
        oracle = live.oracle_answer(ds)

        def answer(skill, system, user):
            if skill in PRE_COMPUTATION_SKILLS:
                prompts_seen.append((ds, system + "\n" + user))
            return oracle(skill, system, user)
        return live_llm.BudgetedPort(live_llm.FakeProvider(answer), model="gemini-2.5-flash-lite", budget_usd=1,
                                     ledger=live_llm.Ledger(str(tmp_path / "l.jsonl")), max_calls=100,
                                     max_total_tokens=10 ** 7, run_label=label)
    bench = live.run_benchmark(live.DATASETS, 1, make, log=lambda *_: None)
    assert bench["v1_executor_tripwire_hits"] == 0 and len(bench["results"]) == 8
    assert {ds for ds, _ in prompts_seen} == set(live.DATASETS)
    for ds, text in prompts_seen:
        leaked = [n for n in _hidden_needles(ds) if n in text]                 # strict: no exclusions
        assert leaked == [], (ds, leaked)


def test_leaked_questions_are_exactly_the_profile_metadata_coincidences():
    assert {d: live.leaked_keys(d) for d in live.DATASETS} == {
        "sales": {"row_count"}, "attendance": {"row_count", "status_counts"},
        "student": {"row_count"}, "ecommerce": {"row_count"}}
    g = live.grade("attendance", {})
    assert g["total_hidden"] == 5 and "status_counts" not in g["primary_keys"]


def test_a_model_cannot_state_a_value_only_choose_an_op():
    path = live.dataset_path("student")
    prof = dsmod.profile(dsmod.load(path))
    values, errors = op_engine.compute_claims([{"key": "pass_rate", "op": "rate_where_equals",
                                                 "args": {"column": "passed", "value": "True"}, "value": 0.99}],
                                              path, prof)
    assert values == {} and "unknown fields" in errors["pass_rate"]


def test_both_architectures_get_identical_profile_ops_and_question_wording():
    t = live.tasks_for("student")[0]
    prof = dsmod.profile(dsmod.load(live.dataset_path("student")))
    single = live.single_claims_prompt(prof, t, 1, [])
    goal = live.multi_goal(live.tasks_for("student"))
    for q in t["questions"].values():
        assert q in single and json.dumps(q)[1:-1] in goal
    from multi_agent import prompts
    planner = prompts.planning({"attempt": 1, "goal": goal, "profile": prof, "max_tasks": 8, "feedback": []})
    assert json.dumps(prof, sort_keys=True) in single and json.dumps(prof, sort_keys=True) in planner


def test_wrong_model_choices_are_graded_as_incorrect_not_hidden(tmp_path):
    oracle = live.oracle_answer("student")

    def wrong(skill, system, user):
        r = oracle(skill, system, user)
        return r.replace('"value": "True"', '"value": "False"')          # pass_rate -> fail rate
    port = live_llm.BudgetedPort(live_llm.FakeProvider(wrong), model="gemini-2.5-flash-lite", budget_usd=1,
                                 ledger=live_llm.Ledger(str(tmp_path / "l.jsonl")), max_calls=100,
                                 max_total_tokens=10 ** 7)
    for runner in (live.run_single_agent, live.run_multi_agent):
        g = live.grade("student", runner("student", port)["claims"])
        assert g["per_key"]["pass_rate"] == "incorrect" and g["correct_hidden"] == 5


def test_a_v1_executor_call_during_a_run_is_counted_and_refused():
    import sandbox.executor as v1
    with live.v1_executor_tripwire() as trip:
        with pytest.raises(RuntimeError, match="V1 executor reached"):
            v1.execute_code("print(1)", "x.csv", "t")
    assert trip["n"] == 1 and v1.execute_code.__name__ != "trip"


def test_the_schedule_is_randomised_reproducibly_and_balanced():
    a, b = live.schedule(live.DATASETS, 5, 1), live.schedule(live.DATASETS, 5, 1)
    assert a == b and a != live.schedule(live.DATASETS, 5, 2) and len(a) == 40
    assert sum(1 for x in a if x[0] == "single_agent") == 20


def test_live_calls_need_explicit_confirmation_and_the_budget_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)
    with pytest.raises(SystemExit, match="prices-confirmed"):
        live.main(["--provider", "gemini", "--mode", "pilot", "--out", str(tmp_path)])
    with pytest.raises(SystemExit, match=r"\$5"):
        live.main(["--provider", "fake", "--mode", "pilot", "--budget-usd", "6", "--out", str(tmp_path)])


def test_an_offline_run_is_labelled_as_not_live(tmp_path):
    assert live.main(["--provider", "fake", "--mode", "pilot", "--out", str(tmp_path)]) == 0
    md = open(tmp_path / "live_benchmark.md", encoding="utf-8").read()
    assert "OFFLINE / MOCK provider: not live results" in md
    assert "not the original unrestricted V1 code-generation pipeline" in md and "ESTIMATE, not billing" in md
    bench = json.load(open(tmp_path / "live_benchmark.json"))
    assert bench["summary"]["multi_agent"]["call_source"] == ["mock_scripted"]
    assert not os.path.exists(live_llm.DEFAULT_LEDGER) or "dry_run" not in live_llm.DEFAULT_LEDGER


# ── OpenAI-compatible transport (Bearer key injected by the session proxy) ────

class _Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_openai_compat_port_one_attempt_usage_and_request_shape():
    sent = []

    def urlopen(req, timeout):
        sent.append(req)
        return _Resp({"choices": [{"message": {"content": "{\"ok\": 1}"}}],
                      "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}})
    p = live_llm.GeminiOpenAICompatPort(env={"GOOGLE_API_KEY": "placeholder"}, urlopen=urlopen)
    r = p.complete("planner", "SYS", "USER")
    assert r.content == '{"ok": 1}' and r.usage_metadata == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    assert len(sent) == 1 and sent[0].full_url.endswith("/v1beta/openai/chat/completions")
    body = json.loads(sent[0].data)
    assert body["model"] == "gemini-2.5-flash-lite" and body["temperature"] == 0 and body["max_tokens"] == 2048
    assert sent[0].get_header("Authorization") == "Bearer placeholder"


def test_openai_compat_errors_are_redacted_and_not_retried():
    import io
    import urllib.error
    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(f"invalid key {KEY}".encode()))
    p = live_llm.GeminiOpenAICompatPort(env={"GOOGLE_API_KEY": KEY}, urlopen=urlopen)
    with pytest.raises(live_llm.ProviderError) as info:
        p.complete("s", "a", "b")
    assert len(calls) == 1 and KEY not in str(info.value) and "HTTP 400" in str(info.value)


def test_openai_compat_model_listing_and_unconfirmed_models_refused():
    p = live_llm.GeminiOpenAICompatPort(env={"GOOGLE_API_KEY": "x"}, urlopen=lambda req, timeout: _Resp(
        {"data": [{"id": "models/gemini-2.5-flash-lite"}, {"id": "models/gemini-2.5-pro"}]}))
    assert p.list_models() == ["models/gemini-2.5-flash-lite", "models/gemini-2.5-pro"]
    with pytest.raises(ValueError, match="no confirmed price"):
        live_llm.GeminiOpenAICompatPort(model="gemini-2.5-flash", env={"GOOGLE_API_KEY": "x"})


def test_a_model_that_refuses_generation_stops_the_run_before_any_benchmark(tmp_path, monkeypatch):
    class Refusing(live_llm.GeminiOpenAICompatPort):
        def list_models(self):
            return ["models/gemini-2.5-flash-lite"]

        def complete(self, *a):
            raise live_llm.ProviderError("HTTP 404: no longer available to new users")
    monkeypatch.setattr(live_llm, "GeminiOpenAICompatPort", Refusing)
    monkeypatch.setenv("GOOGLE_API_KEY", "proxy-injected")
    ledger = str(tmp_path / "l.jsonl")
    with pytest.raises(SystemExit, match="cannot generate"):
        live.main(["--provider", "gemini", "--mode", "pilot", "--prices-confirmed",
                   "--i-understand-this-makes-live-calls", "--ledger", ledger, "--out", str(tmp_path / "o")])
    assert not os.path.exists(tmp_path / "o" / "live_benchmark.json")
    assert live_llm.Ledger(ledger).spent_usd() > 0                 # the failed probe is charged conservatively


def test_http_429_is_retried_explicitly_counted_and_charged_per_policy(tmp_path):
    class RateLimited(_Inner):
        def complete(self, *a):
            self.n += 1
            if self.n <= 2:
                raise live_llm.ProviderError("HTTP 429: RESOURCE_EXHAUSTED", status=429, retry_after_s=7)
            return SimpleNamespace(content="{}", usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6})
    slept = []
    inner = RateLimited()
    p = _port(tmp_path, inner, rate_limit_retries=3, charge_429="zero", sleep=slept.append)
    assert p.complete("s", "a", "b").content == "{}"
    assert inner.n == 3 and p.calls == 3 and p.rate_limited == 2 and slept == [7, 7]
    fails = [r for r in p.records if r["failed"]]
    assert [r["http_status"] for r in fails] == [429, 429] and all(r["cost_usd"] == 0 for r in fails)
    q = _port(tmp_path, RateLimited(), rate_limit_retries=3, sleep=lambda s: None)      # default: worst case
    q.complete("s", "a", "b")
    assert all(r["cost_usd"] > 0 for r in q.records if r["failed"])


def test_other_failures_are_never_retried_and_always_charged_the_worst_case(tmp_path):
    class ServerError(_Inner):
        def complete(self, *a):
            self.n += 1
            raise live_llm.ProviderError("HTTP 503", status=503)
    inner = ServerError()
    p = _port(tmp_path, inner, rate_limit_retries=3, charge_429="zero", sleep=lambda s: None)
    with pytest.raises(live_llm.ProviderError):
        p.complete("s", "a", "b")
    assert inner.n == 1 and p.records[0]["cost_usd"] > 0 and p.records[0]["http_status"] == 503
