"""Host resource admission gates. All limits are conservative GiB/MiB."""
import math
import os
import shutil
from pathlib import Path

MAX_WORKER_MEMORY_MB = 1500
MIN_MEM_AVAILABLE_BYTES = 10 * 1024 * 1024 * 1024
MIN_DISK_FREE_BYTES = 20 * 1024 * 1024 * 1024
WARN_DISK_FREE_BYTES = 30 * 1024 * 1024 * 1024
MAX_DISK_SPIKE_BYTES = 512 * 1024 * 1024


def get_mem_available():
    with open('/proc/meminfo') as f:
        for line in f:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    return 0


def check_resources(requested_memory_mb, requested_cwd, requested_tmpdir,
                    active_mem_mb=0, active_disk_mb=0, repo_root=None,
                    ignore_ram=None, requested_disk_mb=0):
    if requested_memory_mb > MAX_WORKER_MEMORY_MB:
        raise ValueError("requested worker memory > 1500MiB")

    # Human override (experiment/human-ram-override-twentyfive-subagents-20261005.txt):
    # Ignore host-wide MemAvailable floor; worker MemoryMax <= 1500M strictly preserved.
    if ignore_ram is None:
        ignore_ram = os.environ.get("HUMAN_RAM_OVERRIDE", "0") == "1"

    if not ignore_ram:
        mem_avail = get_mem_available()
        req_mem = requested_memory_mb * 1024 * 1024
        act_mem = active_mem_mb * 1024 * 1024
        if mem_avail - act_mem - req_mem < MIN_MEM_AVAILABLE_BYTES:
            raise ValueError("host MemAvailable < 10GiB")

    cwd_stat = shutil.disk_usage(requested_cwd)
    tmp_stat = shutil.disk_usage(requested_tmpdir)

    task_disk = min(int(requested_disk_mb or 0) * 1024 * 1024, MAX_DISK_SPIKE_BYTES)
    required_free = MIN_DISK_FREE_BYTES + (active_disk_mb * 1024 * 1024) + task_disk

    if cwd_stat.free < required_free:
        raise ValueError(f"cwd filesystem free < 20GiB floor + required disk ({required_free} B)")

    if tmp_stat.free < required_free:
        raise ValueError(f"tmpdir filesystem free < 20GiB floor + required disk ({required_free} B)")

    tmp_path = Path(requested_tmpdir).resolve()
    if tmp_path == Path('/tmp') or tmp_path.parts[:2] == ('/', 'tmp'):
        raise ValueError("reject /tmp")

    if repo_root is None:
        raise ValueError("repo root required for TMPDIR containment check")
    owned_root = (Path(repo_root).resolve() / '.local' / 'tmp')
    if not tmp_path.is_relative_to(owned_root):
        raise ValueError(f"TMPDIR must resolve under owned {owned_root}")

    return True
