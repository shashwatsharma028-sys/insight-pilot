"""
Test Suite 8: Rule Engine Governance Tier
Verifies the 5 governance rule files, the load_all_rules preflight,
and that the preflight actually runs at graph entry — with zero
behavior change to existing functionality.
"""

import pytest
from agent.rules_engine import load_all_rules, get_rules_for, RULE_FILES
from agent.nodes.data_ingestion import data_ingestion_node


class TestGovernanceRuleFiles:

    @pytest.mark.parametrize("purpose", [
        "architecture", "coding", "execution", "safety", "testing",
    ])
    def test_governance_rules_load(self, purpose):
        text = get_rules_for(purpose)
        assert len(text) > 100, f"rules/{purpose}.md unexpectedly empty"

    def test_architecture_requires_approval(self):
        text = get_rules_for("architecture")
        assert "approved by the user" in text

    def test_architecture_protects_features(self):
        text = get_rules_for("architecture")
        assert "NEVER be removed" in text

    def test_testing_demands_full_suite_every_change(self):
        text = get_rules_for("testing")
        assert "every code change" in text

    def test_execution_demands_rules_preflight(self):
        text = get_rules_for("execution")
        assert "before every agent task" in text


class TestLoadAllRulesPreflight:

    def test_all_registered_rules_load(self):
        status = load_all_rules()
        assert len(status) == len(RULE_FILES) == 12
        assert all(status.values()), f"Rules failed to load: {[k for k, v in status.items() if not v]}"

    def test_both_tiers_present(self):
        status = load_all_rules()
        # Tier 1 (runtime)
        assert "sandbox_safety" in status and "planning" in status
        # Tier 2 (governance)
        assert "architecture" in status and "testing" in status

    def test_preflight_is_idempotent(self):
        """Cached — calling twice returns the same result, no side effects."""
        assert load_all_rules() == load_all_rules()


class TestPreflightAtGraphEntry:
    """execution.md rule 1: rules load before any task. The graph's entry
    node (data_ingestion) performs the preflight and logs it."""

    def test_ingestion_logs_rules_preflight(self, base_state):
        result = data_ingestion_node(base_state)
        first_log = result["execution_log"][0]
        assert "Rules preflight" in first_log["action"]
        assert "12/12" in first_log["action"]

    def test_ingestion_behavior_unchanged(self, base_state):
        """No behavior change: same routing, same profiling outputs."""
        result = data_ingestion_node(base_state)
        assert result["next_action"] == "plan"
        assert result["df_shape"] == (20, 4)
        assert result["data_quality_report"]["quality_score"] >= 95


class TestSkillsDeclareGovernance:
    """execution.md rule 2: prompt-bearing skills must declare governed_by."""

    @pytest.mark.parametrize("skill", [
        "planner", "code_generator", "business_insights", "followup_chat",
    ])
    def test_prompt_skills_have_governed_by(self, skill):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "skills", f"{skill}.md"
        )
        with open(path) as f:
            raw = f.read()
        # Frontmatter must declare governance
        frontmatter = raw.split("\n---\n")[0]
        assert "governed_by:" in frontmatter
