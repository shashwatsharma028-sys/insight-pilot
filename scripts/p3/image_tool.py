#!/usr/bin/env python3
"""
InsightPilot P3.5a: runtime-image supply-chain tool (host side, development only).

Subcommands
  lock          Query the PyPI JSON API for the exact pins below and write
                v2/execution/image/requirements.lock and dependency-inventory.json
                (network: pypi.org).
  check-lock    Regenerate in memory and fail if the committed files differ
                (network: pypi.org).
  fetch-wheels  Download the exact wheels for one architecture into a cache and
                verify every SHA-256 and size against the committed inventory
                (network: files.pythonhosted.org).
  stage         Build the image build context from the EXACT allowlist only:
                CONTEXT_FILES, the trusted runtime files listed one by one in
                v2/execution/image_layout.RUNTIME_FILES (staged under opt-ip/;
                P3.6-4A) and the verified wheels for one architecture.
                Never walks or copies a directory, so .env, .git, venv/,
                datasets and caches cannot enter. Prints the deterministic
                context digest and the runtime-tree digest.

Nothing here builds or runs a container. See build_image.sh.
Requires Python 3.11 and `packaging` (development dependency) for `lock`.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from v2.execution import image_layout  # noqa: E402  (pure layout data and digest; no process, no network)

IMAGE_DIR = os.path.join(ROOT, "v2", "execution", "image")
LOCK = os.path.join(IMAGE_DIR, "requirements.lock")
INVENTORY = os.path.join(IMAGE_DIR, "dependency-inventory.json")

# Packages the analysis prelude and the sandbox rules allow
# (sandbox/executor.py wrap_code_safely; rules/sandbox_safety.rules.md).
ROOTS = ["pandas", "numpy", "matplotlib", "seaborn", "scipy", "scikit-learn"]

# Exact versions: environment B (golden-verified), except Pillow (replacement decision,
# docs/v2/P3_SECURE_EXECUTION_DESIGN.md §8.7a): 12.3.0 has 58 PyPI advisories, all fixed in 13.0.0.
PINS = {
    "contourpy": "1.3.3", "cycler": "0.12.1", "fonttools": "4.63.0", "joblib": "1.5.3",
    "kiwisolver": "1.5.0", "matplotlib": "3.11.1", "narwhals": "2.24.0", "numpy": "2.4.6",
    "packaging": "26.2", "pandas": "3.0.3", "pillow": "13.0.0", "pyparsing": "3.3.2",
    "python-dateutil": "2.9.0.post0", "scikit-learn": "1.9.0", "scipy": "1.17.1",
    "seaborn": "0.13.2", "six": "1.17.0", "threadpoolctl": "3.6.0",
}
ENV_B_DIFFERENCES = {"pillow": ("12.3.0", "13.0.0")}

PYTHON = (3, 11)
GLIBC_MINOR = 36                      # Debian bookworm (glibc 2.36)
ARCHES = ("x86_64", "aarch64")
OCI_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}

# P3.6-4A: the inert entrypoint left the context; the trusted runtime is staged from image_layout.
CONTEXT_FILES = ("Containerfile", "requirements.lock")
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _get_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def _tags(arch):
    from packaging.tags import compatible_tags, cpython_tags
    plats = [f"manylinux_2_{v}_{arch}" for v in range(GLIBC_MINOR, 16, -1)] + [f"manylinux2014_{arch}"]
    return list(cpython_tags(PYTHON, abis=["cp311", "abi3", "none"], platforms=plats)) + \
        list(compatible_tags(PYTHON, "cp311", platforms=plats))


def build_inventory():
    """Exact wheel per architecture, chosen with pip's tag preference order, from PyPI metadata."""
    from packaging.utils import parse_wheel_filename
    ranks = {a: {t: i for i, t in enumerate(_tags(a))} for a in ARCHES}
    out = {}
    for name, version in sorted(PINS.items()):
        data = _get_json(f"https://pypi.org/pypi/{name}/{version}/json")
        info = data["info"]
        assert _norm(info["name"]) == name and info["version"] == version, (name, version)
        wheels = [f for f in data["urls"] if f["packagetype"] == "bdist_wheel" and not f.get("yanked")]
        entry = {
            "version": version,
            "requires_python": info.get("requires_python"),
            "requires_dist": sorted(info.get("requires_dist") or []),
            "yanked": any(f.get("yanked") for f in data["urls"]),
            "advisories": sorted(v["id"] for v in data.get("vulnerabilities", []) if not v.get("withdrawn")),
            "wheels": {},
        }
        for arch in ARCHES:
            best = None
            for f in wheels:
                _, _, _, ftags = parse_wheel_filename(f["filename"])
                r = min((ranks[arch][t] for t in ftags if t in ranks[arch]), default=None)
                if r is not None and (best is None or r < best[0]):
                    best = (r, f)
            if best is None:
                raise SystemExit(f"no cp311 wheel for {name}=={version} on {arch}; source builds are not allowed")
            f = best[1]
            entry["wheels"][arch] = {"filename": f["filename"], "sha256": f["digests"]["sha256"],
                                     "size": f["size"], "url": f["url"]}
        out[name] = entry
    return {
        "schema": "insightpilot.image-dependency-inventory/1",
        "python": "cp311",
        "platform": f"manylinux_2_{GLIBC_MINOR} (Debian bookworm)",
        "architectures": list(ARCHES),
        "roots": ROOTS,
        "env_b_differences": {k: list(v) for k, v in ENV_B_DIFFERENCES.items()},
        "source": "https://pypi.org/pypi/<name>/<version>/json (digests and advisories as published by PyPI)",
        "packages": out,
    }


def render_lock(inv) -> str:
    lines = [
        "# InsightPilot sandbox runtime image: exact, hash-locked runtime dependencies.",
        "# GENERATED by scripts/p3/image_tool.py lock; do not edit by hand.",
        "# Install only with: pip install --require-hashes --no-deps --only-binary=:all: --no-index",
        "# Hashes: one wheel per architecture (" + ", ".join(ARCHES) + "), cp311, manylinux_2_"
        + str(GLIBC_MINOR) + ".",
        "# Runtime only. Development and test tools (pytest, packaging, uv) are NOT in this file.",
    ]
    for name, e in sorted(inv["packages"].items()):
        hashes = sorted({w["sha256"] for w in e["wheels"].values()})
        lines.append(f"{name}=={e['version']} \\")
        lines += [f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else "") for i, h in enumerate(hashes)]
    return "\n".join(lines) + "\n"


def render_inventory(inv) -> str:
    return json.dumps(inv, indent=1, sort_keys=True) + "\n"


def parse_lock(text):
    """Strict parser: name==version followed only by --hash=sha256:<hex> lines."""
    reqs, cur = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = line[:-1].strip() if line.endswith("\\") else line
        m = re.fullmatch(r"([a-z0-9-]+)==([0-9][0-9A-Za-z.+!-]*)", body)
        if m:
            if m.group(1) in reqs:
                raise ValueError(f"duplicate requirement {m.group(1)}")
            cur = m.group(1)
            reqs[cur] = {"version": m.group(2), "hashes": []}
            continue
        h = re.fullmatch(r"--hash=sha256:([0-9a-f]{64})", body)
        if h and cur:
            reqs[cur]["hashes"].append(h.group(1))
            continue
        raise ValueError(f"unexpected lock line: {raw!r}")
    return reqs


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_inventory(path=INVENTORY):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_wheels(arch, cache, inv=None):
    inv = inv or load_inventory()
    os.makedirs(cache, exist_ok=True)
    results = []
    for name, e in sorted(inv["packages"].items()):
        w = e["wheels"][arch]
        dest = os.path.join(cache, w["filename"])
        if not os.path.exists(dest):
            tmp = dest + ".part"
            with urllib.request.urlopen(w["url"], timeout=300) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            os.replace(tmp, dest)
        got = _sha256(dest)
        size = os.path.getsize(dest)
        ok = got == w["sha256"] and size == w["size"]
        results.append((name, w["filename"], ok, got, size))
        if not ok:
            os.unlink(dest)
    return results


def stage(arch, cache, out_dir, inv=None, image_dir=IMAGE_DIR, source_root=ROOT):
    """
    Stage the build context from the allowlist only. Every wheel is re-hashed
    here; every runtime file is read from source_root by its exact path.
    Returns (file list, context tarball SHA-256).
    """
    inv = inv or load_inventory(os.path.join(image_dir, "dependency-inventory.json"))
    if arch not in ARCHES:
        raise SystemExit(f"unknown architecture {arch!r}")
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise SystemExit(f"refusing to stage into a non-empty directory: {out_dir}")
    os.makedirs(os.path.join(out_dir, "wheels"), exist_ok=True)
    for name in CONTEXT_FILES:
        src = os.path.join(image_dir, name)
        if os.path.islink(src) or not os.path.isfile(src):
            raise SystemExit(f"context file missing or not a regular file: {name}")
        shutil.copyfile(src, os.path.join(out_dir, name))
    try:
        image_layout.check_layout()
        for dest, src in image_layout.RUNTIME_FILES:
            data = image_layout.read_source(src, source_root)
            target = os.path.join(out_dir, image_layout.CONTEXT_DIR, dest)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "xb") as fh:
                fh.write(data)
    except (OSError, image_layout.LayoutError) as exc:
        raise SystemExit(f"runtime file missing or unsafe: {exc}") from None
    for name, e in sorted(inv["packages"].items()):
        w = e["wheels"][arch]
        if "/" in w["filename"] or not w["filename"].endswith(".whl"):
            raise SystemExit(f"bad wheel filename {w['filename']!r}")
        src = os.path.join(cache, w["filename"])
        if os.path.islink(src) or not os.path.isfile(src) or _sha256(src) != w["sha256"] \
                or os.path.getsize(src) != w["size"]:
            raise SystemExit(f"wheel missing or hash/size mismatch: {w['filename']}")
        shutil.copyfile(src, os.path.join(out_dir, "wheels", w["filename"]))
    files = sorted(os.path.relpath(os.path.join(d, f), out_dir).replace(os.sep, "/")
                   for d, _, fs in os.walk(out_dir) for f in fs)
    return files, context_digest(out_dir, files)


def staged_runtime_digest(out_dir):
    """Runtime-tree digest of a STAGED context (what the build will copy), not of the repository."""
    entries = []
    for dest, _ in image_layout.RUNTIME_FILES:
        with open(os.path.join(out_dir, image_layout.CONTEXT_DIR, dest), "rb") as fh:
            entries.append((dest, fh.read()))
    return image_layout.tree_digest(entries)


def context_digest(out_dir, files):
    """SHA-256 of a deterministic tar of the context (sorted names, fixed owner, mode and mtime)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in files:
            data = open(os.path.join(out_dir, rel), "rb").read()
            ti = tarfile.TarInfo(rel)
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = len(data), 0, 0o644, 0, 0, "", ""
            tar.addfile(ti, io.BytesIO(data))
    return hashlib.sha256(buf.getvalue()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lock")
    sub.add_parser("check-lock")
    f = sub.add_parser("fetch-wheels")
    f.add_argument("--arch", choices=ARCHES, required=True)
    f.add_argument("--cache", required=True)
    s = sub.add_parser("stage")
    s.add_argument("--arch", choices=ARCHES, required=True)
    s.add_argument("--cache", required=True)
    s.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd in ("lock", "check-lock"):
        inv = build_inventory()
        new = {LOCK: render_lock(inv), INVENTORY: render_inventory(inv)}
        if a.cmd == "lock":
            for path, text in new.items():
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            print("wrote", *(os.path.relpath(p, ROOT) for p in new))
            return 0
        stale = [os.path.relpath(p, ROOT) for p, t in new.items() if open(p, encoding="utf-8").read() != t]
        print("check-lock:", "OK" if not stale else f"DIFFERS: {stale}")
        return 1 if stale else 0
    if a.cmd == "fetch-wheels":
        res = fetch_wheels(a.arch, a.cache)
        for name, fn, ok, got, size in res:
            print("OK  " if ok else "BAD ", fn, got, size)
        return 0 if all(r[2] for r in res) else 1
    files, digest = stage(a.arch, a.cache, a.out)
    print("\n".join(files))
    print("context-sha256", digest)
    print("runtime-sha256", staged_runtime_digest(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
