"""
Independent verification: recompute every claim from the dataset, never from the analyst's work.

The Verification Agent receives only the claim specs, the values the analyst
reported and a reference to the dataset. It never sees the analyst's code,
stdout or reasoning, and it uses no LLM: each claim is recomputed here with
the standard library (csv, math, statistics) and compared with the reported
value. A second, separate implementation (tests/v2/golden/reference.py) is
used by the tests as ground truth, so the two must agree on all 28 golden
answers.

Statuses per claim:
    verified       reported value equals the recomputed value within tolerance
    refuted        reported value differs (or has the wrong type)
    missing        the analyst reported no value for a planned claim
    malformed      the reported value could not be parsed, was duplicated or is not finite
    unverifiable   the claim cannot be recomputed (e.g. the dataset changed since profiling)
A task's analysis is accepted only if every planned claim is verified.
Feedback to the analyst names the failed claims and the reason, never the recomputed value.
"""

import math
import re
from collections import Counter, defaultdict

from multi_agent import dataset as dsmod

REL_TOL = 1e-9
ABS_TOL = 1e-6
VERIFIED, REFUTED, MISSING, MALFORMED, UNVERIFIABLE = "verified", "refuted", "missing", "malformed", "unverifiable"


class Unverifiable(Exception):
    pass


# ── recomputation ────────────────────────────────────────────────────────────

def _numbers(values):
    out = []
    for v in values:
        if dsmod.is_na(v):
            continue
        try:
            out.append(float(v))
        except ValueError:
            raise Unverifiable(f"non-numeric value in a numeric column") from None
    return out


def _keys(ds, args):
    keys = ds.column(args["by"])
    p = args.get("by_prefix")
    return [k[:p] for k in keys] if p else keys


def _groups(ds, args):
    acc = defaultdict(list)
    for k, v in zip(_keys(ds, args), ds.column(args["column"])):
        if dsmod.is_na(k) or dsmod.is_na(v):      # pandas groupby drops NA keys; sum/mean skip NA values
            continue
        acc[k].append(float(v))
    if not acc:
        raise Unverifiable("no groups")
    return acc


def _mean(xs):
    if not xs:
        raise Unverifiable("no non-missing values")
    return math.fsum(xs) / len(xs)


def _argmax(d):
    return max(sorted(d), key=lambda k: d[k])


def _pearson(xs, ys):
    pairs = [(float(a), float(b)) for a, b in zip(xs, ys) if not dsmod.is_na(a) and not dsmod.is_na(b)]
    if len(pairs) < 2:
        raise Unverifiable("fewer than two complete pairs")
    mx = math.fsum(p[0] for p in pairs) / len(pairs)
    my = math.fsum(p[1] for p in pairs) / len(pairs)
    sxy = math.fsum((a - mx) * (b - my) for a, b in pairs)
    sxx = math.fsum((a - mx) ** 2 for a, _ in pairs)
    syy = math.fsum((b - my) ** 2 for _, b in pairs)
    if sxx == 0 or syy == 0:
        raise Unverifiable("zero variance")
    return sxy / math.sqrt(sxx * syy), len(pairs)


def recompute(claim: dict, ds) -> tuple:
    """(value, evidence) for one claim spec; raises Unverifiable."""
    op, a = claim["op"], claim.get("args", {})
    n = len(ds.rows)
    if op == "count_rows":
        return n, {"rows": n}
    if op in ("sum", "mean"):
        xs = _numbers(ds.column(a["column"]))
        value = math.fsum(xs) if op == "sum" else _mean(xs)
        return value, {"column": a["column"], "non_missing": len(xs), "rows": n}
    if op in ("group_sum", "group_mean", "argmax_group_sum", "argmax_group_mean"):
        g = _groups(ds, a)
        agg = {k: (math.fsum(v) if "sum" in op else _mean(v)) for k, v in g.items()}
        ev = {"by": a["by"], "column": a["column"], "groups": len(agg),
              "values_used": sum(len(v) for v in g.values())}
        if a.get("by_prefix"):
            ev["by_prefix"] = a["by_prefix"]
        return (_argmax(agg) if op.startswith("argmax") else dict(sorted(agg.items()))), ev
    if op == "ratio_of_sums_pct":
        num, den = math.fsum(_numbers(ds.column(a["numerator"]))), math.fsum(_numbers(ds.column(a["denominator"])))
        if den == 0:
            raise Unverifiable("denominator sums to zero")
        return num / den * 100, {"numerator": a["numerator"], "denominator": a["denominator"]}
    if op in ("count_where_equals", "rate_where_equals"):
        hits = sum(1 for v in ds.column(a["column"]) if v == a["value"])
        if op == "count_where_equals":
            return hits, {"column": a["column"], "matches": hits}
        if n == 0:
            raise Unverifiable("no rows")
        return hits / n, {"column": a["column"], "matches": hits, "rows": n}
    if op == "value_counts":
        c = Counter(v for v in ds.column(a["column"]) if not dsmod.is_na(v))
        return dict(sorted(c.items())), {"column": a["column"], "distinct": len(c)}
    if op == "mean_where_equals":
        xs = [float(v) for v, w in zip(ds.column(a["column"]), ds.column(a["where_column"]))
              if w == a["value"] and not dsmod.is_na(v)]
        return _mean(xs), {"column": a["column"], "where_column": a["where_column"], "matches": len(xs)}
    if op == "pearson":
        r, pairs = _pearson(ds.column(a["x"]), ds.column(a["y"]))
        return r, {"x": a["x"], "y": a["y"], "pairs": pairs}
    raise Unverifiable(f"unknown op {op!r}")


# ── comparison ───────────────────────────────────────────────────────────────

def finite(value) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(finite(v) for v in value.values())
    if isinstance(value, list):
        return all(finite(v) for v in value)
    return False


def values_match(reported, expected, rel=REL_TOL, abs_tol=ABS_TOL) -> bool:
    """Golden tolerance. Booleans never equal numbers; dict keys must match exactly."""
    if isinstance(reported, bool) or isinstance(expected, bool):
        return type(reported) is type(expected) and reported == expected
    if isinstance(expected, (int, float)) and isinstance(reported, (int, float)):
        return math.isclose(float(reported), float(expected), rel_tol=rel, abs_tol=abs_tol)
    if isinstance(expected, dict) and isinstance(reported, dict):
        return set(reported) == set(expected) and all(values_match(reported[k], expected[k], rel, abs_tol)
                                                      for k in expected)
    if isinstance(expected, str) and isinstance(reported, str):
        return reported == expected
    return False


def verify_claims(request: dict, ds_loader=dsmod.load) -> dict:
    """Verification report for one analysis (payload of kind verification_report)."""
    claims = {c["key"]: c for c in request["claims_spec"]}
    submitted, errors = request["submitted"], request.get("claim_errors", {})
    per = {}
    integrity_ok, ds = False, None
    try:
        ds = ds_loader(request["dataset_path"])
        integrity_ok = ds.sha256 == request["dataset_sha256"]
    except dsmod.DatasetError as e:
        load_error = str(e)
    for key in sorted(claims):
        entry = {"op": claims[key]["op"], "reported": submitted.get(key)}
        if key in errors:
            entry.update(status=MALFORMED, reason=errors[key])
        elif key not in submitted:
            entry.update(status=MISSING, reason="no value reported for a planned claim")
        elif not finite(submitted[key]):
            entry.update(status=MALFORMED, reason="value is not finite")
        elif ds is None:
            entry.update(status=UNVERIFIABLE, reason=f"dataset unreadable: {load_error}")
        elif not integrity_ok:
            entry.update(status=UNVERIFIABLE, reason="dataset changed since it was profiled (SHA-256 mismatch)")
        else:
            try:
                value, evidence = recompute(claims[key], ds)
            except (Unverifiable, dsmod.DatasetError) as e:
                entry.update(status=UNVERIFIABLE, reason=str(e))
            else:
                ok = values_match(submitted[key], value)
                entry.update(status=VERIFIED if ok else REFUTED, recomputed=value, evidence=evidence,
                             reason=None if ok else "differs from the independent recomputation")
        per[key] = entry
    accepted = bool(per) and all(e["status"] == VERIFIED for e in per.values())
    feedback = [f"claim {k}: {e['status']} ({e['reason']})" for k, e in per.items() if e["status"] != VERIFIED]
    unplanned = sorted(set(request.get("unplanned", [])) | (set(submitted) - set(claims)))
    if unplanned:
        feedback.append(f"unplanned claims ignored: {', '.join(unplanned)}")
    return {"task_id": request["task_id"], "attempt": request["attempt"],
            "verdict": "accepted" if accepted else "rejected", "claims": per, "unplanned": unplanned,
            "dataset_integrity": integrity_ok, "feedback": feedback}


# ── insight grounding ────────────────────────────────────────────────────────

_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?%?")
MAX_INSIGHT_CHARS = 600


def _numeric_values(value):
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [x for v in value.values() for x in _numeric_values(v)]
    return []


def _string_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return list(value) + [s for v in value.values() for s in _string_values(v)]
    return []


def _grounded(token: str, candidates) -> bool:
    text = token.rstrip("%").replace(",", "")
    try:
        x = float(text)
    except ValueError:
        return False
    decimals = len(text.split(".")[1]) if "." in text else 0
    half_unit = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(c - x) <= half_unit or abs(c * 100 - x) <= half_unit for c in candidates)


def check_insights(request: dict) -> dict:
    """Every insight must cite verified claims only, and every number it states must come from them."""
    verified = request["verified_claims"]
    problems = []
    insights = request["insights"]
    if not insights:
        problems.append("no insights")
    for i, ins in enumerate(insights):
        cites, text = ins["cites"], ins["text"]
        unknown = [c for c in cites if c not in verified]
        if not cites:
            problems.append(f"insight {i}: cites no claim")
        if unknown:
            problems.append(f"insight {i}: cites claims that are not verified: {', '.join(unknown)}")
        cited = [verified[c] for c in cites if c in verified]
        scrubbed = text
        for s in sorted({s for v in cited for s in _string_values(v)}, key=len, reverse=True):
            # whole-token removal of cited labels (e.g. "2024-03", "North"), so they are not read as numbers
            scrubbed = re.sub(rf"(?<![\w.]){re.escape(s)}(?![\w])", " ", scrubbed)
        candidates = [x for v in cited for x in _numeric_values(v)]
        for token in _NUMBER.findall(scrubbed):
            if not _grounded(token, candidates):
                problems.append(f"insight {i}: number {token} is not supported by its cited claims")
    return {"task_id": request["task_id"], "verdict": "rejected" if problems else "accepted",
            "problems": problems}
