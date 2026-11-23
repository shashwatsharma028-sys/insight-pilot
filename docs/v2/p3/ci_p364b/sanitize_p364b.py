#!/usr/bin/env python3
"""
Sanitize the P3.6-4B GitHub Actions job logs with EXACTLY the reviewed P3.6-0 rules R1-R6
(docs/v2/p3/ci_run4/sanitize.py: sanitize_line, imported, not copied).

    python3 -I -B docs/v2/p3/ci_p364b/sanitize_p364b.py RAW_LOG RUN_NUMBER JOB_ID OUT_DIR

Writes OUT_DIR/run<N>_job_<JOB_ID>.sanitized.log and OUT_DIR/run<N>_report.md (the evidence
report the job printed, "## P3.6-4B CI ..." to "VERDICT:"), and prints both SHA-256 values.
"""

import hashlib
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("ci_run4_sanitize", os.path.join(HERE, "..", "ci_run4", "sanitize.py"))
_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rules)


def main(raw_path, run, job, out_dir):
    raw = open(raw_path, "rb").read()
    out, in_prov = [], False
    for line in raw.decode("utf-8-sig").split("\n"):
        if "##[group]Runner Image Provisioner" in line:
            in_prov = True
        elif "##[endgroup]" in line:
            in_prov = False
        out.append(_rules.sanitize_line(line, in_prov))
    sanitized = "\n".join(out)
    with open(os.path.join(out_dir, f"run{run}_job_{job}.sanitized.log"), "w", encoding="utf-8") as f:
        f.write(sanitized)
    body = [ln[29:] if re.match(r"^\d{4}-\d\d-\d\dT", ln) else ln for ln in out]
    start = max(i for i, ln in enumerate(body) if ln.startswith("## P3.6-4B CI image build"))
    end = next(i for i in range(start, len(body)) if body[i].startswith("VERDICT:"))
    with open(os.path.join(out_dir, f"run{run}_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(body[start:end + 1]) + "\n")
    print("raw_sha256", hashlib.sha256(raw).hexdigest())
    print("sanitized_sha256", hashlib.sha256(sanitized.encode()).hexdigest())


if __name__ == "__main__":
    main(*sys.argv[1:5])
