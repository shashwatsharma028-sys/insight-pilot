#!/usr/bin/env python3
"""
P3.6-4B: Podman argument-compatibility diagnostic for the REVIEWED run argv (no container is started).

    python3 -I -B scripts/p3/argv_compat.py --image sha256:<64 hex> [--evidence FILE]
    python3 -I -B scripts/p3/argv_compat.py --rootfs DIR [--evidence FILE]      (parse-only, informational)

It asks Podman to `create` (never `start`) containers from v2/execution/podman_argv.run_argv:
  full         the exact reviewed argv, for each supervisor mode;
  add_one      a minimal known-good base (--rm --network=none) plus each reviewed option group alone,
               so every option Podman rejects on its own is found in one pass;
  leave_one    the full reviewed argv minus each option group, so interacting conflicts (like
               --dns=none with --network=none) are found in one pass.
Every created container is removed with `rm --force` and verified gone. Nothing runs inside a
container, no generated code exists here, and the diagnostic argv variants are NEVER used to execute
anything: they only locate incompatibilities for review. It changes no policy.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from v2.execution import podman_argv as pa  # noqa: E402

VALUE_FLAGS = {"--name", "--label", "--tmpfs", "--mount", "--user", "--env"}
# Flags that exist only on `podman run` in 4.9.3 (not on `create`): excluded from the create checks and
# verified against `podman run --help` instead (run #4 also used --passwd=false successfully).
RUN_ONLY = {"--passwd=false": "--passwd "}
# An option group that is valid only together with another reviewed group (add_one then uses both).
REQUIRES = {"--memory-swap=": "--memory="}
ENV_KEEP = ("PATH", "HOME", "XDG_RUNTIME_DIR", "LANG", "USER")


def _env():
    return {k: os.environ[k] for k in ENV_KEEP if k in os.environ}


def groups_of(options):
    """Split podman options into groups: a flag with its separate value counts as one group."""
    out, i = [], 0
    while i < len(options):
        if options[i] in VALUE_FLAGS:
            out.append(options[i:i + 2])
            i += 2
        else:
            out.append(options[i:i + 1])
            i += 1
    return out


def split_run_argv(argv, image):
    """(podman, options without --name, target-and-command) from a reviewed run argv."""
    assert argv[1] == "run"
    at = argv.index(image)
    opts = argv[2:at]
    if "--name" in opts:
        k = opts.index("--name")
        opts = opts[:k] + opts[k + 2:]
    return argv[0], opts, argv[at:]


def _create(podman, opts, target, timeout=60):
    name = "ip-compat-" + uuid.uuid4().hex[:12]
    argv = [podman, "create", "--name", name, *opts, *target]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, env=_env(), timeout=timeout)
        rc, err = r.returncode, r.stderr.strip()[-400:]
    except subprocess.TimeoutExpired:
        rc, err = None, "timeout"
    subprocess.run([podman, "rm", "--force", "--time", "0", name], capture_output=True, env=_env(), timeout=60)
    gone = subprocess.run([podman, "container", "exists", name], capture_output=True, env=_env(), timeout=30)
    if gone.returncode != 1:
        raise SystemExit(f"diagnostic container {name} was not removed (exists rc={gone.returncode})")
    return {"ok": rc == 0, "rc": rc, "error": "" if rc == 0 else err}


def _reviewed(podman, img, run_id, inputs, mode, rootfs):
    argv = pa.run_argv(podman, img, instance_id=uuid.uuid4().hex, run_id=run_id, inputs_dir=inputs, mode=mode)
    _, opts, target = split_run_argv(argv, img)
    opts = [o for o in opts if o not in RUN_ONLY]
    if rootfs:
        target = ["--rootfs", rootfs] + target[1:]
    return opts, target


def diagnose(podman, image=None, rootfs=None):
    with tempfile.TemporaryDirectory() as tmp:
        run_id = uuid.uuid4().hex
        inputs = os.path.join(os.path.realpath(tmp), "runs", run_id, "in")
        os.makedirs(inputs)
        img = image or "sha256:" + "0" * 64
        out = {"podman": podman, "target": "image" if image else "rootfs (parse-only)", "full": {}}
        for mode in pa.SUPERVISOR_MODES:
            opts, target = _reviewed(podman, img, run_id, inputs, mode, rootfs)
            out["full"][mode] = _create(podman, opts, target)
        opts, target = _reviewed(podman, img, run_id, inputs, "attest-only", rootfs)
        base = ["--rm", "--network=none"]
        out["base"] = _create(podman, base, target)
        out["add_one"], out["leave_one"] = {}, {}
        groups = groups_of(opts)
        for g in groups:
            key = " ".join(g)
            if len(g) == 1 and g[0] in base:
                continue
            extra = [o for prefix, need in REQUIRES.items() if key.startswith(prefix) for o in opts if o.startswith(need)]
            out["add_one"][key] = _create(podman, base + extra + g, target)
        for k, g in enumerate(groups):
            rest = [x for j, grp in enumerate(groups) if j != k for x in grp]
            out["leave_one"][" ".join(g)] = _create(podman, rest, target)
        help_text = subprocess.run([podman, "run", "--help"], capture_output=True, text=True, env=_env(),
                                   timeout=60).stdout
        out["run_only"] = {flag: needle in help_text for flag, needle in RUN_ONLY.items()}
        out["rejected_alone"] = sorted(k for k, v in out["add_one"].items() if not v["ok"]) + \
            sorted(f for f, ok in out["run_only"].items() if not ok)
        out["full_ok"] = all(v["ok"] for v in out["full"].values())
        out["passes_when_removed"] = [] if out["full_ok"] else sorted(k for k, v in out["leave_one"].items() if v["ok"])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--image")
    g.add_argument("--rootfs")
    ap.add_argument("--evidence")
    a = ap.parse_args(argv)
    if a.image and not pa.IMAGE_ID.fullmatch(a.image):
        raise SystemExit("--image must be sha256:<64 hex>")
    podman = os.path.realpath(shutil.which("podman") or "/usr/bin/podman")
    result = diagnose(podman, image=a.image, rootfs=a.rootfs)
    text = json.dumps(result, indent=1, sort_keys=True)
    if a.evidence:
        with open(a.evidence, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 0 if result["full_ok"] and not result["rejected_alone"] else 1


if __name__ == "__main__":
    sys.exit(main())
