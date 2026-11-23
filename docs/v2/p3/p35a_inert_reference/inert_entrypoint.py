"""
InsightPilot sandbox image, P3.5a: INERT entrypoint.

No supervisor or runner is installed in this stage. This program reads no
input, arguments, files or environment. It never executes code, prints one
fixed line to stderr, and exits with status 78 (EX_CONFIG). An image built
in P3.5a therefore cannot run generated code through its entrypoint.
"""

import sys

MESSAGE = "insightpilot-sandbox: P3.5a image foundation; no runner is installed; nothing was executed\n"
EXIT_STATUS = 78


def main() -> int:
    sys.stderr.write(MESSAGE)
    return EXIT_STATUS


if __name__ == "__main__":
    sys.exit(main())
