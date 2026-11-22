"""
CLI (offline only):

    python -m evaluation run --out DIR [--reps N] [--datasets sales,student] [--variant v1]
    python -m evaluation compare A/benchmark.json B/benchmark.json --out DIR

Outputs never go to the repository's reports/ or lessons/ folders: --out is required.
"""

import argparse
import json
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m evaluation", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--datasets", default="")
    r.add_argument("--variant", default="v1")
    ab = sub.add_parser("ab", help="interleaved, counterbalanced multi-variant session plus comparisons")
    ab.add_argument("--out", required=True)
    ab.add_argument("--reps", type=int, default=6)
    ab.add_argument("--datasets", default="")
    ab.add_argument("--variants", default="v1,v2-lazy-seaborn,v1-control")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.abspath(a.out)
    for protected in ("reports", "lessons", "data"):
        p = os.path.join(root, protected)
        if out == p or out.startswith(p + os.sep):
            print(f"refusing to write into the repository's {protected}/ folder", file=sys.stderr)
            return 2
    if a.cmd == "run":
        from evaluation import harness, reports
        ds = [d for d in a.datasets.split(",") if d] or None
        result = harness.run_benchmark(out, datasets=ds, repetitions=a.reps, variant=a.variant)
        paths = reports.write_all(result, out)
        with open(paths["markdown"], encoding="utf-8") as f:
            print(f.read())
        s = result["summary"]
        ok = not s["errors"] and all(s["deterministic_across_repetitions"].values())
        return 0 if ok else 1
    if a.cmd == "ab":
        from evaluation import harness, paired, reports
        ds = [d for d in a.datasets.split(",") if d] or None
        variants = [v for v in a.variants.split(",") if v]
        results = harness.run_ab_benchmark(out, variants=variants, datasets=ds, repetitions=a.reps)
        worst = 0
        for v, res in results.items():
            reports.write_all(res, os.path.join(out, v))
        for other in variants[1:]:
            cmp = paired.compare(results[variants[0]], results[other])
            d = os.path.join(out, f"compare_{variants[0]}_vs_{other}")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "comparison.json"), "w", encoding="utf-8") as f:
                json.dump(cmp, f, indent=1, sort_keys=True)
                f.write("\n")
            with open(os.path.join(d, "comparison.md"), "w", encoding="utf-8") as f:
                f.write(paired.markdown(cmp))
            print(paired.markdown(cmp))
            worst = max(worst, 1 if cmp["regressions"] else 0)
        return worst
    from evaluation import paired
    with open(a.a, encoding="utf-8") as f:
        ra = json.load(f)
    with open(a.b, encoding="utf-8") as f:
        rb = json.load(f)
    try:
        cmp = paired.compare(ra, rb)
    except paired.NotComparable as exc:
        print("NOT COMPARABLE:\n  - " + "\n  - ".join(exc.problems), file=sys.stderr)
        return 2
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "comparison.json"), "w", encoding="utf-8") as f:
        json.dump(cmp, f, indent=1, sort_keys=True)
        f.write("\n")
    text = paired.markdown(cmp)
    with open(os.path.join(out, "comparison.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 1 if cmp["regressions"] else 0


if __name__ == "__main__":
    sys.exit(main())
