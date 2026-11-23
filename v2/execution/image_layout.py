"""
P3.6-4A: the exact trusted runtime that the sandbox image installs under /opt/ip.

RUNTIME_FILES maps every file under /opt/ip to its repository source. It is the complete import
closure of the in-image supervisor, runner and self-tests plus the pinned preludes, and nothing
else: tests/v2/p36/test_image_integration_p364a.py proves the closure from a staged copy alone.
scripts/p3/image_tool.py stage copies exactly these files into the build context directory
CONTEXT_DIR; the Containerfile makes them root-owned and read-only (files 0444, directories 0555).

runtime_tree_sha256() is a digest of (destination, size, SHA-256) for every file, in sorted order.
The Containerfile pins it as the io.insightpilot.runtime.sha256 label; the build driver and
ContainerRuntime.preflight() refuse an image whose label differs from the host's own sources, so
a pinned image built from other runtime code is rejected.

This module reads files only when runtime_tree_sha256() is called. It starts no process and
opens no network connection. It describes an image; it proves nothing about isolation.
"""

import hashlib
import os
import re
import stat

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OPT_IP = "/opt/ip"
CONTEXT_DIR = "opt-ip"
ENTRYPOINT = ("/usr/local/bin/python3", "-I", "-B", OPT_IP + "/supervisor.py")
STAGE_LABEL = "P3.6"
RUNTIME_LABEL = "io.insightpilot.runtime.sha256"
DIGEST_SCHEMA = b"insightpilot.image-runtime/1\n"

_RUNTIME = "v2/execution/image/runtime/"
# destination under /opt/ip -> repository source (relative to ROOT)
RUNTIME_FILES = (
    ("config/__init__.py", "config/__init__.py"),
    ("config/feature_flags.py", "config/feature_flags.py"),
    ("preludes.json", _RUNTIME + "preludes.json"),
    ("runner.py", _RUNTIME + "runner.py"),
    ("selftest.py", _RUNTIME + "selftest.py"),
    ("supervisor.py", _RUNTIME + "supervisor.py"),
    ("v2/__init__.py", "v2/__init__.py"),
    ("v2/execution/__init__.py", "v2/execution/__init__.py"),
    ("v2/execution/errors.py", "v2/execution/errors.py"),
    ("v2/execution/frame.py", "v2/execution/frame.py"),
    ("v2/execution/isolation.py", "v2/execution/isolation.py"),
    ("v2/execution/limits.py", "v2/execution/limits.py"),
    ("v2/execution/results.py", "v2/execution/results.py"),
    ("v2/execution/supervisor_helpers.py", "v2/execution/supervisor_helpers.py"),
)
_REL = re.compile(r"[a-z0-9_]+(/[a-z0-9_]+)*\.(py|json)")


class LayoutError(ValueError):
    pass


def check_layout(files=RUNTIME_FILES):
    """Relative, normalised, lowercase paths only; unique destinations; sorted (the order is reviewed)."""
    dests = [d for d, _ in files]
    if dests != sorted(dests) or len(set(dests)) != len(dests):
        raise LayoutError("runtime destinations must be unique and sorted")
    for dest, src in files:
        for rel in (dest, src):
            if not _REL.fullmatch(rel) or os.path.normpath(rel) != rel:
                raise LayoutError(f"unsafe runtime path {rel!r}")


def read_source(src, root=ROOT):
    """Bytes of one source file: a regular file (not a symlink) inside root."""
    path = os.path.join(root, src)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise LayoutError(f"runtime source is not a regular file: {src}")
    real_root = os.path.realpath(root)
    if os.path.commonpath([os.path.realpath(path), real_root]) != real_root:
        raise LayoutError(f"runtime source escapes the repository: {src}")
    with open(path, "rb") as f:
        return f.read()


def tree_digest(entries):
    """entries: iterable of (destination, bytes). Order-independent; sorted by destination."""
    h = hashlib.sha256(DIGEST_SCHEMA)
    for dest, data in sorted(entries):
        h.update(f"{dest}\0{len(data)}\0{hashlib.sha256(data).hexdigest()}\n".encode())
    return h.hexdigest()


def runtime_tree_sha256(root=ROOT, files=RUNTIME_FILES):
    check_layout(files)
    return tree_digest((dest, read_source(src, root)) for dest, src in files)
