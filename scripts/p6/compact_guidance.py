"""
P6 candidate (offline measurement only; NOT wired to any flag or prompt, and kept outside multi_agent/ so the
reviewed runtime package is unchanged): one compact section carrying the same content as
prompts.op_schema_section() + prompts.op_semantics_section(). Every op, argument, column kind and result is generated
from the validator's tables (claims.OPS, claims.OP_RESULTS, MAX_PREFIX); tests check nothing is lost. Its effect on
live accuracy is unmeasured, so it is offered only as a candidate arm for a future controlled comparison.
"""

from multi_agent.claims import ANY, MAX_PREFIX, OP_RESULTS, OPS

COMPACT_RULES = (
    "args is always a JSON object ({} when an op has none); a claim has exactly key, op and args.",
    "Column arguments are exact column names from DATASET PROFILE JSON, as JSON strings.",
    'value is the exact cell text as a JSON string, never a JSON boolean, number or null; '
    'boolean cells are exactly "True" or "False".',
    "Pick the op whose result shape answers the question: one number or one group for a single answer, a mapping "
    "only when a value for every group is asked for; for rows where a column equals one value use *_where_equals, "
    "not group_*.",
    "Groups are the exact cell text (or its first by_prefix characters); ties go to the first group in sorted order.",
    "Date cells start with YYYY-MM-DD: by_prefix 7 groups by calendar month (YYYY-MM), 4 by calendar year (YYYY). "
    "A text column naming a period without its year merges it across years; for a calendar period group a date "
    "column with by_prefix.",
)


def _compact_arg(name, rule) -> str:
    if rule == "str":
        return f"{name}:cell-text"
    if rule == "prefix":
        return f"{name.rstrip('?')}?:int 1-{MAX_PREFIX}"
    return f"{name}:{'any' if rule == ANY else '|'.join(rule)}"


def op_guidance_compact() -> str:
    """Compact equivalent of op_schema_section() + op_semantics_section() (candidate; not used by any prompt)."""
    lines = [f"{op}({', '.join(_compact_arg(n, r) for n, r in spec.items())}) -> {OP_RESULTS[op]}"
             for op, spec in OPS.items()]
    rules = "\n".join(f"- {r}" for r in COMPACT_RULES)
    return "CLAIM OPS (op(args) -> result):\n" + "\n".join(lines) + "\nCLAIM RULES:\n" + rules + "\n"
