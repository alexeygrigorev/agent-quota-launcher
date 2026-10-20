"""Quota admission: fetch and validate fresh quse evidence, fail closed.

Adapter-backed routes for v0.1 are grok, antigravity, zai (ordinary paid
allowance) and opencode (Go remaining via quse key go, exact subscription
model opencode-go/muse-spark-1.3-contributor). Codex requires the protected
wrapper scripts/launch-codex.sh, which this repo does not ship, so codex is
explicitly unsupported here; its window gate is still evaluated and recorded
so the <=15%/unknown fail-closed rule is testable and visible in rejection
reasons. Free OpenCode provider ids (opencode/*-free) are not mapped; Go
remaining does not prove those routes. GLM models never route to opencode
(enforced in launcher.launch.build_adapter_argv).
"""
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ADAPTER_ROUTES = ("grok", "antigravity", "zai", "opencode", "codex")
ADAPTER_MODELS = {
    "grok": "grok-4.6",
    "antigravity": "gemini-3.8-flash-high",
    "zai": "glm-5.3-flash",
    "opencode": "opencode-go/muse-spark-1.3-contributor",
    "codex": "gpt-6.1-sol",
}
ADAPTER_SUPPORTED_MODELS = {
    "grok": ("grok-4.6",),
    "antigravity": ("gemini-3.8-flash-high", "gemini-4-argon-high"),
    "zai": ("glm-5.3-flash",),
    "opencode": ("opencode-go/muse-spark-1.3-contributor",),
    "codex": ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-6-luna"),
}
CODEX_MIN_REMAINING = 15.0


def get_codex_wrapper(config_dir=None):
    env_wrapper = os.environ.get("CODEX_LAUNCH_WRAPPER")
    if env_wrapper and os.path.isfile(env_wrapper) and os.access(env_wrapper, os.X_OK):
        return str(Path(env_wrapper).resolve())
    for cand in (Path("scripts/launch-codex.sh"), Path(config_dir or "") / "launch-codex.sh"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand.resolve())
    return None

# Provider window metadata. Codex plans have no 5-hour (or monthly) window:
# quse reports those slots with a null reading AND a null reset. Such a slot is
# "not applicable", not "unknown"; Codex is judged on the windows it has. A
# slot that carries any reading/reset is still evaluated, and the weekly
# window stays mandatory, so a missing 7d reading still fails closed.
CODEX_REQUIRED_WINDOWS = ("7d", "weekly")
CODEX_NOT_APPLICABLE_WINDOWS = ("5h", "monthly")

# Task kinds each adapter route is a good fit for. Used when a task declares a
# kind (directly as model_requirements["kind"] or via its task profile) instead
# of pinning a provider/model. Explicit provider/model pins stay exclusions.
GENERAL_TASK_KINDS = ("coding", "implementation", "docs", "review")
ROOT_TASK_KINDS = ("root-run",)
PROVIDER_TASK_KINDS = {
    "grok": GENERAL_TASK_KINDS + ROOT_TASK_KINDS,
    "antigravity": GENERAL_TASK_KINDS + ROOT_TASK_KINDS,
    "zai": GENERAL_TASK_KINDS + ROOT_TASK_KINDS,
    "opencode": GENERAL_TASK_KINDS + ROOT_TASK_KINDS,
}
PROFILE_TASK_KINDS = {
    "model-task": "implementation",
    "model-review": "review",
    "root-run": "root-run",
}


def task_requirements_for(payload):
    """model_requirements plus the task kind implied by the payload profile."""
    reqs = payload.get("model_requirements") if isinstance(payload, dict) else None
    kind = PROFILE_TASK_KINDS.get(payload.get("profile")) if isinstance(payload, dict) else None
    if kind is None:
        return reqs
    if reqs is None:
        return {"kind": kind}
    if isinstance(reqs, dict) and "kind" not in reqs:
        return dict(reqs, kind=kind)
    return reqs
GROK_MIN_REMAINING = 5.0

# Promotion stays disabled until a verified ZCode >=3.10 GLM-5.3-Flash
# subscription route exists; even inside the campaign window the multiplier is
# 1.0 and the conservative cutoff bounds any future reliance.
PROMO_MULTIPLIER = 1.0
PROMO_CUTOFF_UTC = "2026-10-06T16:00:00+00:00"
PROMO_WINDOW_UTC = (15, 1)


def fetch_managed_quse(account_label, provider=None, config_dir=None):
    """Fetch fresh quota context for a named centrally managed account."""
    hub_config = Path(os.environ.get("AGENT_HUB_CONFIG_DIR", Path.home() / ".config" / "agent-hub"))
    managed_dir = hub_config / "managed-engines"
    state_file = managed_dir / "state.json"
    if not state_file.is_file():
        raise ValueError(f"Managed engines state file not found for account {account_label}")
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        raise ValueError(f"Failed to read managed engines state: {e}")
    record = next((b for b in state if b.get("label") == account_label), None)
    if not record:
        raise ValueError(f"Managed account {account_label!r} not found in managed state")
    rev = record.get("revision")
    prov = provider or record.get("provider")
    dest = managed_dir / f"{account_label}-{rev}"
    bundle_file = dest / "bundle.json"
    if not bundle_file.is_file():
        raise ValueError(f"Managed bundle not found for account {account_label!r}")
    try:
        with open(bundle_file, "r", encoding="utf-8") as f:
            bundle = json.load(f)
    except Exception as e:
        raise ValueError(f"Failed to read bundle for account {account_label!r}: {e}")

    home = dest / "home"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        CODEX_HOME=str(home / ".codex"),
        ZCODE_HOME=str(home / ".zcode"),
    )
    if bundle.get("env"):
        env.update(bundle["env"])

    quse_cmd = ["quse", "--json"] if not prov else ["quse", prov, "--json"]
    res = subprocess.run(quse_cmd, env=env, capture_output=True, text=True, timeout=30)
    if res.returncode != 0 and not res.stdout:
        raise ValueError(f"Failed to fetch quse for managed account {account_label!r} (rc={res.returncode})")
    text = res.stdout.lstrip()
    if not text.startswith("{"):
        raise ValueError(f"quse stdout for managed account {account_label!r} does not start with JSON")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid quse JSON output for managed account {account_label!r}: {e}")
    if not isinstance(obj, dict):
        raise ValueError(f"Invalid quse structure for managed account {account_label!r}")
    return obj


def fetch_quse(account_label=None, provider=None, config_dir=None):
    """Run quse --json and parse the first JSON document from stdout.

    When account_label is provided, probes quota strictly within that managed
    account's isolated credential environment. Never falls back to default operator quota.
    """
    if account_label:
        return fetch_managed_quse(account_label, provider=provider, config_dir=config_dir)
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
        
    has_weekly = False
    
    for win_name, win in windows.items():
        if not isinstance(win, dict):
            return f"codex fail-closed: malformed window {win_name}"
            
        if win.get("present") is False:
            continue

        if (win_name in CODEX_NOT_APPLICABLE_WINDOWS
                and win.get("percent_remaining") is None
                and win.get("reset_at") is None):
            continue  # window does not exist for this plan

        if win_name in CODEX_REQUIRED_WINDOWS:
            has_weekly = True
            
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
            
    if not has_weekly:
        return "codex fail-closed: missing required weekly window"
        
    return None


def _validate_route(name, route, now, task_requirements, check_capacity=False, config_dir=None):
    """Validate one route. Returns (candidate|None, rejection_reason|None)."""
    if name == "gemini":
        provider = "antigravity"
    elif name in ("go", "opencode", "opencode-go"):
        provider = "opencode"
    else:
        provider = name

    if provider == "codex":
        gate = codex_gate_reason(route, now)
        if gate:
            return None, gate
        wrapper_path = get_codex_wrapper(config_dir=config_dir)
        if not wrapper_path:
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
    task_fit, model = _eval_task_fit(provider, task_requirements)

    return {
        "name": name,
        "provider": provider,
        "model": model,
        "health": health,
        "remaining_fraction": min_rem / 100.0,
        "hours_to_reset": min_hours,
        "task_fit": task_fit,
        "promo_multiplier": PROMO_MULTIPLIER,
    }, None


def _eval_task_fit(provider, task_requirements):
    """Evaluate task fit and model selection for a provider.

    Returns (task_fit, selected_model).
    task_fit is:
      - None when no recognized requirements are declared (or task_requirements is None/empty)
      - 0.0 when requirements conflict, are malformed, or do not match provider/supported models
      - 1.0 when declared requirements are matched by this provider

    selected_model is:
      - The matched supported model if model requirements matched
      - ADAPTER_MODELS[provider] (default model) otherwise (including when task_fit is 0.0 or None)
    """
    default_model = ADAPTER_MODELS.get(provider)

    if not isinstance(task_requirements, dict) or not task_requirements:
        return None, default_model

    recognized_keys = {"provider", "providers", "model", "models", "kind"}
    declared_keys = recognized_keys.intersection(task_requirements.keys())
    if not declared_keys:
        return None, default_model

    # Validate provider requirements if present
    req_provider = None
    if "provider" in task_requirements:
        raw_p = task_requirements["provider"]
        if not isinstance(raw_p, str) or isinstance(raw_p, bool) or not raw_p.strip():
            return 0.0, default_model
        req_provider = raw_p.strip()

    req_providers = None
    if "providers" in task_requirements:
        raw_ps = task_requirements["providers"]
        if (
            not isinstance(raw_ps, (list, tuple))
            or isinstance(raw_ps, (str, bytes))
            or len(raw_ps) == 0
            or not all(
                isinstance(x, str)
                and not isinstance(x, bool)
                and x.strip()
                for x in raw_ps
            )
        ):
            return 0.0, default_model
        req_providers = [x.strip() for x in raw_ps]

    # Check provider conflicts
    if req_provider is not None and req_providers is not None:
        if req_provider not in req_providers:
            return 0.0, default_model

    # Check match against this provider
    if req_provider is not None and provider != req_provider:
        return 0.0, default_model
    if req_providers is not None and provider not in req_providers:
        return 0.0, default_model

    if "kind" in task_requirements:
        raw_k = task_requirements["kind"]
        if not isinstance(raw_k, str) or not raw_k.strip():
            return 0.0, default_model
        if raw_k.strip() not in PROVIDER_TASK_KINDS.get(provider, ()):
            return 0.0, default_model

    # Validate model requirements if present
    req_m = None
    if "model" in task_requirements:
        raw_m = task_requirements["model"]
        if not isinstance(raw_m, str) or isinstance(raw_m, bool) or not raw_m.strip():
            return 0.0, default_model
        req_m = raw_m.strip()

    req_models = None
    if "models" in task_requirements:
        raw_ms = task_requirements["models"]
        if (
            not isinstance(raw_ms, (list, tuple))
            or isinstance(raw_ms, (str, bytes))
            or len(raw_ms) == 0
            or not all(
                isinstance(x, str)
                and not isinstance(x, bool)
                and x.strip()
                for x in raw_ms
            )
        ):
            return 0.0, default_model
        req_models = [x.strip() for x in raw_ms]

    # Check conflicting / ambiguous dual specification
    if req_m is not None and req_models is not None:
        if req_m not in req_models:
            return 0.0, default_model
        target_models = [req_m]
    elif req_m is not None:
        target_models = [req_m]
    elif req_models is not None:
        target_models = req_models
    else:
        # Provider-only and/or kind requirement declared and matched
        return 1.0, default_model

    supported = ADAPTER_SUPPORTED_MODELS.get(provider, (default_model,))
    for m in target_models:
        if m in supported:
            return 1.0, m

    return 0.0, default_model


def _task_fit(provider, task_requirements):
    """Known (1.0/0.0) only against declared model requirements; unknown (None)
    otherwise so ranking weights the route at 0 instead of fabricating fit."""
    fit, _ = _eval_task_fit(provider, task_requirements)
    return fit


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
