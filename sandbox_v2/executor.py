"""
P4.1 V2 sandbox executor: the V1 executor with the lazy-seaborn prelude.

execute_code mirrors sandbox.executor.execute_code statement for statement:
- the AST safety scan;
- the temp-file subprocess with [sys.executable, tmp_path] and the same environment;
- MAX_EXECUTION_TIME;
- CHART_SAVED and default-chart detection;
- classify_error, timeout and exception handling, and temp-file clean-up.

The only difference is the wrapper (wrap_code_lazy_seaborn). V1 module
state (CHARTS_DIR, MAX_EXECUTION_TIME, classify_error) is read from
sandbox.executor at call time, so any redirection of the V1 module applies
to both. tests/v2/p4/test_lazy_seaborn_p41.py proves the statement-level
equivalence with V1 on the syntax tree.
"""

import os
import re
import subprocess
import sys
import tempfile
from typing import Optional, Tuple

import sandbox.executor as _v1
from agent.state import ErrorType
from sandbox_v2.lazy_prelude import wrap_code_lazy_seaborn


def execute_code(
    code: str,
    csv_path: str,
    task_id: str
) -> Tuple[bool, str, Optional[str], ErrorType]:
    """V1 execute_code with the lazy-seaborn prelude (same return contract)."""
    from agent.rules_engine import scan_code_safety
    violation = scan_code_safety(code)
    if violation:
        return False, violation, None, ErrorType.RUNTIME

    wrapped = wrap_code_lazy_seaborn(code, csv_path, task_id)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                     delete=False, encoding='utf-8') as f:
        f.write(wrapped)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=_v1.MAX_EXECUTION_TIME,
            env={**os.environ, 'MPLBACKEND': 'Agg'}
        )

        if result.returncode == 0:
            output = result.stdout.strip()
            chart_path = None

            chart_match = re.search(r"CHART_SAVED:(.+)", output)
            if chart_match:
                chart_path = chart_match.group(1).strip()
                output = output.replace(f"CHART_SAVED:{chart_path}", "").strip()

            default_chart = os.path.join(_v1.CHARTS_DIR, f"{task_id}.png")
            if not chart_path and os.path.exists(default_chart):
                chart_path = default_chart

            return True, output or "Execution completed successfully.", chart_path, None

        else:
            error_output = result.stderr.strip()
            error_type = _v1.classify_error(error_output)
            return False, error_output, None, error_type

    except subprocess.TimeoutExpired:
        return False, f"Execution timed out after {_v1.MAX_EXECUTION_TIME}s.", None, ErrorType.RUNTIME
    except Exception as e:
        return False, str(e), None, ErrorType.UNKNOWN
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
