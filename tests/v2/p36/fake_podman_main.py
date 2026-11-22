"""
Fake `podman` executable for the P3.6-3 ContainerRuntime tests. It NEVER starts a container and
NEVER executes generated code.

A per-test launcher script (see tests/v2/p36/conftest.py) calls main(argv, state_dir). The state
directory holds scenario.json (what to answer), calls.jsonl (every invocation, recorded) and
containers/ (one marker file per "running" container, so `container exists` and `rm` behave).

`podman run`: the argv is checked for the reviewed security flags, the mounted workspace is read
(code.py and, for executions, dataset.csv, whose SHA-256 is recorded), and the REAL in-image
supervisor (v2/execution/image/runtime/supervisor.py) is driven with a FakeOS whose child only
interprets literal print/fail statements. The frame it produces is written to stdout.
"""

import base64
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
REQUIRED_FLAGS = ("--rm", "--read-only", "--read-only-tmpfs=false", "--network=none", "--dns=none", "--no-hosts",
                  "--cap-drop=ALL", "--security-opt=no-new-privileges", "--ipc=none", "--cgroupns=private",
                  "--pull=never", "--passwd=false", "--env-host=false", "--http-proxy=false")


def _record(state, argv, **extra):
    with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, **extra}) + "\n")


def _scenario(state):
    with open(os.path.join(state, "scenario.json"), encoding="utf-8") as f:
        return json.load(f)


def _marker(state, name):
    return os.path.join(state, "containers", name)


def main(argv, state):
    sc = _scenario(state)
    cmd = argv[:2]
    if argv[:1] == ["version"]:
        _record(state, argv)
        sys.stdout.write(json.dumps({"Client": {"Version": sc.get("version", "4.9.3")}}))
        return 0
    if argv[:1] == ["info"]:
        _record(state, argv)
        sys.stdout.write(json.dumps(sc["info"]))
        return 0
    if cmd == ["image", "inspect"]:
        _record(state, argv)
        if sc.get("image") is None:
            sys.stderr.write("Error: no such image\n")
            return 125
        sys.stdout.write(json.dumps([sc["image"]]))
        return 0
    if argv[:1] == ["kill"]:
        _record(state, argv)
        return 0
    if argv[:1] == ["rm"]:
        _record(state, argv)
        name = argv[-1]
        if os.path.exists(_marker(state, name)) and not sc.get("persist"):
            os.unlink(_marker(state, name))
            return 0
        return 0 if sc.get("persist") else 1
    if cmd == ["container", "exists"]:
        _record(state, argv)
        return 0 if os.path.exists(_marker(state, argv[-1])) else 1
    if argv[:1] == ["run"]:
        return _run(argv, state, sc)
    _record(state, argv, unknown=True)
    return 125


def _run(argv, state, sc):
    missing = [f for f in REQUIRED_FLAGS if f not in argv]
    name = argv[argv.index("--name") + 1]
    mount = next(a for a in argv if a.startswith("type=bind,source="))
    source = mount.split(",")[1].split("=", 1)[1]
    image_at = next(i for i, a in enumerate(argv) if a.startswith("sha256:"))
    sup_args = argv[image_at + 1:]
    mode = sup_args[sup_args.index("--mode") + 1]
    code = ""
    if os.path.exists(os.path.join(source, "code.py")):
        with open(os.path.join(source, "code.py"), encoding="utf-8") as f:
            code = f.read()
    dataset = os.path.join(source, "dataset.csv")
    ds_sha = None
    if os.path.exists(dataset):
        with open(dataset, "rb") as f:
            ds_sha = hashlib.sha256(f.read()).hexdigest()
    _record(state, ["run"] + argv[1:], mode=mode, missing_flags=missing, code=code, dataset_sha256=ds_sha,
            inputs=sorted(os.listdir(source)))
    open(_marker(state, name), "w").close()
    behaviour = dict(sc.get("modes", {}).get(mode, {}))
    if "run_id_override" in behaviour:                       # a frame that belongs to another run
        sup_args = list(sup_args)
        sup_args[sup_args.index("--run-id") + 1] = behaviour["run_id_override"]
    if behaviour.get("sleep"):
        time.sleep(behaviour["sleep"])
    if "raw_stdout_b64" in behaviour:
        out = base64.b64decode(behaviour["raw_stdout_b64"])
        rc = behaviour.get("exit", 0)
    else:
        sys.path.insert(0, os.path.join(ROOT, "v2", "execution", "image", "runtime"))
        import supervisor
        from tests.v2.p36.fake_os import FakeOS
        kw = {k: v for k, v in behaviour.items() if k in ("hang", "oom", "exit_code", "net", "dev_writable",
                                                          "memory_max", "pids_max", "survivors", "out_extra")}
        if "stdout" in behaviour:
            kw["stdout"] = behaviour["stdout"].encode()
        if "chart_b64" in behaviour and mode == "run":
            kw["chart"] = base64.b64decode(behaviour["chart_b64"])
        os_api = FakeOS(code=code if mode == "run" else "", **kw)
        rc = supervisor.supervise(sup_args, os_api)
        out = os_api.frames[0] if os_api.frames else b""
        out += base64.b64decode(behaviour.get("append_b64", ""))
        rc = behaviour.get("exit", rc)
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    if not sc.get("persist") and os.path.exists(_marker(state, name)):
        os.unlink(_marker(state, name))          # --rm
    return rc
