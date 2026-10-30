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
    """Effective cgroup cap for a Hub task; None for other launch contracts.

    Intake a10c41b5: an explicit host ``memory_mb`` is the cap for every
    provider (Hub sends 1500 for ZAI/zcodex, Codex and Claude alike). The
    Hub ``memory_estimate_mb`` is a placement hint, never a cap: round 2
    probe 26308b9a ran at 1706 MiB because a 1706 estimate outranked the
    1500 request. Admission charges the estimate clipped to this cap. With
    no host ``memory_mb``, the cap is the larger of the estimate and the
    4096 MiB cold-start floor. Cargo workspaces keep their documented
    compile floor (rustc/linker OOM below it), independent of provider.
    Cargo detection reads the requested cwd only, never goal text. The same
    sizing policy is evaluated at submit and launch. Oversized hints fail
    closed.
    """
    if not is_hub_headless(payload):
        return None
    hints = {}
    for key in ("memory_estimate_mb", "memory_mb"):
        if key in payload:
            value = payload[key]
            if type(value) is not int or not 1 <= value <= MAX_TASK_MEMORY_MB:
                raise ValueError(f"{key} outside bounded range [1, 32768] MiB")
            hints[key] = value
    if "memory_mb" in hints:
        values = [hints["memory_mb"]]
    else:
        values = [HUB_MEMORY_FLOOR_MB] + list(hints.values())
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and (Path(cwd) / "Cargo.toml").is_file():
        values.append(CARGO_MEMORY_FLOOR_MB)
    return max(values)


def hub_headless_admission_mb(payload, estimate_mb):
    """Memory charged against host headroom for a starting Hub task.

    Intake 63a9ee3a: the cgroup cap is not a reservation; charging the 4096
    MiB cold-start floor refused Tabo starts with 4.7 GB free. Admission
    uses the measured estimate. Cargo workspaces keep their compile floor.
    """
    if not is_hub_headless(payload):
        return estimate_mb
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and (Path(cwd) / "Cargo.toml").is_file():
        return max(estimate_mb, CARGO_MEMORY_FLOOR_MB)
    return estimate_mb
