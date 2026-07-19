"""
Test Suite 1: Sandbox Executor
The sandbox is the most safety-critical component — it runs LLM-generated
code. These tests verify isolation, output capture, chart saving, timeouts,
and error classification.
"""

import os
import pytest
from sandbox.executor import execute_code, classify_error, MAX_EXECUTION_TIME
from agent.state import ErrorType


class TestErrorClassification:
    """classify_error must map raw stderr to the right ErrorType,
    because the retry loop picks its fix strategy based on this."""

    def test_syntax_error(self):
        assert classify_error("SyntaxError: invalid syntax") == ErrorType.SYNTAX

    def test_indentation_error(self):
        assert classify_error("IndentationError: unexpected indent") == ErrorType.SYNTAX

    def test_key_error_is_data(self):
        assert classify_error("KeyError: 'missing_column'") == ErrorType.DATA

    def test_value_error_is_data(self):
        assert classify_error("ValueError: could not convert string to float") == ErrorType.DATA

    def test_type_error_is_data(self):
        assert classify_error("TypeError: unsupported operand") == ErrorType.DATA

    def test_name_error_is_runtime(self):
        assert classify_error("NameError: name 'undefined_var' is not defined") == ErrorType.RUNTIME

    def test_import_error_is_runtime(self):
        assert classify_error("ModuleNotFoundError: No module named 'fake'") == ErrorType.RUNTIME

    def test_unknown_error(self):
        assert classify_error("Some bizarre unrecognizable failure") == ErrorType.UNKNOWN

    def test_case_insensitive(self):
        assert classify_error("SYNTAXERROR IN LINE 4") == ErrorType.SYNTAX


class TestSandboxExecution:
    """execute_code must run code safely and report results accurately."""

    def test_successful_execution(self, sample_csv):
        success, output, chart, err_type = execute_code(
            code="print('result:', len(df))",
            csv_path=sample_csv,
            task_id="t_success"
        )
        assert success is True
        assert "result: 20" in output
        assert err_type is None

    def test_df_preloaded(self, sample_csv):
        """The sandbox must inject df — generated code never loads files itself."""
        success, output, _, _ = execute_code(
            code="print(list(df.columns))",
            csv_path=sample_csv,
            task_id="t_columns"
        )
        assert success is True
        assert "region" in output and "sales" in output

    def test_syntax_error_caught_and_classified(self, sample_csv):
        success, output, chart, err_type = execute_code(
            code="this is not valid python !!!",
            csv_path=sample_csv,
            task_id="t_syntax"
        )
        assert success is False
        assert err_type == ErrorType.SYNTAX

    def test_missing_column_classified_as_data_error(self, sample_csv):
        success, output, chart, err_type = execute_code(
            code="print(df['nonexistent_column'].sum())",
            csv_path=sample_csv,
            task_id="t_keyerror"
        )
        assert success is False
        assert err_type == ErrorType.DATA

    def test_chart_gets_saved(self, sample_csv):
        code = """
import matplotlib.pyplot as plt
df.groupby('region')['sales'].sum().plot(kind='bar')
plt.savefig(CHART_PATH, bbox_inches='tight')
plt.close('all')
"""
        success, output, chart, _ = execute_code(
            code=code, csv_path=sample_csv, task_id="t_chart"
        )
        assert success is True
        assert chart is not None
        assert os.path.exists(chart)

    def test_auto_chart_save_when_agent_forgets(self, sample_csv):
        """Wrapper must auto-save any open figure even if code omits savefig."""
        code = "df.groupby('region')['sales'].sum().plot(kind='bar')"
        success, output, chart, _ = execute_code(
            code=code, csv_path=sample_csv, task_id="t_autosave"
        )
        assert success is True
        assert chart is not None and os.path.exists(chart)

    def test_infinite_loop_times_out(self, sample_csv):
        """A hanging script must be killed, not freeze the whole agent."""
        success, output, chart, err_type = execute_code(
            code="while True: pass",
            csv_path=sample_csv,
            task_id="t_timeout"
        )
        assert success is False
        assert "timed out" in output.lower()

    def test_stdout_captured_fully(self, sample_csv):
        success, output, _, _ = execute_code(
            code="print('line1')\nprint('line2')\nprint('total:', df['sales'].sum())",
            csv_path=sample_csv,
            task_id="t_stdout"
        )
        assert success is True
        assert "line1" in output and "line2" in output and "total:" in output
