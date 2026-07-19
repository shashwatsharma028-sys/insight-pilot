"""
Sandboxed Code Executor
Runs LLM-generated Python code safely using subprocess isolation.
Classifies errors so the retry loop can fix them intelligently.
"""

import subprocess
import sys
import os
import tempfile
import textwrap
import re
from typing import Tuple, Optional
from agent.state import ErrorType


MAX_EXECUTION_TIME = 30  # seconds
CHARTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports", "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)


def classify_error(error_output: str) -> ErrorType:
    """Classify the type of error from stderr output."""
    error_lower = error_output.lower()

    if "syntaxerror" in error_lower or "indentationerror" in error_lower:
        return ErrorType.SYNTAX
    elif ("keyerror" in error_lower or "columnnotfound" in error_lower or
          "valueerror" in error_lower or "typeerror" in error_lower):
        return ErrorType.DATA
    elif ("nameerror" in error_lower or "attributeerror" in error_lower or
          "importerror" in error_lower or "modulenotfounderror" in error_lower):
        return ErrorType.RUNTIME
    elif "assertionerror" in error_lower or "logicerror" in error_lower:
        return ErrorType.LOGIC
    else:
        return ErrorType.UNKNOWN


def wrap_code_safely(user_code: str, csv_path: str, task_id: str) -> str:
    """
    Wrap generated code with:
    - Pre-loaded DataFrame (so LLM just uses `df`)
    - Chart save path injection
    - Output capture
    """
    chart_path = os.path.join(CHARTS_DIR, f"{task_id}.png")

    wrapper = textwrap.dedent(f"""
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend — essential for subprocess
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns
import warnings
warnings.filterwarnings('ignore')

# Pre-load the dataset — agent code should use `df` directly
df = pd.read_csv(r"{csv_path}")

# Chart save path — agent code should use CHART_PATH to save figures
CHART_PATH = r"{chart_path}"

plt.style.use('seaborn-v0_8-darkgrid')
plt.rcParams['figure.figsize'] = (10, 6)
plt.rcParams['figure.dpi'] = 120

# ── Agent-generated code starts here ──────────────────────────────
{user_code}
# ── Agent-generated code ends here ────────────────────────────────

# Auto-save any open figure if agent forgot to save
if plt.get_fignums():
    plt.tight_layout()
    plt.savefig(CHART_PATH, bbox_inches='tight')
    plt.close('all')
    print(f"CHART_SAVED:{{CHART_PATH}}")
""")
    return wrapper


def execute_code(
    code: str,
    csv_path: str,
    task_id: str
) -> Tuple[bool, str, Optional[str], ErrorType]:
    """
    Execute code in a subprocess sandbox.

    Returns:
        success (bool): Whether execution succeeded
        output (str): stdout from execution
        chart_path (str | None): Path to saved chart if any
        error_type (ErrorType): Classification of error if failed
    """
    # ── Rule enforcement gate (sandbox_safety.rules.md, rules 3-6) ──
    # Static AST scan blocks forbidden imports/calls BEFORE execution.
    # Fail closed: violating code never runs.
    from agent.rules_engine import scan_code_safety
    violation = scan_code_safety(code)
    if violation:
        return False, violation, None, ErrorType.RUNTIME

    wrapped = wrap_code_safely(code, csv_path, task_id)

    # Write to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                     delete=False, encoding='utf-8') as f:
        f.write(wrapped)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=MAX_EXECUTION_TIME,
            env={**os.environ, 'MPLBACKEND': 'Agg'}
        )

        if result.returncode == 0:
            output = result.stdout.strip()
            chart_path = None

            # Extract chart path if agent saved one
            chart_match = re.search(r"CHART_SAVED:(.+)", output)
            if chart_match:
                chart_path = chart_match.group(1).strip()
                output = output.replace(f"CHART_SAVED:{chart_path}", "").strip()

            # Also check default path
            default_chart = os.path.join(CHARTS_DIR, f"{task_id}.png")
            if not chart_path and os.path.exists(default_chart):
                chart_path = default_chart

            return True, output or "Execution completed successfully.", chart_path, None

        else:
            error_output = result.stderr.strip()
            error_type = classify_error(error_output)
            return False, error_output, None, error_type

    except subprocess.TimeoutExpired:
        return False, f"Execution timed out after {MAX_EXECUTION_TIME}s.", None, ErrorType.RUNTIME
    except Exception as e:
        return False, str(e), None, ErrorType.UNKNOWN
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
