"""
Run the corpus against whichever source tree is first on sys.path and write one JSON line per case.

    python -B -m security_bench.runner --label baseline --split dev --out runs/sec/baseline_dev.jsonl

The tree under test is recorded (multi_agent's file path and that tree's git HEAD) so a result can never be
attributed to the wrong code. No network: every model is a scripted CaptureModel.
"""

import argparse
import hashlib
import inspect
import json
import os
import sys
import time

from security_bench import attacks_a, attacks_b, benign, core, registry  # noqa: F401 - registers the scenarios
from security_bench import families
from security_bench.registry import SCENARIOS, assign_splits, execute

assign_splits(SCENARIOS)


def corpus_digest() -> str:
    """SHA-256 over the scenario definitions (ids, families, splits, objectives and source code)."""
    h = hashlib.sha256()
    for mod in (core, registry, families, attacks_a, attacks_b, benign):
        h.update(inspect.getsource(mod).encode())
    for s in SCENARIOS:
        h.update(f"{s.id}|{s.kind}|{s.family}|{s.variant}|{s.category}|{s.split}|{s.severity}|{s.objective}".encode())
    return h.hexdigest()


def _git_head(root):
    """Best-effort HEAD read from the repository files (no subprocess)."""
    try:
        g = os.path.join(root, ".git")
        if os.path.isfile(g):
            g = open(g).read().split("gitdir:", 1)[1].strip()
        head = open(os.path.join(g, "HEAD")).read().strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(" ", 1)[1]
        for base in (g, os.path.join(g, "..", "..")):
            p = os.path.join(base, ref)
            if os.path.isfile(p):
                return open(p).read().strip()
        return ref
    except (OSError, IndexError):
        return None


def runtime_digest(root):
    """SHA-256 over every .py file under multi_agent/ and v2/ of the tree under test (path + bytes)."""
    h = hashlib.sha256()
    for top in ("multi_agent", "v2"):
        for dirpath, dirs, files in sorted(os.walk(os.path.join(root, top))):
            dirs.sort()
            for name in sorted(files):
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    h.update(os.path.relpath(path, root).encode() + b"\0" + open(path, "rb").read())
    return h.hexdigest()


def tree_info():
    import multi_agent
    root = os.path.dirname(os.path.dirname(os.path.abspath(multi_agent.__file__)))
    return {"tree": root, "git_head": _git_head(root), "runtime_sha256": runtime_digest(root)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--split", choices=("dev", "heldout", "all"), default="dev")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None, help="comma-separated scenario ids (debugging)")
    a = ap.parse_args(argv)
    chosen = [s for s in SCENARIOS if a.split == "all" or s.split == a.split]
    if a.only:
        ids = set(a.only.split(","))
        chosen = [s for s in chosen if s.id in ids]
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids)), "duplicate scenario ids"
    info = {"label": a.label, "split": a.split, "corpus_sha256": corpus_digest(), "cases": len(chosen),
            "python": sys.version.split()[0], "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **tree_info()}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        f.write(json.dumps({"meta": info}) + "\n")
        for s in chosen:
            rec = execute(s)
            rec["label"] = a.label
            f.write(json.dumps(rec, default=str) + "\n")
            f.flush()
            if rec["harness_error"]:
                print(f"HARNESS ERROR {s.id}: {rec['harness_error']}", file=sys.stderr)
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
