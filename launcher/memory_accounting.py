"""Conservative credit for live memory already excluded from MemAvailable."""
import os
from pathlib import Path

MIB = 1024 * 1024
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")


def _kernel_identity():
    root = CGROUP_ROOT.stat()
    identity = {
        "boot_id": (PROC_ROOT / "sys/kernel/random/boot_id").read_text().strip(),
        "cgroup_root_device": root.st_dev,
        "cgroup_root_inode": root.st_ino,
    }
    for namespace, label in (("cgroup", "cgroup"), ("mnt", "mount")):
        ns = (PROC_ROOT / "self/ns" / namespace).stat()
        identity[f"{label}_namespace_device"] = ns.st_dev
        identity[f"{label}_namespace_inode"] = ns.st_ino
    fd = os.open(CGROUP_ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fields = (PROC_ROOT / f"self/fdinfo/{fd}").read_text().splitlines()
        identity["cgroup_mount_id"] = int(next(x.split()[1] for x in fields if x.startswith("mnt_id:")))
    finally:
        os.close(fd)
    return identity


def _read_at(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    with os.fdopen(fd) as handle:
        value = handle.read(65537)
    if len(value) > 65536:
        raise ValueError("oversized cgroup field")
    return value


def _pid_identity(pid, relative):
    base = PROC_ROOT / str(pid)
    if (base / "cgroup").read_text().strip() != f"0::{relative}":
        raise ValueError("workload moved cgroup")
    # comm can contain spaces and parentheses; state and starttime follow it.
    fields = (base / "stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] in ("Z", "X", "x"):
        raise ValueError("workload is no longer live")
    return int(fields[19])


def resident_credit_mb(binding, info, reserved_mb):
    """Return rounded-down resident credit, or zero if any proof is uncertain.

    Caller supplies the exact-identity live status from probe_native_lifecycle.
    File cache is reclaimable in MemAvailable, so memory.current alone is not
    sufficient: credit is capped at memory.stat anon, excluding cache/kernel.
    No lifecycle state or lease is changed by this observation.
    """
    fd = None
    try:
        if info.get("phase") not in ("running", "working") or info.get("containment_empty") is not False:
            return 0
        for field, expected in (("id", "native_id"), ("tag", "tag"), ("workspace", "workspace"),
                                ("engine", "engine"), ("created_at_ms", "incarnation")):
            if info.get(field) != binding.get(expected):
                return 0
        pid = info.get("workload_pid")
        if type(pid) is not int or pid <= 0 or reserved_mb <= 0:
            return 0
        relative = info.get("workload_cgroup")
        if not isinstance(relative, str) or not relative.startswith("/"):
            return 0
        path = CGROUP_ROOT / relative.lstrip("/")
        if path.name != f"aplexer-workload-{binding['native_id']}.scope":
            return 0
        if path != path.resolve() or not path.is_relative_to(CGROUP_ROOT):
            return 0
        if info.get("containment_cgroup") != str(path):
            return 0
        identity = _kernel_identity()
        if info.get("containment_cgroup_identity") != identity:
            return 0
        process = _pid_identity(pid, relative)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        original = os.fstat(fd)

        def sample():
            limit = int(_read_at(fd, "memory.max").strip())
            current = int(_read_at(fd, "memory.current").strip())
            anon = int(dict(line.split() for line in _read_at(fd, "memory.stat").splitlines())["anon"])
            events = dict(line.split() for line in _read_at(fd, "cgroup.events").splitlines())
            members = [int(p) for p in _read_at(fd, "cgroup.procs").split()]
            if not (0 < limit <= reserved_mb * MIB and 0 <= anon <= current <= limit):
                raise ValueError("invalid or unbounded memory sample")
            if events.get("populated") != "1" or pid not in members:
                raise ValueError("workload membership is uncertain")
            return min(current, anon)

        credit = min(sample(), sample())
        after = path.stat()
        if (original.st_dev, original.st_ino) != (after.st_dev, after.st_ino):
            return 0
        if process != _pid_identity(pid, relative) or identity != _kernel_identity():
            return 0
        return min(reserved_mb, credit // MIB)
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration):
        return 0
    finally:
        if fd is not None:
            os.close(fd)
