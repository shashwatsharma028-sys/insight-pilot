"""
UI-2: Streamlit AppTest coverage of v2/app_page.py through the OFFLINE entry tests/v2/ui2/offline_app.py.
Every model reply comes from an injected mock; no network, key or live call.
"""

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tests.v2.ui1.test_app_service_ui1 import FLEET  # noqa: E402
from v2 import app_page as v2_page  # noqa: E402
from v2.app_service import Phase  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
APP = os.path.join(ROOT, "tests", "v2", "ui2", "offline_app.py")
GOAL = "Which depot spends the most on maintenance?"


def _app(tmp_path, **state):
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["ip_v2_test_base_dir"] = str(tmp_path)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    _ok(at)
    return at


def _ok(at):
    assert not at.exception, [e.value for e in at.exception]


def _svc(at):
    return at.session_state["ip_v2_service"]


def _calls(at):
    return at.session_state["ip_v2_test_mock"].calls


def _upload(at, name="fleet.csv", data=FLEET):
    at.file_uploader(key="ip_v2_file").upload(name, data, "text/csv").run()
    _ok(at)


def _ask(at, goal=GOAL):
    at.text_area(key="ip_v2_goal").input(goal).run()
    at.button(key="ip_v2_start").click().run()
    _ok(at)


def _texts(at):
    """Every rendered text element EXCEPT the constant stylesheet (asserted separately)."""
    out = []
    for kind in ("markdown", "caption", "info", "warning", "error", "success", "title"):
        out += [str(e.value) for e in getattr(at, kind) if not (kind == "markdown" and e.proto.allow_html)]
    for m in at.metric:
        out += [str(m.label), str(m.value)]
    return out


def _plain(at):
    """What a reader sees: every rendered text with Markdown backslash-escapes removed."""
    import re
    return re.sub(r"\\(.)", r"\1", " ".join(_texts(at)))


def _df_rows(at):
    rows = []
    for d in at.dataframe:
        rows += d.value.to_dict("records")
    return rows


# ── dataset workspace ───────────────────────────────────────────────────────────────────────────────────

def test_initial_page_is_labelled_offline_and_cannot_start_without_a_dataset(tmp_path):
    at = _app(tmp_path)
    assert any("Offline mock provider" in w.value for w in at.warning)
    assert at.button(key="ip_v2_start").disabled and "No dataset loaded." in [c.value for c in at.caption]
    assert at.text_area(key="ip_v2_goal").disabled


def test_upload_shows_size_columns_and_schema(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Rows"] == "30" and metrics["Columns"] == "5" and metrics["Missing values"] == "0"
    assert any(r.get("Column") == "depot" and r.get("Type") == "text" for r in _df_rows(at))
    assert any("Dataset loaded" in s.value for s in at.success)
    assert at.button(key="ip_v2_start").disabled                                  # no goal yet
    at.text_area(key="ip_v2_goal").input(GOAL).run()
    assert not at.button(key="ip_v2_start").disabled


def test_invalid_upload_shows_a_safe_error(tmp_path):
    at = _app(tmp_path)
    _upload(at, "bad.csv", b"a,b\n1,2\n3\n")
    assert any("could not be read" in e.value for e in at.error) and _svc(at).dataset is None


def test_invalid_goal_is_refused_with_no_model_call(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at, "ab")
    assert any("goal must be" in e.value for e in at.error) and at.session_state["ip_v2_test_mock"].calls == []


# ── plan presentation and approval ──────────────────────────────────────────────────────────────────────

def test_the_plan_shows_exact_operations_and_nothing_is_computed_before_approval(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    assert _svc(at).phase == Phase.AWAITING_APPROVAL and _calls(at) == ["planner"]
    assert any("Nothing has been computed yet" in i.value for i in at.info)
    rows = [r for r in _df_rows(at) if "Arguments (exact)" in r]
    by_key = {r["Claim"]: r for r in rows}
    a = _svc(at).approval
    for t in a.tasks:
        for c in t.claims:
            assert by_key[c.key]["Operation"] == c.op
            assert by_key[c.key]["Arguments (exact)"] == json.dumps(c.args, sort_keys=True, ensure_ascii=False)
    assert at.multiselect(key=f"ip_v2_keep_{a.plan_id}").value == ["t1", "t2"]


def test_approve_shows_verified_results_insights_activity_and_mock_usage(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    at.button(key="ip_v2_approve").click().run()
    _ok(at)
    assert _svc(at).phase == Phase.COMPLETED and _calls(at).count("planner") == 1
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["top_depot"] == "N" and metrics["serviced_rate"] == "0.666667"
    assert any("Analysis completed" in s.value for s in at.success)
    text = " ".join(_texts(at))
    assert "Based on" in text and "does not prove" in text.replace("\\", "")
    assert any("MOCK provider" in w.value for w in at.warning)                   # usage tab label
    assert metrics["Provider attempts"] == "3" and metrics["Failed"] == "0"
    rows = _df_rows(at)
    assert any(r.get("Status") == "completed" for r in rows)                      # task table
    assert any("Event" in r for r in rows) and any("Node" in r for r in rows)     # recorded activity, timings
    assert any(r.get("Group") == "N" for r in rows)                               # mapping result table


def test_the_question_is_kept_after_an_analysis_so_a_new_one_can_start(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    at.button(key="ip_v2_approve").click().run()
    assert at.text_area(key="ip_v2_goal").value == GOAL and not at.button(key="ip_v2_start").disabled


def test_reject_analyses_nothing(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    at.button(key="ip_v2_reject").click().run()
    _ok(at)
    assert _svc(at).phase == Phase.REJECTED and _calls(at) == ["planner"]
    assert any("rejected" in i.value for i in at.info)


def test_keeping_a_subset_runs_only_the_selected_tasks(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    a = _svc(at).approval
    at.multiselect(key=f"ip_v2_keep_{a.plan_id}").set_value(["t2"]).run()
    at.button(key="ip_v2_approve").click().run()
    _ok(at)
    r = _svc(at).result
    assert {c.key for c in r.verified_claims} == {"serviced_rate", "mean_km"} and _calls(at).count("business_insight") == 1


def test_duplicate_and_stale_decisions_are_refused_and_reported(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    a = _svc(at).approval
    at.session_state["ip_v2_pending"] = {"action": "decide", "run_id": a.run_id, "plan_id": "0" * 64,
                                         "decision": "approve", "keep": None}
    at.run()
    assert any("different or outdated plan" in w.value for w in at.warning) and _svc(at).phase == Phase.AWAITING_APPROVAL
    at.button(key="ip_v2_approve").click().run()
    calls = len(_calls(at))
    at.session_state["ip_v2_pending"] = {"action": "decide", "run_id": a.run_id, "plan_id": a.plan_id,
                                         "decision": "approve", "keep": None}  # a second click arriving late
    at.run()
    assert any("no plan waiting" in w.value for w in at.warning) and len(_calls(at)) == calls


def test_decision_controls_are_absent_once_the_plan_is_decided(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    at.button(key="ip_v2_approve").click().run()
    assert "ip_v2_approve" not in [b.key for b in at.button] and "ip_v2_reject" not in [b.key for b in at.button]


# ── partial, failed and review-only analyses ────────────────────────────────────────────────────────────

def _bad_plan():
    from tests.v2.ui1.test_app_service_ui1 import PLAN
    p = json.loads(json.dumps(PLAN))
    p["tasks"][1]["claims"][0]["args"]["value"] = True
    return p


def test_abandoned_tasks_are_shown_in_the_plan_and_results(tmp_path):
    at = _app(tmp_path, ip_v2_test_plans=[_bad_plan()], ip_v2_test_config={"selective_recovery": True})
    _upload(at)
    _ask(at)
    assert any("could not be repaired" in w.value for w in at.warning)
    at.button(key="ip_v2_approve").click().run()
    _ok(at)
    assert _svc(at).phase == Phase.COMPLETED_WITH_FAILURES and any("some tasks failed" in w.value for w in at.warning)
    assert any(r.get("Task") == "t2" and "Problems" in r for r in _df_rows(at))


def test_an_invalid_plan_is_reported_as_a_failure(tmp_path):
    at = _app(tmp_path, ip_v2_test_plans=[_bad_plan()])
    _upload(at)
    _ask(at)
    assert _svc(at).phase == Phase.FAILED and any("plan\\_invalid" in e.value for e in at.error)


def test_review_only_results_are_never_shown_as_accepted(tmp_path):
    at = _app(tmp_path, ip_v2_test_config={"max_steps": 6})
    _upload(at)
    _ask(at)
    at.button(key="ip_v2_approve").click().run()
    _ok(at)
    r = _svc(at).result
    assert not r.complete and r.verified_claims == () and r.review_only_claims
    assert any("NOT accepted" in w.value for w in at.warning)
    assert not any(row.get("Status") == "verified, accepted" for row in _df_rows(at))
    assert any(row.get("Status") == "verified, review only" for row in _df_rows(at))


def test_a_provider_failure_is_shown_safely(tmp_path):
    at = _app(tmp_path, ip_v2_test_fail={"planner": "HTTP 503 Bearer sk-TOPSECRET123456 /srv/keys/k.pem"})
    _upload(at)
    _ask(at)
    text = _plain(at)
    assert _svc(at).phase == Phase.FAILED and "TOPSECRET" not in text and "/srv/keys" not in text


# ── session usage, reset and isolation ──────────────────────────────────────────────────────────────────

def test_cumulative_usage_is_shown_and_survives_reset(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    at.button(key="ip_v2_approve").click().run()
    at.button(key="ip_v2_reset").click().run()
    _ok(at)
    progress = " ".join(p.proto.text for p in at.get("progress"))
    assert "Analyses: 1 of 20" in progress and "Provider attempts: 3 of 200" in progress
    assert "Estimated spend\\: \\$" in progress and " of \\$0\\.25" in progress    # escaped: no LaTeX span
    assert _svc(at).phase == Phase.IDLE and any("Session usage is kept" in i.value for i in at.info)


def test_usage_limit_disables_new_analyses(tmp_path):
    at = _app(tmp_path, ip_v2_test_config={"limits": {"max_runs_per_session": 1}})
    _upload(at)
    _ask(at)
    at.button(key="ip_v2_reject").click().run()
    assert at.button(key="ip_v2_start").disabled and at.text_area(key="ip_v2_goal").disabled


def test_end_session_deletes_files_and_a_new_session_starts_clean(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    d = _svc(at)._dir
    at.button(key="ip_v2_close").click().run()
    _ok(at)
    assert not os.path.exists(d) and "ip_v2_service" not in at.session_state
    assert any("session has ended" in i.value for i in at.info)
    at.button(key="ip_v2_new").click().run()
    assert _svc(at)._dir != d and _svc(at).session_usage().runs_started == 0


def test_two_sessions_are_isolated(tmp_path):
    a1, a2 = _app(tmp_path), _app(tmp_path)
    _upload(a1)
    _upload(a2, "fleet.csv", FLEET)
    assert _svc(a1) is not _svc(a2) and _svc(a1)._dir != _svc(a2)._dir
    _ask(a1)
    assert _svc(a1).phase == Phase.AWAITING_APPROVAL and _svc(a2).phase == Phase.IDLE
    assert a2.session_state["ip_v2_test_mock"].calls == []


def test_remove_dataset_clears_the_workspace(tmp_path):
    at = _app(tmp_path)
    _upload(at)
    at.button(key="ip_v2_remove").click().run()
    assert _svc(at).dataset is None and os.listdir(_svc(at)._data_dir) == []


# ── untrusted text never renders as HTML or Markdown ────────────────────────────────────────────────────

EVIL = ('<script>alert(1)</script> <img src=x onerror=alert(2)> [click](javascript:alert(3)) '
        '![pic](https://evil.example/p.png) **bold** `code` # Heading https://evil.example/x '
        'api_key=LEAKME123456 /home/user/.ssh/id_rsa $x$')


def _evil_plan():
    return {"tasks": [{"id": "t1", "title": EVIL[:120], "description": EVIL, "analysis_type": "descriptive",
                       "claims": [{"key": "total", "op": "sum", "args": {"column": "cost"}}]}]}


def _assert_inert(at):
    import re
    html_blocks = [m for m in at.markdown if m.proto.allow_html]
    assert [m.value.strip() for m in html_blocks] == [v2_page._CSS.strip()]    # only the constant stylesheet
    for body in _texts(at):
        inert = re.sub(r"\\[!-/:-@\[-`{-~]", "", body)                         # drop backslash-escaped characters
        for raw in ("<", "](", "![", "`", "javascript:", "**bold**", "$x$"):
            assert raw not in inert, (raw, body[:160])                            # nothing unescaped survives
    assert "LEAKME123456" not in _plain(at)                                       # credential-like token redacted


def test_malicious_strings_render_as_inert_text(tmp_path):
    at = _app(tmp_path, ip_v2_test_plans=[_evil_plan()],
              ip_v2_test_insight={"insights": [{"text": EVIL.replace("1", "").replace("2", "").replace("3", ""),
                                                "cites": ["total"]}]})
    evil_csv = ("<b>depot</b>,cost\n" + "\n".join(f"x{i},{i}" for i in range(5)) + "\n").encode()
    _upload(at, "<img src=x>[a](javascript:b).csv", evil_csv)
    _assert_inert(at)
    _ask(at, "Show <script>alert(9)</script> [x](javascript:y) totals")
    _assert_inert(at)                                                             # plan screen: title, description, goal
    plan_text = " ".join(_texts(at))
    assert "\\<script\\>" in plan_text and "\\[click\\]" in plan_text          # visible, escaped, as text
    assert any(r.get("Column") == "<b>depot</b>" for r in _df_rows(at))           # dataframes: exact plain text
    at.button(key="ip_v2_approve").click().run()
    _ok(at)
    _assert_inert(at)                                                             # results screen
    assert _svc(at).phase in (Phase.COMPLETED, Phase.COMPLETED_WITH_FAILURES)


def test_md_escapes_every_markdown_construct():
    for s in ("[a](b)", "![a](b)", "<i>x</i>", "**x**", "_x_", "`x`", "# h", "https://x.y", "$x$", "a|b", "~~x~~"):
        e = v2_page.md(s)
        assert all(e[i - 1] == "\\" for i, ch in enumerate(e) if ch in "[]()!<>*_`#:$|~/." and i > 0) or e[0] == "\\"


# ── failures, purity and V1 ─────────────────────────────────────────────────────────────────────────────

def test_an_unexpected_service_exception_shows_a_safe_message(tmp_path, monkeypatch):
    from v2.app_service import AnalysisService

    def boom(self, goal):
        raise RuntimeError("internal failure at /opt/secret/module.py")
    monkeypatch.setattr(AnalysisService, "start", boom)
    at = _app(tmp_path)
    _upload(at)
    _ask(at)
    assert any("Something went wrong" in e.value for e in at.error)
    assert "/opt/secret" not in _plain(at) and "internal failure" not in _plain(at) and not at.exception


def test_a_failing_factory_shows_a_safe_message(tmp_path, monkeypatch):
    from v2 import app_service

    def boom(*a, **k):
        raise RuntimeError("cannot create /var/lib/x")
    monkeypatch.setattr(app_service.AnalysisService, "__init__", boom)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert any("could not start" in e.value for e in at.error) and "/var/lib" not in _plain(at)
    assert not at.exception


def test_the_page_imports_no_tests_harness_v1_or_executor_modules():
    code = ("import sys; sys.path.insert(0, %r); import v2.app_page; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('tests', 'evaluation', 'agent', 'sandbox', 'sandbox_v2', 'utils', 'ui')))" % ROOT)
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).stdout.strip()
    assert out == "[]"
    src = open(os.path.join(ROOT, "v2", "app_page.py")).read()
    for forbidden in ("approve_all", "golden", "tasks_for", "CLAIM_SPECS", "Supervisor", "build_team", "getenv"):
        assert forbidden not in src.split('"""', 2)[2], forbidden                  # code after the docstring


def test_v1_app_is_byte_identical_and_untouched():
    out = subprocess.run(["git", "diff", "--stat", "ff618163", "--", "ui/app.py", "agent", "sandbox", "utils"],
                         capture_output=True, text=True, cwd=ROOT, check=True).stdout
    assert out.strip() == ""
