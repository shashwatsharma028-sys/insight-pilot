"""
P6 optimisation 2: bounded transient-provider retries (offline only; injected failures, no network, no key).

Retries happen in BudgetedPort, BELOW the agents: a temporary HTTP 5xx or timeout does not consume a planning or
claims attempt while provider retries remain. Each attempt is budget-checked and every failed attempt is charged
the conservative worst case. 429 handling and the daily-quota stop are unchanged. Default: off.
"""

import io
import json
import socket
import urllib.error

import pytest

from evaluation import live
from multi_agent import live_llm
from multi_agent.llm import MOCK


def _err(status=None, timeout=False, retry_after=None):
    return live_llm.ProviderError(f"HTTP {status}" if status else "TimeoutError: timed out", status=status,
                                  retry_after_s=retry_after, timeout=timeout)


class Flaky:
    """Inner provider: raises the queued failures, then answers via `answer` (oracle or fixed text)."""
    call_source = MOCK
    code_trust = live_llm.FakeProvider.code_trust

    def __init__(self, failures, answer=lambda *a: "{}", only_skill=None):
        self.failures, self.answer, self.only_skill = list(failures), answer, only_skill
        self.attempts = []

    def complete(self, skill, system, user):
        self.attempts.append(skill)
        if self.failures and (self.only_skill is None or skill == self.only_skill):
            raise self.failures.pop(0)
        return live_llm.FakeProvider(self.answer).complete(skill, system, user)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _port(tmp_path, inner, budget=1.0, **kw):
    clock = kw.pop("clock", Clock())
    slept = kw.pop("slept", [])

    def sleep(s):
        slept.append(s)
        clock.t += s
    port = live_llm.BudgetedPort(inner, model="gemini-2.5-flash-lite", budget_usd=budget,
                                 ledger=live_llm.Ledger(str(tmp_path / "l.jsonl")), max_calls=100,
                                 max_total_tokens=10 ** 8, clock=clock, sleep=sleep, **kw)
    return port, slept


# ── classification ───────────────────────────────────────────────────────────────────────────────────────

def test_only_5xx_gateway_errors_and_timeouts_are_transient():
    assert all(live_llm.is_transient(_err(s)) for s in (500, 502, 503, 504))
    assert live_llm.is_transient(_err(timeout=True))
    for s in (400, 401, 403, 404, 429, 501, 505):
        assert not live_llm.is_transient(_err(s))
    assert not live_llm.is_transient(_err())                                  # unknown failure: not retried
    assert not live_llm.is_transient(live_llm.QuotaOrBillingStop("daily quota"))
    assert not live_llm.is_transient(ValueError("x"))


@pytest.mark.parametrize("exc,timeout", [
    (TimeoutError("timed out"), True), (socket.timeout("timed out"), True),
    (urllib.error.URLError(TimeoutError("timed out")), True),
    (urllib.error.URLError(ConnectionRefusedError("refused")), False), (OSError("reset"), False)])
def test_the_http_port_marks_only_timeouts(exc, timeout):
    def urlopen(req, timeout_s=None, **kw):
        raise exc
    p = live_llm.GeminiOpenAICompatPort(env={"GOOGLE_API_KEY": "x"}, urlopen=lambda req, timeout: urlopen(req))
    with pytest.raises(live_llm.ProviderError) as e:
        p.complete("s", "a", "b")
    assert e.value.timeout is timeout and e.value.status is None and live_llm.is_transient(e.value) is timeout


# ── the four required scenarios ──────────────────────────────────────────────────────────────────────────

def test_eventual_success_after_transient_failures(tmp_path):
    inner = Flaky([_err(503), _err(timeout=True)])
    port, slept = _port(tmp_path, inner, transient_retries=3, transient_backoff_s=2.0)
    r = port.complete("planner", "sys", "user")
    assert r.content == "{}" and len(inner.attempts) == 3
    assert 1.6 <= slept[0] <= 2.4 and 3.2 <= slept[1] <= 4.8                  # exponential backoff with jitter
    import random
    rng = random.Random(20261124)                                              # the port's default seeded rng
    assert slept == [2.0 * rng.uniform(0.8, 1.2), 4.0 * rng.uniform(0.8, 1.2)] and slept[0] != 2.0
    assert port.transient_retried == 2 and port.transient_given_up == 0 and port.rate_limited == 0
    assert port.transient_wait_s == pytest.approx(sum(slept)) and port.wait_s == pytest.approx(sum(slept))
    rec = port.records
    assert [x["provider_attempt"] for x in rec] == [1, 2, 3]
    assert [x["retry_reason"] for x in rec] == [None, "transient_503", "transient_timeout"]
    assert [x["failed"] for x in rec] == ["ProviderError", "ProviderError", None]
    assert rec[0]["charged_worst_case"] and rec[1]["charged_worst_case"] and rec[0]["transient"] and rec[1]["timeout"]
    assert rec[0]["billing_status"] == "conservative_worst_case" and not rec[2]["charged_worst_case"]
    assert port.spent_usd == pytest.approx(sum(x["cost_usd"] for x in rec))


def test_repeated_failures_stop_after_the_retry_limit(tmp_path):
    inner = Flaky([_err(503)] * 10)
    port, slept = _port(tmp_path, inner, transient_retries=2)
    with pytest.raises(live_llm.ProviderError):
        port.complete("planner", "sys", "user")
    assert len(inner.attempts) == 3 and len(slept) == 2 and port.transient_retried == 2
    assert port.transient_given_up == 1 and all(x["charged_worst_case"] for x in port.records)
    assert not port.exhausted                                                  # a failed call, not a stop


def test_an_exhausted_budget_refuses_the_next_retry_before_calling(tmp_path):
    (tmp_path / "probe").mkdir()
    probe, _ = _port(tmp_path / "probe", Flaky([]))
    worst = probe.worst_case("sys", "user")[2]
    inner = Flaky([_err(503)] * 10)
    port, _ = _port(tmp_path, inner, budget=worst * 2.5, transient_retries=5)
    with pytest.raises(live_llm.BudgetExceeded):
        port.complete("planner", "sys", "user")
    assert len(inner.attempts) == 2 and port.exhausted and port.ledger.spent_usd() <= worst * 2.5
    assert port.transient_retried == 2                                         # the 3rd attempt was refused pre-call


@pytest.mark.parametrize("status", [400, 401, 403, 404, 501])
def test_non_retryable_errors_are_never_retried(tmp_path, status):
    inner = Flaky([_err(status)])
    port, slept = _port(tmp_path, inner, transient_retries=5)
    with pytest.raises(live_llm.ProviderError):
        port.complete("planner", "sys", "user")
    assert len(inner.attempts) == 1 and slept == [] and port.transient_retried == 0


# ── strict limits ────────────────────────────────────────────────────────────────────────────────────────

def test_the_elapsed_time_limit_ends_retries(tmp_path):
    inner = Flaky([_err(503)] * 10)
    port, slept = _port(tmp_path, inner, transient_retries=10, transient_backoff_s=10.0, transient_max_wait_s=100,
                        transient_max_elapsed_s=25.0)
    with pytest.raises(live_llm.ProviderError):
        port.complete("planner", "sys", "user")
    assert sum(slept) <= 25.0 and port.transient_given_up == 1 and len(inner.attempts) == len(slept) + 1


def test_retry_after_is_honoured_but_capped(tmp_path):
    inner = Flaky([_err(503, retry_after=7.0), _err(503, retry_after=500.0)])
    port, slept = _port(tmp_path, inner, transient_retries=3, transient_max_wait_s=30.0)
    port.complete("planner", "sys", "user")
    assert slept == [7.0, 30.0]


def test_the_policy_is_off_by_default(tmp_path):
    inner = Flaky([_err(503)])
    port, slept = _port(tmp_path, inner)
    with pytest.raises(live_llm.ProviderError):
        port.complete("planner", "sys", "user")
    assert len(inner.attempts) == 1 and slept == [] and port.transient_retried == 0


# ── unchanged: daily-quota stop and 429 handling ─────────────────────────────────────────────────────────

def test_the_daily_quota_stop_is_never_retried_and_stays_latched(tmp_path):
    body = '{"error": {"code": 429, "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}'
    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(body.encode()))
    inner = live_llm.GeminiOpenAICompatPort(env={"GOOGLE_API_KEY": "x"}, urlopen=urlopen)
    port, slept = _port(tmp_path, inner, transient_retries=5, rate_limit_retries=5, charge_429="zero")
    with pytest.raises(live_llm.QuotaOrBillingStop):
        port.complete("planner", "sys", "user")
    with pytest.raises(live_llm.BudgetExceeded):
        port.complete("planner", "sys", "user")
    assert len(calls) == 1 and slept == [] and port.exhausted


def test_429_and_transient_retries_are_counted_separately(tmp_path):
    inner = Flaky([_err(429, retry_after=3.0), _err(503), _err(429, retry_after=4.0)])
    port, slept = _port(tmp_path, inner, rate_limit_retries=2, transient_retries=2, charge_429="zero")
    port.complete("planner", "sys", "user")
    assert port.rate_limited == 2 and port.transient_retried == 1 and len(inner.attempts) == 4
    assert port.rate_limit_wait_s_total == pytest.approx(7.0) and port.transient_wait_s == pytest.approx(slept[1])
    assert [x["billing_status"] for x in port.records if x["failed"]] == [
        "provisional_unbilled_429", "conservative_worst_case", "provisional_unbilled_429"]


# ── agents: a transient failure no longer consumes an agent attempt; same policy for both architectures ──

def _oracle_port(tmp_path, ds, failures, skill, **kw):
    inner = Flaky(failures, live.oracle_answer(ds), only_skill=skill)
    port, _ = _port(tmp_path, inner, **kw)
    return port, inner


def test_multi_agent_planning_attempts_are_not_consumed_by_a_retried_503(tmp_path):
    ds = "student"
    on, inner = _oracle_port(tmp_path, ds, [_err(503), _err(503)], "planner", transient_retries=2)
    out = live.run_multi_agent(ds, on, selective_recovery=True)
    assert out["status"] == "completed" and out["diagnostics"]["plan_problems"] == []
    assert inner.attempts.count("planner") == 3 and on.transient_retried == 2
    off, inner_off = _oracle_port(tmp_path, ds, [_err(503), _err(503)], "planner")
    out_off = live.run_multi_agent(ds, off, selective_recovery=True)
    assert out_off["status"] == "plan_invalid" and len(out_off["diagnostics"]["plan_problems"]) == 2
    assert all("ProviderError" in p["problems"][0] for p in out_off["diagnostics"]["plan_problems"])


def test_single_agent_claims_attempts_are_not_consumed_by_a_retried_503(tmp_path):
    ds = "sales"
    on, _ = _oracle_port(tmp_path, ds, [_err(503)], "single_agent_claims", transient_retries=2)
    out = live.run_single_agent(ds, on)
    assert out["llm_errors"] == 0 and out["retries"] == 0 and on.transient_retried == 1
    off, _ = _oracle_port(tmp_path, ds, [_err(503)], "single_agent_claims")
    out_off = live.run_single_agent(ds, off)
    assert out_off["llm_errors"] == 1 and out_off["retries"] == 1                # the agent paid with an attempt


# ── the matched experiment (offline dry run through the CLI) ─────────────────────────────────────────────

@pytest.mark.parametrize("arch", ["multi_agent", "single_agent"])
def test_the_provider_resilience_experiment_runs_offline_for_both_architectures(tmp_path, arch):
    rc = live.main(["--provider", "fake", "--mode", "full", "--experiment", "provider_resilience", "--arch", arch,
                    "--transient-retries", "2", "--out", str(tmp_path)])
    exp = json.load(open(tmp_path / "provider_resilience_experiment.json"))
    assert rc == 0 and exp["arch"] == arch and exp["summary"]["matched_pairs"] == 20
    base = [[ds, rep] for a, ds, rep in live.schedule(live.DATASETS, 5, 20261124) if a == arch]
    assert exp["slots"] == base
    s = exp["summary"]
    for k in ("provider_attempts", "provider_retries_transient", "provider_retries_429", "wait_s_transient",
              "provider_tokens_total", "cost_usd_provider_reported", "cost_usd_worst_case_failed_attempts"):
        assert k in s["provider_retry"] and k in s["control"]
    assert {r["variant"] for r in exp["results"]} == {"provider_retry", "control"}
    if arch == "multi_agent":                                                  # selective recovery ON in both arms
        assert all("plan_recovery" in r["run"] for r in exp["results"])


def test_the_experiment_refuses_to_run_without_a_retry_policy(tmp_path):
    with pytest.raises(SystemExit):
        live.main(["--provider", "fake", "--mode", "full", "--experiment", "provider_resilience",
                   "--out", str(tmp_path)])
