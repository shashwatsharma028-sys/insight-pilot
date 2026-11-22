#!/usr/bin/env python3
"""
Generate v2/execution/image/runtime/preludes.json from the HOST wrapper functions (P3.6-2).

    python3 -I -B scripts/p3/gen_preludes.py [--check]

The V1 prelude and epilogue are cut from sandbox.executor.wrap_code_safely(SENTINEL,
"/in/dataset.csv", "chart") with CHARTS_DIR="/out" (so CHART_PATH is /out/chart.png); the
lazy-seaborn prelude is sandbox_v2.lazy_prelude.derive_lazy_wrapper_text of that V1 prelude.
--check exits 1 if the committed file differs. Nothing is executed.
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, "v2", "execution", "image", "runtime", "preludes.json")
SENTINEL = "__INSIGHTPILOT_GENERATED_CODE_SENTINEL__\n"


def generate():
    sys.path.insert(0, ROOT)
    import sandbox.executor as v1
    from sandbox_v2.lazy_prelude import derive_lazy_wrapper_text
    saved = v1.CHARTS_DIR
    try:
        v1.CHARTS_DIR = "/out"
        text = v1.wrap_code_safely(SENTINEL, "/in/dataset.csv", "chart")
    finally:
        v1.CHARTS_DIR = saved
    if text.count(SENTINEL) != 1:
        raise SystemExit("sentinel not found exactly once in the V1 wrapper output")
    prelude, epilogue = text.split(SENTINEL)
    preludes = {"v1": {"prelude": prelude, "epilogue": epilogue},
                "lazy_seaborn": {"prelude": derive_lazy_wrapper_text(prelude)}}
    body = json.dumps(preludes, sort_keys=True, ensure_ascii=True).encode()
    return {"schema": 1, "source": "sandbox.executor.wrap_code_safely + sandbox_v2.lazy_prelude (CHARTS_DIR=/out)",
            "sha256": hashlib.sha256(body).hexdigest(), "preludes": preludes}


def main(argv):
    data = json.dumps(generate(), indent=1, sort_keys=True) + "\n"
    if "--check" in argv:
        with open(OUT, encoding="utf-8") as f:
            return 0 if f.read() == data else 1
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
