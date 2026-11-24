"""
P6: trusted analytical operations, so live models can drive analysis WITHOUT any model-written code running.

In "trusted-ops" mode a model never writes executable code. It chooses claims from the fixed, validated
vocabulary in multi_agent.claims (an op name plus column/value arguments: data, not code), and this module
computes them with reviewed pandas code in-process. Nothing is exec'd, no subprocess is started, no
execution port or container is involved, and the V1 executors are never reached.

This is a SECOND, independent implementation of the op semantics (pandas). The Verification Agent recomputes
every claim with the standard library (multi_agent.verification), and tests/v2/p6 require both to agree with
all 28 golden answers, so the verifier still cross-checks a separate implementation.

TrustedOpsAnalyst is a drop-in Data Analyst Agent: same request and reply contract
(analysis_request -> analysis_result), no LLM, no execution port.
"""

import time

import pandas as pd

from multi_agent import claims as claim_vocab
from multi_agent import contracts as c
from multi_agent.agents import SPECS, AgentSpec

BACKEND = "trusted_ops"
ISOLATION = "none_required_no_generated_code"


class OpError(ValueError):
    """The claim cannot be computed from this dataset (reported as a claim error, never raised to the model)."""


class Frame:
    """The dataset as pandas sees it (`parsed`) and as raw CSV text (`raw`, for equality tests and group keys)."""

    def __init__(self, path):
        self.parsed = pd.read_csv(path)
        self.raw = pd.read_csv(path, dtype=str, keep_default_na=False)

    def num(self, col):
        s = self.parsed[col]
        if not pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
            raise OpError(f"column {col!r} is not numeric")
        return s.astype(float)


def _f(x):
    return float(x)


def _groups(fr, a):
    keys = fr.raw[a["by"]]
    if a.get("by_prefix"):
        keys = keys.str.slice(0, a["by_prefix"])
    keep = ~fr.parsed[a["by"]].isna()
    vals = fr.num(a["column"])
    frame = pd.DataFrame({"k": keys[keep], "v": vals[keep]}).dropna(subset=["v"])
    if frame.empty:
        raise OpError("no groups")
    return frame.groupby("k", sort=True)["v"]


def _argmax(series):
    s = series.sort_index()
    return str(s.idxmax())          # first maximum in sorted key order (the verifier's tie rule)


def compute(claim: dict, fr: Frame):
    op, a = claim["op"], claim.get("args", {})
    if op == "count_rows":
        return int(len(fr.parsed))
    if op == "sum":
        return _f(fr.num(a["column"]).sum())
    if op == "mean":
        s = fr.num(a["column"]).dropna()
        if s.empty:
            raise OpError("no non-missing values")
        return _f(s.mean())
    if op in ("group_sum", "group_mean", "argmax_group_sum", "argmax_group_mean"):
        g = _groups(fr, a)
        agg = g.sum() if "sum" in op else g.mean()
        if op.startswith("argmax"):
            return _argmax(agg)
        return {str(k): _f(v) for k, v in agg.sort_index().items()}
    if op == "ratio_of_sums_pct":
        den = fr.num(a["denominator"]).sum()
        if den == 0:
            raise OpError("denominator sums to zero")
        return _f(fr.num(a["numerator"]).sum() / den * 100)
    if op in ("count_where_equals", "rate_where_equals"):
        hits = int((fr.raw[a["column"]] == a["value"]).sum())
        if op == "count_where_equals":
            return hits
        if len(fr.raw) == 0:
            raise OpError("no rows")
        return hits / len(fr.raw)
    if op == "value_counts":
        s = fr.raw[a["column"]][~fr.parsed[a["column"]].isna()]
        return {str(k): int(v) for k, v in s.value_counts().sort_index().items()}
    if op == "mean_where_equals":
        s = fr.num(a["column"])[fr.raw[a["where_column"]] == a["value"]].dropna()
        if s.empty:
            raise OpError("no non-missing values")
        return _f(s.mean())
    if op == "pearson":
        pair = pd.DataFrame({"x": fr.num(a["x"]), "y": fr.num(a["y"])}).dropna()
        if len(pair) < 2 or pair["x"].std() == 0 or pair["y"].std() == 0:
            raise OpError("pearson needs two complete pairs with non-zero variance")
        return _f(pair["x"].corr(pair["y"]))
    raise OpError(f"unknown op {op!r}")


def compute_claims(claims: list, path: str, profile: dict) -> tuple:
    """({key: value}, {key: error}) for validated claim specs; invalid specs become errors, never computed."""
    values, errors = {}, {}
    fr = Frame(path)
    for cl in claims:
        key = cl.get("key") if isinstance(cl, dict) else None
        problems = claim_vocab.validate_claim(cl, profile)
        if problems:
            errors[str(key)[:64]] = "; ".join(problems)[:500]
            continue
        try:
            values[key] = compute(cl, fr)
        except (OpError, KeyError, ValueError, TypeError) as e:
            errors[key] = f"{type(e).__name__}: {e}"[:500]
    return values, errors


class TrustedOpsAnalyst:
    """Data Analyst Agent for trusted-ops mode: computes the planned claims; holds no LLM and no execution port."""
    spec = AgentSpec(c.ANALYST, "compute one task's planned claims with trusted operations (no generated code)",
                     ("analysis_request",), ("analysis_result",), ("trusted_ops",),
                     SPECS[c.ANALYST].sees, SPECS[c.ANALYST].never_sees)
    hardened = False

    def __init__(self, clock=time.perf_counter):
        self.clock = clock

    def run(self, req: dict) -> dict:
        task = req["task"]
        t0 = self.clock()
        values, errors = compute_claims(task["claims"], req["dataset_path"], req["profile"])
        planned = {cl["key"] for cl in task["claims"]}
        execution = {"ok": True, "stdout": "", "error": "", "error_type": None, "chart_path": None,
                     "backend": BACKEND, "isolation": ISOLATION, "seconds": self.clock() - t0,
                     "blocked_by_safety_scan": False}
        return {"task_id": task["id"], "attempt": req["attempt"], "code": "", "execution": execution,
                "claims": {k: v for k, v in values.items() if k in planned},
                "claim_errors": {k: v for k, v in errors.items() if k in planned}, "unplanned": []}
