"""Bounded sizing shared by Hub native sessions and detached task units.

Linux pids.max counts threads, including compiler and language-server pools.
Resource hints affect sizing only; they grant no provider/namespace authority.
"""
from pathlib import Path

MIN_TASKS = 1024
MAX_TASKS = 8192
MAX_TASK_MEMORY_MB = 32768
HUB_MEMORY_FLOOR_MB = 4096
CARGO_MEMORY_FLOOR_MB = 8192


def tasks_max_for(memory_mb):
    if type(memory_mb) is not int or not 1 <= memory_mb <= MAX_TASK_MEMORY_MB:
        raise ValueError("task memory outside bounded range [1, 32768] MiB")
    return min(MAX_TASKS, max(MIN_TASKS, ((memory_mb + 1023) // 1024) * 256))


def is_hub_headless(payload):
    return payload.get("owner") == "hub-fleet" and payload.get("profile") == "model-task"


def hub_headless_memory_mb(payload):
    """Hub estimate plus cold-start floor; None for other launch contracts.

    Cargo detection reads the requested cwd only, never goal text. The same
    sizing policy is evaluated at submit and launch. Oversized hints fail closed.
    """
    if not is_hub_headless(payload):
        return None
    floor = HUB_MEMORY_FLOOR_MB
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and (Path(cwd) / "Cargo.toml").is_file():
        floor = CARGO_MEMORY_FLOOR_MB
    values = [floor]
    for key in ("memory_estimate_mb", "memory_mb"):
        if key in payload:
            value = payload[key]
            if type(value) is not int or not 1 <= value <= MAX_TASK_MEMORY_MB:
                raise ValueError(f"{key} outside bounded range [1, 32768] MiB")
            values.append(value)
    return max(values)


def hub_headless_admission_mb(payload, estimate_mb):
    """Memory charged against host headroom for a starting Hub task.

    Intake 63a9ee3a: the 4096 MiB cold-start floor is the cgroup cap, not a
    reservation; charging it refused Tabo starts with 4.7 GB free. Admission
    uses the measured estimate. Cargo workspaces keep their compile floor.
    """
    if not is_hub_headless(payload):
        return estimate_mb
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and (Path(cwd) / "Cargo.toml").is_file():
        return max(estimate_mb, CARGO_MEMORY_FLOOR_MB)
    return estimate_mb
