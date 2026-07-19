"""
Test Suite 6: Rules Engine
Verifies rule loading, prompt injection formatting, and — most importantly —
the AST-based code safety enforcement gate.
"""

import pytest
from agent.rules_engine import (
    get_rules_for, rules_available, scan_code_safety,
    FORBIDDEN_IMPORTS, FORBIDDEN_CALLS,
)
from sandbox.executor import execute_code
from agent.state import ErrorType


class TestRuleLoading:

    def test_rules_folder_detected(self):
        assert rules_available() is True

    def test_loads_each_known_purpose(self):
        for purpose in ["planning", "code_generation", "interpretation",
                        "sandbox_safety", "data_handling",
                        "retry_and_failure", "llm_usage"]:
            text = get_rules_for(purpose)
            assert len(text) > 100, f"{purpose} rules unexpectedly empty"

    def test_injection_format_has_markers(self):
        text = get_rules_for("planning")
        assert "GOVERNANCE RULES" in text
        assert "END OF RULES" in text

    def test_unknown_purpose_returns_empty(self):
        assert get_rules_for("nonexistent_purpose") == ""

    def test_rules_content_matches_file(self):
        text = get_rules_for("code_generation")
        assert "CHART_PATH" in text  # A phrase we know is in that file


class TestCodeSafetyScan:
    """The AST gate: forbidden operations must be caught pre-execution."""

    def test_clean_analysis_code_passes(self):
        code = "import pandas as pd\nresult = df.groupby('region')['sales'].sum()\nprint(result)"
        assert scan_code_safety(code) is None

    def test_allowed_science_imports_pass(self):
        code = "import numpy as np\nfrom scipy import stats\nimport seaborn as sns"
        assert scan_code_safety(code) is None

    @pytest.mark.parametrize("bad_import", ["os", "subprocess", "socket", "pickle", "requests"])
    def test_forbidden_imports_blocked(self, bad_import):
        violation = scan_code_safety(f"import {bad_import}")
        assert violation is not None
        assert bad_import in violation

    def test_from_import_blocked(self):
        violation = scan_code_safety("from subprocess import run")
        assert violation is not None

    def test_dotted_import_blocked(self):
        violation = scan_code_safety("import urllib.request")
        assert violation is not None

    @pytest.mark.parametrize("bad_call", ["eval", "exec", "open"])
    def test_forbidden_calls_blocked(self, bad_call):
        violation = scan_code_safety(f"{bad_call}('something')")
        assert violation is not None
        assert bad_call in violation

    def test_syntax_error_defers_to_executor(self):
        """Unparseable code returns None — the sandbox classifies it properly."""
        assert scan_code_safety("this is !!! not python") is None

    def test_violation_message_mentions_rules_file(self):
        violation = scan_code_safety("import os")
        assert "sandbox_safety.rules.md" in violation


class TestSandboxEnforcement:
    """End-to-end: the executor must block violating code before running it."""

    def test_forbidden_code_blocked_by_executor(self, sample_csv):
        success, output, chart, err_type = execute_code(
            code="import os\nprint(os.listdir('/'))",
            csv_path=sample_csv,
            task_id="t_blocked"
        )
        assert success is False
        assert "SANDBOX SAFETY VIOLATION" in output
        assert err_type == ErrorType.RUNTIME

    def test_network_attempt_blocked(self, sample_csv):
        success, output, _, _ = execute_code(
            code="import requests\nrequests.get('http://evil.example')",
            csv_path=sample_csv,
            task_id="t_network"
        )
        assert success is False
        assert "VIOLATION" in output

    def test_clean_code_still_runs_normally(self, sample_csv):
        """The gate must not break legitimate analysis code."""
        success, output, _, _ = execute_code(
            code="print('total sales:', df['sales'].sum())",
            csv_path=sample_csv,
            task_id="t_clean_after_gate"
        )
        assert success is True
        assert "total sales:" in output
