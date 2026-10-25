"""Host resource admission gates. All limits are conservative GiB/MiB."""
import math
import os
import shutil
from pathlib import Path

MAX_WORKER_MEMORY_MB = 1500
DEFAULT_NATIVE_MEMORY_MB = 1500
CODEX_MANAGED_NATIVE_MEMORY_MB = 4096
CODEX_MANAGED_PROVIDERS = ("codex", "openai")
MIN_MEM_AVAILABLE_BYTES = 10 * 1024 * 1024 * 1024
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
    """Narrow exception predicate: only a managed native Codex worker."""
    try:
        name = str(provider or "").strip().lower()
    except Exception:
        return False
    return bool(is_managed) and name in CODEX_MANAGED_PROVIDERS


def native_memory_request_for(provider=None, is_managed=False, explicit_mb=None):
    """Single policy for native aplexer memory (MiB).

    Covers submit reservation, admission, resource-status, and the actual
    native ``--memory`` argv. Managed native Codex defaults to 4096MiB
    (kernel-proved midrun kill at 1500MiB); every other provider defaults
    to 1500MiB. Explicit payload requests are honored up to the matching
    ceiling, never as a blanket increase.
    """
    if is_codex_managed_native(provider, is_managed):
        if explicit_mb is None:
            return CODEX_MANAGED_NATIVE_MEMORY_MB
        try:
            value = int(explicit_mb)
        except (TypeError, ValueError):
            raise ValueError(f"invalid explicit worker memory {explicit_mb!r}")
        if isinstance(explicit_mb, bool) or value <= 0:
            raise ValueError(f"invalid explicit worker memory {explicit_mb!r}")
        if value > CODEX_MANAGED_NATIVE_MEMORY_MB:
            raise ValueError("requested worker memory > 4096MiB (managed codex ceiling)")
        return value
    if explicit_mb is None:
        return DEFAULT_NATIVE_MEMORY_MB
    try:
        value = int(explicit_mb)
    except (TypeError, ValueError):
        raise ValueError(f"invalid explicit worker memory {explicit_mb!r}")
    if isinstance(explicit_mb, bool) or value <= 0:
        raise ValueError(f"invalid explicit worker memory {explicit_mb!r}")
    if value > MAX_WORKER_MEMORY_MB:
        raise ValueError("requested worker memory > 1500MiB")
    return value

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
                    provider=None, is_managed=False):
    try:
        requested_value = int(requested_memory_mb)
    except (TypeError, ValueError):
        raise ValueError(f"invalid requested worker memory {requested_memory_mb!r}")
    if is_codex_managed_native(provider, is_managed):
        if requested_value > CODEX_MANAGED_NATIVE_MEMORY_MB:
            raise ValueError("requested worker memory > 4096MiB (managed codex ceiling)")
    elif requested_value > MAX_WORKER_MEMORY_MB:
        raise ValueError("requested worker memory > 1500MiB")

    # Human override (experiment/human-ram-override-twentyfive-subagents-20261005.txt):
    # Ignore host-wide MemAvailable floor; worker MemoryMax stays at the policy
    # ceiling (1500M, or 4096M for managed native Codex only).
    if ignore_ram is None:
        ignore_ram = os.environ.get("HUMAN_RAM_OVERRIDE", "0") == "1"

    mem_avail = get_mem_available()
    if not ignore_ram:
        req_mem = requested_value * 1024 * 1024
        act_mem = active_mem_mb * 1024 * 1024
        if mem_avail - act_mem - req_mem < MIN_MEM_AVAILABLE_BYTES:
            raise ValueError("host MemAvailable < 10GiB")

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
