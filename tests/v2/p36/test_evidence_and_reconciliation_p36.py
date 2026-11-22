"""P3.6-0: preserved CI evidence is sanitized and checksummed; probe values match the Limits contract (R2)."""

import hashlib
import json
import os
import re
import subprocess
import sys

from tests.v2.golden.reference import ROOT
from v2.execution.limits import DEFAULT_LIMITS, GIB

EVIDENCE = os.path.join(ROOT, "docs", "v2", "p3", "ci_run4")
SANITIZED = os.path.join(EVIDENCE, "job_137241676343.sanitized.log")


def _provenance_value(label):
    with open(os.path.join(EVIDENCE, "PROVENANCE.md"), encoding="utf-8") as f:
        for line in f:
            if line.startswith(f"| {label} |"):
                return re.search(r"`([0-9a-f]{64})`", line).group(1)
    raise AssertionError(label)


def test_sanitized_log_matches_its_recorded_checksum():
    with open(SANITIZED, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == _provenance_value("Sanitized log")


def test_raw_log_is_not_committed_and_sanitized_log_has_no_credentials_or_identifiers():
    tracked = subprocess.run(["git", "ls-files", "docs/v2/p3/ci_run4"], cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    assert not [p for p in tracked if p.endswith("job_137241676343.log")]      # never the raw log
    assert not os.path.exists(os.path.join(EVIDENCE, "job_137241676343.log"))
    text = open(SANITIZED, encoding="utf-8").read()
    for pattern in (r"x-access-token", r"AUTHORIZATION", r"\bTOKEN\b", r"ghs_[A-Za-z0-9]", r"ghp_[A-Za-z0-9]",
                    r"/home/runner", r"/run/user/\d", r"Worker ID: \{", r"Region: us-", r"lowerdir=/",
                    r"[\w.+-]+@[\w-]+\.[a-z]{2,}", r"\x1b\["):
        assert not re.search(pattern, text), pattern
    assert "VERDICT: every stage ran and passed" in text
    assert "root-equivalent" in open(os.path.join(EVIDENCE, "report.md"), encoding="utf-8").read()


def test_sanitizer_is_deterministic_on_its_own_output_shape(tmp_path):
    # Re-sanitizing the sanitized log must not remove any measured value (idempotent on clean input).
    out = tmp_path / "o"
    out.mkdir()
    r = subprocess.run([sys.executable, "-I", "-B", os.path.join(EVIDENCE, "sanitize.py"), SANITIZED, str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    again = (out / "job_137241676343.sanitized.log").read_text(encoding="utf-8")
    assert again == open(SANITIZED, encoding="utf-8").read().lstrip("﻿")


def test_probe_and_image_expectations_use_the_limits_contract():
    sys.path.insert(0, os.path.join(ROOT, "scripts", "p3"))
    try:
        import rootless
    finally:
        sys.path.pop(0)
    assert DEFAULT_LIMITS.memory_bytes == 1 * GIB
    assert rootless.LIMITS == {"memory": "1g", "pids": str(DEFAULT_LIMITS.pids), "cpus": "1"}
    with open(os.path.join(ROOT, "v2", "execution", "image", "image-expectations.json"), encoding="utf-8") as f:
        cg = json.load(f)["runtime"]["cgroup"]
    assert cg["memory.max"] == str(DEFAULT_LIMITS.memory_bytes) and cg["pids.max"] == str(DEFAULT_LIMITS.pids)
    assert cg["memory.swap.max"] == "0" and cg["cpu.max"] == "100000 100000"
