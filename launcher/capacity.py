"""Hostwide provider capacity, live occupancy discovery, and atomic reservation.

Enforces:
1. Fixed shared hostwide ZAI concurrency ceiling of 26 across ALL projects
   (including outside projects like ai-shipping-labs), per C2661 / C2664 / C2670.
2. 429 Retry-After backoff cooldown state tracking.
3. Atomic reservation lifecycle with fcntl.flock to prevent concurrent admission races.
4. Provider capacity queries for multi-engine selection and fallback in the maintained CLI.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import pathlib
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MAX_CONCURRENT_ZAI = 26
DEFAULT_COOLDOWN_SEC = 60.0
RESERVATION_EXPIRY_SEC = 300.0


class CapacityError(Exception):
    """Base exception for provider capacity errors."""
    pass


class ConcurrencyLimitExceeded(CapacityError):
    """Raised when hostwide concurrent processes meet or exceed approved ceiling."""
    pass


class CooldownActive(CapacityError):
    """Raised when provider is inside an active backoff cooldown period."""
    pass


def get_live_zai_pids() -> List[int]:
    """Scan host for live zcode-cli backend processes across all user projects.

    Checks both pgrep and /proc to discover all active processes executing
    zcode-cli anywhere on the host (internal or external projects).
    """
    pids = []
    try:
        res = subprocess.run(
            ["pgrep", "-f", "zcode-cli"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if os.path.exists(f"/proc/{pid}"):
                        pids.append(pid)
    except Exception:
        pass

    if not pids:
        try:
            for entry in pathlib.Path("/proc").iterdir():
                if entry.name.isdigit():
                    try:
                        cmdline = (entry / "cmdline").read_bytes().decode("utf-8", errors="ignore")
                        if "zcode-cli" in cmdline:
                            pids.append(int(entry.name))
                    except Exception:
                        pass
        except Exception:
            pass

    return sorted(set(pids))


def _state_paths(config_dir: Optional[pathlib.Path] = None) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    base = config_dir or pathlib.Path(os.path.expanduser("~/.config/agent-quota-launcher"))
    base.mkdir(parents=True, exist_ok=True)
    lock_file = base / "provider_capacity.lock"
    cooldown_file = base / "provider_cooldown.json"
    res_file = base / "provider_reservations.json"
    return lock_file, cooldown_file, res_file


def check_cooldown(provider: str = "zai", config_dir: Optional[pathlib.Path] = None,
                   now_ts: Optional[float] = None) -> Tuple[bool, float]:
    """Check if provider is in active backoff cooldown. Returns (is_active, remaining_seconds)."""
    _, cooldown_file, _ = _state_paths(config_dir)
    ts = now_ts or time.time()
    if not cooldown_file.exists():
        return False, 0.0
    try:
        data = json.loads(cooldown_file.read_text(encoding="utf-8"))
        prov_data = data.get(provider, {})
        until = prov_data.get("cooldown_until", 0.0)
        if ts < until:
            return True, until - ts
    except Exception:
        pass
    return False, 0.0


def record_429_event(provider: str = "zai", retry_after_sec: float = DEFAULT_COOLDOWN_SEC,
                     reason: str = "429 Rate Limit", config_dir: Optional[pathlib.Path] = None):
    """Record a 429 rate limit event with backoff cooldown for a provider."""
    lock_file, cooldown_file, _ = _state_paths(config_dir)
    now_ts = time.time()
    with open(lock_file, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            current = {}
            if cooldown_file.exists():
                try:
                    current = json.loads(cooldown_file.read_text(encoding="utf-8"))
                except Exception:
                    current = {}
            current[provider] = {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "reason": reason,
                "retry_after_sec": retry_after_sec,
                "cooldown_until": now_ts + retry_after_sec,
            }
            tmp = cooldown_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
            tmp.replace(cooldown_file)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load_reservations(res_file: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    if not res_file.exists():
        return {}
    try:
        return json.loads(res_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_reservations(res_file: pathlib.Path, data: Dict[str, Dict[str, Any]]):
    tmp = res_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(res_file)


def check_provider_capacity(
    provider: str,
    max_cap: Optional[int] = None,
    config_dir: Optional[pathlib.Path] = None,
    now_ts: Optional[float] = None,
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """Check provider capacity and cooldown without reserving a slot.

    Returns (can_admit, rejection_reason, info_dict).
    """
    ts = now_ts or time.time()
    lock_file, _, res_file = _state_paths(config_dir)

    # 1. Cooldown check
    is_cooling, remaining = check_cooldown(provider, config_dir=config_dir, now_ts=ts)
    if is_cooling:
        return False, f"{provider} 429 cooldown active ({round(remaining, 1)}s remaining)", {
            "cooldown": True,
            "remaining_seconds": remaining,
        }

    # 2. Frozen providers
    if provider == "grok":
        return False, "grok frozen per policy (cutoff <= 5%)", {"frozen": True}

    if provider == "anthropic":
        return False, "anthropic Sonnet 5.5 route unverified: no direct quse route", {"unverified": True}

    # 3. ZAI ceiling check
    if provider == "zai":
        ceiling = max_cap if max_cap is not None else DEFAULT_MAX_CONCURRENT_ZAI
        with open(lock_file, "a") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                raw_res = _load_reservations(res_file)
                zai_res = {
                    tok: info for tok, info in raw_res.items()
                    if info.get("provider") == "zai" and ts < info.get("expires_at", 0)
                }
                live_pids = get_live_zai_pids()
                live_count = len(live_pids)
                reserved_count = len(zai_res)
                total_active = live_count + reserved_count

                info = {
                    "provider": "zai",
                    "live_pids": live_pids,
                    "live_count": live_count,
                    "reserved_count": reserved_count,
                    "total_active": total_active,
                    "ceiling": ceiling,
                    "headroom": max(0, ceiling - total_active),
                }

                if total_active >= ceiling:
                    reason = (
                        f"zai hostwide capacity ceiling ({ceiling}) reached: "
                        f"{live_count} live processes, {reserved_count} reserved (total {total_active} >= {ceiling})"
                    )
                    return False, reason, info

                return True, None, info
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # 4. Explicit max_cap ceiling check (when max_cap is explicitly specified)
    if max_cap is not None:
        ceiling = max_cap
        with open(lock_file, "a") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                raw_res = _load_reservations(res_file)
                prov_res = {
                    tok: info for tok, info in raw_res.items()
                    if info.get("provider") == provider and ts < info.get("expires_at", 0)
                }
                reserved_count = len(prov_res)
                if reserved_count >= ceiling:
                    reason = (
                        f"{provider} hostwide capacity ceiling ({ceiling}) reached: "
                        f"{reserved_count} reserved (total {reserved_count} >= {ceiling})"
                    )
                    return False, reason, {
                        "provider": provider,
                        "reserved_count": reserved_count,
                        "ceiling": ceiling,
                        "headroom": 0,
                    }
                return True, None, {
                    "provider": provider,
                    "reserved_count": reserved_count,
                    "ceiling": ceiling,
                    "headroom": max(0, ceiling - reserved_count),
                }
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # Default for other providers (codex, antigravity, opencode)
    return True, None, {"provider": provider, "admitted": True}


def reserve_provider_slot(
    provider: str,
    task_id: str,
    max_cap: Optional[int] = None,
    config_dir: Optional[pathlib.Path] = None,
    now_ts: Optional[float] = None,
) -> str:
    """Atomically reserve a provider slot.

    Raises CooldownActive or ConcurrencyLimitExceeded if capacity unavailable.
    Returns reservation token string.
    """
    ts = now_ts or time.time()
    lock_file, _, res_file = _state_paths(config_dir)

    is_cooling, remaining = check_cooldown(provider, config_dir=config_dir, now_ts=ts)
    if is_cooling:
        raise CooldownActive(f"{provider} reservation rejected: 429 cooldown active ({round(remaining, 1)}s remaining)")

    with open(lock_file, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            reservations = _load_reservations(res_file)
            cleaned = {tok: info for tok, info in reservations.items() if ts < info.get("expires_at", 0)}

            if provider == "zai":
                ceiling = max_cap if max_cap is not None else DEFAULT_MAX_CONCURRENT_ZAI
                zai_res = {tok: info for tok, info in cleaned.items() if info.get("provider") == "zai"}
                live_pids = get_live_zai_pids()
                live_count = len(live_pids)
                reserved_count = len(zai_res)
                total_active = live_count + reserved_count

                if total_active >= ceiling:
                    raise ConcurrencyLimitExceeded(
                        f"Hostwide ZAI concurrency ceiling ({ceiling}) reached! "
                        f"Live processes: {live_count}, Pending reservations: {reserved_count}. "
                        f"Total active: {total_active} >= {ceiling}. Reservation rejected."
                    )

            if max_cap is not None and provider != "zai":
                ceiling = max_cap
                prov_res = {tok: info for tok, info in cleaned.items() if info.get("provider") == provider}
                if len(prov_res) >= ceiling:
                    raise ConcurrencyLimitExceeded(
                        f"Hostwide {provider} capacity ceiling ({ceiling}) reached! "
                        f"Pending reservations: {len(prov_res)} >= {ceiling}. Reservation rejected."
                    )

            token = f"slot-{provider}-{uuid.uuid4().hex[:12]}"
            cleaned[token] = {
                "provider": provider,
                "task_id": task_id,
                "created_at": ts,
                "expires_at": ts + RESERVATION_EXPIRY_SEC,
            }
            _save_reservations(res_file, cleaned)
            return token
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def release_provider_slot(
    token: str,
    config_dir: Optional[pathlib.Path] = None,
):
    """Release a held reservation slot on completion, timeout, or failure."""
    if not token:
        return
    lock_file, _, res_file = _state_paths(config_dir)
    with open(lock_file, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            reservations = _load_reservations(res_file)
            if token in reservations:
                del reservations[token]
                _save_reservations(res_file, reservations)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def provider_reservation(
    provider: str,
    task_id: str,
    max_cap: Optional[int] = None,
    config_dir: Optional[pathlib.Path] = None,
):
    """Context manager for atomic provider slot reservation and release."""
    token = reserve_provider_slot(provider, task_id, max_cap=max_cap, config_dir=config_dir)
    try:
        yield token
    finally:
        release_provider_slot(token, config_dir=config_dir)
