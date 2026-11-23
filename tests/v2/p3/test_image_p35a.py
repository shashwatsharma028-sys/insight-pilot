"""
P3.5a: reproducible sandbox-image foundation and supply chain (offline checks).

These checks need no network, no container runtime and no image. They prove
properties of the committed INPUTS:
- the base-image digest chain;
- the hash lock and dependency closure;
- the Containerfile;
- the staged build context;
- the inert entrypoint;
- the inspector logic.

P3.6-4A: the live image definition became the P3.6 supervisor image. The
Containerfile and expectations checked here are the FROZEN P3.5a inert
reference (docs/v2/p3/p35a_inert_reference/, byte copies from commit
41536829), so every inert-image property stays tested for comparison. The
staging-tool tests use the live allowlist. The live P3.6 definition is tested
by tests/v2/p36/test_image_integration_p364a.py.

Properties of a BUILT image (real size, setuid scan inside the image,
non-root at runtime, isolation) are PENDING. They need rootless Podman with
cgroup v2 (scripts/p3/verify_image.sh, P3.5b); this environment has neither.
"""

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
IMAGE = os.path.join(ROOT, "v2", "execution", "image")
BASE = os.path.join(IMAGE, "base")
SCRIPTS = os.path.join(ROOT, "scripts", "p3")
P35A_REFERENCE = os.path.join(ROOT, "docs", "v2", "p3", "p35a_inert_reference")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"p35a_{name}", os.path.join(SCRIPTS, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool = _load("image_tool")
inspector = _load("image_inspect")


def _read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def _json(path):
    return json.loads(_read(path))


def _sha(path):
    return hashlib.sha256(_read(path, "rb")).hexdigest()


RECORD = _json(os.path.join(BASE, "base-image.json"))
INVENTORY = _json(os.path.join(IMAGE, "dependency-inventory.json"))
EXPECT = _json(os.path.join(P35A_REFERENCE, "image-expectations.json"))
LOCK_TEXT = _read(os.path.join(IMAGE, "requirements.lock"))
LOCK = tool.parse_lock(LOCK_TEXT)
CONTAINERFILE = _read(os.path.join(P35A_REFERENCE, "Containerfile"))


def _instructions(text):
    """Containerfile instructions as (KEYWORD, argument text), continuations joined, comments dropped."""
    out, buf = [], ""
    for line in text.splitlines():
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        buf += line.rstrip("\\").rstrip() + " " if line.rstrip().endswith("\\") else line
        if not line.rstrip().endswith("\\"):
            kw, _, arg = buf.strip().partition(" ")
            out.append((kw.upper(), arg.strip()))
            buf = ""
    assert buf == "", "dangling continuation"
    return out


INSTR = _instructions(CONTAINERFILE)


def _stages():
    stages, cur = [], None
    for kw, arg in INSTR:
        if kw == "FROM":
            cur = {"from": arg, "instr": []}
            stages.append(cur)
        else:
            assert cur is not None, f"{kw} before FROM"
            cur["instr"].append((kw, arg))
    return stages


# ── 1. pinned base image: the digest chain verifies offline ─────────────────

def test_base_image_digest_chain_verifies_offline():
    digest = RECORD["index_digest"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    assert "sha256:" + _sha(os.path.join(BASE, "index.json")) == digest           # content-addressed
    index = _json(os.path.join(BASE, "index.json"))
    by_arch = {m["platform"]["architecture"]: m["digest"] for m in index["manifests"]
               if m.get("platform", {}).get("os") == "linux"}
    for arch, plat in RECORD["platforms"].items():
        assert by_arch[arch] == plat["manifest"]
        manifest_path = os.path.join(BASE, f"manifest-{arch}.json")
        assert "sha256:" + _sha(manifest_path) == plat["manifest"]
        manifest = _json(manifest_path)
        assert manifest["config"]["digest"] == plat["config"]
        assert len(manifest["layers"]) == plat["layers"]
        assert sum(layer["size"] for layer in manifest["layers"]) == plat["compressed_bytes"]
        config_path = os.path.join(BASE, f"config-{arch}.json")
        assert "sha256:" + _sha(config_path) == plat["config"]
        config = _json(config_path)
        assert config["architecture"] == arch and config["os"] == "linux"
        assert f"PYTHON_VERSION={RECORD['python_version']}" in config["config"]["Env"]
        assert config["created"] == plat["created"]
    assert RECORD["python_version"] == "3.11.18"


def test_every_stage_uses_the_verified_base_digest():
    stages = _stages()
    assert [s["from"].split(" AS ")[-1] for s in stages] == ["build", "runtime"]
    for s in stages:
        ref = s["from"].split(" AS ")[0]
        assert ref == f"{RECORD['reference']}@{RECORD['index_digest']}"
    assert not re.search(r"(?im)^\s*ARG\b", CONTAINERFILE), "no build arguments (they could override the base)"
    assert ":latest" not in CONTAINERFILE


def test_base_image_was_old_enough_when_pinned():
    # Supply-chain hygiene: the pinned build had existed for over a week when verified.
    for plat in RECORD["platforms"].values():
        assert plat["created"][:10] <= "2026-11-14" < RECORD["verified_on"]


# ── 2. exact versions and verified hashes ───────────────────────────────────

def test_lock_is_exact_hash_pinned_and_parses_strictly():
    assert set(LOCK) == set(tool.PINS) and len(LOCK) == 18
    for name, req in LOCK.items():
        assert req["version"] == tool.PINS[name]
        expected = {w["sha256"] for w in INVENTORY["packages"][name]["wheels"].values()}
        assert set(req["hashes"]) == expected and len(req["hashes"]) == len(expected)
    for bad in ("numpy>=2.4\n", "-i https://example.invalid/simple\n", "numpy==2.4.6 --hash=md5:00\n",
                "git+https://example.invalid/x.git\n", "numpy==2.4.6\nnumpy==2.4.6\n", "--extra-index-url x\n"):
        with pytest.raises(ValueError):
            tool.parse_lock(bad)


def test_lock_and_inventory_render_reproducibly_from_the_recorded_metadata():
    assert tool.render_lock(INVENTORY) == LOCK_TEXT
    assert tool.render_inventory(INVENTORY) == _read(os.path.join(IMAGE, "dependency-inventory.json"))


def test_inventory_records_exactly_one_wheel_per_architecture():
    assert INVENTORY["architectures"] == ["x86_64", "aarch64"]
    for name, e in INVENTORY["packages"].items():
        for arch in ("x86_64", "aarch64"):
            w = e["wheels"][arch]
            assert re.fullmatch(r"[0-9a-f]{64}", w["sha256"]) and w["size"] > 0
            assert w["filename"].endswith(".whl") and "/" not in w["filename"]
            assert w["url"].startswith("https://files.pythonhosted.org/") and w["url"].endswith("/" + w["filename"])
            tags = w["filename"][:-4].split("-")[-3:]
            assert tags[0] in ("cp311", "py3", "py2.py3")
            if tags[2] != "any":
                assert arch in tags[2]
                glibc = [int(g) for g in re.findall(r"manylinux_2_(\d+)", tags[2])]
                glibc += [17] if "manylinux2014" in tags[2] else []
                assert glibc and min(glibc) <= 36, w["filename"]              # installable on bookworm


def test_dependency_closure_of_the_prelude_packages_is_complete_and_consistent():
    packaging = pytest.importorskip("packaging")                    # development dependency
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    for arch in ("x86_64", "aarch64"):
        env = dict(default_environment(), python_version="3.11", python_full_version="3.11.18",
                   implementation_name="cpython", platform_python_implementation="CPython",
                   sys_platform="linux", platform_system="Linux", os_name="posix", platform_machine=arch,
                   extra="")
        seen, todo = set(), list(INVENTORY["roots"])
        while todo:
            name = tool._norm(todo.pop())
            if name in seen:
                continue
            seen.add(name)
            assert name in INVENTORY["packages"], f"{name} is required but not locked"
            for spec in INVENTORY["packages"][name]["requires_dist"]:
                req = Requirement(spec)
                if req.marker is not None and not req.marker.evaluate(env):
                    continue
                dep = tool._norm(req.name)
                assert dep in INVENTORY["packages"], f"{name} requires {spec}, which is not locked"
                assert req.specifier.contains(INVENTORY["packages"][dep]["version"], prereleases=True), spec
                todo.append(dep)
        assert seen == set(INVENTORY["packages"]), f"{arch}: lock has unused packages {set(INVENTORY['packages']) - seen}"
    assert packaging


def test_every_locked_package_supports_python_3_11_18():
    from packaging.specifiers import SpecifierSet
    for name, e in INVENTORY["packages"].items():
        assert SpecifierSet(e["requires_python"]).contains("3.11.18"), name


def test_locked_versions_equal_env_b_except_the_documented_replacement():
    lines = _read(os.path.join(ROOT, "docs", "v2", "ENV_B_PINS.txt")).splitlines()
    env_b = {tool._norm(n): v for n, v in (ln.strip().split("==") for ln in lines
                                           if ln.strip() and not ln.startswith("#"))}
    diffs = {n: (env_b.get(n), v) for n, v in tool.PINS.items() if env_b.get(n) != v}
    assert diffs == {"pillow": ("12.3.0", "13.0.0")}
    assert {k: tuple(v) for k, v in INVENTORY["env_b_differences"].items()} == diffs


def test_no_locked_version_had_a_published_advisory_when_verified():
    assert {n: e["advisories"] for n, e in INVENTORY["packages"].items() if e["advisories"]} == {}
    assert not any(e["yanked"] for e in INVENTORY["packages"].values())


def test_lock_holds_runtime_packages_only():
    for dev in ("pytest", "pytest-mock", "iniconfig", "pluggy", "uv", "pip", "setuptools", "wheel", "streamlit",
                "langgraph", "langchain", "plotly", "reportlab", "python-dotenv", "requests", "watchdog"):
        assert dev not in LOCK, dev
    assert set(INVENTORY["roots"]) == {"pandas", "numpy", "matplotlib", "seaborn", "scipy", "scikit-learn"}


def test_roots_cover_the_v1_prelude_and_the_sandbox_allowlist():
    prelude = _read(os.path.join(ROOT, "sandbox", "executor.py"))
    for module in ("pandas", "numpy", "matplotlib", "seaborn"):
        assert f"import {module}" in prelude
    rules = _read(os.path.join(ROOT, "rules", "sandbox_safety.rules.md"))
    allowed = rules.split("## Allowed imports (whitelist)")[1].split("##")[0]
    third_party = {"pandas", "numpy", "matplotlib", "seaborn", "scipy", "sklearn"}
    assert third_party <= {w.strip(", \n") for w in allowed.split()}
    assert {"scikit-learn" if m == "sklearn" else m for m in third_party} == set(INVENTORY["roots"])


# ── Containerfile ───────────────────────────────────────────────────────────

ALLOWED_FINAL = {"RUN", "COPY", "LABEL", "ENV", "WORKDIR", "USER", "ENTRYPOINT", "CMD"}


def test_containerfile_uses_only_reviewed_instructions():
    used = {kw for kw, _ in INSTR}
    assert used <= {"FROM"} | ALLOWED_FINAL, used
    for kw in ("ADD", "ARG", "EXPOSE", "VOLUME", "HEALTHCHECK", "ONBUILD", "SHELL", "STOPSIGNAL"):
        assert kw not in used
    for forbidden in ("--mount", "type=secret", "curl", "wget", "apt-get", "--network", "sudo", "chmod 777",
                      "chmod u+s", "setcap", "http://", "ssh"):
        assert forbidden not in CONTAINERFILE.split("# Base:")[1], forbidden


def test_build_stage_installs_only_hash_verified_wheels_offline():
    build = _stages()[0]["instr"]
    copies = [arg for kw, arg in build if kw == "COPY"]
    assert copies == ["requirements.lock /build/requirements.lock", "wheels/ /build/wheels/"]
    run = " ".join(arg for kw, arg in build if kw == "RUN")
    for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-index", "--find-links /build/wheels",
                 "--target /stage/site-packages", "-r /build/requirements.lock", "--invalidation-mode checked-hash"):
        assert flag in run, flag
    assert "rm -rf /stage/site-packages/bin /stage/site-packages/share" in run


def test_final_stage_removes_every_build_tool_the_base_ships():
    final = _stages()[1]["instr"]
    run = " ".join(arg for kw, arg in final if kw == "RUN")
    assert "pip uninstall --yes --no-input pip setuptools wheel packaging" in run
    removed = set(re.findall(r"(/usr/local/\S+)", run))
    base = RECORD["base_contents_amd64"]
    for tool_name in base["usr_local_bin"]:
        if not tool_name.startswith("python3") and tool_name != "python":
            assert f"/usr/local/bin/{tool_name}" in removed, tool_name
    for path in inspector.FORBIDDEN_PATHS:
        in_base = path.split("/usr/local/lib/python3.11/site-packages/")[-1] in base["site_packages"] or \
            path.rsplit("/", 1)[-1] in base["usr_local_bin"] or path.endswith("ensurepip")
        if in_base:
            by_uninstall = path.rsplit("/", 1)[-1] in ("pip", "setuptools", "wheel")
            assert path in removed or by_uninstall, path
    assert "/usr/local/lib/python3.11/site-packages/distutils-precedence.pth" in removed
    # the base's packaging 26.3 is uninstalled so the locked 26.2 is the only copy
    assert "packaging-26.3.dist-info" in base["site_packages"] and LOCK["packaging"]["version"] == "26.2"


def test_final_stage_strips_setid_bits_and_creates_the_unprivileged_user():
    final = _stages()[1]["instr"]
    run = " ".join(arg for kw, arg in final if kw == "RUN")
    assert "find / -xdev -type f -perm /6000 -exec chmod a-s {} +" in run
    assert "groupadd --gid 10001 ipsandbox" in run
    assert "useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin" in run
    assert run.index("useradd") < run.index("chmod a-s")
    assert len(RECORD["base_contents_amd64"]["setid_files"]) == 11      # what that step removes


def test_final_stage_copies_only_allowlisted_files():
    final = _stages()[1]["instr"]
    copies = [arg for kw, arg in final if kw == "COPY"]
    assert copies == ["--from=build /stage/site-packages/ /usr/local/lib/python3.11/site-packages/",
                      "--chmod=0444 inert_entrypoint.py /opt/ip/inert_entrypoint.py"]


def test_default_user_is_10001_and_nothing_runs_as_root_after_it():
    final = _stages()[1]["instr"]
    kws = [kw for kw, _ in final]
    users = [arg for kw, arg in final if kw == "USER"]
    assert users == [EXPECT["user"]] == ["10001:10001"]
    assert "RUN" not in kws[kws.index("USER"):]


def test_entrypoint_is_the_inert_program_in_exec_form():
    final = _stages()[1]["instr"]
    entry = [json.loads(arg) for kw, arg in final if kw == "ENTRYPOINT"]
    cmd = [json.loads(arg) for kw, arg in final if kw == "CMD"]
    assert entry == [EXPECT["entrypoint"]] and cmd == [EXPECT["cmd"]] == [[]]


def test_labels_pin_the_lock_and_the_base():
    labels = dict(re.findall(r'([\w.]+)="([^"]*)"', " ".join(arg for kw, arg in INSTR if kw == "LABEL")))
    assert sorted(labels) == EXPECT["labels"]
    assert labels["io.insightpilot.lock.sha256"] == _sha(os.path.join(IMAGE, "requirements.lock"))
    assert labels["org.opencontainers.image.base.digest"] == RECORD["index_digest"]
    assert labels["io.insightpilot.stage"] == "P3.5a-inert"


def test_environment_has_no_credentials_and_matches_the_expected_names():
    final = _stages()[1]["instr"]
    ours = set(re.findall(r"(\w+)=", " ".join(arg for kw, arg in final if kw == "ENV")))
    base = {e.split("=", 1)[0] for e in _json(os.path.join(BASE, "config-amd64.json"))["config"]["Env"]}
    assert sorted(ours | base) == EXPECT["env_names_allowed"]
    assert {n for n in ours | base if inspector.SECRET_ENV.search(n)} == {"GPG_KEY"}   # public signing key id
    for build_env in re.findall(r"(\w+)=", " ".join(arg for kw, arg in _stages()[0]["instr"] if kw == "ENV")):
        assert not inspector.SECRET_ENV.search(build_env), build_env


def test_image_definition_declares_no_ports_volumes_or_host_mounts():
    assert not re.search(r"(?im)^\s*(EXPOSE|VOLUME)\b", CONTAINERFILE)
    assert "--mount" not in CONTAINERFILE and "bind" not in CONTAINERFILE.lower()
    assert EXPECT["exposed_ports"] == [] and EXPECT["volumes"] == []


def test_runtime_controls_are_documented_as_launcher_requirements_not_image_properties():
    head = CONTAINERFILE.split("FROM ")[0]
    assert "does NOT implement network isolation, seccomp, cgroup limits" in " ".join(head.split())
    reqs = " ".join(EXPECT["runtime_requirements_not_enforced_by_the_image"])
    for item in ("cgroup v2", "--read-only", "--network=none", "--cap-drop=ALL", "no-new-privileges", "seccomp"):
        assert item in reqs, item


# ── 3/4. exact build-context allowlist; no secrets, history or dev artifacts ─

def _fake_image_dir(tmp_path, wheels):
    img = tmp_path / "repo" / "v2" / "execution" / "image"
    img.mkdir(parents=True)
    for name in tool.CONTEXT_FILES:
        shutil.copyfile(os.path.join(IMAGE, name), img / name)
    repo = tmp_path / "repo"
    for decoy in (".env", "secrets.txt"):
        (repo / decoy).write_text("GOOGLE_API_KEY=not-a-real-key\n")
        (img / decoy).write_text("GOOGLE_API_KEY=not-a-real-key\n")
    for d in (".git", "venv", "__pycache__"):
        (repo / d).mkdir()
        (img / d).mkdir()
        (img / d / "x").write_text("x")
    cache = tmp_path / "cache"
    cache.mkdir()
    packages = {}
    for i, (name, data) in enumerate(wheels.items()):
        fn = f"{name}-1.0-py3-none-any.whl"
        (cache / fn).write_bytes(data)
        w = {"filename": fn, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
             "url": f"https://files.pythonhosted.org/x/{fn}"}
        packages[name] = {"version": "1.0", "wheels": {"x86_64": w, "aarch64": w}}
    inv = {"packages": packages}
    (img / "dependency-inventory.json").write_text(json.dumps(inv))
    return str(img), str(cache), inv


def test_staged_context_contains_exactly_the_allowlist(tmp_path):
    img, cache, inv = _fake_image_dir(tmp_path, {"alpha": b"A" * 100, "beta": b"B" * 50})
    files, digest = tool.stage("x86_64", cache, str(tmp_path / "ctx"), image_dir=img)
    # P3.6-4A: the inert entrypoint left the context; the trusted runtime is staged under opt-ip/
    assert files == sorted(["Containerfile", "requirements.lock",
                            "wheels/alpha-1.0-py3-none-any.whl", "wheels/beta-1.0-py3-none-any.whl"]
                           + [f"opt-ip/{d}" for d, _ in tool.image_layout.RUNTIME_FILES])
    for d, dirs, fs in os.walk(tmp_path / "ctx"):
        assert not {".git", "venv", "__pycache__"} & set(dirs)
        assert not {".env", "secrets.txt", "dependency-inventory.json"} & set(fs)
    files2, digest2 = tool.stage("x86_64", cache, str(tmp_path / "ctx2"), image_dir=img)
    assert (files2, digest2) == (files, digest)                         # deterministic context


def test_context_digest_changes_with_any_byte(tmp_path):
    img, cache, _ = _fake_image_dir(tmp_path, {"alpha": b"A" * 100})
    files, digest = tool.stage("x86_64", cache, str(tmp_path / "ctx"), image_dir=img)
    p = tmp_path / "ctx" / "requirements.lock"
    p.write_bytes(p.read_bytes() + b"\n")
    assert tool.context_digest(str(tmp_path / "ctx"), files) != digest


@pytest.mark.parametrize("problem", ["tampered", "missing", "symlink", "oversize", "slash_name", "nonempty_out",
                                     "unknown_arch"])
def test_staging_refuses_unverified_inputs(tmp_path, problem):
    img, cache, inv = _fake_image_dir(tmp_path, {"alpha": b"A" * 100})
    out = tmp_path / "ctx"
    w = inv["packages"]["alpha"]["wheels"]["x86_64"]
    arch = "x86_64"
    if problem == "tampered":
        (tmp_path / "cache" / w["filename"]).write_bytes(b"B" * 100)
    elif problem == "missing":
        os.unlink(tmp_path / "cache" / w["filename"])
    elif problem == "symlink":
        real = tmp_path / "elsewhere.whl"
        os.replace(tmp_path / "cache" / w["filename"], real)
        os.symlink(real, tmp_path / "cache" / w["filename"])
    elif problem == "oversize":
        inv["packages"]["alpha"]["wheels"]["x86_64"] = dict(w, size=w["size"] + 1)
    elif problem == "slash_name":
        inv["packages"]["alpha"]["wheels"]["x86_64"] = dict(w, filename="../" + w["filename"])
    elif problem == "nonempty_out":
        out.mkdir()
        (out / "stray").write_text("x")
    else:
        arch = "riscv64"
    with pytest.raises(SystemExit):
        tool.stage(arch, cache, str(out), inv=inv, image_dir=img)


SECRET_PATTERNS = [r"AIza[0-9A-Za-z_\-]{20,}", r"GOOGLE_API_KEY", r"BEGIN [A-Z ]*PRIVATE KEY", r"\.env\b",
                   r"ghp_[0-9A-Za-z]{20,}", r"sk-[0-9A-Za-z]{20,}", r"password\s*=", r"/Users/", r"/home/"]


@pytest.mark.parametrize("name", list(tool.CONTEXT_FILES) + ["requirements.lock", "dependency-inventory.json",
                                                             "image-expectations.json", "base/base-image.json"])
def test_image_inputs_contain_no_credentials_or_host_paths(name):
    text = _read(os.path.join(IMAGE, name))
    body = text.split("# Base:")[1] if name == "Containerfile" else text
    for pat in SECRET_PATTERNS:
        assert not re.search(pat, body, re.I), (name, pat)


def test_image_directory_holds_only_reviewed_files():
    found = sorted(os.path.relpath(os.path.join(d, f), IMAGE).replace(os.sep, "/")
                   for d, _, fs in os.walk(IMAGE) for f in fs)
    assert found == sorted(["Containerfile", "requirements.lock", "inert_entrypoint.py", "dependency-inventory.json",
                            "image-expectations.json", "base/base-image.json", "base/index.json",
                            "base/manifest-amd64.json", "base/manifest-arm64.json", "base/config-amd64.json",
                            "base/config-arm64.json",
                            # P3.6-2 (approved): trusted runtime SOURCE only; not in CONTEXT_FILES, so the
                            # image built from this directory is unchanged and still inert until P3.6-4.
                            "runtime/supervisor.py", "runtime/runner.py", "runtime/selftest.py",
                            "runtime/preludes.json"])
    # P3.6-4A: the runtime files now enter the context through image_layout.RUNTIME_FILES (exact list);
    # inert_entrypoint.py stays as source of the frozen P3.5a reference but is no longer staged.
    assert "inert_entrypoint.py" not in tool.CONTEXT_FILES


# ── 7. no unexpected executable entrypoints ─────────────────────────────────

def test_inert_entrypoint_is_statically_inert():
    tree = ast.parse(_read(os.path.join(IMAGE, "inert_entrypoint.py")))
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports == {"sys"} and not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"exec", "eval", "compile", "open", "__import__", "input", "getattr", "breakpoint"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert attrs <= {"stderr", "write", "exit"}, attrs                  # no argv, stdin or environ


def test_inert_entrypoint_ignores_arguments_and_input():
    out = subprocess.run([sys.executable, "-I", "-B", os.path.join(IMAGE, "inert_entrypoint.py"),
                          "-c", "print('owned')", "/in/code.py"],
                         input=b"import os; os.system('id')\n", capture_output=True, timeout=30,
                         env={"PATH": os.environ.get("PATH", "")})
    assert out.returncode == EXPECT["inert_exit_status"] == 78
    assert out.stdout == b"" and out.stderr.decode() == EXPECT["inert_message"] + "\n"


def test_only_one_program_is_installed_under_opt_ip():
    assert EXPECT["opt_ip_files"] == ["inert_entrypoint.py"]


# ── 5/6. inspector logic (used on a built image in P3.5b) ───────────────────

def _fake_root(tmp_path):
    root = tmp_path / "root"
    site = root / inspector.SITE
    for name, e in INVENTORY["packages"].items():
        d = site / f"{name.replace('-', '_')}-{e['version']}.dist-info"
        d.mkdir(parents=True)
        (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {e['version']}\n\nbody\n")
    (root / "opt" / "ip").mkdir(parents=True)
    (root / "opt" / "ip" / "inert_entrypoint.py").write_text("x")
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "python3").write_text("x")
    return root


def _evaluate(root):
    img = inspector.inspect_image(str(root))
    lock_versions = {n: e["version"] for n, e in INVENTORY["packages"].items()}
    return inspector.evaluate_image(img, EXPECT, lock_versions)


def test_inspector_accepts_a_root_that_matches_the_expectations(tmp_path):
    assert _evaluate(_fake_root(tmp_path)) == []


@pytest.mark.parametrize("defect,expected", [
    ("setuid", "setuid/setgid"), ("dotenv", "secret-like files"), ("git", "repository/development"),
    ("venv", "virtual environments"), ("pip", "forbidden tools"), ("gcc", "forbidden tools"),
    ("extra_dist", "distributions differ"), ("wrong_version", "distributions differ"),
    ("missing_dist", "distributions differ"), ("extra_opt", "/opt/ip files"),
    ("world_writable", "world-writable"), ("ssh_key", "secret-like files"),
])
def test_inspector_detects_each_defect(tmp_path, defect, expected):
    root = _fake_root(tmp_path)
    site = root / inspector.SITE
    if defect == "setuid":
        p = root / "usr" / "bin" / "su"
        p.write_text("x")
        os.chmod(p, 0o4755)
    elif defect == "dotenv":
        (root / "opt" / ".env").write_text("K=V")
    elif defect == "git":
        (root / "opt" / "repo" / ".git").mkdir(parents=True)
    elif defect == "venv":
        (root / "opt" / "venv").mkdir()
        (root / "opt" / "venv" / "pyvenv.cfg").write_text("home = /usr")
    elif defect == "pip":
        (site / "pip").mkdir()
    elif defect == "gcc":
        (root / "usr" / "bin" / "gcc").write_text("x")
    elif defect == "extra_dist":
        (site / "requests-2.0.dist-info").mkdir()
        (site / "requests-2.0.dist-info" / "METADATA").write_text("Name: requests\nVersion: 2.0\n\n")
    elif defect == "wrong_version":
        meta = next(site.glob("numpy-*.dist-info")) / "METADATA"
        meta.write_text("Name: numpy\nVersion: 1.0\n\n")
    elif defect == "missing_dist":
        shutil.rmtree(next(site.glob("seaborn-*.dist-info")))
    elif defect == "extra_opt":
        (root / "opt" / "ip" / "runner.py").write_text("x")
    elif defect == "world_writable":
        p = root / "opt" / "ip" / "inert_entrypoint.py"
        os.chmod(p, 0o666)
    elif defect == "ssh_key":
        (root / "opt" / "id_ed25519").write_text("x")
    problems = _evaluate(root)
    assert any(expected in p for p in problems) and all(p.startswith("IMAGE: ") for p in problems), problems


def test_inspector_never_imports_or_executes_what_it_finds():
    tree = ast.parse(_read(os.path.join(SCRIPTS, "image_inspect.py")))
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports <= {"json", "os", "re", "stat", "sys"}
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"exec", "eval", "compile", "__import__"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"system", "popen", "run", "Popen", "import_module", "exec_module", "spawn", "fork"}


def test_inspector_skips_virtual_filesystems(tmp_path):
    root = _fake_root(tmp_path)
    (root / "proc" / "1").mkdir(parents=True)
    (root / "proc" / "1" / ".env").write_text("x")
    assert _evaluate(root) == []


# ── 8. real isolation tests need rootless Linux with cgroup v2 ──────────────

@pytest.mark.parametrize("script,sub", [("build_image.sh", "build"), ("verify_image.sh", "verify")])
def test_scripts_refuse_root_and_delegate_to_the_tested_driver(script, sub):
    text = _read(os.path.join(SCRIPTS, script))
    assert '[ "$(id -u)" != "0" ] || die' in text
    assert f'rootless.py" {sub} "$@"' in text
    assert "docker " not in text and "sudo" not in text and "podman " not in text   # all podman use is in rootless.py


@pytest.mark.skipif(os.geteuid() != 0, reason="checks the refusal path, which needs uid 0")
@pytest.mark.parametrize("script,args", [("build_image.sh", ["--wheel-cache", "/nonexistent"]),
                                         ("verify_image.sh", ["localhost/none"])])
def test_scripts_refuse_to_run_as_root_here(tmp_path, script, args):
    before = set(os.listdir(tmp_path))
    out = subprocess.run(["bash", os.path.join(SCRIPTS, script), *args], capture_output=True, text=True,
                         timeout=60, cwd=tmp_path)
    assert out.returncode == 2 and "REFUSED" in out.stderr and "rootless" in out.stderr
    assert set(os.listdir(tmp_path)) == before


def test_host_check_reports_this_environment_as_unsuitable_when_it_is():
    out = subprocess.run([sys.executable, "-I", "-B", os.path.join(SCRIPTS, "rootless.py"), "check-host"],
                         capture_output=True, text=True, timeout=120)
    rootless_cgroup2 = os.geteuid() != 0 and shutil.which("podman") and \
        os.path.exists("/sys/fs/cgroup/cgroup.controllers")
    if not rootless_cgroup2:
        assert out.returncode != 0 and "suitable for P3.5b build and isolation tests: NO" in out.stdout


APPROVED_WORKFLOW = "workflows/p3-ubuntu-rootless-probe.yml"     # probe branch only (reviewer approval)


def test_no_ci_workflow_except_the_approved_probe():
    gh = os.path.join(ROOT, ".github")
    if not os.path.exists(gh):
        return                                                    # the P3 development branch: no CI at all
    found = sorted(os.path.relpath(os.path.join(d, f), gh).replace(os.sep, "/")
                   for d, _, fs in os.walk(gh) for f in fs)
    assert found == [APPROVED_WORKFLOW]
    import yaml                                                   # required: this check must never be skipped
    text = _read(os.path.join(gh, APPROVED_WORKFLOW))
    wf = yaml.safe_load(text)
    on = wf.get(True, wf.get("on"))
    assert on == {"push": {"branches": ["feature/v2-github-ubuntu-probe"], "paths": [".github/" + APPROVED_WORKFLOW]}}
    assert wf["permissions"] == {"contents": "read"}
    assert list(wf["jobs"]) == ["probe"]
    job = wf["jobs"]["probe"]
    assert job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] <= 60
    assert all("uses" not in step for step in job["steps"])            # no third-party actions
    assert all(0 < step.get("timeout-minutes", 0) <= 20 for step in job["steps"])
    assert {step.get("if") for step in job["steps"]} <= {None, "always()"}
    assert "secrets." not in text and "pull_request" not in text and "schedule" not in text
    assert "workflow_dispatch" not in text
    envs = [k for step in job["steps"] for k in (step.get("env") or {})]
    assert envs == ["TOKEN"]                                          # the read-only token, one step only
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    # containers only via rootless.py; `podman run --help` (help text, no container) is the one exception
    assert not re.search(r"(^|[;&|(]\s*)(sudo\s+)?(docker|podman)\s+(run|build|pull|create|exec)\b(?!\s+--help\b)",
                         runs, re.M)
    code = "\n".join(ln for ln in runs.splitlines() if not ln.strip().startswith("#"))
    direct = re.findall(r"(?:^|[;&|(]|&&)\s*podman\s+(\S+(?:\s+\S+)?)", code, re.M)
    assert {d.split()[0] for d in direct} <= {"--version", "run", "tag", "rmi"}, direct
    assert all(d == "run --help" for d in direct if d.split()[0] == "run"), direct
    executed = re.findall(r"(?:^|[;&|]|\$\()\s*sudo\s+([^;&|)\n]*)", code, re.M)  # sudo as a command, not in text
    assert executed and all(c.startswith(("loginctl enable-linger", "-n true")) for c in executed), executed
    assert sum(c.startswith("loginctl enable-linger") for c in executed) == 1   # the one marked admin step
