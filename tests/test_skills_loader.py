"""
Test Suite 7: Skills Loader
Verifies skill loading, frontmatter stripping, fallback behavior, and —
the critical guarantee — that loaded prompts are byte-identical to the
embedded defaults (no behavior change from modularization).
"""

import pytest
from agent.skills_loader import load_skill, skills_available, _strip_frontmatter


class TestFrontmatterStripping:

    def test_strips_frontmatter_block(self):
        text = "---\nskill: x\ntype: prompt\n---\nActual prompt body"
        assert _strip_frontmatter(text) == "Actual prompt body"

    def test_no_frontmatter_passes_through(self):
        text = "Just a prompt with no frontmatter"
        assert _strip_frontmatter(text) == text

    def test_dashes_inside_body_not_confused(self):
        text = "---\nmeta: yes\n---\nbody with --- dashes inside"
        assert "dashes inside" in _strip_frontmatter(text)


class TestSkillLoading:

    def test_skills_folder_detected(self):
        assert skills_available() is True

    @pytest.mark.parametrize("skill", [
        "planner", "code_generator", "business_insights", "followup_chat",
        "executor", "memory", "reflection", "chart_analysis",
    ])
    def test_all_eight_skills_load(self, skill):
        body = load_skill(skill)
        assert len(body) > 50, f"skills/{skill}.md unexpectedly empty"

    def test_frontmatter_not_leaked_into_prompt(self):
        body = load_skill("planner")
        assert "used_by:" not in body
        assert "governed_by:" not in body

    def test_missing_skill_uses_fallback(self):
        assert load_skill("no_such_skill", fallback="DEFAULT TEXT") == "DEFAULT TEXT"

    def test_missing_skill_without_fallback_returns_empty(self):
        assert load_skill("no_such_skill") == ""


class TestNoBehaviorChange:
    """THE guarantee of this refactor: loaded prompts must be byte-identical
    to the previously embedded prompts."""

    def test_planner_prompt_identical(self):
        from agent.nodes.planner import PLANNER_SYSTEM_PROMPT, _DEFAULT_PLANNER_SYSTEM_PROMPT
        assert PLANNER_SYSTEM_PROMPT == _DEFAULT_PLANNER_SYSTEM_PROMPT

    def test_code_gen_prompt_identical(self):
        from agent.nodes.code_generator import CODE_GEN_SYSTEM_PROMPT, _DEFAULT_CODE_GEN_SYSTEM_PROMPT
        assert CODE_GEN_SYSTEM_PROMPT == _DEFAULT_CODE_GEN_SYSTEM_PROMPT

    def test_interpreter_prompt_identical(self):
        from agent.nodes.interpreter import INTERPRETER_SYSTEM_PROMPT, _DEFAULT_INTERPRETER_SYSTEM_PROMPT
        assert INTERPRETER_SYSTEM_PROMPT == _DEFAULT_INTERPRETER_SYSTEM_PROMPT

    def test_followup_prompt_identical(self):
        from agent.memory.conversation import FOLLOWUP_SYSTEM_PROMPT, _DEFAULT_FOLLOWUP_SYSTEM_PROMPT
        assert FOLLOWUP_SYSTEM_PROMPT == _DEFAULT_FOLLOWUP_SYSTEM_PROMPT
