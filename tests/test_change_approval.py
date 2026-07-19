"""
Test Suite 12: Change Approval Protocol (Human-in-the-Loop)
Verifies classification of system changes vs data analysis, risk levels,
proposal structure/formatting, the approval workflow, and the boundary:
analyses never require change approval.
"""

import pytest
from agent.change_approval import (
    classify_change, needs_approval, build_proposal, format_proposal,
    record_decision, can_proceed, ChangeType, RiskLevel,
)


class TestClassification:

    def test_graph_change_is_architecture_high(self):
        ctype, risk = classify_change(["agent/graph.py"])
        assert ctype == ChangeType.ARCHITECTURE and risk == RiskLevel.HIGH

    def test_state_schema_is_architecture_high(self):
        ctype, risk = classify_change(["agent/state.py"])
        assert ctype == ChangeType.ARCHITECTURE and risk == RiskLevel.HIGH

    def test_sandbox_is_high_risk(self):
        ctype, risk = classify_change(["sandbox/executor.py"])
        assert risk == RiskLevel.HIGH

    def test_rules_folder_is_high_risk(self):
        ctype, risk = classify_change(["rules/safety.md"])
        assert ctype == ChangeType.ARCHITECTURE and risk == RiskLevel.HIGH

    def test_node_change_is_workflow_medium(self):
        ctype, risk = classify_change(["agent/nodes/planner.py"])
        assert ctype == ChangeType.WORKFLOW and risk == RiskLevel.MEDIUM

    def test_engine_change_is_core_feature_medium(self):
        ctype, risk = classify_change(["agent/reflection_engine.py"])
        assert ctype == ChangeType.CORE_FEATURE and risk == RiskLevel.MEDIUM

    def test_ui_change_is_core_feature(self):
        ctype, risk = classify_change(["ui/app.py"])
        assert ctype == ChangeType.CORE_FEATURE

    def test_test_addition_is_low(self):
        ctype, risk = classify_change(["tests/test_new_thing.py"])
        assert ctype == ChangeType.DOCS_OR_TESTS and risk == RiskLevel.LOW

    def test_readme_is_low(self):
        ctype, risk = classify_change(["README.md"])
        assert risk == RiskLevel.LOW

    def test_analysis_outputs_are_data_analysis(self):
        ctype, risk = classify_change(["reports/Sales_report.md"])
        assert ctype == ChangeType.DATA_ANALYSIS

    def test_lessons_data_is_data_analysis(self):
        ctype, risk = classify_change(["lessons/lessons.json"])
        assert ctype == ChangeType.DATA_ANALYSIS

    def test_mixed_paths_highest_risk_wins(self):
        ctype, risk = classify_change(["README.md", "agent/graph.py"])
        assert ctype == ChangeType.ARCHITECTURE and risk == RiskLevel.HIGH

    def test_folder_creation_keyword_is_architecture(self):
        ctype, risk = classify_change(["somewhere/x.py"],
                                      description="Create folder /plugins for extensions")
        assert ctype == ChangeType.ARCHITECTURE and risk == RiskLevel.HIGH

    def test_unknown_path_treated_cautiously(self):
        ctype, risk = classify_change(["mystery_new_module.py"])
        assert risk == RiskLevel.MEDIUM


class TestApprovalBoundary:
    """The core requirement: system changes need approval; analyses don't."""

    @pytest.mark.parametrize("ctype,expected", [
        (ChangeType.ARCHITECTURE, True),
        (ChangeType.WORKFLOW, True),
        (ChangeType.CORE_FEATURE, True),
        (ChangeType.DOCS_OR_TESTS, False),
        (ChangeType.DATA_ANALYSIS, False),
    ])
    def test_needs_approval_matrix(self, ctype, expected):
        assert needs_approval(ctype) is expected

    def test_data_analysis_auto_approved(self):
        p = build_proposal("Run sales analysis", ["reports/out.md"],
                           impact="Generates a report file")
        assert p["needs_approval"] is False
        assert p["status"] == "auto_approved"
        assert can_proceed(p) is True


class TestProposals:

    def _arch_proposal(self):
        return build_proposal(
            "Add critic agent to graph",
            ["agent/graph.py", "agent/nodes/critic.py"],
            impact="Adds a verification node between interpret and report; +1 LLM call per task",
        )

    def test_proposal_has_all_required_fields(self):
        p = self._arch_proposal()
        for field in ["title", "change_type", "affected_files", "impact",
                      "risk", "needs_approval", "status", "timestamp"]:
            assert field in p

    def test_pending_until_decided(self):
        p = self._arch_proposal()
        assert p["status"] == "pending"
        assert can_proceed(p) is False           # The gate holds

    def test_approval_unlocks(self):
        p = record_decision(self._arch_proposal(), approved=True)
        assert p["status"] == "approved"
        assert p["decision_timestamp"] is not None
        assert can_proceed(p) is True

    def test_rejection_blocks(self):
        p = record_decision(self._arch_proposal(), approved=False)
        assert p["status"] == "rejected"
        assert can_proceed(p) is False

    def test_decision_is_immutable_style(self):
        original = self._arch_proposal()
        decided = record_decision(original, approved=True)
        assert original["status"] == "pending"   # Original untouched
        assert decided is not original


class TestFormatting:

    def test_format_shows_files_impact_risk_question(self):
        p = build_proposal(
            "Refactor state schema",
            ["agent/state.py"],
            impact="All nodes must adapt to renamed fields",
        )
        text = format_proposal(p)
        assert "agent/state.py" in text
        assert "All nodes must adapt" in text
        assert "Risk:** High" in text
        assert "explicit approval" in text and "(yes/no)" in text

    def test_format_no_approval_note_for_low_risk(self):
        p = build_proposal("Add screenshots", ["assets/results.png"],
                           impact="README gets images")
        text = format_proposal(p)
        assert "No approval needed" in text
