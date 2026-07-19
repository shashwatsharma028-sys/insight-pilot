"""
Change Approval Protocol (Human-in-the-Loop for system changes)

Operationalizes rules/architecture.md rule 1: before any architecture,
workflow, folder-structure, or core-feature change, a structured proposal
must be presented and explicitly approved by the user.

Scope boundary (important):
- GOVERNS: changes to InsightPilot itself — its modules, graph, folders,
  engines, rules, skills.
- DOES NOT GOVERN: normal data analysis runs. Analyses are the product
  working as designed; they flow through the existing (optional) safe-mode
  plan gate and never through this protocol.

A proposal must state: affected files/modules, expected impact, a risk
level (Low/Medium/High), and end with an explicit approval question.
Nothing classified as requiring approval proceeds without a recorded
approval.
"""

from datetime import datetime
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class ChangeType(str, Enum):
    ARCHITECTURE = "architecture"        # Graph, state schema, frameworks, folders
    WORKFLOW = "workflow"                # Pipeline ordering, node routing
    CORE_FEATURE = "core_feature"        # Engines, nodes, sandbox, UI behavior
    DOCS_OR_TESTS = "docs_or_tests"      # Additive docs, tests, assets
    DATA_ANALYSIS = "data_analysis"      # Running analyses — the product itself


# ── Classification ───────────────────────────────────────────────────────────

# Path prefixes/files → (change_type, risk). First match wins; order matters.
_CLASSIFICATION_TABLE = [
    # Architecture: the spine of the system
    ("agent/graph.py",        ChangeType.ARCHITECTURE, RiskLevel.HIGH),
    ("agent/state.py",        ChangeType.ARCHITECTURE, RiskLevel.HIGH),
    ("sandbox/",              ChangeType.ARCHITECTURE, RiskLevel.HIGH),   # Safety-critical
    ("rules/",                ChangeType.ARCHITECTURE, RiskLevel.HIGH),   # Governance itself
    # Core features: engines, nodes, prompts, UI
    ("agent/rules_engine.py",      ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    ("agent/skills_loader.py",     ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    ("agent/reflection_engine.py", ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    ("agent/token_monitor.py",     ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    ("agent/context_manager.py",   ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    ("agent/nodes/",               ChangeType.WORKFLOW,     RiskLevel.MEDIUM),
    ("agent/memory/",              ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    ("utils/",                     ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    ("skills/",                    ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    ("ui/",                        ChangeType.CORE_FEATURE, RiskLevel.MEDIUM),
    # Additive, low-risk
    ("tests/",   ChangeType.DOCS_OR_TESTS, RiskLevel.LOW),
    ("README",   ChangeType.DOCS_OR_TESTS, RiskLevel.LOW),
    ("assets/",  ChangeType.DOCS_OR_TESTS, RiskLevel.LOW),
    ("lessons/README", ChangeType.DOCS_OR_TESTS, RiskLevel.LOW),
    # Run outputs — the product doing its job, not a system change
    ("data/",     ChangeType.DATA_ANALYSIS, RiskLevel.LOW),
    ("reports/",  ChangeType.DATA_ANALYSIS, RiskLevel.LOW),
    ("lessons/lessons.json", ChangeType.DATA_ANALYSIS, RiskLevel.LOW),
]

# Structural operations always classified as architecture/High
_STRUCTURAL_KEYWORDS = (
    "create folder", "new directory", "delete folder", "remove directory",
    "rename module", "move module", "restructure",
)


def classify_change(affected_paths: list, description: str = "") -> tuple:
    """
    Classify a proposed change from its affected paths (+ description).
    Returns (ChangeType, RiskLevel). Aggregation: the highest-risk /
    most structural classification among all paths wins.
    """
    desc = description.lower()
    if any(kw in desc for kw in _STRUCTURAL_KEYWORDS):
        return ChangeType.ARCHITECTURE, RiskLevel.HIGH

    best = None
    rank = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
    type_rank = {ChangeType.DATA_ANALYSIS: 0, ChangeType.DOCS_OR_TESTS: 1,
                 ChangeType.CORE_FEATURE: 2, ChangeType.WORKFLOW: 2,
                 ChangeType.ARCHITECTURE: 3}

    def _consider(candidate):
        nonlocal best
        if best is None:
            best = candidate
        elif (rank[candidate[1]], type_rank[candidate[0]]) > (rank[best[1]], type_rank[best[0]]):
            best = candidate

    for path in affected_paths:
        norm = str(path).replace("\\", "/").lstrip("./")
        for prefix, ctype, risk in _CLASSIFICATION_TABLE:
            if norm.startswith(prefix) or prefix in norm:
                _consider((ctype, risk))
                break
        else:
            # Unknown path → new file/folder somewhere → treat cautiously
            _consider((ChangeType.CORE_FEATURE, RiskLevel.MEDIUM))

    if best is None:
        return ChangeType.DOCS_OR_TESTS, RiskLevel.LOW
    return best


def needs_approval(change_type: ChangeType) -> bool:
    """Architecture, workflow, and core-feature changes require approval.
    Docs/tests additions and normal data analysis do not."""
    return change_type in (
        ChangeType.ARCHITECTURE, ChangeType.WORKFLOW, ChangeType.CORE_FEATURE
    )


# ── Proposals ────────────────────────────────────────────────────────────────

def build_proposal(title: str, affected_paths: list, impact: str,
                   description: str = "") -> dict:
    """Assemble a structured change proposal."""
    ctype, risk = classify_change(affected_paths, description)
    return {
        "title": title,
        "change_type": ctype,
        "affected_files": sorted(set(str(p) for p in affected_paths)),
        "impact": impact,
        "risk": risk,
        "needs_approval": needs_approval(ctype),
        "status": "pending" if needs_approval(ctype) else "auto_approved",
        "timestamp": datetime.now().isoformat(),
        "decision_timestamp": None,
    }


def format_proposal(proposal: dict) -> str:
    """Render a proposal for presentation to the user."""
    lines = [
        f"## Proposed change: {proposal['title']}",
        "",
        f"**Type:** {proposal['change_type'].value} · **Risk:** {proposal['risk'].value}",
        "",
        "**Affected files/modules:**",
    ]
    lines += [f"- `{p}`" for p in proposal["affected_files"]]
    lines += ["", f"**Expected impact:** {proposal['impact']}", ""]
    if proposal["needs_approval"]:
        lines.append("⚠️ **This change requires your explicit approval. Proceed? (yes/no)**")
    else:
        lines.append("ℹ️ No approval needed (additive docs/tests or normal analysis output).")
    return "\n".join(lines)


def record_decision(proposal: dict, approved: bool) -> dict:
    """Record the user's decision. Returns a NEW proposal dict."""
    return {
        **proposal,
        "status": "approved" if approved else "rejected",
        "decision_timestamp": datetime.now().isoformat(),
    }


def can_proceed(proposal: dict) -> bool:
    """The gate: only auto-approved or explicitly approved changes proceed."""
    return proposal["status"] in ("approved", "auto_approved")
