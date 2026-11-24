"""
The claim vocabulary: declarative, machine-checkable questions about a dataset.

The Planning Agent attaches claims to every task, for example
    {"key": "total_sales_amount", "op": "sum", "args": {"column": "sales_amount"}}
The Data Analyst Agent must print one line per claim from its sandboxed code:
    CLAIM total_sales_amount=13573929.0
The Verification Agent recomputes the claim from the dataset itself
(multi_agent.verification), so a claim is accepted only if the data supports it.

Each op lists its required arguments; column arguments name the column kinds
they accept. Comparison arguments ("value") are raw CSV text, e.g. "True".
"""

import re

from multi_agent.dataset import BOOLEAN, DATE, NUMERIC, TEXT

ANY = (NUMERIC, BOOLEAN, DATE, TEXT)
NUM = (NUMERIC,)
GROUPABLE = (BOOLEAN, DATE, TEXT)

# op -> {arg: kinds tuple for a column argument, "str" for a text value, "prefix" for a positive int}
OPS = {
    "count_rows": {},
    "sum": {"column": NUM},
    "mean": {"column": NUM},
    "group_sum": {"by": GROUPABLE, "column": NUM, "by_prefix?": "prefix"},
    "group_mean": {"by": GROUPABLE, "column": NUM, "by_prefix?": "prefix"},
    "argmax_group_sum": {"by": GROUPABLE, "column": NUM, "by_prefix?": "prefix"},
    "argmax_group_mean": {"by": GROUPABLE, "column": NUM, "by_prefix?": "prefix"},
    "ratio_of_sums_pct": {"numerator": NUM, "denominator": NUM},
    "count_where_equals": {"column": ANY, "value": "str"},
    "rate_where_equals": {"column": ANY, "value": "str"},
    "value_counts": {"column": GROUPABLE},
    "mean_where_equals": {"column": NUM, "where_column": ANY, "value": "str"},
    "pearson": {"x": NUM, "y": NUM},
}

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_PREFIX = 64


def describe_ops() -> dict:
    """Op name -> argument names (optional ones end with '?'); used in prompts."""
    return {op: sorted(spec) for op, spec in OPS.items()}


CLAIM_RULES = (
    "args is always a JSON object; use {} for an op that has no arguments.",
    "A claim has exactly the fields key, op and args.",
    "Every column argument is the exact name of a column in DATASET PROFILE JSON, as a JSON string.",
    'value is the exact cell text to compare with, as a JSON string, never a JSON boolean, number or null. '
    'A boolean column contains only the text "True" or "False", with that exact capitalisation.',
)


def _describe_arg(rule) -> str:
    if rule == "str":
        return "cell text (JSON string)"
    if rule == "prefix":
        return f"optional integer 1-{MAX_PREFIX}: group by the first N characters"
    return "column name, any kind" if rule == ANY else f"column name, {'/'.join(rule)}"


def describe_ops_schema() -> dict:
    """
    P6 (opt-in, schema-aware planner): op name -> {argument: what it must be}, generated from OPS (the table the
    validator enforces), so the guidance cannot drift from validate_claim. It states representation only (JSON
    types, column kinds); it names no dataset, column or value, and the validator stays exactly as strict.
    """
    return {op: {name.rstrip("?"): _describe_arg(rule) for name, rule in spec.items()} for op, spec in OPS.items()}


def validate_claim(claim, profile) -> list:
    """Problems with one claim spec against a dataset profile ([] = valid)."""
    if not isinstance(claim, dict):
        return ["claim must be an object"]
    problems = []
    extra = set(claim) - {"key", "op", "args"}
    if extra:
        problems.append(f"claim has unknown fields {sorted(extra)}")
    key, op, args = claim.get("key"), claim.get("op"), claim.get("args", {})
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        problems.append(f"claim key {key!r} must match {KEY_PATTERN.pattern}")
    if op not in OPS:
        return problems + [f"claim {key!r}: unknown op {op!r}"]
    if not isinstance(args, dict):
        return problems + [f"claim {key!r}: args must be an object"]
    kinds = {c["name"]: c["kind"] for c in profile["columns"]}
    spec = OPS[op]
    allowed = {a.rstrip("?") for a in spec}
    for a in sorted(set(args) - allowed):
        problems.append(f"claim {key!r}: unexpected argument {a!r} for op {op!r}")
    for raw_name, rule in spec.items():
        name, optional = raw_name.rstrip("?"), raw_name.endswith("?")
        if name not in args:
            if not optional:
                problems.append(f"claim {key!r}: op {op!r} requires argument {name!r}")
            continue
        value = args[name]
        if rule == "str":
            if not isinstance(value, str) or len(value) > 200:
                problems.append(f"claim {key!r}: {name!r} must be a string of at most 200 characters")
        elif rule == "prefix":
            if type(value) is not int or not 1 <= value <= MAX_PREFIX:
                problems.append(f"claim {key!r}: {name!r} must be an integer between 1 and {MAX_PREFIX}")
        else:
            if not isinstance(value, str) or value not in kinds:
                problems.append(f"claim {key!r}: unknown column {value!r}")
            elif kinds[value] not in rule:
                problems.append(f"claim {key!r}: column {value!r} is {kinds[value]}, op {op!r} needs {list(rule)}")
    return problems
