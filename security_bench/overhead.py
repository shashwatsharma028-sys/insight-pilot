"""
Matched offline end-to-end overhead: the four golden datasets, the real Supervisor and TrustedOpsAnalyst, the same
scripted models and approvals, timed on whichever source tree is first on sys.path (no network).

    python -B -m security_bench.overhead --reps 10 --out overhead_secured.json

Every timed run must complete with the golden answers, so a faster but wrong tree cannot look better.
"""

import argparse
import json
import os
import statistics
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    from multi_agent import op_engine
    from multi_agent.team import build_team
    from multi_agent.verification import values_match
    from security_bench.runner import tree_info
    from tests.v2.golden.reference import DATA_DIR, DATASETS, load_expected
    from tests.v2.p5 import scripted

    def once(ds):
        llm = scripted.ScriptedAgentLLM(ds, None, mode="literal")
        t0 = time.perf_counter()
        sup, _ = build_team(llm, None, scripted.approve_all, analyst=op_engine.TrustedOpsAnalyst())
        st = sup.run(os.path.join(DATA_DIR, DATASETS[ds]), "Analyse this dataset.")
        secs = time.perf_counter() - t0
        final, golden = st["final"], load_expected(ds)
        assert final["status"] == "completed", (ds, final["status"])
        assert all(values_match(v, golden[k]) for k, v in final["accepted_claims"].items() if k in golden)
        return secs
    names = ("sales", "attendance", "student", "ecommerce")
    for ds in names:                       # warm-up (imports, caches); not timed
        once(ds)
    per = {ds: [] for ds in names}
    for _ in range(a.reps):
        for ds in names:
            per[ds].append(once(ds))
    allt = sorted(x for v in per.values() for x in v)
    out = {"tree": tree_info(), "reps": a.reps, "runs": len(allt),
           "median_ms": round(1000 * statistics.median(allt), 3),
           "p95_ms": round(1000 * allt[min(len(allt) - 1, int(round(0.95 * (len(allt) - 1))))], 3),
           "median_ms_by_dataset": {ds: round(1000 * statistics.median(v), 3) for ds, v in per.items()}}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
