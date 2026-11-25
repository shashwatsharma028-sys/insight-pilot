"""
Outbound information control and output policy.

Local analytical data never changes: every value is computed, verified and shown to the user exactly. What changes
is only the PROVIDER-BOUND view built from it:
- a claim computed over a declared-sensitive value column (e.g. salary) is LOCAL-ONLY: it is not sent at all;
- group labels from a declared-sensitive column, or labels that look like emails or credentials (heuristic, not
  comprehensive PII detection), or labels carrying instruction-like/markup content, are replaced by stable
  pseudonyms (label_1, label_2, ...) with their numbers intact; the insight check grounds against the same view;
- a mapping with more than MAX_OUTBOUND_ENTRIES entries is local-only.
Every prompt is then checked as serialized, immediately before the provider call: over MAX_PROMPT_CHARS, or
containing an email address or credential-shaped string, the call is refused (explicit policy refusal).
"""

import re

from multi_agent.security import detect
from multi_agent.security.policy import PolicyRefused

MAX_PROMPT_CHARS = 200_000
MAX_REPLY_CHARS = 200_000
MAX_OUTBOUND_ENTRIES = 500
NEUTRAL_FILE_NAME = "dataset.csv"

_LABEL_ARG = {"group_sum": "by", "group_mean": "by", "argmax_group_sum": "by", "argmax_group_mean": "by",
              "value_counts": "column"}
_VALUE_ARGS = {"sum": ("column",), "mean": ("column",), "group_sum": ("column",), "group_mean": ("column",),
               "argmax_group_sum": ("column",), "argmax_group_mean": ("column",), "mean_where_equals": ("column",),
               "pearson": ("x", "y"), "ratio_of_sums_pct": ("numerator", "denominator")}


def check_prompt(authority, agent, skill, system, user, run_id=None):
    t0 = authority.clock()
    text = system + "\n" + user
    code = None
    if len(text) > MAX_PROMPT_CHARS:
        code = "prompt_too_large"
    elif detect.CREDENTIAL.search(text):
        code = "credential_in_outbound"
    elif detect.EMAIL.search(text):
        code = "email_in_outbound"
    if code:
        authority.emit("deny", code, "outbound", t0, agent=agent, run_id=run_id, location="provider_prompt")
        raise PolicyRefused(code, f"provider call refused by the outbound policy ({code})")
    authority.emit("allow", "outbound_permitted", "outbound", t0, agent=agent, run_id=run_id, location="provider_prompt")


def check_reply(authority, agent, content, run_id=None) -> bool:
    t0 = authority.clock()
    if len(content) > MAX_REPLY_CHARS:
        authority.emit("deny", "reply_too_large", "resource", t0, agent=agent, run_id=run_id, location="provider_reply")
        return False
    authority.emit("allow", "reply_size_ok", "resource", t0, agent=agent, run_id=run_id, location="provider_reply")
    return True


def model_prose(authority, text, task_id=None):
    """Model-written prose (task titles/descriptions) sent back to the provider: email addresses and credential-shaped
    strings are replaced by [redacted] (recorded). Prose is never an analytical value, so nothing computed changes."""
    if not isinstance(text, str):
        return text
    out = detect.CREDENTIAL.sub("[redacted]", detect.EMAIL.sub("[redacted]", text))
    if out != text:
        authority.emit("redact", "model_prose_redacted", "outbound", task_id=task_id, location="model_output")
    return out


def scan(authority, text, location, run_id=None, task_id=None, agent=None):
    codes = detect.signals(text, location)
    if codes:
        authority.emit("signal", "injection_signal" if detect.is_injection(codes) else "sensitive_pattern",
                       "detection", agent=agent, run_id=run_id, task_id=task_id, location=location, signals=codes)
    return codes


def scan_inputs(authority, goal, profile, run_id=None):
    """Signals for the user's goal, every dataset header and the file name (no effect on any decision)."""
    scan(authority, goal, "goal", run_id)
    for col in profile.get("columns") or []:
        scan(authority, col.get("name"), "dataset_header", run_id)
    scan(authority, profile.get("file_name"), "file_name", run_id)


def planner_profile(authority, profile, run_id=None) -> dict:
    """The provider-bound profile: the file name is not needed for planning, so a name carrying an identifier,
    a credential or an instruction is replaced by a neutral one (recorded). Local profile unchanged."""
    t0 = authority.clock()
    name = profile.get("file_name")
    codes = detect.signals(name, "file_name") if isinstance(name, str) else []
    if codes and (detect.is_injection(codes) or {"email_address", "credential_pattern", "link_or_markup"} & set(codes)):
        authority.emit("redact", "file_name_withheld", "outbound", t0, run_id=run_id, location="file_name")
        return dict(profile, file_name=NEUTRAL_FILE_NAME)
    return profile


def insight_view(authority, verified, claims_spec, sensitive_columns, run_id=None, task_id=None):
    """(view, local_only_keys): the verified claims as they may be sent to the provider."""
    t0 = authority.clock()
    sensitive = set(sensitive_columns or ())
    spec = {cl["key"]: cl for cl in claims_spec}
    view, local_only, pseudo_count, quarantined = {}, [], 0, set()
    mapping = {}                                   # label column -> {label: pseudonym}, stable within the task

    def labels_of(key, value):
        if isinstance(value, dict):
            return list(value)
        return [value] if isinstance(value, str) else []

    def must_pseudonymize(col, labels):
        bad = [x for x in labels if detect.quarantine_label(x)]
        for x in bad:                                  # signals are recorded whatever else applies
            quarantined.update(detect.signals(x, "dataset_value"))
        return bool(bad) or col in sensitive or any(detect.sensitive_label(x) for x in labels)

    plan = {}
    for key, value in verified.items():
        cl = spec.get(key) or {}
        op, args = cl.get("op"), cl.get("args") or {}
        if any(args.get(a) in sensitive for a in _VALUE_ARGS.get(op, ())):
            local_only.append(key)
            continue
        if isinstance(value, dict) and len(value) > MAX_OUTBOUND_ENTRIES:
            local_only.append(key)
            continue
        col = args.get(_LABEL_ARG[op]) if op in _LABEL_ARG else None
        plan[key] = (col, value)
        if col is not None:
            mapping.setdefault(col, set()).update(labels_of(key, value))
    pseudo_cols = {col for col, labels in mapping.items() if must_pseudonymize(col, sorted(labels, key=str))}
    names = {col: {lab: f"label_{i + 1}" for i, lab in enumerate(sorted(mapping[col], key=str))} for col in pseudo_cols}
    for key, (col, value) in plan.items():
        if col in pseudo_cols:
            pseudo_count += 1
            if isinstance(value, dict):
                value = {names[col][k]: v for k, v in value.items()}
            elif isinstance(value, str):
                value = names[col].get(value, "label_?")
        view[key] = value
    if quarantined:
        authority.emit("signal", "injection_signal", "detection", run_id=run_id, task_id=task_id,
                       location="dataset_value", signals=quarantined)
    if local_only or pseudo_count:
        authority.emit("redact", "insight_request_minimised", "outbound", t0, run_id=run_id, task_id=task_id,
                       location="insight_request", count=len(local_only) + pseudo_count)
    else:
        authority.emit("allow", "insight_request_unchanged", "outbound", t0, run_id=run_id, task_id=task_id,
                       location="insight_request")
    return view, local_only


_LINK = re.compile(r"(https?://|ftp://|www\.|javascript:|data:)", re.I)
_MARKUP = re.compile(r"(<\s*/?\s*[a-zA-Z!][^>]*>|!\[[^\]]*\]\(|\[[^\]]*\]\([^)]*\))")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤⁦-⁯﻿]")


def unsafe_output(authority, insights, view, run_id=None, task_id=None) -> list:
    """Structural output policy for model-written insights: no links, markup, email addresses, credential-shaped
    strings or control/bidi characters (text equal to a cited label is exempt). Not keyword blocking: a request for a
    password written in plain words is only a detection signal (see detect)."""
    t0 = authority.clock()
    problems = []
    for i, ins in enumerate(insights or []):
        text = ins.get("text") if isinstance(ins, dict) else None
        if not isinstance(text, str):
            continue
        scrub = text
        for lab in sorted({k for v in view.values() if isinstance(v, dict) for k in v} |
                          {v for v in view.values() if isinstance(v, str)}, key=len, reverse=True):
            scrub = scrub.replace(lab, " ")
        found = [name for name, rx in (("a link", _LINK), ("markup", _MARKUP), ("an email address", detect.EMAIL),
                                       ("a credential-like string", detect.CREDENTIAL), ("control characters", _CONTROL))
                 if rx.search(scrub)]
        if found:
            problems.append(f"insight {i}: contains {', '.join(found)}, which insights may not include")
        codes = detect.signals(text, "model_output")
        if codes:
            authority.emit("signal", "injection_signal" if detect.is_injection(codes) else "sensitive_pattern",
                           "detection", run_id=run_id, task_id=task_id, location="model_output", signals=codes)
    authority.emit("deny" if problems else "allow", "unsafe_output" if problems else "output_permitted", "output", t0,
                   agent="business_insight_agent", run_id=run_id, task_id=task_id, location="model_output",
                   count=len(problems) or None)
    return problems
