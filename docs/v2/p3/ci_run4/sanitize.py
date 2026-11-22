#!/usr/bin/env python3
"""
Deterministic sanitizer for the GitHub Actions run #4 job log (P3.6-0 evidence preservation).

    python3 -I -B docs/v2/p3/ci_run4/sanitize.py RAW_LOG OUT_DIR

Writes OUT_DIR/job_137241676343.sanitized.log, OUT_DIR/report.md and OUT_DIR/enforcement_probe.txt.
Rules (each one is applied to every line; nothing else is changed):
  R1 strip ANSI colour escapes;
  R2 lines mentioning credential handling (TOKEN, AUTH, AUTHORIZATION, x-access-token,
     extraheader, "Secret source") are replaced by a fixed placeholder;
  R3 hosted-runner identifiers (Worker ID, Region, Cloud, provisioner Commit/Build Date) -> <redacted>;
  R4 host paths /home/runner and /run/user/<uid> -> <runner-home> / <runtime-dir>;
  R5 overlay storage layer ids (lowerdir/upperdir/workdir values) -> <overlay-redacted>;
  R6 any e-mail address or IPv4-like token that is not a dotted version -> <redacted>.
Timestamps, commands, results, versions and all measured values are kept.
"""

import hashlib
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*m")
CRED = re.compile(r"TOKEN|AUTH|AUTHORIZATION|x-access-token|extraheader|Secret source", re.I)
RUNNER_ID = re.compile(r"^(\S+\s+)(Worker ID|Region|Cloud|Commit|Build Date):.*$")
PATHS = [(re.compile(r"/home/runner"), "<runner-home>"), (re.compile(r"/run/user/\d+"), "<runtime-dir>")]
OVERLAY = re.compile(r"((?:lowerdir|upperdir|workdir)=)[^,\"]+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")


def sanitize_line(line, in_provisioner):
    line = ANSI.sub("", line)
    if CRED.search(line):
        return line[:29] + "<line removed by sanitizer rule R2: credential handling>"
    if in_provisioner:
        m = RUNNER_ID.match(line)
        if m:
            return m.group(1) + m.group(2) + ": <redacted>"
    for rx, rep in PATHS:
        line = rx.sub(rep, line)
    line = OVERLAY.sub(r"\1<overlay-redacted>", line)
    line = EMAIL.sub("<redacted>", line)
    line = IPV4.sub(lambda m: "<redacted>" if all(int(p) <= 255 for p in m.group(0).split(".")) else m.group(0), line)
    return line


def main(raw_path, out_dir):
    raw = open(raw_path, "rb").read()
    text = raw.decode("utf-8-sig")
    out, in_prov = [], False
    for line in text.split("\n"):
        if "##[group]Runner Image Provisioner" in line:
            in_prov = True
        elif "##[endgroup]" in line:
            in_prov = False
        out.append(sanitize_line(line, in_prov))
    sanitized = "\n".join(out)
    with open(f"{out_dir}/job_137241676343.sanitized.log", "w", encoding="utf-8") as f:
        f.write(sanitized)
    body = [ln[29:] if re.match(r"^\d{4}-\d\d-\d\dT", ln) else ln for ln in out]
    start = next(i for i, ln in enumerate(body) if ln.startswith("## P3.5b CI image build"))
    end = next(i for i, ln in enumerate(body) if ln.startswith("VERDICT:"))
    with open(f"{out_dir}/report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(body[start:end + 1]) + "\n")
    probe = next(i for i, ln in enumerate(body) if '"write_probes"' in ln)       # Gate 3 enforcement output
    g3 = max(i for i in range(probe) if body[i].startswith("##[group]Run"))
    g3_end = next(i for i in range(probe, len(body)) if body[i].startswith("##[group]Run"))
    with open(f"{out_dir}/enforcement_probe.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(body[g3:g3_end]) + "\n")
    print("raw_sha256", hashlib.sha256(raw).hexdigest())
    print("sanitized_sha256", hashlib.sha256(sanitized.encode()).hexdigest())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
