#!/usr/bin/env python3
"""
P3.5b CI evidence report (standard library only); P3.6-4B adds the content gate and Phase F.

    python3 -I -B scripts/p3/ci_report.py EVIDENCE_DIR

Reads the stage exit codes (<stage>.rc) and the JSON evidence written by
rootless.py, and prints a Markdown report. Every number in it comes from those
files; stages that did not run are reported as NOT RUN, never as passed. The
exit status is 0 only if every stage ran and passed.
"""

import json
import os
import sys

STAGES = [
    ("content", "Approved-content gate (pushed code == reviewed P3.6-4B content)"),
    ("gate1", "Gate 1: host facts as provided (informational)"),
    ("gate2", "Gate 2: host facts with a user manager"),
    ("gate3", "Gate 3: enforcement probe (real rootless container)"),
    ("wheels", "Phase C: SHA-256-verified wheel cache"),
    ("build1", "Phase C: build 1 (offline, digest-pinned base)"),
    ("verify1", "Phase C: verification of build 1"),
    ("build2", "Phase D: clean build 2 (identical inputs)"),
    ("compare", "Phase D: build 1 vs build 2 comparison"),
    ("startup", "Phase E: warm/cold start-up benchmark"),
    ("golden", "Phase E: hash-pinned golden fixtures in the image"),
    ("ospkgs", "Phase E: OS package inventory"),
    ("hostenv", "Phase F: host test environment"),
    ("compat", "Phase F: Podman argument compatibility (create only, reviewed argv)"),
    ("runtime", "Phase F: real ContainerRuntime (preflight, isolation, fail-closed, golden numbers and charts)"),
    ("p5_v1", "Phase F: P5 20-case suite, V1 prelude, real container port"),
    ("p5_lazy", "Phase F: P5 20-case suite, lazy-seaborn prelude, real container port"),
    ("tests", "Readiness tests (incl. workflow-policy test)"),
]
REQUIRED = [s for s, _ in STAGES if s != "gate1"]


def rc(d, stage):
    try:
        with open(os.path.join(d, f"{stage}.rc"), encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def load(d, name):
    try:
        with open(os.path.join(d, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def mib(n):
    return f"{n / 1048576:.1f} MiB" if isinstance(n, (int, float)) else "n/a"


def report(d):
    out = ["## P3.6-4B CI image build and real-runtime validation (GitHub ubuntu-24.04, rootless Podman)", ""]
    commit = os.path.join(d, "commit.txt")
    if os.path.exists(commit):
        out += [open(commit, encoding="utf-8").read().strip(), ""]
    out += ["| Stage | Result |", "|---|---|"]
    codes = {}
    for stage, label in STAGES:
        codes[stage] = rc(d, stage)
        c = codes[stage]
        out.append(f"| {label} | {'PASS' if c == 0 else 'NOT RUN' if c is None else f'FAIL (exit {c})'} |")
    b1, b2 = load(d, "build1.json"), load(d, "build2.json")
    for name, b in (("Build 1", b1), ("Build 2", b2)):
        if b:
            out += ["", f"### {name}", "",
                    f"- tag `{b.get('tag')}`", f"- image ID `{b.get('image_id')}`", f"- digest `{b.get('digest')}`",
                    f"- size {b.get('size')} bytes ({mib(b.get('size'))})",
                    f"- build duration {b.get('build_seconds')} s",
                    f"- dependency lock SHA-256 `{b.get('lock_sha256')}`",
                    f"- build context SHA-256 `{b.get('context_sha256')}` ({b.get('context_files')} files)",
                    f"- RootFS layers: {len(b.get('rootfs_layers') or [])}"]
    v = load(d, "verify1.json")
    if v:
        out += ["", "### Verification of build 1", "",
                "- result: all image (incl. /opt/ip hashes, owners, modes), runtime and no-argument-refusal checks "
                "passed (verify exits non-zero otherwise)",
                f"- distributions found: {len((v.get('report') or {}).get('image', {}).get('distributions', {}))}"]
    c = load(d, "compare.json")
    if c:
        out += ["", "### Reproducibility", "",
                f"- same image ID: {c.get('same_id')}", f"- same RootFS layers: {c.get('same_layers')}",
                f"- build 1 ID `{(c.get('a') or {}).get('id')}`, build 2 ID `{(c.get('b') or {}).get('id')}`"]
        for ld in c.get("layer_diffs") or []:
            if not ld.get("same"):
                diff = ld.get("diff") or {}
                out.append(f"- layer {ld['index']} differs: only-in-1 {diff.get('only_in_a')}, only-in-2 "
                           f"{diff.get('only_in_b')}, changed {diff.get('changed')} by field "
                           f"{diff.get('changed_by_field')}")
                for ex in (diff.get("examples_changed") or [])[:5]:
                    out.append(f"  - `{ex['name']}`: {ex['fields']}")
    s = load(d, "startup.json")
    if s:
        out += ["", "### Start-up (supervisor entrypoint with no arguments: imports, refuses, exits 78; podman client wall clock)", ""]
        for kind in ("warm", "cold"):
            x = s.get(kind)
            if x:
                out.append(f"- {kind}: n={x['n']}, median {x['median_ms']} ms, p95 {x['p95_ms']} ms "
                           f"(min {x['min_ms']}, max {x['max_ms']})")
        out.append(f"- cold definition: {s.get('cold_definition')}")
    g = load(d, "golden.json")
    if g:
        tasks = g.get("tasks") or {}
        answers = g.get("answers") or {}
        checked = sum(a.get("checked", 0) for a in answers.values())
        wrong = sum(len(a.get("mismatches") or {}) + len(a.get("missing") or []) for a in answers.values())
        charts = [t for t in tasks.values() if t.get("chart") is not None]
        chart_ok = [t for t in tasks.values() if t.get("chart_matches_reference")]
        env_bad = [t for t in tasks.values() if t.get("render_env_problems")]
        p35a = [t for t in tasks.values() if t.get("chart_matches_p35a_host_reference")]
        out += ["", "### Golden fixtures inside the image", "",
                f"- numerical: {checked - wrong}/{checked} golden answers match (mismatched or missing: {wrong})",
                f"- charts: {len(charts)} produced; {len(chart_ok)}/{len(tasks)} tasks match chart_hermetic exactly "
                f"(identical decoded pixels, or correctly absent)",
                f"- render environment: {len(tasks) - len(env_bad)}/{len(tasks)} tasks report the committed hermetic "
                f"environment" + (f"; first difference: {env_bad[0]['render_env_problems'][:3]}" if env_bad else ""),
                f"- informational: {len(p35a)}/{len(tasks)} match the preserved P3.5a host reference "
                f"(rendered with the host's MATPLOTLIBRC and system fonts)",
                f"- fixture exits: {sorted({t.get('exit') for t in tasks.values()})}",
                f"- Matplotlib temporary-cache warnings: {sum(1 for t in tasks.values() if t.get('matplotlib_cache_warning'))}"]
        for tid, t in sorted(tasks.items()):
            if t.get("exit") != 0 or not t.get("chart_matches_reference"):
                out.append(f"  - {tid}: exit {t.get('exit')}, chart {t.get('chart')}, stderr tail "
                           f"{(t.get('stderr_tail') or '')[-200:]!r}")
    o = load(d, "ospkgs.json")
    if o:
        out += ["", "### OS package inventory", "", f"- {o.get('count')} dpkg packages (full list in the log)",
                "- vulnerability scan: NOT RUN (no approved scanner and database source)"]
    out += phase_f(d)
    out += ["", "Runner account is root-equivalent (passwordless sudo, docker group): this is not VE-2 non-root "
            "or cross-account validation."]
    ok = all(codes[s] == 0 for s in REQUIRED)
    out += ["", "VERDICT: " + ("every stage ran and passed" if ok else "NOT all stages passed - see the table")]
    return "\n".join(out) + "\n", ok


def phase_f(d):
    out = []
    c = load(d, "compat.json")
    if c:
        out += ["", "### Phase F: Podman argument compatibility (create only, never started)", "",
                f"- full reviewed argv accepted in every mode: {c.get('full_ok')}",
                f"- run-only flags present in `podman run --help`: {c.get('run_only')}",
                f"- options rejected on their own: {c.get('rejected_alone')}",
                f"- removal that makes the full argv pass: {c.get('passes_when_removed')}"]
    rt = load(d, "runtime_runtime.json")
    if rt:
        obs, res, tim = rt.get("observations") or {}, rt.get("results") or {}, rt.get("timings") or {}
        out += ["", "### Phase F: real ContainerRuntime", "",
                f"- preflight: {res.get('preflight')}", f"- preflight wall time: {tim.get('preflight_ms')} ms",
                f"- attestation differences from the required isolation: {obs.get('attestation_differences')}",
                f"- self-tests: oom {obs.get('selftest-oom')}, pids {obs.get('selftest-pids')}"]
        for prelude in ("v1", "lazy_seaborn"):
            g = res.get(f"golden_{prelude}")
            if g:
                out.append(f"- golden ({prelude}): numbers {g['numbers']['correct']}/{g['numbers']['checked']}, "
                           f"hermetic charts {g['charts']['matched']}/{g['charts']['total']}")
        if res.get("fail_closed"):
            out.append(f"- fail-closed outcomes: " + ", ".join(f"{k}={v['outcome']}/{v['chart_rejected_reason']}"
                                                            for k, v in sorted(res["fail_closed"].items())))
        out.append(f"- execution latency (container, host wall): {tim.get('execution_container_ms')}, "
                   f"{tim.get('execution_host_wall_ms')}")
        out.append(f"- leftovers: {res.get('leftovers')}")
    for name, label in (("p5_v1", "V1"), ("p5_lazy", "lazy-seaborn")):
        e = load(d, f"runtime_{name}.json")
        r = ((e or {}).get("results") or {}).get("p5_" + ("v1" if name == "p5_v1" else "lazy-seaborn"))
        if r:
            s = r["summary"]
            out += ["", f"### Phase F: P5 suite through the real container port ({label})", "",
                    f"- expectations met {s['expectations_met']}/{s['cases']}; clean-run golden answers "
                    f"{s['clean_runs_golden_correct']}/{s['clean_runs_golden_expected']}; incorrect claims accepted "
                    f"{s['incorrect_claims_accepted']}; provider calls {s['provider_calls']}",
                    f"- wall time {r['wall_s']} s; preflight {((e.get('timings') or {}).get('preflight_ms'))} ms"]
    return out


def main(argv):
    text, ok = report(argv[1])
    sys.stdout.write(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
