"""Structured security audit events. Privacy-preserving by construction: only ids, codes, counts and timings.

An event never carries prompt text, dataset values, model output, file names or secrets. `signals` are fixed
signal CODES (detect.SIGNAL_CODES), `location` is one of LOCATIONS, and every other field is an identifier."""

import time
import uuid

POLICY_VERSION = "v2-sec-1"
DECISIONS = ("allow", "deny", "signal", "redact")
CATEGORIES = ("integrity", "capability", "outbound", "approval", "authorization", "output", "resource", "detection")
LOCATIONS = ("goal", "dataset_header", "file_name", "dataset_value", "model_output", "feedback", "provider_prompt",
             "provider_reply", "approval", "operation", "state", "insight_request")


def event(decision, reason_code, *, category, agent=None, session_id=None, run_id=None, task_id=None, location=None,
          signals=None, check_ms=None, count=None) -> dict:
    assert decision in DECISIONS and category in CATEGORIES, (decision, category)
    assert location is None or location in LOCATIONS, location
    return {"event_id": uuid.uuid4().hex, "ts": round(time.time(), 3), "session_id": _id(session_id),
            "run_id": _id(run_id), "task_id": _id(task_id), "agent": _id(agent), "decision": decision,
            "reason_code": str(reason_code)[:64], "category": category, "location": location,
            "signals": sorted(set(signals))[:12] if signals else None,
            "count": int(count) if count is not None else None,
            "policy_version": POLICY_VERSION, "check_ms": round(float(check_ms), 4) if check_ms is not None else None}


def _id(v):
    """Identifiers only: clipped, and only characters an id can contain."""
    if v is None:
        return None
    s = str(v)[:80]
    return "".join(ch for ch in s if ch.isalnum() or ch in "-_.:") or None
