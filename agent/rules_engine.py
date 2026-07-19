"""
Rules Engine
Loads governance rules from /rules and serves them to agent nodes.

Integration contract:
- Prompt-level: nodes call get_rules_for("code_generation") and append the
  returned text to their system prompt.
- Code-level: the sandbox calls scan_code_safety() to enforce the
  sandbox_safety import rules via AST analysis before execution.

Design notes:
- Rules are cached after first load (files don't change mid-run).
- Missing rule files degrade gracefully (empty string + warning), so the
  agent still functions if the rules folder is absent.
"""

import os
import ast
from functools import lru_cache
from typing import Optional

RULES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules"
)

# Maps a node/purpose to its rule file
RULE_FILES = {
    "sandbox_safety": "sandbox_safety.rules.md",
    "code_generation": "code_generation.rules.md",
    "planning": "planning.rules.md",
    "interpretation": "interpretation.rules.md",
    "data_handling": "data_handling.rules.md",
    "retry_and_failure": "retry_and_failure.rules.md",
    "llm_usage": "llm_usage.rules.md",
}


@lru_cache(maxsize=None)
def get_rules_for(purpose: str) -> str:
    """
    Return the rule text for a purpose, formatted for prompt injection.
    Returns "" (with a console warning) if the file is missing, so the
    agent degrades gracefully rather than crashing.
    """
    filename = RULE_FILES.get(purpose)
    if not filename:
        return ""

    path = os.path.join(RULES_DIR, filename)
    if not os.path.exists(path):
        print(f"⚠️ Rules file missing: {path} — running without {purpose} rules")
        return ""

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    return (
        f"\n\n=== GOVERNANCE RULES ({purpose}) — YOU MUST FOLLOW THESE ===\n"
        f"{content}\n"
        f"=== END OF RULES ===\n"
    )


def rules_available() -> bool:
    """True if the rules directory exists and has at least one rule file."""
    return os.path.isdir(RULES_DIR) and any(
        f.endswith(".rules.md") for f in os.listdir(RULES_DIR)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Code-level enforcement: sandbox_safety rules 3-6 (forbidden imports/calls)
# Enforced by AST scan — this is a hard gate, not a prompt suggestion.
# ─────────────────────────────────────────────────────────────────────────────

FORBIDDEN_IMPORTS = {
    "os", "sys", "subprocess", "shutil", "socket", "ctypes",
    "importlib", "pickle", "requests", "urllib", "http",
    "multiprocessing", "pty", "signal",
}

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "open",
}


def scan_code_safety(code: str) -> Optional[str]:
    """
    Statically scan generated code for sandbox_safety violations.

    Returns:
        None if the code is safe.
        A human-readable violation description if not.
        None also if the code doesn't parse — syntax errors are handled
        downstream by the executor with proper classification.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # Let the sandbox produce a properly classified SyntaxError

    violations = []

    for node in ast.walk(tree):
        # import X / import X.Y
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_IMPORTS:
                    violations.append(f"forbidden import '{alias.name}'")

        # from X import Y
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_IMPORTS:
                violations.append(f"forbidden import 'from {node.module}'")

        # eval(...) / exec(...) / open(...) etc.
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                violations.append(f"forbidden call '{func.id}()'")

    if violations:
        return (
            "SANDBOX SAFETY VIOLATION — execution blocked before running. "
            f"Violations: {'; '.join(sorted(set(violations)))}. "
            "Generated code must not access the filesystem, network, or "
            "process controls (see rules/sandbox_safety.rules.md)."
        )
    return None
