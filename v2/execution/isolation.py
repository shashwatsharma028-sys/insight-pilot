"""
Execution isolation levels (P3.1 contract).

There are exactly two levels. There is deliberately no "hardened process"
level: hardened execution exists only as a rootless container (see
config/feature_flags.validate and docs/v2/P3_SECURE_EXECUTION_DESIGN.md).
"""

from enum import Enum


class IsolationLevel(str, Enum):
    # The unchanged V1 subprocess: same OS user, same environment, no
    # filesystem, network or resource isolation. Never a security boundary.
    V1_UNISOLATED = "v1_unisolated"

    # Rootless OCI container (user namespace, no network, read-only root,
    # no capabilities, cgroup v2 limits). Planned for P3.6; not implemented.
    ROOTLESS_CONTAINER = "rootless_container"

    @property
    def os_isolated(self) -> bool:
        """True only for an OS-enforced isolation boundary."""
        return self is IsolationLevel.ROOTLESS_CONTAINER


# Output of config.feature_flags.resolve_sandbox_backend() -> isolation level.
_BY_RESOLVED_BACKEND = {
    "legacy": IsolationLevel.V1_UNISOLATED,
    "container": IsolationLevel.ROOTLESS_CONTAINER,
}


def isolation_for_backend(resolved_backend: str) -> IsolationLevel:
    """
    Map a value returned by resolve_sandbox_backend() to its isolation level.

    Anything else (for example "subprocess", which is a flag value and never
    a resolved hardened backend) raises ValueError.
    """
    try:
        return _BY_RESOLVED_BACKEND[resolved_backend]
    except (KeyError, TypeError):
        raise ValueError(f"unknown resolved sandbox backend: {resolved_backend!r}") from None
