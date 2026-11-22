"""
InsightPilot sandbox runner (P3.6-2): runs ONE generated program inside the container. Trusted image code.

Started by the PID-1 supervisor as `python3 -I -B /opt/ip/runner.py --prelude <v1|lazy_seaborn>`.

compose() rebuilds exactly the text V1 executes (sandbox.executor.wrap_code_safely with the
container paths /in/dataset.csv and /out/chart.png), and for `lazy_seaborn` exactly the P4.1
variant (sandbox_v2.lazy_prelude.wrap_code_lazy_seaborn):

    v1_text = textwrap.dedent(V1_PRELUDE + code + V1_EPILOGUE)
    lazy    = LAZY_PRELUDE + v1_text[len(V1_PRELUDE):]

The prelude and epilogue are trusted image content, generated at build time from the host
functions into preludes.json (scripts/p3/gen_preludes.py) and pinned by SHA-256; tests prove the
composition equals the host functions byte for byte. The generated code is never modified,
never placed on an argv and never written anywhere but read from /in/code.py.

This module only defines functions; it does nothing when imported. main() runs only as __main__.
"""

import hashlib
import json
import os
import sys
import textwrap

PRELUDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preludes.json")
CODE_FILE = "/in/code.py"
PRELUDE_IDS = ("v1", "lazy_seaborn")
EXIT_USAGE = 78


class RunnerError(RuntimeError):
    pass


def load_preludes(path=PRELUDES_FILE):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    body = json.dumps(data["preludes"], sort_keys=True, ensure_ascii=True).encode()
    if hashlib.sha256(body).hexdigest() != data["sha256"]:
        raise RunnerError("prelude file does not match its pinned SHA-256")
    return data["preludes"]


def compose(code, prelude_id, preludes):
    if prelude_id not in PRELUDE_IDS:
        raise RunnerError("unknown prelude")
    if not isinstance(code, str):
        raise RunnerError("code must be text")
    v1 = preludes["v1"]
    text = textwrap.dedent(v1["prelude"] + code + v1["epilogue"])
    if not text.startswith(v1["prelude"]):            # holds for all code (template lines are at column 0)
        raise RunnerError("composed text does not start with the trusted prelude")
    if prelude_id == "lazy_seaborn":
        text = preludes["lazy_seaborn"]["prelude"] + text[len(v1["prelude"]):]
    return text


def parse_args(argv):
    if len(argv) != 2 or argv[0] != "--prelude" or argv[1] not in PRELUDE_IDS:
        raise RunnerError("usage: runner.py --prelude v1|lazy_seaborn")
    return argv[1]


def main(argv=None):
    try:
        prelude_id = parse_args(sys.argv[1:] if argv is None else argv)
        with open(CODE_FILE, "rb") as f:
            code = f.read().decode("utf-8")             # strict: invalid UTF-8 is a usage error
        text = compose(code, prelude_id, load_preludes())
    except (RunnerError, OSError, UnicodeDecodeError, ValueError, KeyError) as exc:
        sys.stderr.write(f"insightpilot-runner: {type(exc).__name__}: {exc}\n")
        return EXIT_USAGE
    namespace = {"__name__": "__main__", "__builtins__": __builtins__}
    exec(compile(text, "<generated>", "exec"), namespace)   # the ONLY execution point, inside the container
    return 0


if __name__ == "__main__":
    sys.exit(main())
