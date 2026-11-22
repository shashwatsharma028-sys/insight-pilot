"""
Token provenance: where every token number comes from.

Each LLM call gets two independent labels:

call_source
    "mock_scripted"  served by the offline scripted model (tests/v2/mock_llm.py). Not a provider.
    "provider"       a real provider response. P4.0 never produces one; the label exists so that
                     future live runs cannot be mixed up with mock runs.
token_source
    "provider_reported"      the response carried valid usage_metadata (non-negative integers).
    "estimated_chars_div_4"  no or invalid usage_metadata: len(str(prompt args)) // 4 and
                             len(str(content)) // 4, exactly the V1 token monitor's fallback.

Rules (enforced here and in evaluation.paired):
- token totals are kept per (call_source, token_source) class and never summed across classes;
- a cost is only computed for provider calls with provider-reported tokens, and is labelled
  an estimate (list prices vary); everything else has cost None with a reason;
- mock-call tokens are character estimates of scripted prompts: never cost or savings.
"""

CHARS_PER_TOKEN_ESTIMATE = 4          # must equal agent.token_monitor.CHARS_PER_TOKEN_ESTIMATE
MOCK = "mock_scripted"
PROVIDER = "provider"
REPORTED = "provider_reported"
ESTIMATED = "estimated_chars_div_4"


def _valid_count(v):
    return type(v) is int and v >= 0


def classify_usage(response):
    """(token_source, input_tokens, output_tokens, anomaly) for one response; never raises."""
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict) and "input_tokens" in usage:
        i, o = usage.get("input_tokens"), usage.get("output_tokens", 0)
        if _valid_count(i) and _valid_count(o):
            return REPORTED, i, o, None
        return ESTIMATED, None, None, "invalid_usage_metadata"
    return ESTIMATED, None, None, None


def estimate(prompt_args, response):
    content = getattr(response, "content", "")
    return len(str(prompt_args)) // CHARS_PER_TOKEN_ESTIMATE, len(str(content)) // CHARS_PER_TOKEN_ESTIMATE


def call_record(skill, call_source, prompt_args, response, seconds):
    token_source, i, o, anomaly = classify_usage(response)
    if token_source == ESTIMATED:
        i, o = estimate(prompt_args, response)
    return {"skill": skill, "call_source": call_source, "token_source": token_source,
            "input_tokens": i, "output_tokens": o, "anomaly": anomaly, "seconds": seconds,
            "prompt_chars": len(str(prompt_args)), "response_chars": len(str(getattr(response, "content", "")))}


def aggregate(calls, input_price_per_m=None, output_price_per_m=None):
    """Totals per provenance class; cost only where it is meaningful."""
    classes = {}
    for c in calls:
        key = f"{c['call_source']}/{c['token_source']}"
        b = classes.setdefault(key, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "anomalies": 0,
                                     "by_skill": {}})
        b["calls"] += 1
        b["input_tokens"] += c["input_tokens"]
        b["output_tokens"] += c["output_tokens"]
        b["anomalies"] += 1 if c.get("anomaly") else 0
        s = b["by_skill"].setdefault(c["skill"], {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        s["calls"] += 1
        s["input_tokens"] += c["input_tokens"]
        s["output_tokens"] += c["output_tokens"]
    for key, b in classes.items():
        billable = key == f"{PROVIDER}/{REPORTED}"
        if billable and input_price_per_m is not None and output_price_per_m is not None:
            b["cost_usd_estimate"] = round(b["input_tokens"] / 1e6 * input_price_per_m
                                           + b["output_tokens"] / 1e6 * output_price_per_m, 6)
            b["cost_note"] = "provider-reported tokens x configured list prices (an estimate)"
        else:
            b["cost_usd_estimate"] = None
            b["cost_note"] = ("not a provider call: tokens are character estimates of scripted prompts; never "
                              "cost or savings" if key.startswith(MOCK) else
                              "no list prices given" if billable else
                              "provider call without usable usage metadata: estimate only, not billable evidence")
    return {"classes": dict(sorted(classes.items())), "total_calls": sum(b["calls"] for b in classes.values()),
            "provider_calls": sum(b["calls"] for k, b in classes.items() if k.startswith(PROVIDER)),
            "mock_calls": sum(b["calls"] for k, b in classes.items() if k.startswith(MOCK))}
