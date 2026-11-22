#!/usr/bin/env python3
"""
P3.5b: reproduce the hermetic golden chart references on a host (no container).

    <lock-only venv>/bin/python -I -B scripts/p3/hermetic_reference.py [--check]

It runs exactly the scripts the container runs (rootless.golden_scripts():
the hash-pinned fixtures, the V1 prelude and the trusted epilogue) with this
interpreter. The environment is cleared: only PATH, HOME, LANG, MPLBACKEND
and a fresh MPLCONFIGDIR are set, so a host MATPLOTLIBRC or other variable
cannot leak in.

It REFUSES (exit 2) at the first fixture whose reported render environment
differs from golden_fixtures.json "render_environment". That includes
visible system fonts, a non-packaged matplotlibrc, a different font file,
hinting, FreeType, Matplotlib or Pillow. On a normal Linux host, hide the
system fonts first, e.g. as root in a private mount namespace:

    unshare --mount --propagation private sh -c 'for d in /usr/share/fonts /usr/local/share/fonts \\
        /var/cache/fontconfig; do [ -d $d ] && mount -t tmpfs -o ro,size=1k none $d; done; exec "$0" "$@"' \\
        <venv>/bin/python -I -B scripts/p3/hermetic_reference.py --check

It prints the chart references as JSON. It never writes the manifest.
--check exits 1 unless every chart equals the committed "chart_hermetic"
reference exactly.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rootless as rl  # noqa: E402


def run(check=False, python=sys.executable):
    expected = rl._json(rl.FIXTURES)["render_environment"]
    out = {}
    for fx, data, script in rl.golden_scripts():
        with tempfile.TemporaryDirectory() as t:
            text = script.replace('r"/in/dataset.csv"', 'r"' + data + '"').replace('r"/out/', 'r"' + t + '/')
            env = {"PATH": "/usr/bin:/bin", "HOME": t, "LANG": "C.UTF-8", "MPLBACKEND": "Agg",
                   "MPLCONFIGDIR": os.path.join(t, "mpl")}
            p = subprocess.run([python, "-I", "-B", "-"], input=text.encode(), capture_output=True, env=env,
                               timeout=300)
        chart, renv = rl.parse_render_output(p.stdout.decode("utf-8", "replace"))
        problems = rl.check_render_env(renv, expected)
        if p.returncode != 0 or problems:
            raise rl.Refused([f"{fx['task_id']}: exit {p.returncode}; not a hermetic render environment"] + problems
                             + ([p.stderr.decode('utf-8', 'replace')[-300:]] if p.returncode else []))
        out[fx["task_id"]] = chart
    result = {"render_environment": expected, "charts": out}
    if check:
        result["matches_chart_hermetic"] = {fx["task_id"]: out[fx["task_id"]] == fx["chart_hermetic"]
                                            for fx in rl._json(rl.FIXTURES)["fixtures"]}
    return result


def main(argv):
    check = "--check" in argv
    try:
        result = run(check=check)
    except rl.Refused as exc:
        print("hermetic_reference: REFUSED", file=sys.stderr)
        for line in exc.problems:
            print("  - " + line, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1, sort_keys=True))
    return 1 if check and not all(result["matches_chart_hermetic"].values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
