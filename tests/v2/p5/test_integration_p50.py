"""P5.0 integration: real sandbox subprocess ports on the four demo datasets; V1 preserved; opt-in only."""

import ast
import os
import subprocess

import pytest

import sandbox.executor
from config.feature_flags import load_flags
from evaluation import multi_agent as ma
from evaluation.__main__ import main as eval_main
from evaluation.metrics import check_png
from multi_agent import is_enabled
from multi_agent.execution import CodeTrust, resolve_port
from multi_agent.team import build_team
from multi_agent.verification import values_match
from tests.v2.golden.reference import DATA_DIR, DATASETS, ROOT
from tests.v2.p5 import scripted

BASE = "d321785c9a891e09600dfb892d470cf3d30fb093"
EXPECTED_STATUS = {"sales": "completed_with_failures", "attendance": "completed", "student": "completed",
                   "ecommerce": "completed"}


@pytest.mark.parametrize("strategy", ["v1", "lazy-seaborn"])
@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_real_subprocess_pipeline_accepts_exactly_the_golden_answers(dataset, strategy):
    llm = scripted.ScriptedAgentLLM(dataset)
    sup, calls = build_team(llm, resolve_port(CodeTrust.SCRIPTED_FIXTURES, strategy), scripted.approve_all)
    out = sup.run(os.path.join(DATA_DIR, DATASETS[dataset]))
    f = out["final"]
    golden = scripted.golden_answers(dataset)
    assert f["status"] == EXPECTED_STATUS[dataset]
    assert set(f["accepted_claims"]) == set(golden)
    assert all(values_match(f["accepted_claims"][k], golden[k]) for k in golden)
    for tid, t in f["tasks"].items():
        expected = sc_status(dataset, tid)
        assert t["status"] == expected, (tid, t["status"])
    # charts produced by the analyst land in the (test-isolated) V1 charts directory and are valid PNGs
    charts = [m["payload"]["execution"]["chart_path"] for m in out["messages"]
              if m["kind"] == "analysis_result" and m["payload"]["execution"]["chart_path"]]
    assert len(charts) == len(scripted.sc.SCENARIOS[dataset]["expected_charts"])
    for p in charts:
        c = check_png(p, sandbox.executor.CHARTS_DIR)
        assert c["path_inside_output_dir"] and c["png_signature"] and c["decodes"]
    assert not os.path.commonpath([os.path.realpath(sandbox.executor.CHARTS_DIR), ROOT]) == ROOT


def sc_status(dataset, tid):
    return scripted.sc.SCENARIOS[dataset]["expected_status"][tid]


def test_real_subprocess_faults_are_detected_and_recovered(tmp_path):
    r = ma.run_case("wrong-group-and-label-recovered", str(tmp_path / "a"), strategy="lazy-seaborn")
    assert r["expectations"]["met"], r["expectations"]["problems"]
    assert r["verification"]["tp"] == 2 and r["verification"]["fn"] == 0
    r = ma.run_case("non-finite", str(tmp_path / "b"))
    assert r["expectations"]["met"] and r["verification"]["tp"] == 1


def test_literal_and_subprocess_modes_reach_the_same_outcome(tmp_path):
    a = ma.run_case("missing-and-malformed", str(tmp_path / "lit"), mode="literal")
    b = ma.run_case("missing-and-malformed", str(tmp_path / "sub"))
    assert a["status"] == b["status"] and a["tasks"] == b["tasks"]
    assert {k: a["verification"][k] for k in ("tp", "tn", "fp", "fn")} == \
           {k: b["verification"][k] for k in ("tp", "tn", "fp", "fn")}


def test_evaluation_records_mock_token_estimates_only_and_never_cost(tmp_path):
    r = ma.run_case("clean-student", str(tmp_path), mode="literal")
    t = r["tokens"]
    assert t["provider_calls"] == 0 and t["mock_calls"] == t["total_calls"] == 7
    assert list(t["classes"]) == ["mock_scripted/estimated_chars_div_4"]
    assert t["classes"]["mock_scripted/estimated_chars_div_4"]["cost_usd_estimate"] is None
    agents = r["agents"]
    assert agents["verification_agent"]["llm_calls"] == 0 and agents["verification_agent"]["requests"] == 6
    assert agents["planning_agent"]["llm_calls"] == 1 and agents["data_analyst_agent"]["llm_calls"] == 3
    assert r["latency"]["per_node"]["supervisor"]["n"] == r["steps"] + 1


def test_cli_runs_offline_and_refuses_protected_folders(tmp_path, capsys):
    assert eval_main(["multi-agent", "--out", str(tmp_path), "--mode", "literal",
                      "--cases", "clean-sales,human-reject"]) == 0
    assert (tmp_path / "multi_agent.json").is_file() and (tmp_path / "summary.md").is_file()
    assert eval_main(["multi-agent", "--out", os.path.join(ROOT, "reports", "x"), "--mode", "literal"]) == 2
    assert eval_main(["multi-agent", "--out", str(tmp_path), "--cases", "nope"]) == 2


# ── V1 preserved; P5 is opt-in ───────────────────────────────────────────────

def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def test_no_protected_v1_path_differs_from_the_p41_base():
    probe = _git("cat-file", "-e", BASE + "^{commit}")
    if probe.returncode != 0:
        pytest.fail("P4.1 base commit not available in this checkout")
    diff = _git("diff", "--name-only", BASE, "--", "agent", "sandbox", "utils", "ui", "rules", "skills", "data",
                "lessons", "config", "sandbox_v2", "tests/v2/golden", "requirements.txt")
    assert diff.returncode == 0 and diff.stdout.strip() == ""


def test_no_v1_module_imports_multi_agent():
    for top in ("agent", "sandbox", "utils", "ui", "config", "sandbox_v2"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                if name.endswith(".py"):
                    tree = ast.parse(open(os.path.join(dirpath, name), encoding="utf-8").read())
                    for node in ast.walk(tree):
                        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                            [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                        assert not any(m.split(".")[0] == "multi_agent" for m in mods), (dirpath, name)


def test_v1_graph_is_unchanged():
    from agent.graph import analyst_graph
    nodes = set(analyst_graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"ingest", "plan", "await_approval", "generate_code", "execute", "interpret", "next_task",
                     "report", "followup", "replan", "error_handler"}


def test_multi_agent_is_off_by_default_and_opt_in_by_flag():
    assert is_enabled(load_flags({})) is False
    assert is_enabled(load_flags({"IP_FF_ORCHESTRATOR": "v2"})) is True
    assert is_enabled(load_flags({"IP_FF_ORCHESTRATOR": "v1"})) is False


def test_multi_agent_requires_no_new_dependency():
    req = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read().lower()
    imported = set()
    for dirpath, _, files in os.walk(os.path.join(ROOT, "multi_agent")):
        for name in files:
            if name.endswith(".py"):
                for node in ast.walk(ast.parse(open(os.path.join(dirpath, name), encoding="utf-8").read())):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module.split(".")[0])
                    elif isinstance(node, ast.Import):
                        imported |= {a.name.split(".")[0] for a in node.names}
    third_party = imported - {"csv", "hashlib", "os", "re", "math", "collections", "dataclasses", "json", "copy",
                              "operator", "time", "typing", "enum", "multi_agent", "config", "sandbox",
                              "sandbox_v2", "agent"}
    assert third_party == {"langgraph", "langchain_core"}
    assert "langgraph" in req and "langchain" in req


def test_case_trace_has_one_line_per_handoff_and_no_claim_values(tmp_path):
    r = ma.run_case("wrong-value-recovered", str(tmp_path), mode="literal")
    assert len(r["trace"]) == r["handoffs"]["requests"] + r["handoffs"]["replies"]
    text = "\n".join(r["trace"])
    assert "verification_report [sales_t3 #1] | rejected: 1 refuted" in text
    assert "verification_report [sales_t3 #2] | accepted: 1 verified" in text
    assert "13573929" not in text and "93.13" not in text
