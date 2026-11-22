"""
Resource-limit definitions for hardened execution (P3.1 contract).

All values are PROVISIONAL (design decision D9). They are not enforced by
anything yet: enforcement is planned for the container backend (P3.6/P3.7)
and must be proven on a real rootless Linux runtime before any claim is made.

Byte budgets are separate per channel. The result frame planned for P3.4
carries the already-capped sections, so its maximum size is derived here
(max_frame_bytes) rather than configured independently.
"""

from dataclasses import dataclass, fields

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


@dataclass(frozen=True)
class Limits:
    # Time. code_wall_seconds must equal V1's sandbox.executor.MAX_EXECUTION_TIME
    # (checked by tests, not imported here to keep this module free of V1 imports).
    code_wall_seconds: int = 30
    host_grace_seconds: int = 15

    # Container cgroup / rlimits.
    memory_bytes: int = 1 * GIB          # memory.max; swap disabled (memory.swap.max = 0)
    cpus: float = 1.0
    pids: int = 64
    open_files: int = 256

    # Writable storage: in-container tmpfs only, charged to the memory cgroup.
    tmp_bytes: int = 64 * MIB
    tmp_inodes: int = 4096
    out_bytes: int = 32 * MIB
    out_inodes: int = 64

    # Host-written inputs.
    code_bytes: int = 1 * MIB
    dataset_bytes: int = 256 * MIB

    # Result channels (each bounded independently).
    stdout_bytes: int = 1 * MIB
    stderr_head_bytes: int = 128 * KIB
    stderr_tail_bytes: int = 128 * KIB
    chart_bytes: int = 10 * MIB
    chart_max_side: int = 8192
    chart_max_pixels: int = 16_777_216   # 4096 * 4096
    frame_header_bytes: int = 64 * KIB

    # Host scheduling.
    max_concurrent: int = 2
    queue_wait_seconds: int = 120

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "cpus":
                # The chained comparison also rejects NaN (all comparisons are
                # false) and +/-infinity.
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not 0 < value < float("inf"):
                    raise ValueError(f"Limits.cpus must be a positive finite number, got {value!r}")
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Limits.{f.name} must be a positive int, got {value!r}")

        if self.chart_max_pixels > self.chart_max_side * self.chart_max_side:
            raise ValueError("Limits.chart_max_pixels cannot exceed chart_max_side squared")
        if self.chart_bytes > self.out_bytes:
            raise ValueError("Limits.chart_bytes cannot exceed out_bytes (the chart lives in /out)")
        if self.tmp_bytes + self.out_bytes >= self.memory_bytes:
            raise ValueError("Limits: tmpfs sizes must stay below memory_bytes "
                             "(tmpfs pages are charged to the container memory cgroup)")

    @property
    def stderr_bytes(self) -> int:
        """Maximum stderr kept: head plus tail, so the exception line survives."""
        return self.stderr_head_bytes + self.stderr_tail_bytes

    @property
    def max_frame_bytes(self) -> int:
        """Upper bound for one result frame: header plus every capped section."""
        return self.frame_header_bytes + self.stdout_bytes + self.stderr_bytes + self.chart_bytes

    @property
    def host_deadline_seconds(self) -> int:
        """Hard host-side deadline for one execution, startup included."""
        return self.code_wall_seconds + self.host_grace_seconds


DEFAULT_LIMITS = Limits()
