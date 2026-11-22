"""
Trusted self-test children for ContainerRuntime.preflight() (P3.6-2). Fixed programs; no input code.

    selftest.py oom  <memory_max_bytes>   allocate until the memory cgroup kills the process
    selftest.py pids <pids_max>           start sleepers until the pids limit refuses one

The host expects `oom_killed` for the first and a non-zero exit for the second. Both are run
only by the supervisor in a self-test mode; never with generated code. Nothing runs on import.
"""

import os
import sys
import time


def oom(limit):
    blocks = []
    step = 16 * 1024 * 1024
    for _ in range(4 * limit // step + 8):
        blocks.append(bytearray(step))      # touched pages: charged to the memory cgroup
    return 3                                # reaching here means the limit was NOT enforced


def pids(limit):
    for _ in range(limit + 8):
        try:
            if os.fork() == 0:
                os.close(1)                 # release the supervisor's pipes at once
                os.close(2)
                time.sleep(60)
                os._exit(0)
        except OSError:
            return 4                        # EAGAIN: the pids limit was enforced
    return 0                                # no refusal: the limit was NOT enforced


def main(argv):
    if len(argv) != 2 or argv[0] not in ("oom", "pids") or not argv[1].isdigit():
        return 78
    return oom(int(argv[1])) if argv[0] == "oom" else pids(int(argv[1]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
