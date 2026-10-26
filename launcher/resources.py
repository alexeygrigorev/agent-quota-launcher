"""Host resource admission gates. Placement (intake 4fcd3139) is gated on
live data: MemAvailable - ramp_reserve(inflight starts) - estimate(new)
>= floor. Worker MemoryMax caps (1500M, 4096M managed Codex) are
per-session safety caps and are never charged as reservations."""
import math
import os
import shutil
from pathlib import Path

from launcher.resource_policy import (
    RESOURCE_DEFAULTS,
    _validate_knob,
    check_cpu_guard,
    valid_estimate_mb,
)
from launcher.cpu_resources import (
    check_cpu_choking,
    format_cpu_refusal,
    sample_cpu_usage,
    trusted_choking_from_env,
)

MAX_WORKER_MEMORY_MB = 1500
DEFAULT_NATIVE_MEMORY_MB = 1500
CODEX_MANAGED_NATIVE_MEMORY_MB = 4096
CODEX_MANAGED_PROVIDERS = ("codex", "openai")
# Host MemAvailable floor default (Alexey t1476u 2026-10-26: 2 GiB).
# Configurable per launch via memory_floor_mb; this constant is the default.
MIN_MEM_AVAILABLE_BYTES = RESOURCE_DEFAULTS["memory_floor_mb"] * 1024 * 1024
DEFAULT_MEMORY_FLOOR_MB = RESOURCE_DEFAULTS["memory_floor_mb"]
MIN_DISK_FREE_BYTES = 20 * 1024 * 1024 * 1024
WARN_DISK_FREE_BYTES = 30 * 1024 * 1024 * 1024
MAX_DISK_SPIKE_BYTES = 512 * 1024 * 1024

STAGE_1_CONCURRENCY = 10
STAGE_2_CONCURRENCY = 25
STAGE_3_CONCURRENCY = 50
STAGES = (10, 25, 50)
TYPICAL_WORKER_RSS_BYTES = 350 * 1024 * 1024  # 350 MiB empirical
MAX_WORKER_RSS_BYTES = 768 * 1024 * 1024      # 768 MiB (cgroup limit)
CONTROLLER_RSS_BYTES = 140 * 1024 * 1024      # 140 MiB
MIN_HOST_RESERVE_RAM_BYTES = 3 * 1024 * 1024 * 1024  # 3 GiB host headroom

def is_codex_managed_native(provider=None, is_managed=False):
    """Only managed native Codex receives the larger bounded allowance."""
    return bool(is_managed) and isinstance(provider, str) and provider.lower() in CODEX_MANAGED_PROVIDERS


def native_memory_request_for(provider=None, is_managed=False, explicit_mb=None):
    """Shared reservation/admission/argv policy; all values are integer MiB."""
    ceiling = CODEX_MANAGED_NATIVE_MEMORY_MB if is_codex_managed_native(provider, is_managed) else MAX_WORKER_MEMORY_MB
    if explicit_mb is None:
        return ceiling
    if type(explicit_mb) is not int or explicit_mb <= 0:
        raise ValueError("invalid explicit worker memory")
    if explicit_mb > ceiling:
        raise ValueError(f"requested worker memory > {ceiling}MiB")
    return explicit_mb


def estimate_stage_memory_bytes(concurrency: int, peak: bool = False) -> int:
    worker_mem = MAX_WORKER_RSS_BYTES if peak else TYPICAL_WORKER_RSS_BYTES
    return concurrency * (worker_mem + CONTROLLER_RSS_BYTES)

def evaluate_stage_capacity(concurrency: int = 10, current_active: int = 0, mem_available_bytes: int = None, peak: bool = False, ignore_ram: bool = None) -> dict:
    if ignore_ram is None:
        ignore_ram = os.environ.get("HUMAN_RAM_OVERRIDE", "0") == "1"

    if mem_available_bytes is None:
        mem_available_bytes = get_mem_available()
    
    needed_workers = max(0, concurrency - current_active)
    projected_needed_bytes = estimate_stage_memory_bytes(needed_workers, peak)
    remaining_after_stage_bytes = mem_available_bytes - projected_needed_bytes
    
    if ignore_ram:
        feasible = remaining_after_stage_bytes > 0
    else:
        feasible = remaining_after_stage_bytes >= MIN_HOST_RESERVE_RAM_BYTES
        
    recommendation = "proceed" if feasible else "scale_down"
    
    return {
        "target_stage": concurrency,
        "feasible": feasible,
        "human_ram_override_active": ignore_ram,
        "projected_needed_bytes": projected_needed_bytes,
        "projected_needed_gib": projected_needed_bytes / (1024**3),
        "mem_available_bytes": mem_available_bytes,
        "mem_available_gib": mem_available_bytes / (1024**3),
        "remaining_after_stage_bytes": remaining_after_stage_bytes,
        "remaining_after_stage_gib": remaining_after_stage_bytes / (1024**3),
        "peak_estimate": peak,
        "recommendation": recommendation,
    }

def get_max_feasible_stage(mem_available_bytes: int = None, peak: bool = False) -> int:
    if mem_available_bytes is None:
        mem_available_bytes = get_mem_available()
    for stage in (50, 25, 10):
        cap = evaluate_stage_capacity(concurrency=stage, current_active=0, mem_available_bytes=mem_available_bytes, peak=peak)
        if cap["feasible"]:
            return stage
    return 0




def get_mem_available():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except (FileNotFoundError, PermissionError, OSError):
        pass
    if os.name == 'nt':
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ('dwLength', ctypes.c_ulong),
                ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]
        try:
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullAvailPhys)
        except Exception:
            pass
    return 0


def _get_disk_usage(path):
    try:
        return shutil.disk_usage(path)
    except FileNotFoundError:
        p = Path(path).resolve()
        while not p.exists() and p.parent != p:
            p = p.parent
        return shutil.disk_usage(str(p))


def check_resources(requested_memory_mb, requested_cwd, requested_tmpdir,
                    active_mem_mb=0, active_disk_mb=0, repo_root=None,
                    ignore_ram=None, requested_disk_mb=0, target_stage=None,
                    provider=None, is_managed=False,
                    memory_floor_mb=None, memory_estimate_mb=None,
                    cpu_load_factor=None, check_cpu=True,
                    _load5=None, _cpu_count=None,
                    cpu_usage=None, trusted_choking=None,
                    expected_cpu_cores=None, ramping_cpu_cores=0.0,
                    cpu_policy=None, _now_ms=None,
                    _sample_cpu=False, _proc_root="/proc"):
    """Live MemAvailable admission + choking-only CPU gate (USER t1511u V2).

    The worker cap (``requested_memory_mb`` validated against 1500/4096M)
    is the cgroup safety cap only. What is charged against MemAvailable is
    the placement ``estimate(new)``: ``memory_estimate_mb`` when valid,
    otherwise the full requested allowance (fail closed). ``active_mem_mb``
    is the ramp reserve of other inflight starts (see
    ``Store.get_active_resources``); resident workers are already inside
    MemAvailable and are not charged again. Admission requires::

        MemAvailable - ramp_reserve - estimate(new) >= floor

    with ``floor`` = ``memory_floor_mb`` (default 2048).

    CPU (t1511u): heavy busy% is FINE. Refusal happens ONLY on fresh actual
    choking: PSI CPU FULL > cap (cpu_pressure), PSI memory FULL > cap
    (memory_pressure), fresh OOM delta > 0 (host_oom), or fresh trusted host
    runtime evidence (host_runtime_choking/host_oom). Load average, busy%,
    predicted load, and unknown telemetry NEVER refuse. ``cpu_load_factor``
    is accepted for compatibility but UNUSED. ``HUMAN_RAM_OVERRIDE`` skips
    the memory floor only, never the disk, tmpdir, stage or CPU choking gate.

    On CPU choking the error is the exact refusal slug line followed by a
    ``CPU_ADMISSION_JSON`` object (no arbitrary text), e.g.::

        cpu_pressure
        CPU_ADMISSION_JSON {"current_cpu_cores": ..., ...}
    """
    if requested_memory_mb is None:
        raise ValueError("invalid requested worker memory")
    requested_value = native_memory_request_for(provider, is_managed, requested_memory_mb)

    floor_mb = DEFAULT_MEMORY_FLOOR_MB if memory_floor_mb is None else memory_floor_mb
    try:
        floor_mb = _validate_knob("memory_floor_mb", floor_mb)
    except ValueError:
        raise ValueError(f"invalid memory_floor_mb: {memory_floor_mb!r}") from None
    floor_bytes = floor_mb * 1024 * 1024

    estimate_mb = valid_estimate_mb(memory_estimate_mb, cap_mb=requested_value)
    if estimate_mb is None:
        if memory_estimate_mb is not None:
            raise ValueError(f"invalid memory_estimate_mb: {memory_estimate_mb!r}")
        estimate_mb = requested_value

    factor = RESOURCE_DEFAULTS["cpu_load_factor"] if cpu_load_factor is None else cpu_load_factor
    # Deprecated compatibility: validate bounds (fail closed) but UNUSED for
    # admission (t1511u choking-only). Load/busy/predicted never refuse.
    try:
        _validate_knob("cpu_load_factor", factor)
    except ValueError:
        raise ValueError(f"invalid cpu_load_factor: {cpu_load_factor!r}") from None

    # Human override (experiment/human-ram-override-twentyfive-subagents-20261005.txt):
    # Ignore host-wide MemAvailable floor; worker MemoryMax stays at the policy
    # ceiling (1500M, or 4096M for managed native Codex only).
    if ignore_ram is None:
        ignore_ram = os.environ.get("HUMAN_RAM_OVERRIDE", "0") == "1"

    mem_avail = get_mem_available()
    if not ignore_ram:
        req_mem = estimate_mb * 1024 * 1024
        act_mem = active_mem_mb * 1024 * 1024
        if mem_avail - act_mem - req_mem < floor_bytes:
            raise ValueError(
                f"host MemAvailable below {floor_mb}MiB floor after reservation "
                f"(launcher_memory_reserve)")

    if check_cpu:
        policy_cpu = dict(cpu_policy or {})
        for key in ("cpu_target_utilization", "cpu_psi_full_hard_cap_pct",
                    "memory_psi_full_hard_cap_pct", "choking_cooldown_seconds"):
            if key in RESOURCE_DEFAULTS and key not in policy_cpu:
                policy_cpu[key] = RESOURCE_DEFAULTS[key]
        live_usage = cpu_usage
        if live_usage is None and _sample_cpu:
            try:
                sampled = sample_cpu_usage(proc_root=_proc_root)
                live_usage = {k: sampled.get(k) for k in
                              ("busy_percent", "busy_cores", "cores", "psi_avg10",
                               "psi_full_avg10", "memory_psi_full_avg10",
                               "oom_kills_delta", "observed_at", "interval_s")}
            except Exception:
                live_usage = None
        hint = trusted_choking
        if hint is None:
            try:
                hint = trusted_choking_from_env(
                    now_ms=_now_ms,
                    cooldown_s=policy_cpu.get("choking_cooldown_seconds", 300))
            except Exception:
                hint = None
        import time as _time
        now_ms = _now_ms if _now_ms is not None else int(_time.time() * 1000)
        try:
            cores_for_gate = _cpu_count
            if cores_for_gate is None and isinstance(live_usage, dict):
                cores_for_gate = live_usage.get("cores")
        except Exception:
            cores_for_gate = None
        allowed, reason, admission = check_cpu_choking(
            cpu_usage=live_usage, policy=policy_cpu, cores=cores_for_gate,
            expected_cpu_cores=expected_cpu_cores
            if expected_cpu_cores is not None else 0.0,
            ramping_cpu_cores=ramping_cpu_cores or 0.0,
            trusted_choking=hint, now_ms=now_ms)
        if not allowed:
            raise ValueError(format_cpu_refusal(reason, admission))

    if target_stage is not None:
        cap = evaluate_stage_capacity(concurrency=target_stage, mem_available_bytes=mem_avail, ignore_ram=ignore_ram)
        if not cap["feasible"]:
            raise ValueError(f"stage {target_stage} capacity exceeded")

    if requested_tmpdir is None:
        if repo_root is not None:
            requested_tmpdir = str(Path(repo_root).resolve() / '.local' / 'tmp')
        else:
            raise ValueError("requested_tmpdir is required")

    tmp_path = Path(requested_tmpdir).resolve()
    if tmp_path == Path('/tmp') or tmp_path.parts[:2] == ('/', 'tmp'):
        raise ValueError("reject /tmp")

    if repo_root is None:
        raise ValueError("repo root required for TMPDIR containment check")
    owned_root = (Path(repo_root).resolve() / '.local' / 'tmp')
    if not tmp_path.is_relative_to(owned_root):
        raise ValueError(f"TMPDIR must resolve under owned {owned_root}")

    cwd_stat = _get_disk_usage(requested_cwd)
    tmp_stat = _get_disk_usage(requested_tmpdir)

    task_disk = min(int(requested_disk_mb or 0) * 1024 * 1024, MAX_DISK_SPIKE_BYTES)
    required_free = MIN_DISK_FREE_BYTES + (active_disk_mb * 1024 * 1024) + task_disk

    if cwd_stat.free < required_free:
        raise ValueError(f"cwd filesystem free < 20GiB floor + required disk ({required_free} B)")

    if tmp_stat.free < required_free:
        raise ValueError(f"tmpdir filesystem free < 20GiB floor + required disk ({required_free} B)")

    return True


def check_disk_pressure(cwd, tmpdir, episode_state=None) -> dict:
    """Samples cwd and tmpdir disk usage against 20GiB hard floor and 30GiB warning threshold."""
    cwd_stat = _get_disk_usage(cwd)
    tmp_stat = _get_disk_usage(tmpdir)
    free = min(cwd_stat.free, tmp_stat.free)

    if free < MIN_DISK_FREE_BYTES:
        return {
            "status": "hard_floor_exceeded",
            "pressure": True,
            "free_bytes": free,
            "eligible_continue": False,
        }

    if free < WARN_DISK_FREE_BYTES:
        enqueue = True
        if episode_state is not None:
            if isinstance(episode_state, dict):
                if episode_state.get("in_episode", False):
                    enqueue = False
                else:
                    episode_state["in_episode"] = True
                    cur_ep = episode_state.get("episode_id") or episode_state.get("episode") or 0
                    episode_state["episode_id"] = cur_ep + 1
                    episode_state["episode"] = cur_ep + 1
            elif getattr(episode_state, "in_episode", False):
                enqueue = False
            elif hasattr(episode_state, "in_episode"):
                episode_state.in_episode = True
        return {
            "status": "pressure",
            "pressure": True,
            "free_bytes": free,
            "eligible_continue": True,
            "enqueue_cleanup": enqueue,
        }

    # free >= WARN_DISK_FREE_BYTES
    if episode_state is not None:
        if isinstance(episode_state, dict):
            episode_state["in_episode"] = False
        elif hasattr(episode_state, "in_episode"):
            episode_state.in_episode = False
    return {
        "status": "ok",
        "pressure": False,
        "free_bytes": free,
        "eligible_continue": True,
        "enqueue_cleanup": False,
        "rearm": True,
    }
