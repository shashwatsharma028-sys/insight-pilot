"""Scenario registry. A scenario is one function returning observations; the split is fixed by its FAMILY."""

import hashlib
import tempfile
import time
import traceback
from dataclasses import dataclass, field

CATEGORIES = {
    1: "prompt_injection",
    2: "unauthorized_operation",
    3: "sensitive_disclosure",
    4: "approval_tampering",
    5: "cross_session_context",
    6: "fabricated_or_unsafe_output",
    7: "resource_exhaustion",
    8: "forged_message",
}
HELD_OUT_SHARE = 0.4          # per category, the first 40% of families in sha256 order are held out


def _rank(family: str) -> str:
    return hashlib.sha256(("insightpilot-v2-secbench-split|" + family).encode()).hexdigest()


def assign_splits(scenarios):
    """Stratified by category: within each category, families sorted by a fixed hash; the first
    max(1, round(40%)) are held out. Every variant of a family gets its family's split."""
    by_cat = {}
    for s in scenarios:
        by_cat.setdefault(s.category, set()).add(s.family)
    held = set()
    for fams in by_cat.values():
        ordered = sorted(fams, key=_rank)
        held |= set(ordered[:max(1, round(HELD_OUT_SHARE * len(ordered)))])
    for s in scenarios:
        s.split = "heldout" if s.family in held else "dev"


@dataclass
class Scenario:
    id: str
    kind: str                 # attack | benign
    variant: str              # the variant label given at registration (descriptive only)
    category: int             # 1..8 for attacks, 0 for benign
    objective: str            # the predefined unauthorized objective (attacks) or the legitimate outcome (benign)
    severity: str             # critical | high | medium (attacks); "-" for benign
    fn: object
    injection: bool = False   # the case carries a prompt-injection payload (detection positives)
    family: str = field(init=False)
    split: str = field(init=False, default="unassigned")

    def __post_init__(self):
        from security_bench.families import family_of
        self.family = family_of(self.id)        # mechanism-level family (security_bench/families.py)


SCENARIOS = []


def attack(sid, family, category, objective, severity, injection=False):
    def reg(fn):
        SCENARIOS.append(Scenario(sid, "attack", family, category, objective, severity, fn, injection))
        return fn
    return reg


def benign(sid, family, objective):
    def reg(fn):
        SCENARIOS.append(Scenario(sid, "benign", family, 0, objective, "-", fn))
        return fn
    return reg


OBS_KEYS = ("delivered", "detected", "attempted", "refused", "effect", "legit_completed")


def execute(s: Scenario) -> dict:
    """Run one scenario in a fresh temporary directory. An unexpected exception is recorded, never hidden."""
    with tempfile.TemporaryDirectory(prefix="ip-secbench-") as d:
        t0 = time.perf_counter()
        try:
            obs = s.fn(d) or {}
            error = None
        except Exception as e:  # noqa: BLE001 - recorded as a harness error and counted separately
            obs, error = {}, f"{type(e).__name__}: {e}"[:300] + " | " + traceback.format_exc().splitlines()[-2][:200]
        secs = time.perf_counter() - t0
    rec = {"id": s.id, "kind": s.kind, "family": s.family, "variant": s.variant, "category": s.category,
           "category_name": CATEGORIES.get(s.category, "benign"), "split": s.split, "severity": s.severity,
           "objective": s.objective, "injection": s.injection, "seconds": round(secs, 4), "harness_error": error}
    for k in OBS_KEYS:
        rec[k] = obs.get(k)
    rec["notes"] = obs.get("notes")
    rec["security_events"] = obs.get("security_events")
    rec["policy_check_ms"] = obs.get("policy_check_ms") or []
    return rec
