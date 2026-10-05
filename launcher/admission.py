"""Quota admission: fetch and validate fresh quse evidence, fail closed.

Adapter-backed routes for v0.1 are grok, antigravity and zai (ordinary paid
allowance). Codex requires the protected wrapper scripts/launch-codex.sh, which
this repo does not ship, so codex is explicitly unsupported here; its window
gate is still evaluated and recorded so the <=15%/unknown fail-closed rule is
testable and visible in rejection reasons.
"""
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ADAPTER_ROUTES = ("grok", "antigravity", "zai")
ADAPTER_MODELS = {
    "grok": "grok-4.6",
    "antigravity": "gemini-3.1-pro-high",
    "zai": "glm-5.3-flash",
}
CODEX_MIN_REMAINING = 15.0
GROK_MIN_REMAINING = 5.0

# Promotion stays disabled until a verified ZCode >=3.10 GLM-5.3-Flash
# subscription route exists; even inside the campaign window the multiplier is
# 1.0 and the conservative cutoff bounds any future reliance.
PROMO_MULTIPLIER = 1.0
PROMO_CUTOFF_UTC = "2026-10-06T16:00:00+00:00"
PROMO_WINDOW_UTC = (15, 1)


def fetch_quse():
    """Run quse --json and parse the first JSON document from stdout.

    quse can emit trailing garbage on stdout (observed 2026-10-04: a Go panic
    trace after the complete JSON document). Salvage the first complete JSON
    value; truncated or unparsable output fails closed.
    """
    res = subprocess.run(["quse", "--json"], capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise ValueError(f"Failed to fetch quse (rc={res.returncode})")
    text = res.stdout.lstrip()
    if not text.startswith("{"):
        raise ValueError("quse stdout does not start with a JSON object")
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid quse JSON output: {e}")
    if not isinstance(obj, dict):
        raise ValueError("Invalid quse structure")
    return obj


def parse_iso(s):
    """Parse an ISO timestamp; returns an aware datetime or None.

    A string without an offset yields None: guessing UTC would fabricate
    urgency, so the caller must fail that window closed instead.
    """
    if not s or not isinstance(s, str):
        return None
    text = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def _percent(win):
    """Return a finite float percent, or None when unknown/invalid."""
    perc = win.get("percent_remaining")
    if isinstance(perc, bool) or not isinstance(perc, (int, float)):
        return None
    if not math.isfinite(perc):
        return None
    return float(perc)


def codex_gate_reason(route, now):
    """Codex fail-closed gate: any window <=15% remaining, unknown or stale
    blocks the route. Returns a rejection reason or None when all known."""
    windows = route.get("windows")
    if not isinstance(windows, dict) or not windows:
        return "codex fail-closed: no window evidence"
    for win_name, win in windows.items():
        if not isinstance(win, dict):
            return f"codex fail-closed: malformed window {win_name}"
        perc = _percent(win)
        if perc is None:
            return f"codex fail-closed: unknown window reading ({win_name})"
        reset_time = parse_iso(win.get("reset_at"))
        if reset_time is None:
            return f"codex fail-closed: unknown window timezone/reset ({win_name})"
        if reset_time < now:
            return f"codex fail-closed: stale window evidence ({win_name})"
        if perc <= CODEX_MIN_REMAINING:
            return f"codex window {win_name} <= {CODEX_MIN_REMAINING:g}% remaining"
    return None


def _validate_route(name, route, now, task_requirements, check_capacity=False, config_dir=None):
    """Validate one route. Returns (candidate|None, rejection_reason|None)."""
    provider = "antigravity" if name == "gemini" else name

    if provider == "codex":
        gate = codex_gate_reason(route, now)
        if gate:
            return None, gate
        wrapper_path = Path("scripts/launch-codex.sh")
        if not wrapper_path.exists():
            return None, ("codex explicitly unsupported in launcher v0.1 "
                          "(protected wrapper scripts/launch-codex.sh not configured)")

    if provider not in ADAPTER_ROUTES:
        return None, "Unsupported route explicitly blocked"

    if route.get("error"):
        return None, f"Error: {route.get('error')}"

    if route.get("status") != "ok":
        return None, f"Status not ok: {route.get('status')}"

    details = route.get("details") if isinstance(route.get("details"), dict) else {}
    if details.get("limit_reached"):
        return None, "Limit reached"

    if provider == "grok" and details.get("has_grok_code_access") is not True:
        # Grok entitlement evidence, per SPEC: quota availability alone is
        # insufficient.
        return None, "Missing Grok entitlement evidence (has_grok_code_access)"

    windows = route.get("windows")
    if not isinstance(windows, dict) or not windows:
        return None, "Missing route evidence (no windows)"

    min_rem = 100.0
    min_hours = None
    exhausted = False
    valid_evidence = False
    stale_evidence = False

    for win_name, win in windows.items():
        if not isinstance(win, dict):
            continue
        perc = _percent(win)
        reset_time = parse_iso(win.get("reset_at"))
        if perc is None or reset_time is None:
            # Unknown percent or missing/naive reset timezone: this window is
            # not evidence and adds no reset bonus.
            continue
        if reset_time < now:
            stale_evidence = True
            continue

        valid_evidence = True
        if perc <= 0:
            exhausted = True
        if perc < min_rem:
            min_rem = perc
        hours = (reset_time - now).total_seconds() / 3600.0
        if min_hours is None or hours < min_hours:
            min_hours = hours

    if stale_evidence and not valid_evidence:
        return None, "Stale route evidence"
    if not valid_evidence:
        return None, ("Missing valid route evidence (nonfinite percent, missing/naive "
                      "reset timezone, or stale reset_at)")
    if exhausted:
        return None, "Quota exhausted"
    if provider == "grok" and min_rem <= GROK_MIN_REMAINING:
        return None, f"Grok window <= {GROK_MIN_REMAINING:g}% remaining (cutoff policy)"

    if check_capacity:
        from launcher.capacity import check_provider_capacity
        can_admit, cap_reason, _ = check_provider_capacity(provider, config_dir=config_dir)
        if not can_admit:
            return None, cap_reason

    health = 1.0 if route.get("status") == "ok" else None
    task_fit = _task_fit(provider, task_requirements)

    return {
        "name": name,
        "provider": provider,
        "model": ADAPTER_MODELS[provider],
        "health": health,
        "remaining_fraction": min_rem / 100.0,
        "hours_to_reset": min_hours,
        "task_fit": task_fit,
        "promo_multiplier": PROMO_MULTIPLIER,
    }, None


def _task_fit(provider, task_requirements):
    """Known (1.0/0.0) only against declared model requirements; unknown (None)
    otherwise so ranking weights the route at 0 instead of fabricating fit."""
    if not isinstance(task_requirements, dict):
        return None
    req_provider = task_requirements.get("provider")
    if req_provider and provider != req_provider:
        return 0.0
    providers = task_requirements.get("providers")
    if isinstance(providers, (list, tuple)):
        if provider not in providers:
            return 0.0
    models = task_requirements.get("models")
    if isinstance(models, (list, tuple)):
        if ADAPTER_MODELS[provider] not in models:
            return 0.0
    return 1.0


def validate_quse(quse_data, task_requirements=None, check_capacity=False, config_dir=None):
    """Validate all routes; one broken route must not crash the others.

    Returns (valid_routes, rejections). Routes with unknown health or task_fit
    are returned with those fields None; ranking treats unknown as weight 0.
    """
    if not isinstance(quse_data, dict):
        raise ValueError("Invalid quse structure")

    valid_routes = []
    rejections = {}
    now = datetime.now(timezone.utc)

    for name, route in quse_data.items():
        try:
            if not isinstance(route, dict):
                rejections[name] = "Malformed route record"
                continue
            candidate, reason = _validate_route(
                name, route, now, task_requirements,
                check_capacity=check_capacity, config_dir=config_dir
            )
            if candidate:
                valid_routes.append(candidate)
            else:
                # Provider errors can embed full crash dumps; keep records bounded.
                rejections[name] = (reason or "")[:300]
        except Exception as e:  # per-route isolation
            rejections[name] = f"Route evaluation failed: {type(e).__name__}: {e}"

    return valid_routes, rejections
