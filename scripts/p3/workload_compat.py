#!/usr/bin/env python3
"""
P3.5a analytical-compatibility check for a candidate dependency set.

Run it with the interpreter of the environment under test, for example a
venv installed only from v2/execution/image/requirements.lock:

    <venv>/bin/python -I -B scripts/p3/workload_compat.py --out result.json

What it does:
- For every golden dataset, it takes the FINAL code attempt of every task
  whose expected status is "completed". That code is the hand-written
  fixture in tests/v2/golden/scenarios.py, the same code the existing
  end-to-end tests run. It is not LLM-generated.
- It runs that code through the UNMODIFIED V1 sandbox
  (sandbox.executor.execute_code: the V1 safety scan, the prelude, and a
  subprocess using this interpreter), with charts going to a temporary
  directory.
- It compares every GOLDEN answer with the committed independent answers,
  using the golden tolerance.
- For every expected chart it records the size, mode and SHA-256 of the
  decoded pixels, and whether a stage-A-style canonical copy passes the host
  validator (v2.execution.artifacts.reencode_png).

It does not use a container and makes no network or LLM calls.
"""

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    import importlib.metadata as md
    from PIL import Image

    import sandbox.executor as ex
    from tests.v2.golden import reference as ref
    from tests.v2.golden import scenarios as sc
    from tests.v2.golden.compare import mismatches, parse_golden
    from v2.execution.artifacts import reencode_png

    result = {"python": sys.version.split()[0],
              "versions": {n: md.version(n) for n in ("pandas", "numpy", "matplotlib", "seaborn", "scipy",
                                                      "scikit-learn", "pillow")},
              "datasets": {}}
    ok = True
    with tempfile.TemporaryDirectory() as charts:
        ex.CHARTS_DIR = charts
        for name, scen in sorted(sc.SCENARIOS.items()):
            csv_path = os.path.join(ref.DATA_DIR, scen["file"])
            printed, charts_info, failures = {}, {}, []
            for task in scen["tasks"]:
                tid = task["id"]
                if scen["expected_status"].get(tid) != "completed":
                    continue
                code = scen["code"][task["title"]][-1]
                success, output, chart_path, err = ex.execute_code(code, csv_path, tid)
                if not success:
                    failures.append({"task": tid, "error_type": str(err), "output": output[-400:]})
                    continue
                printed.update(parse_golden(output))
                if tid in scen["expected_charts"]:
                    if not chart_path or not os.path.isfile(chart_path):
                        failures.append({"task": tid, "error_type": "missing chart"})
                        continue
                    with Image.open(chart_path) as img:
                        img.load()
                        canon = io.BytesIO()
                        Image.frombytes(img.mode, img.size, img.tobytes()).save(canon, format="PNG")
                        charts_info[tid] = {"size": list(img.size), "mode": img.mode,
                                            "pixels_sha256": hashlib.sha256(img.tobytes()).hexdigest()}
                    trusted = reencode_png(canon.getvalue())
                    charts_info[tid]["stage_b_accepts_canonical_copy"] = (trusted.width, trusted.height) \
                        == tuple(charts_info[tid]["size"])
            expected = ref.load_expected(name)["answers"]
            bad = mismatches(printed, expected)
            missing = sorted(set(expected) - set(printed))
            ds_ok = not failures and not bad and not missing
            ok = ok and ds_ok
            result["datasets"][name] = {"ok": ds_ok, "answers_checked": len(expected), "mismatches": bad,
                                        "missing": missing, "failures": failures, "charts": charts_info}
    result["ok"] = ok
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, sort_keys=True)
    print(json.dumps({k: (v["ok"], v["answers_checked"], len(v["charts"])) for k, v in result["datasets"].items()}),
          "ok" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
