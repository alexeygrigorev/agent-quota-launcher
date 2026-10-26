"""Launcher CPU choking-only admission (Hub umbrella 4fcd3139, USER t1511u V2).

USER POLICY: heavy utilization is fine (busy 90%+ acceptable). Throttle ONLY
actual choking signals:

- PSI CPU FULL avg10 > cpu_psi_full_hard_cap_pct (default 5) => cpu_pressure
- PSI memory FULL avg10 > memory_psi_full_hard_cap_pct (default 1) => memory_pressure
- classified host-wide OOM from a fresh trusted hint => host_oom
- trusted host runtime evidence (fresh, within choking_cooldown_seconds) =>
  host_runtime_choking

Raw /proc/vmstat oom_kill includes isolated session memcg kills and is
diagnostic only; it never refuses admission or creates a host cooldown.

NEVER throttle on loadavg, busy%, predicted load, or unknown CPU telemetry.
Unknown stays visibly null/advisory, never fake-clear. Busy + expected new +
sum of remaining ramps vs cores*target is ADVISORY (predicted_over_target)
only, never an admission condition.

Wire mirrors the frozen Hub contract (root c0a3ad1, fixture
test/fixtures/cpu-admission-v2.json sha256
8ea420054a3152f6bddaa97d388408b5397f04f3dd7dc864dd80fbaa0b400100):

- heartbeat ``cpu_usage``: {busy_percent, busy_cores, cores, psi_avg10 (SOME,
  display only), psi_full_avg10 (CPU FULL), memory_psi_full_avg10, oom_kills_delta,
  observed_at, interval_s}. /proc/stat delta excludes idle+iowait and avoids
  guest double-count. Missing/reset/unreadable stays unknown (None), never 0.
- OOM from /proc/vmstat oom_kill measured delta; first sample unknown.
- expected CPU per task: payload ``expected_cpu_cores`` + ``expected_cpu_source``
  (repo_kind/repo/global/default) is authoritative when present and valid;
  otherwise the conservative ``cpu_default_cores`` (default 1.0). Frozen per
  task under the existing launch lock.
- local refusals: exact slug line (cpu_pressure/memory_pressure/host_oom/
  host_runtime_choking) then a ``CPU_ADMISSION_JSON`` object with
  {current_cpu_cores, expected_cpu_cores, ramping_cpu_cores, limit_cpu_cores,
  target_utilization, psi_avg10, psi_full_avg10, memory_psi_full_avg10,
  oom_kills_delta, observed_at}. Finite bounded numbers or null (unknown).
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

CPU_REASONS = ("cpu_pressure", "memory_pressure", "host_oom", "host_runtime_choking")
CHOKING_REASONS = ("host_oom", "host_runtime_choking")
EXPECTED_SOURCES = ("repo_kind", "repo", "global", "default")

CPU_DEFAULTS = {
    "cpu_target_utilization": 1.0,
    "cpu_default_cores": 1.0,
    "cpu_estimate_multiplier": 1.25,
    "cpu_psi_hard_cap_pct": 50.0,
    "cpu_psi_full_hard_cap_pct": 5.0,
    "memory_psi_full_hard_cap_pct": 1.0,
    "choking_cooldown_seconds": 300,
    # Deprecated compatibility: accepted but UNUSED for admission.
    "cpu_load_factor": 3.0,
}

CPU_BOUNDS = {
    "cpu_target_utilization": (0.1, 1.0),
    "cpu_default_cores": (0.05, 128.0),
    "cpu_estimate_multiplier": (1.0, 4.0),
    "cpu_psi_hard_cap_pct": (1.0, 100.0),
    "cpu_psi_full_hard_cap_pct": (0.1, 100.0),
    "memory_psi_full_hard_cap_pct": (0.1, 100.0),
    "choking_cooldown_seconds": (30, 3600),
    "cpu_load_factor": (0.5, 4.0),
}

CPU_FLOAT_KNOBS = (
    "cpu_target_utilization",
    "cpu_default_cores",
    "cpu_estimate_multiplier",
    "cpu_psi_hard_cap_pct",
    "cpu_psi_full_hard_cap_pct",
    "memory_psi_full_hard_cap_pct",
    "cpu_load_factor",
)


def _finite_nonneg(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0


def validate_cpu_knob(name, value):
    lo, hi = CPU_BOUNDS[name]
    if isinstance(value, bool):
        raise ValueError(f"invalid resource_management[{name}]: {value!r}")
    if name in CPU_FLOAT_KNOBS:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"invalid resource_management[{name}]: {value!r}")
    else:
        if type(value) is not int:
            raise ValueError(f"invalid resource_management[{name}]: {value!r}")
    if not (lo <= value <= hi):
        raise ValueError(f"resource_management[{name}]={value!r} outside [{lo}, {hi}]")
    return value


def overlay_cpu_defaults(base, patch):
    for key in CPU_DEFAULTS:
        if key in patch:
            base[key] = validate_cpu_knob(key, patch[key])
    return base


def _parse_observed_ms(observed_at):
    try:
        if not isinstance(observed_at, str) or len(observed_at) > 64:
            return None
        text = observed_at.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return None
        return int(parsed.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return None


def sanitize_cpu_admission(value):
    """Fixed numeric refusal DTO. Arbitrary output never crosses this boundary."""
    required = {"expected_cpu_cores", "ramping_cpu_cores", "limit_cpu_cores",
                "target_utilization"}
    optional = {"current_cpu_cores", "psi_avg10", "psi_full_avg10",
                "memory_psi_full_avg10", "oom_kills_delta", "observed_at"}
    if not isinstance(value, dict) or not required.issubset(value) \
            or set(value) - required - optional:
        return None
    for key, number in value.items():
        if key == "observed_at":
            if number is not None and _parse_observed_ms(number) is None:
                return None
        elif number is None and key in optional:
            continue
        elif not _finite_nonneg(number) or number > (
                100 if "psi" in key else 1 if key == "target_utilization" else 1e9):
            return None
        elif key == "target_utilization" and number < 0.1:
            return None
        elif key == "oom_kills_delta" and not isinstance(number, int):
            return None
    return dict(value)


def sanitize_cpu_usage(value):
    """Validate a cpu_usage heartbeat dict; unknown fields become None."""
    if not isinstance(value, dict):
        return None
    observed_ms = _parse_observed_ms(value.get("observed_at"))
    if observed_ms is None:
        return None
    cores = value.get("cores")
    cores = cores if type(cores) is int and 1 <= cores <= 65536 else None
    busy_percent = value.get("busy_percent")
    busy_cores = value.get("busy_cores")
    measured = (
        cores is not None and _finite_nonneg(busy_percent) and busy_percent <= 100
        and _finite_nonneg(busy_cores) and busy_cores <= cores
        and abs(busy_cores - busy_percent * cores / 100) <= 0.01
    )

    def pressure(key):
        val = value.get(key)
        return val if (_finite_nonneg(val) and val <= 100) else None

    oom = value.get("oom_kills_delta")
    oom = oom if (type(oom) is int and 0 <= oom <= 1_000_000_000) else None
    interval = value.get("interval_s")
    interval = interval if (_finite_nonneg(interval) and 0 < interval <= 180) else None
    return {
        "busy_percent": value.get("busy_percent") if measured else None,
        "busy_cores": value.get("busy_cores") if measured else None,
        "cores": cores,
        "psi_avg10": pressure("psi_avg10"),
        "psi_full_avg10": pressure("psi_full_avg10"),
        "memory_psi_full_avg10": pressure("memory_psi_full_avg10"),
        "oom_kills_delta": oom,
        "observed_at": value.get("observed_at"),
        "interval_s": interval,
    }


def read_cpu_stat(proc_root="/proc"):
    """Read /proc/stat. Busy excludes idle+iowait; guest already in user/nice."""
    try:
        lines = (Path(proc_root) / "stat").read_text().splitlines()
        if not lines or not lines[0].startswith("cpu "):
            return None
        fields = [int(n) for n in lines[0].split()[1:9]]
        cores = sum(line.split()[0][3:].isdigit() for line in lines
                    if line.startswith("cpu"))
        if len(fields) != 8 or min(fields) < 0 or cores < 1:
            return None
        return {"ticks": fields, "cores": cores}
    except (OSError, ValueError, IndexError):
        return None


def read_cpu_psi(proc_root="/proc", resource="cpu", category="some"):
    try:
        for line in (Path(proc_root) / "pressure" / resource).read_text().splitlines():
            if line.startswith(category + " "):
                fields = dict(pair.split("=", 1) for pair in line.split()[1:])
                value = float(fields["avg10"])
                return value if (_finite_nonneg(value) and value <= 100) else None
    except (OSError, ValueError, KeyError):
        pass
    return None


def read_oom_counter(proc_root="/proc"):
    """Current /proc/vmstat oom_kill counter, or None when unreadable."""
    try:
        values = dict(line.split() for line in
                      (Path(proc_root) / "vmstat").read_text().splitlines())
        counter = int(values["oom_kill"])
        return counter if counter >= 0 else None
    except (OSError, ValueError, KeyError):
        return None


def oom_delta(previous_counter, current_counter):
    """Measured OOM delta; None on first sample/reset/unreadable."""
    if type(previous_counter) is not int or type(current_counter) is not int:
        return None
    if previous_counter < 0 or current_counter < 0:
        return None
    if current_counter < previous_counter:
        return None
    return current_counter - previous_counter


def cpu_delta(before, after):
    if not before or not after or before["cores"] != after["cores"]:
        return None
    ticks = [b - a for a, b in zip(before["ticks"], after["ticks"])]
    total = sum(ticks)
    if min(ticks) < 0 or total <= 0:
        return None
    busy = total - ticks[3] - ticks[4]
    return {"busy_percent": busy / total * 100,
            "busy_cores": busy / total * after["cores"],
            "cores": after["cores"]}


def sample_cpu_usage(proc_root="/proc", interval_s=0.2, sleep=time.sleep,
                     monotonic=time.monotonic, clock=time.time,
                     oom_previous=None):
    before = read_cpu_stat(proc_root)
    started = monotonic()
    sleep(interval_s)
    after = read_cpu_stat(proc_root)
    elapsed = monotonic() - started
    result = cpu_delta(before, after)
    if result is None:
        result = {"busy_percent": None, "busy_cores": None,
                  "cores": (after or before or {}).get("cores")}
    if not _finite_nonneg(elapsed) or elapsed <= 0:
        elapsed = None
    oom_current = read_oom_counter(proc_root)
    return {**result,
            "psi_avg10": read_cpu_psi(proc_root),
            "psi_full_avg10": read_cpu_psi(proc_root, category="full"),
            "memory_psi_full_avg10": read_cpu_psi(proc_root, resource="memory",
                                                  category="full"),
            "oom_kills_delta": oom_delta(oom_previous, oom_current)
            if oom_previous is not None or oom_current is not None else None,
            "oom_counter": oom_current,
            "interval_s": elapsed,
            "observed_at": datetime.fromtimestamp(clock(), timezone.utc)
            .isoformat().replace("+00:00", "Z")}


def cpu_verdict(raw_usage, cores, expected_cpu_cores, ramping_cpu_cores,
                policy, now_ms=None):
    """Choking-only verdict. Busy/predicted/unknown never refuse.

    Returns dict with the fixed admission numbers plus advisory
    predicted_over_target/telemetry_state/advisory and reason/admitted where
    local reason is cpu_pressure/memory_pressure or None. Trusted host
    OOM/runtime hints are applied separately by check_cpu_choking.
    """
    target = policy.get("cpu_target_utilization", 1.0)
    full_cap = policy.get("cpu_psi_full_hard_cap_pct", 5.0)
    mem_cap = policy.get("memory_psi_full_hard_cap_pct", 1.0)
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    usage = sanitize_cpu_usage(raw_usage) if raw_usage is not None else None
    host_at = _parse_observed_ms((usage or {}).get("observed_at"))
    fresh = host_at is not None and now_ms - host_at <= 180000 \
        and now_ms - host_at >= -30000
    known = (fresh and usage is not None and usage["cores"] == cores
             and usage["busy_cores"] is not None and usage["interval_s"] is not None
             and _finite_nonneg(expected_cpu_cores) and _finite_nonneg(ramping_cpu_cores))
    numbers = {
        "current_cpu_cores": usage["busy_cores"] if known else None,
        "expected_cpu_cores": expected_cpu_cores,
        "ramping_cpu_cores": ramping_cpu_cores,
        "limit_cpu_cores": cores * target if type(cores) is int and cores > 0
        and _finite_nonneg(target) else None,
        "target_utilization": target,
        "psi_avg10": (usage or {}).get("psi_avg10"),
        "psi_full_avg10": (usage or {}).get("psi_full_avg10"),
        "memory_psi_full_avg10": (usage or {}).get("memory_psi_full_avg10"),
        "oom_kills_delta": (usage or {}).get("oom_kills_delta"),
        "observed_at": (usage or {}).get("observed_at"),
        "predicted_over_target": (usage["busy_cores"] + expected_cpu_cores
                                   + ramping_cpu_cores > cores * target)
        if known else None,
        "telemetry_state": ("measured" if (known
                                            and usage["psi_full_avg10"] is not None
                                            and usage["memory_psi_full_avg10"] is not None)
                            else "unknown"),
        "advisory": None if known else "cpu_usage_unknown",
    }
    reason = None
    if fresh and usage["psi_full_avg10"] is not None \
            and usage["psi_full_avg10"] > full_cap:
        reason = "cpu_pressure"
    elif fresh and usage["memory_psi_full_avg10"] is not None \
            and usage["memory_psi_full_avg10"] > mem_cap:
        reason = "memory_pressure"
    # /proc/vmstat includes isolated session memcg kills. It is diagnostic,
    # not evidence of host-wide choking; scoped host OOM arrives via the
    # separately validated trusted_choking channel.
    return {**numbers, "reason": reason, "admitted": reason is None}


def resolve_expected_cpu(payload=None, policy=None):
    """Authoritative expected CPU: payload wins, else conservative default.

    Payload ``expected_cpu_cores`` + ``expected_cpu_source`` (repo_kind/repo/
    global/default) comes from the Hub estimator. Missing payload falls back
    to ``cpu_default_cores`` (default 1.0) with source ``default``. Present
    but invalid payload fails closed.
    """
    default = (policy or CPU_DEFAULTS).get("cpu_default_cores",
                                           CPU_DEFAULTS["cpu_default_cores"])
    if isinstance(payload, dict) and ("expected_cpu_cores" in payload
                                      or "expected_cpu_source" in payload):
        cores = payload.get("expected_cpu_cores")
        source = payload.get("expected_cpu_source")
        if not isinstance(cores, (int, float)) or isinstance(cores, bool) \
                or not math.isfinite(cores) or not (0.05 <= cores <= 1024):
            raise ValueError(f"invalid expected_cpu_cores: {cores!r}")
        if source not in EXPECTED_SOURCES:
            raise ValueError(f"invalid expected_cpu_source: {source!r}")
        return float(cores), source
    return float(default), "default"


def validate_resource_choking(value, now_ms=None, cooldown_s=300):
    """Trusted host runtime hint only; never derived from task text.

    Accepts {reason: host_oom|host_runtime_choking, observed_at: ISO} fresh
    within cooldown_s, else None. Stale/malformed/future evidence is ignored
    (advisory), never a refusal by itself.
    """
    if value is None:
        return None
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if not isinstance(value, dict):
        return None
    reason = value.get("reason")
    if reason not in CHOKING_REASONS:
        return None
    at = _parse_observed_ms(value.get("observed_at"))
    if at is None:
        return None
    if at > now_ms + 30000:
        return None
    if now_ms - at > cooldown_s * 1000:
        return None
    return {"reason": reason, "observed_at": value.get("observed_at")}


def trusted_choking_from_env(now_ms=None, cooldown_s=300):
    """Host-provided hint via LAUNCHER_TRUSTED_CHOKING_JSON (JSON object).

    This is the ONLY runtime-hint intake: task payload/goal text is never
    parsed for choking signals. Unset/malformed/stale => None.
    """
    import json as _json
    raw = os.environ.get("LAUNCHER_TRUSTED_CHOKING_JSON")
    if not raw:
        return None
    try:
        value = _json.loads(raw)
    except (ValueError, TypeError):
        return None
    return validate_resource_choking(value, now_ms=now_ms, cooldown_s=cooldown_s)


def collect_admission_context(store, policy, dry_run=False):
    """Independent live signals and durable positive hints for every backend.

    A failed busy sampler must not consume an OOM delta or hide full PSI.
    Dry-run reads exactly the same evidence without advancing any baseline.
    """
    def safe(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            return None

    now_ms = int(time.time() * 1000)
    sampled = safe(sample_cpu_usage)
    sampled = sampled if isinstance(sampled, dict) else {}
    keys = ("busy_percent", "busy_cores", "cores", "psi_avg10", "psi_full_avg10",
            "memory_psi_full_avg10", "observed_at", "interval_s")
    usage = {key: sampled.get(key) for key in keys}
    if not sampled:
        usage.update(observed_at=datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
                     psi_avg10=safe(read_cpu_psi),
                     psi_full_avg10=safe(read_cpu_psi, category="full"),
                     memory_psi_full_avg10=safe(read_cpu_psi, resource="memory", category="full"))
    current = sampled.get("oom_counter")
    if current is None:
        current = safe(read_oom_counter)
    previous = safe(store.get_oom_baseline, "/proc")
    usage["oom_kills_delta"] = oom_delta(previous, current)
    # Keep the aggregate counter/baseline for diagnostics only. A session's
    # MemoryMax kill must not create a host-wide durable cooldown. Genuine
    # kernel-classified host OOM is preserved by the trusted hint below.
    if current is not None and not dry_run:
        store.set_oom_baseline("/proc", current)
    cooldown = policy.get("choking_cooldown_seconds", 300)
    hint = trusted_choking_from_env(now_ms=now_ms, cooldown_s=cooldown)
    stored = safe(store.get_fresh_choking, cooldown_s=cooldown, now_ms=now_ms)
    stored = validate_resource_choking(stored, now_ms=now_ms, cooldown_s=cooldown)
    if hint is not None and not dry_run:
        store.record_choking_event(hint["reason"], hint["observed_at"])
    candidates = [value for value in (hint, stored) if value is not None]
    trusted = max(candidates, key=lambda value: _parse_observed_ms(value["observed_at"])) if candidates else None
    return usage, trusted


def check_cpu_choking(cpu_usage=None, policy=None, cores=None,
                      expected_cpu_cores=0.0, ramping_cpu_cores=0.0,
                      trusted_choking=None, now_ms=None):
    """Admission gate: refuse ONLY on fresh choking evidence.

    Returns (allowed: bool, reason: slug|None, admission: fixed DTO dict).
    Unknown/stale telemetry always admits (advisory). Busy/predicted load
    never refuses. Trusted fresh host_oom/host_runtime_choking refuses even
    when busy counters are unknown (independent signals).
    """
    policy = dict(policy or CPU_DEFAULTS)
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if cores is None:
        try:
            cores = os.cpu_count()
        except Exception:
            cores = None
    if type(cores) is not int or cores <= 0:
        cores = None
    verdict = cpu_verdict(cpu_usage, cores if cores is not None else 0,
                          expected_cpu_cores, ramping_cpu_cores, policy,
                          now_ms=now_ms)
    # Unknown core count: cannot compute a limit; telemetry stays advisory.
    if cores is None:
        verdict = {**verdict, "limit_cpu_cores": None,
                   "predicted_over_target": None,
                   "telemetry_state": "unknown",
                   "advisory": verdict.get("advisory") or "cpu_usage_unknown"}
    if verdict.get("reason") is not None:
        admission = {k: verdict.get(k) for k in
                     ("current_cpu_cores", "expected_cpu_cores", "ramping_cpu_cores",
                      "limit_cpu_cores", "target_utilization", "psi_avg10",
                      "psi_full_avg10", "memory_psi_full_avg10", "oom_kills_delta",
                      "observed_at")}
        return False, verdict["reason"], sanitize_cpu_admission(admission)
    choking = validate_resource_choking(
        trusted_choking, now_ms=now_ms,
        cooldown_s=policy.get("choking_cooldown_seconds", 300))
    if choking is not None:
        admission = {k: verdict.get(k) for k in
                     ("current_cpu_cores", "expected_cpu_cores", "ramping_cpu_cores",
                      "limit_cpu_cores", "target_utilization", "psi_avg10",
                      "psi_full_avg10", "memory_psi_full_avg10", "oom_kills_delta",
                      "observed_at")}
        admission["expected_cpu_cores"] = expected_cpu_cores
        admission["ramping_cpu_cores"] = ramping_cpu_cores
        return False, choking["reason"], sanitize_cpu_admission(admission)
    admission = {k: verdict.get(k) for k in
                 ("current_cpu_cores", "expected_cpu_cores", "ramping_cpu_cores",
                  "limit_cpu_cores", "target_utilization", "psi_avg10",
                  "psi_full_avg10", "memory_psi_full_avg10", "oom_kills_delta",
                  "observed_at")}
    return True, None, sanitize_cpu_admission(admission)


def format_cpu_refusal(reason, admission):
    """Exact refusal: slug line then CPU_ADMISSION_JSON object, nothing else."""
    import json as _json
    clean = sanitize_cpu_admission(admission or {})
    if reason not in CPU_REASONS or clean is None:
        raise ValueError("invalid CPU refusal")
    return f"{reason}\nCPU_ADMISSION_JSON {_json.dumps(clean, sort_keys=True)}"


def cpu_current_cores(prev_counter, prev_ms, cur_counter, cur_ms):
    """Proven exact-incarnation current CPU; None on first/reset/out-of-order."""
    for value in (prev_counter, cur_counter):
        if not _finite_nonneg(value):
            return None
    for value in (prev_ms, cur_ms):
        if not _finite_nonneg(value):
            return None
    if not isinstance(prev_counter, (int, float)) or not isinstance(cur_counter, (int, float)):
        return None
    dt = cur_ms - prev_ms
    dc = cur_counter - prev_counter
    if dt <= 0 or dc < 0:
        return None
    return dc / (dt / 1000)


def ramping_cpu_cores(entries, ramp_seconds=180):
    """Advisory remaining ramp: sum max(0, expected - proven fresh current).

    entries: [{state, expected_cpu_cores, current_cpu_cores|None,
    age_seconds|None}]. Queued => 0. Stable residents at/after the ramp => 0
    (host busy already includes them). Young/unknown-age non-queued tasks
    retain max(0, expected - current_or_0). Purely advisory, never admission.
    """
    total = 0.0
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("state") == "queued":
            continue
        age = entry.get("age_seconds")
        if isinstance(age, (int, float)) and not isinstance(age, bool) \
                and math.isfinite(age) and age >= ramp_seconds:
            continue
        expected = entry.get("expected_cpu_cores")
        if not _finite_nonneg(expected):
            continue
        current = entry.get("current_cpu_cores")
        current = current if _finite_nonneg(current) else 0.0
        total += max(0.0, expected - current)
    return total
