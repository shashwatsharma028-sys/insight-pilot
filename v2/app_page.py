"""
InsightPilot V2 page (UI-2, v2/app_page.py): a Streamlit front end for v2.app_service.AnalysisService.

    render(service_factory, provider_label=None)

- One AnalysisService per Streamlit session, created by the injected `service_factory` and kept in
  st.session_state. Nothing user-specific is cached globally. The page never touches LangGraph state: it renders
  the service's typed view models only, and all orchestration stays in the service and the V2 Supervisor.
- Untrusted text (dataset headers, goals, model titles, descriptions, insights, messages) never reaches HTML. It is
  shown with native widgets, either through `md()` (every Markdown/HTML-significant character escaped) or in
  dataframes (plain text). The only unsafe_allow_html call injects a constant stylesheet.
- Decisions carry the run_id and plan_id of the plan the user saw (bound at render time through on_click args);
  the service refuses duplicate, stale and concurrent decisions.
- The service is synchronous. While a call runs, an honest spinner is shown; recorded activity is shown after the
  call returns. No progress is simulated.

Not wired to any entry point in this phase (no launcher admission change). It lives in v2/, not ui/: the
V1 folders (ui/, agent/, sandbox/, utils/) stay untouched, and ui/app.py (V1) is byte-identical.
"""

import hashlib
import json
import re

import streamlit as st

from multi_agent.claims import OP_RESULTS
from v2.app_service import BUSY, TERMINAL, VERIFICATION_SCOPE, Phase, ServiceError

SERVICE_KEY = "ip_v2_service"
PENDING_KEY = "ip_v2_pending"
FLASH_KEY = "ip_v2_flash"
UPLOAD_SIG_KEY = "ip_v2_upload_sig"
CLOSED_KEY = "ip_v2_closed"
LAST_GOAL_KEY = "ip_v2_last_goal"

# Constant styling only (no interpolation). Colours come from the active Streamlit light/dark theme.
_CSS = """
<style>
div[data-testid="stMetric"] {padding: 0.55rem 0.75rem; border: 1px solid rgba(128,128,128,0.25);
  border-radius: 0.5rem;}
div[data-testid="stMetricValue"] {font-size: 1.35rem;}
div[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 0.6rem;}
section.main > div {padding-top: 1.2rem;}
</style>
"""

_MD_PUNCT = re.compile(r"([!-/:-@\[-`{-~])")


def md(text) -> str:
    """Escape every ASCII punctuation character so untrusted text renders literally: no links, images, HTML,
    emphasis, code, headings, tables, autolinks or LaTeX."""
    return _MD_PUNCT.sub(r"\\\1", str(text if text is not None else ""))


def _fmt(v) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.6g}"
    return str(v)


# ── session plumbing ─────────────────────────────────────────────────────────────────────────────────────

def _service(factory):
    if st.session_state.get(CLOSED_KEY):
        return None
    svc = st.session_state.get(SERVICE_KEY)
    if svc is None:
        try:
            svc = factory()
        except Exception:                                           # never show a traceback
            st.error("The analysis service could not start. Please try again later.")
            return None
        st.session_state[SERVICE_KEY] = svc
    return svc


def _flash(kind, text):
    st.session_state[FLASH_KEY] = (kind, str(text))


def _show_flash():
    item = st.session_state.pop(FLASH_KEY, None)
    if item:
        kind, text = item
        getattr(st, kind if kind in ("success", "info", "warning", "error") else "info")(md(text))


def _queue(action, **kw):
    """on_click callback: remember the action with the identifiers bound at render time."""
    st.session_state[PENDING_KEY] = {"action": action, **kw}


def _queue_start():
    """The goal is read when the button is clicked (callbacks run after widget values are updated)."""
    goal = st.session_state.get("ip_v2_goal", "")
    st.session_state[LAST_GOAL_KEY] = goal                      # restored if Streamlit drops the widget state
    st.session_state[PENDING_KEY] = {"action": "start", "goal": goal}


def _queue_decide(run_id, plan_id, decision, ids):
    """run_id / plan_id are those of the plan RENDERED to the user; the task selection is read at click time."""
    keep = list(st.session_state.get(f"ip_v2_keep_{plan_id}", ids) or [])
    if decision != "reject":
        decision = "approve" if set(keep) == set(ids) else "edit"
    st.session_state[PENDING_KEY] = {"action": "decide", "run_id": run_id, "plan_id": plan_id, "decision": decision,
                                     "keep": keep if decision == "edit" else None}


def _run_pending(svc):
    pending = st.session_state.pop(PENDING_KEY, None)
    if not pending:
        return
    action = pending["action"]
    try:
        if action == "start":
            with st.spinner("Planning: the Planning Agent is proposing tasks..."):
                svc.start(pending["goal"])
        elif action == "decide":
            label = "Rejecting the plan..." if pending["decision"] == "reject" else \
                "Running the approved tasks: trusted computation, verification and insights..."
            with st.spinner(label):
                svc.decide(pending["run_id"], pending["plan_id"], pending["decision"],
                           keep_task_ids=pending.get("keep"))
        elif action == "reset":
            svc.reset(clear_dataset=pending.get("clear_dataset", False))
            st.session_state.pop(UPLOAD_SIG_KEY, None)
            _flash("info", "The analysis was reset. Session usage is kept.")
        elif action == "close":
            svc.close()
            st.session_state.pop(SERVICE_KEY, None)
            st.session_state[CLOSED_KEY] = True
    except ServiceError as e:
        _flash("warning" if e.code in ("stale", "not_awaiting", "busy") else "error", e.message)
    except Exception:
        _flash("error", "Something went wrong. Reset the analysis and try again.")
    st.rerun()


# ── sections ─────────────────────────────────────────────────────────────────────────────────────────────

def _sidebar(svc):
    with st.sidebar:
        st.markdown("### Session")
        u = svc.session_usage()
        st.caption(md(u.usage_label) if u.usage_source != "none" else "No provider calls yet.")
        st.progress(min(1.0, u.runs_started / max(1, u.max_runs)), text=f"Analyses: {u.runs_started} of {u.max_runs}")
        st.progress(min(1.0, u.provider_calls / max(1, u.max_calls)),
                    text=f"Provider attempts: {u.provider_calls} of {u.max_calls}")
        st.progress(min(1.0, u.tokens / max(1, u.max_tokens)), text=f"Tokens: {u.tokens:,} of {u.max_tokens:,}")
        st.progress(min(1.0, u.estimated_spend_usd / max(1e-12, u.budget_usd)),          # "$..$" would be LaTeX
                    text=md(f"Estimated spend: ${u.estimated_spend_usd:.4f} of ${u.budget_usd:.2f}"))
        if u.limit_reached:
            st.warning("The session usage limit has been reached. New analyses are disabled.")
        busy = svc.phase in BUSY
        st.markdown("### Controls")
        st.button("Reset analysis", key="ip_v2_reset", disabled=busy, width="stretch",
                  on_click=_queue, args=("reset",), help="Discards the current analysis. Session usage is kept.")
        st.button("Remove dataset", key="ip_v2_remove", disabled=busy or svc.dataset is None,
                  width="stretch", on_click=_queue, args=("reset",), kwargs={"clear_dataset": True})
        st.button("End session", key="ip_v2_close", disabled=busy, width="stretch",
                  on_click=_queue, args=("close",), help="Deletes this session's files.")


def _dataset_section(svc):
    lim = svc.limits
    st.markdown("#### 1. Dataset")
    can_change = svc.phase not in BUSY and svc.phase != Phase.AWAITING_APPROVAL
    f = st.file_uploader(
        "Upload a CSV dataset", type=["csv"], key="ip_v2_file", disabled=not can_change,
        help=(f"UTF-8 CSV with a header row; up to {lim.max_upload_bytes // (1024 * 1024)} MB, "
              f"{lim.max_rows:,} rows, {lim.max_columns} columns and {lim.max_column_name_chars}-character names."))
    if f is not None and can_change:
        data = f.getvalue()
        sig = (f.name, hashlib.sha256(data).hexdigest())
        if st.session_state.get(UPLOAD_SIG_KEY) != sig:
            st.session_state[UPLOAD_SIG_KEY] = sig
            try:
                svc.upload(f.name, data)
                _flash("success", "Dataset loaded.")
            except ServiceError as e:
                _flash("error", e.message)
            st.rerun()
    ds = svc.dataset
    if ds is None:
        st.caption("No dataset loaded.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Rows", f"{ds.rows:,}")
    c2.metric("Columns", f"{len(ds.columns):,}")
    c3.metric("Missing values", f"{sum(c.missing for c in ds.columns):,}")
    st.markdown("**Dataset:** " + md(ds.display_name))
    with st.expander("Column schema", expanded=False):
        st.dataframe([{"Column": c.name, "Type": c.kind, "Missing": c.missing} for c in ds.columns],
                     hide_index=True, width="stretch")


def _question_section(svc):
    lim = svc.limits
    st.markdown("#### 2. Question")
    startable = svc.dataset is not None and svc.phase in (Phase.IDLE,) + tuple(TERMINAL) \
        and not svc.session_usage().limit_reached
    if not st.session_state.get("ip_v2_goal") and st.session_state.get(LAST_GOAL_KEY):
        st.session_state["ip_v2_goal"] = st.session_state[LAST_GOAL_KEY]   # keep the question across runs
    goal = st.text_area("What do you want to know about this dataset?", key="ip_v2_goal",
                        max_chars=lim.max_goal_chars, disabled=not startable, height=90,
                        placeholder="For example: Which depot spends the most on maintenance?")
    st.button("Start analysis", key="ip_v2_start", type="primary", disabled=not startable or not goal.strip(),
              on_click=_queue_start)
    st.caption(f"{lim.min_goal_chars} to {lim.max_goal_chars} characters. Nothing is computed before you approve "
               "the plan.")


def _plan_section(svc):
    a = svc.approval
    if a is None:
        return
    st.markdown("#### 3. Plan for approval")
    st.info("Nothing has been computed yet. These are the exact operations that will run if you approve them.")
    st.markdown("**Question:** " + md(a.goal))
    for t in a.tasks:
        with st.container(border=True):
            st.markdown(f"**{md(t.title)}**  \n" + md(f"Task {t.task_id} · {t.analysis_type}"))
            if t.description:
                st.markdown(md(t.description))
            st.dataframe([{"Claim": c.key, "Operation": c.op,
                           "Arguments (exact)": json.dumps(c.args, sort_keys=True, ensure_ascii=False),
                           "Returns": OP_RESULTS.get(c.op, "")} for c in t.claims],
                         hide_index=True, width="stretch")
    if getattr(a, "security_signals", ()):
        st.warning("Security signals were recorded for this request (detection only: review the plan carefully; "
                   "the deterministic policies still apply whatever you decide):")
        st.dataframe([{"Where": x.location, "Signals": ", ".join(x.signals)} for x in a.security_signals],
                     hide_index=True, width="stretch")
    if a.abandoned_tasks:
        st.warning("These tasks could not be repaired and will not run:")
        st.dataframe([{"Task": x.task_id, "Title": x.title or "", "Claims": ", ".join(x.claim_keys),
                       "Problems": "; ".join(x.problems)} for x in a.abandoned_tasks],
                     hide_index=True, width="stretch")
    ids = [t.task_id for t in a.tasks]
    keep = st.multiselect("Tasks to run", options=ids, default=ids, key=f"ip_v2_keep_{a.plan_id}",
                          format_func=lambda i: next((f"{i}: {t.title}" for t in a.tasks if t.task_id == i), i))
    c1, c2 = st.columns(2)
    c1.button("Approve selected tasks", key="ip_v2_approve", type="primary", disabled=not keep,
              width="stretch", on_click=_queue_decide, args=(a.run_id, a.plan_id, "approve", ids))
    c2.button("Reject plan", key="ip_v2_reject", width="stretch", on_click=_queue_decide,
              args=(a.run_id, a.plan_id, "reject", ids))
    st.caption(f"{len(keep)} of {len(ids)} tasks selected.")


def _claims_block(claims, accepted: bool):
    scalars = [c for c in claims if c.result is None or c.result.kind != "mapping"]
    mappings = [c for c in claims if c.result is not None and c.result.kind == "mapping"]
    for i in range(0, len(scalars), 4):
        cols = st.columns(4)
        for col, c in zip(cols, scalars[i:i + 4]):
            col.metric(c.key, _fmt(c.value) + (" (truncated)" if c.result and c.result.truncated else ""))
    for c in mappings:
        with st.container(border=True):
            st.markdown(f"**{md(c.key)}**")
            st.dataframe([{"Group": k, "Value": v} for k, v in c.value.items()], hide_index=True,
                         width="stretch")
            if c.result.truncated:
                st.caption(f"Showing {c.result.shown_entries:,} of {c.result.total_entries:,} groups (truncated).")
    rows = [{"Claim": c.key, "Task": c.task_id or "", "Operation": c.spec.op if c.spec else "",
             "Arguments": json.dumps(c.spec.args, sort_keys=True, ensure_ascii=False) if c.spec else "",
             "Status": "verified, accepted" if accepted else "verified, review only"} for c in claims]
    with st.expander("How each value was computed", expanded=False):
        st.dataframe(rows, hide_index=True, width="stretch")


def _insights_block(insights):
    for ins in insights:
        with st.container(border=True):
            st.markdown(md(ins.text))
            st.caption("Based on: " + md(", ".join(ins.cites)))


def _results_section(svc):
    r = svc.result
    if r is None:
        return
    st.markdown("#### 4. Results")
    banner = {"completed": st.success, "completed_with_failures": st.warning, "rejected": st.info}.get(r.phase, st.error)
    banner(md(r.message) + (md(f" (code: {r.error_code})") if r.error_code else ""))
    st.caption(md(r.verification_scope or VERIFICATION_SCOPE))
    tabs = st.tabs(["Findings", "Tasks", "Activity", "Usage", "Security"])
    with tabs[0]:
        if r.verified_claims:
            st.markdown("**Verified results**")
            _claims_block(r.verified_claims, accepted=True)
        if r.insights:
            st.markdown("**Business insights**")
            _insights_block(r.insights)
        if r.review_only_claims and not r.complete:
            st.warning("The analysis did not complete. These values were verified, but they are NOT accepted "
                       "results: review them only.")
            _claims_block(r.review_only_claims, accepted=False)
            if r.review_only_insights:
                _insights_block(r.review_only_insights)
        if not (r.verified_claims or r.review_only_claims):
            st.caption("No results were produced.")
    with tabs[1]:
        if r.tasks:
            st.dataframe([{"Task": t.task_id, "Title": t.title, "Status": t.status, "Attempts": t.attempts,
                           "Failure": t.failure or ""} for t in r.tasks], hide_index=True, width="stretch")
        else:
            st.caption("No tasks ran.")
        if r.recovered_task_ids:
            st.caption("Recovered by selective task recovery: " + md(", ".join(r.recovered_task_ids)))
        if r.abandoned_tasks:
            st.markdown("**Abandoned tasks** (not run)")
            st.dataframe([{"Task": x.task_id, "Title": x.title or "", "Claims": ", ".join(x.claim_keys),
                           "Problems": "; ".join(x.problems)} for x in r.abandoned_tasks],
                         hide_index=True, width="stretch")
    with tabs[2]:
        st.caption("Recorded execution events, shown after the analysis returned. No live progress is simulated.")
        if r.activity:
            st.dataframe([{"Event": e.kind, "Detail": e.detail} for e in r.activity], hide_index=True,
                         width="stretch")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Time by graph node (s)**")
            st.dataframe([{"Node": k, "Seconds": v} for k, v in sorted(r.usage.seconds_by_node.items())],
                         hide_index=True, width="stretch")
        with c2:
            st.markdown("**Time in model calls by agent (s)**")
            st.dataframe([{"Agent": k, "Seconds": v} for k, v in sorted(r.usage.seconds_by_agent.items())],
                         hide_index=True, width="stretch")
    with tabs[3]:
        u = r.usage
        (st.warning if u.usage_source in ("mock", "mixed") else st.caption)(md(u.usage_label))
        c = st.columns(4)
        c[0].metric("Provider attempts", f"{u.provider_calls:,}")
        c[1].metric("Successful", f"{u.provider_calls - u.failed_provider_calls:,}")
        c[2].metric("Failed", f"{u.failed_provider_calls:,}")
        c[3].metric("Tokens (in / out)", f"{u.input_tokens:,} / {u.output_tokens:,}")
        c = st.columns(2)
        c[0].metric("Estimated cost of successful calls", f"${u.estimated_cost_usd:.6f}")
        c[1].metric("Conservative charges for failed attempts", f"${u.estimated_cost_failed_attempts_usd:.6f}")
        st.caption(md(u.conservative_label))
        if u.calls_by_skill:
            st.dataframe([{"Skill": k, "Attempts": v} for k, v in sorted(u.calls_by_skill.items())],
                         hide_index=True, width="stretch")
    with tabs[4]:
        _security_block(getattr(r, "security", None))


def _security_block(sv):
    """Measured security events of this analysis (counts and reason codes; no score, no payloads)."""
    if sv is None:
        st.caption("No security events were recorded.")
        return
    st.caption(md(f"Policy version {sv.policy_version}. Counts of recorded policy decisions for this analysis; "
                  "signals are detection only and never allow or block anything by themselves."))
    c = st.columns(4)
    c[0].metric("Policy decisions allowed", f"{sv.allowed:,}")
    c[1].metric("Refused", f"{sv.denied:,}")
    c[2].metric("Withheld from the provider", f"{sv.redacted:,}")
    c[3].metric("Detection signals", f"{sv.signals:,}")
    if sv.refusals:
        st.markdown("**Policy refusals**")
        st.dataframe([{"Reason": k, "Count": v} for k, v in sv.refusals], hide_index=True, width="stretch")
    if sv.signal_locations:
        st.markdown("**Detection signals by input**")
        st.dataframe([{"Where": k, "Count": v} for k, v in sv.signal_locations], hide_index=True, width="stretch")
    if sv.median_check_ms is not None:
        st.caption(md(f"Median policy-check time: {sv.median_check_ms:.3f} ms."))


# ── entry ────────────────────────────────────────────────────────────────────────────────────────────────

def render(service_factory, provider_label=None):
    """Render the V2 page for this Streamlit session. `service_factory()` returns an AnalysisService."""
    st.markdown(_CSS, unsafe_allow_html=True)                       # constant stylesheet only
    st.title("InsightPilot V2")
    st.caption("Multi-agent data analysis with human approval and independent verification.")
    if provider_label:
        st.warning(md(provider_label))
    if st.session_state.get(CLOSED_KEY):
        st.info("This session has ended and its files were deleted.")
        if st.button("Start a new session", key="ip_v2_new"):
            st.session_state.pop(CLOSED_KEY, None)
            st.rerun()
        return
    svc = _service(service_factory)
    if svc is None:
        return
    _run_pending(svc) if st.session_state.get(PENDING_KEY) else None
    _show_flash()
    _sidebar(svc)
    _dataset_section(svc)
    _question_section(svc)
    _plan_section(svc)
    _results_section(svc)
