"""Hub resource-management policy: placement knobs, estimates, CPU guard.

Placement (intake 4fcd3139) is gated on live data, not on a fixed sum of
per-session reservations::

    MemAvailable - ramp_reserve(inflight starts) - estimate(new) >= floor

- Resident sessions are already counted inside MemAvailable, so they are
  not reserved again. Only sessions still starting up hold a ramp reserve.
- ``estimate(new)`` is the measured p90 of resident memory scaled by
  ``estimate_multiplier`` (``ceil(max(64, p90 * multiplier))``, as on the
  Hub), falling back to ``ramp_reserve_default_mb`` when nothing has been
  measured yet, and capped to the worker cgroup ceiling.
- The cgroup ``MemoryMax`` per worker (1500M, 4096M managed Codex) is a
  per-session safety cap and is unchanged by this module.

Knobs arrive from the Hub payload as ``resource_management``::

    {memory_floor_mb, ramp_reserve_default_mb, estimate_percentile,
     estimate_multiplier, ramp_seconds, cpu_target_utilization,
     cpu_default_cores, cpu_estimate_multiplier, cpu_psi_hard_cap_pct,
     cpu_psi_full_hard_cap_pct, memory_psi_full_hard_cap_pct,
     choking_cooldown_seconds, cpu_load_factor (deprecated, unused)}

with an optional per-task ``memory_estimate_mb``. A JSON file with the
same keys may be supplied, but only via an explicit path (payload
``resource_config_path``, ``--resource-config``, or
``LAUNCHER_RESOURCE_CONFIG``); the repo ``config/resource-management.json``
is never auto-loaded. Missing knobs fall back to safe defaults below.
Bounds match the Hub contract (``src/resource-management.ts``);
irrelevant watchdog keys (``watchdog_interval_s``, ``idle_minutes``,
``rss_flag_gb``, ``keep``) are ignored. Unknown measurements stay unknown.

CPU admission (USER t1511u V2) is choking-only: heavy busy% is fine. Only
fresh PSI CPU FULL / PSI memory FULL, fresh OOM kills, or fresh trusted
host runtime evidence refuses. Load average, busy%, predicted load, and
unknown telemetry never refuse. ``cpu_load_factor`` is accepted for
compatibility (default 3.0) but UNUSED for admission.
"""

import json
import math
import os
from pathlib import Path

from launcher.cpu_resources import CPU_BOUNDS as _CPU_BOUNDS
from launcher.cpu_resources import CPU_DEFAULTS as _CPU_DEFAULTS
from launcher.cpu_resources import CPU_FLOAT_KNOBS as _CPU_FLOAT_KNOBS
from launcher.cpu_resources import overlay_cpu_defaults

RESOURCE_DEFAULTS = {
    "memory_floor_mb": 2048,
    "ramp_reserve_default_mb": 512,
    "estimate_percentile": 90,
    "estimate_multiplier": 1.25,
    "ramp_seconds": 180,
    # Deprecated compatibility: accepted but UNUSED for admission (t1511u).
    "cpu_load_factor": 3.0,
    **_CPU_DEFAULTS,
}

RESOURCE_CONFIG_ENV = "LAUNCHER_RESOURCE_CONFIG"
PAYLOAD_CONFIG_KEY = "resource_config_path"

# Sane validation bounds; out-of-range values fail closed, never clamp.
# Must stay identical to the Hub contract in src/resource-management.ts.
_BOUNDS = {
    "memory_floor_mb": (128, 1048576),
    "ramp_reserve_default_mb": (64, 65536),
    "estimate_percentile": (50, 100),
    "estimate_multiplier": (1, 4),
    "ramp_seconds": (30, 900),
    **_CPU_BOUNDS,
}

# Knobs that accept a finite float (ints are accepted and kept as given).
_FLOAT_KNOBS = ("estimate_multiplier", *_CPU_FLOAT_KNOBS)


def _validate_knob(name, value):
    lo, hi = _BOUNDS[name]
    if isinstance(value, bool):
        raise ValueError(f"invalid resource_management[{name}]: {value!r}")
    if name in _FLOAT_KNOBS:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"invalid resource_management[{name}]: {value!r}")
    else:
        if type(value) is not int:
            raise ValueError(f"invalid resource_management[{name}]: {value!r}")
    if not (lo <= value <= hi):
        raise ValueError(f"resource_management[{name}]={value!r} outside [{lo}, {hi}]")
    return value


def _overlay(base, patch, source):
    for key in RESOURCE_DEFAULTS:
        if key in patch:
            base[key] = _validate_knob(key, patch[key])
    overlay_cpu_defaults(base, patch)
    return base


def load_resource_policy(payload=None, explicit_path=None):
    """Resolve placement knobs: defaults < file < payload. Fail closed.

    ``explicit_path`` (or ``LAUNCHER_RESOURCE_CONFIG``) must point at a
    readable JSON object file when given; a missing/unreadable/invalid
    file raises instead of silently falling back. Payload
    ``resource_management`` must be a dict when present.
    """
    policy = dict(RESOURCE_DEFAULTS)
    path = explicit_path
    if path is None and isinstance(payload, dict):
        path = payload.get(PAYLOAD_CONFIG_KEY)
    if path is None:
        path = os.environ.get(RESOURCE_CONFIG_ENV)
    if path is not None:
        p = Path(str(path))
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as e:
            raise ValueError(f"resource config {p} unreadable: {e}") from e
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"resource config {p} is not valid JSON: {e}") from e
        if not isinstance(doc, dict):
            raise ValueError(f"resource config {p} must be a JSON object")
        _overlay(policy, doc, f"file {p}")
    if isinstance(payload, dict):
        patch = payload.get("resource_management")
        if patch is not None:
            if not isinstance(patch, dict):
                raise ValueError("payload resource_management must be an object")
            _overlay(policy, patch, "payload")
    return policy


def percentile_of(values, pct):
    """Nearest-rank percentile over positive ints; None when empty/unknown."""
    nums = [v for v in values if type(v) is int and v > 0]
    if not nums:
        return None
    if not (1 <= pct <= 100):
        raise ValueError(f"percentile {pct!r} outside [1, 100]")
    nums.sort()
    rank = math.ceil(pct / 100 * len(nums)) - 1
    return nums[max(0, min(rank, len(nums) - 1))]


def valid_estimate_mb(value, cap_mb=None):
    """A usable per-task estimate: positive int within the worker cap."""
    if type(value) is not int or value <= 0:
        return None
    if cap_mb is not None and (type(cap_mb) is not int or value > cap_mb):
        return None
    return value


def resolve_memory_estimate_mb(payload=None, measured_p90_mb=None, policy=None,
                               cap_mb=None):
    """Estimate for a new/start-ramp worker.

    Precedence (Hub contract): explicit payload ``memory_estimate_mb``
    (the Hub p90 by project/engine/kind) wins, else the measured local p90
    scaled by ``estimate_multiplier`` (``ceil(max(64, p90 * multiplier))``,
    mirroring the Hub), else the configured ramp default. The result is
    capped to the worker cgroup ceiling (1500M, 4096M managed Codex) when
    ``cap_mb`` is given: a historic oversized sample must shrink the
    estimate, never block placement with a ValueError.
    """
    pol = policy or RESOURCE_DEFAULTS
    default = pol.get("ramp_reserve_default_mb",
                      RESOURCE_DEFAULTS["ramp_reserve_default_mb"])
    mult = pol.get("estimate_multiplier",
                   RESOURCE_DEFAULTS["estimate_multiplier"])
    if isinstance(mult, bool) or not isinstance(mult, (int, float)) \
            or not math.isfinite(mult) or mult <= 0:
        raise ValueError(f"invalid estimate_multiplier: {mult!r}")

    def _cap(value):
        if cap_mb is None:
            return value
        return min(value, cap_mb)

    if isinstance(payload, dict) and "memory_estimate_mb" in payload:
        hit = valid_estimate_mb(payload["memory_estimate_mb"])
        if hit is None:
            raise ValueError("invalid memory_estimate_mb")
        return _cap(hit)
    if type(measured_p90_mb) is int and measured_p90_mb > 0:
        return _cap(max(64, math.ceil(measured_p90_mb * mult)))
    return _cap(default)


def read_load5(path="/proc/loadavg"):
    """5-minute load average; None when unknown (fail closed downstream)."""
    try:
        parts = Path(path).read_text(encoding="utf-8").split()
        if len(parts) < 2:
            return None
        load5 = float(parts[1])
        if not math.isfinite(load5) or load5 < 0:
            return None
        return load5
    except (OSError, ValueError, IndexError):
        return None


def read_cpu_count():
    """Logical CPU count; None when unknown (fail closed downstream)."""
    try:
        count = os.cpu_count()
        if type(count) is not int or count <= 0:
            return None
        return count
    except Exception:
        return None


def check_cpu_guard(load5=None, cpu_count=None, factor=1.5):
    """Deprecated compatibility (USER t1511u): load NEVER refuses admission.

    Kept for callers/tests importing the old symbol. Always returns True.
    Real CPU admission is choking-only via launcher.cpu_resources
    (fresh PSI FULL CPU/memory, fresh OOM, fresh trusted runtime evidence).
    ``read_load5``/``read_cpu_count`` remain for diagnostics display only.
    """
    return True


def cpu_guard_status(load5=None, cpu_count=None, factor=1.5):
    """Machine-readable legacy CPU gate state (always allowed, display only)."""
    if load5 is None:
        load5 = read_load5()
    if cpu_count is None:
        cpu_count = read_cpu_count()
    return {"allowed": True, "load5": load5, "cores": cpu_count,
            "factor": factor, "reason": None,
            "note": "loadavg display only; admission is choking-only (t1511u)"}
