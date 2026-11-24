"""
P3.6-4B D2 (approved, Option A): the required writable mounts are EXACTLY /out, /tmp and the eight /dev
entries rootless Podman 4.9.3 creates (six character-device binds, devpts, mqueue; CI run 45341414519).

Exact matching is preserved: one extra writable mount of any kind, or one missing entry, fails closed.
Residual risk (P3.7): the runtime attestation matches PATHS only; filesystem types and device numbers are
checked by the real-container suite (tests/container/p364b), not on every run.
"""

import pytest

from tests.v2.p36.fake_os import GOOD_MOUNTINFO, GOOD_STATUS
from v2.execution import frame
from v2.execution import supervisor_helpers as sh
from v2.execution.errors import SandboxUnavailableError

EXACT = ["/dev/full", "/dev/mqueue", "/dev/null", "/dev/pts", "/dev/random", "/dev/tty", "/dev/urandom", "/dev/zero",
         "/out", "/tmp"]


def _att(mountinfo):
    return sh.build_attestation(proc_status=GOOD_STATUS, mountinfo=mountinfo, net_interfaces=["lo"], dev_writable=False,
                                memory_max="1073741824\n", memory_swap_max="0\n", pids_max="64\n",
                                cpu_max="100000 100000\n")


def test_the_required_set_is_exactly_storage_plus_the_rootless_dev_entries():
    assert frame.expected_attestation()["writable_mounts"] == EXACT
    assert set(frame.STORAGE_WRITABLE_MOUNTS) == {"/out", "/tmp"}
    assert len(frame.ROOTLESS_DEV_WRITABLE_MOUNTS) == 8 and all(m.startswith("/dev/") for m in frame.ROOTLESS_DEV_WRITABLE_MOUNTS)
    frame.verify_attestation(_att(GOOD_MOUNTINFO), frame.expected_attestation())


EXTRA = {
    "shm": "40 28 0:40 / /dev/shm rw,nosuid,nodev - tmpfs shm rw,size=65536k",
    "block_device": "41 28 0:5 /sda /dev/sda rw,nosuid - devtmpfs udev rw",
    "storage_var_tmp": "42 22 0:41 / /var/tmp rw,nosuid,nodev - tmpfs tmpfs rw",
    "host_bind": "43 22 8:1 /home/runner /data rw,relatime - ext4 /dev/sda1 rw",
    "in_writable": "44 22 0:25 / /in rw,relatime - ext4 /dev/sda1 rw",
    "dev_subdir": "45 28 0:42 / /dev/fuse rw,nosuid - devtmpfs udev rw",
}


@pytest.mark.parametrize("name", sorted(EXTRA))
def test_any_additional_writable_mount_fails_closed(name):
    with pytest.raises(SandboxUnavailableError, match="writable_mounts"):
        frame.verify_attestation(_att(GOOD_MOUNTINFO + EXTRA[name] + "\n"), frame.expected_attestation())


@pytest.mark.parametrize("missing", EXACT)
def test_a_missing_entry_also_fails_closed(missing):
    lines = [ln for ln in GOOD_MOUNTINFO.splitlines() if ln.split()[4:5] != [missing]]
    with pytest.raises(SandboxUnavailableError, match="writable_mounts"):
        frame.verify_attestation(_att("\n".join(lines) + "\n"), frame.expected_attestation())


def test_a_permitted_dev_path_mounted_read_write_as_storage_is_still_only_path_checked():
    """Documents the residual risk: the runtime check cannot see types; the real-container suite does."""
    replaced = GOOD_MOUNTINFO.replace("/tty /dev/tty rw,nosuid,noexec,relatime - devtmpfs udev rw",
                                      "/ /dev/tty rw,nosuid,noexec,relatime - tmpfs tmpfs rw")
    frame.verify_attestation(_att(replaced), frame.expected_attestation())        # passes: path-only (P3.7)
