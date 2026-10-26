"""Task admission profiles for agent-quota-launcher.

Formalizes timeout and resource bounds per task category to prevent
premature timeouts on model code reviews and long-running test suites,
while maintaining fast fail-fast deadlines on deterministic checks.
Incorporates measured historical durations (e.g. model cleanups taking 173-346s).
"""

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

DEFAULT_TIMEOUT_SEC: float = 300.0
DEFAULT_MEMORY_MB: int = 768

ROOT_RUN_PROFILE_NAME = "root-run"

# Environment keys a root-trusted job may carry through the private 0600
# EnvironmentFile. HR_* are the CAG script hooks (validated HR_RUN_ID maps to
# HUB_EXPECTED_RUN_ID, HR_HUB_JOB_ID to HUB_JOB_ID, HR_HUB_LEASE_EPOCH to
# HUB_LEASE_EPOCH); HUB_* are the agreed Hub job_env fields. Nothing else may be
# transported, so an arbitrary command/profile/env cannot widen the surface.
ROOT_RUN_ENV_ALLOWLIST: tuple[str, ...] = (
    "HR_RUN_ID",
    "HR_HUB_JOB_ID",
    "HR_HUB_LEASE_EPOCH",
    "HUB_JOB_ID",
    "HUB_URL",
    "HUB_LEASE_EPOCH",
    "HUB_JOB_TOKEN",
    "HUB_LAUNCHER_TASK_ID",
    "HUB_EXPECTED_RUN_ID",
    "HUB_FIRST_ACTION_FILE",
    "HUB_START_ACK_FILE",
    "HUB_HOST_ID",
    "HUB_LAUNCHER_UNIT",
)

# Root-trusted identity keys that must be present and must equal the trusted
# evidence identity: a borrowed or mismatched identity is denied.
ROOT_RUN_IDENTITY_ENV_KEYS: tuple[str, ...] = (
    "HR_RUN_ID",
    "HR_HUB_JOB_ID",
    "HR_HUB_LEASE_EPOCH",
)

# Names a private env may never carry: they would subvert the prelude wrapper,
# the interpreter start-up path, or the unit's own isolation. R4 adds the
# PYTHON prefix (PYTHONUSERBASE, PYTHONBREAKPOINT, PYTHONPATH, etc. would
# alter sys.executable start-up under the prelude wrapper).
RESERVED_ENV_KEYS = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONBREAKPOINT",
        "BASH_ENV",
        "ENV",
        "IFS",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "SYSTEMD_EXEC_PID",
        "NOTIFY_SOCKET",
    }
)
RESERVED_ENV_PREFIXES: tuple[str, ...] = ("LD_", "SYSTEMD_", "PYTHON")

ENV_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

# Roles allowed to present trusted root-job admission evidence.
ROOT_RUN_AUTH_ROLES: tuple[str, ...] = ("root", "hub-root-host")

# Namespace policies: generic workers keep a private PID namespace and masked
# aplexer/systemd/Bus; only an explicitly trusted root-run admission may ask for
# the host PID namespace and its own systemd/Bus.
NAMESPACE_POLICY_GENERIC = "generic-isolated"
NAMESPACE_POLICY_ROOT_TRUSTED = "root-trusted-candidate"

MAX_ENV_FILE_BYTES = 64 * 1024

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_FULL_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceValidationError(ValueError):
    """Raised when a declared git source commit or repository state cannot be validated."""
    pass


TASK_PROFILES: Dict[str, Dict[str, Any]] = {
    "model-review": {
        "timeout": 900.0,
        "memory_mb": 768,
        "description": "Full-suite test execution and independent LLM code review",
    },
    "model-task": {
        "timeout": 600.0,
        "memory_mb": 768,
        "description": "Autonomous model agent reasoning, tool use, and artifact drafting",
    },
    "model-cleanup": {
        "timeout": 600.0,
        "memory_mb": 768,
        "description": "Model-directed disk pressure mitigation and audit pruning (historical 173-346s)",
    },
    "deterministic-check": {
        "timeout": 60.0,
        "memory_mb": 256,
        "description": "Fast deterministic test runner, linter, or syntax verification",
    },
    "deterministic-cleanup": {
        "timeout": 120.0,
        "memory_mb": 256,
        "description": "Bounded deterministic shell/file pruning without model invocation",
    },
    "cleanup": {
        "timeout": 120.0,
        "memory_mb": 256,
        "description": "Generic deterministic cleanup alias",
    },
    "root-run": {
        "timeout": 1500.0,
        "memory_mb": 1500,
        "description": "Bounded root-run execution profile with mandatory trusted pre-action receiver guard and private EnvironmentFile transport",
    },
    "default": {
        "timeout": DEFAULT_TIMEOUT_SEC,
        "memory_mb": DEFAULT_MEMORY_MB,
        "description": "Standard general task execution profile (non-model tasks)",
    },
}


def get_profile(name: Optional[str]) -> Dict[str, Any]:
    """Retrieve profile definition by name.
    
    Fail-closed behavior (F2 / C3014):
    - None or empty string maps to 'default' (absent declaration).
    - An unknown explicit name raises ValueError to prevent silent fallback to 300s.
    """
    if not name:
        return TASK_PROFILES["default"]
    if name not in TASK_PROFILES:
        raise ValueError(
            f"Unknown task profile '{name}'. Valid profiles: {sorted(list(TASK_PROFILES.keys()))}"
        )
    return TASK_PROFILES[name]


def require_full_sha(value: Any, field_name: str = "sha") -> str:
    """Normalize a claimed digest to a full 40-character lowercase hex SHA.

    Fail closed: a truncated, uppercase-padded, non-hex or empty claim is
    rejected instead of being silently ignored. An exact script or source
    digest is only evidence when the whole SHA is present.
    """
    if not isinstance(value, str):
        raise SourceValidationError(
            f"{field_name} must be a 40-character lowercase hex SHA string, got {type(value).__name__}"
        )
    candidate = value.strip().lower()
    if not _FULL_SHA_RE.match(candidate):
        raise SourceValidationError(
            f"{field_name} must be exactly 40 lowercase hex characters (full SHA), got "
            f"{len(candidate)} character(s)"
        )
    return candidate


def require_full_sha256(value: Any, field_name: str = "sha256") -> str:
    """Normalize a claimed content digest to a full 64-character lowercase hex SHA-256.

    Same fail-closed rule as require_full_sha: an exact script digest is only
    evidence when the whole digest is present.
    """
    if not isinstance(value, str):
        raise SourceValidationError(
            f"{field_name} must be a 64-character lowercase hex SHA-256 string, got {type(value).__name__}"
        )
    candidate = value.strip().lower()
    if not _FULL_SHA256_RE.match(candidate):
        raise SourceValidationError(
            f"{field_name} must be exactly 64 lowercase hex characters (full SHA-256), got "
            f"{len(candidate)} character(s)"
        )
    return candidate


def is_reserved_env_key(key: Any) -> bool:
    """True when a private env key is never allowed to be transported."""
    if not isinstance(key, str) or not key:
        return True
    if key in RESERVED_ENV_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in RESERVED_ENV_PREFIXES)


def is_root_run_env_key_allowed(key: Any) -> bool:
    """True only for keys on the agreed root-run private-env allowlist."""
    return isinstance(key, str) and key in ROOT_RUN_ENV_ALLOWLIST


def namespace_policy_for_profile(profile_name: Optional[str]) -> str:
    """Declare the namespace policy implied by a task profile.

    Unknown or absent profiles fail closed to the generic isolated policy.
    """
    name = (profile_name or "").strip()
    if not name:
        return NAMESPACE_POLICY_GENERIC
    if name not in TASK_PROFILES:
        raise ValueError(
            f"Unknown task profile '{profile_name}'. Valid profiles: {sorted(list(TASK_PROFILES.keys()))}"
        )
    if name == ROOT_RUN_PROFILE_NAME:
        return NAMESPACE_POLICY_ROOT_TRUSTED
    return NAMESPACE_POLICY_GENERIC


def validate_source_commit(
    repo_path: str | Path,
    commit_ref: str,
    expected_full_sha: Optional[str] = None,
    require_clean: bool = False,
) -> Dict[str, Any]:
    """Validate declared git source ref via rev-parse before admission (C2999, C3002).
    
    Guarantees:
    1. Commit ref resolves to a valid 40-character SHA commit object.
    2. If expected_full_sha is provided, it must itself be a full 40-character
       hex SHA and must match exactly (truncated or malformed claims raise
       instead of silently skipping the comparison).
    3. If require_clean is True, verifies git worktree has no uncommitted changes.
    4. Returns an immutable source receipt record.
    """
    p = Path(repo_path).resolve()
    if not p.is_dir():
        raise SourceValidationError(f"Target repository/worktree path does not exist: {p}")

    ref = (commit_ref or "").strip()
    if not ref:
        raise SourceValidationError("commit_ref cannot be empty")

    res = subprocess.run(
        ["git", "-C", str(p), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        err = res.stderr.strip() or "commit object not found"
        raise SourceValidationError(
            f"Invalid or unresolvable commit ref '{ref}' in {p}: {err}"
        )

    resolved_sha = res.stdout.strip()
    if len(resolved_sha) != 40 or not re.match(r"^[0-9a-f]{40}$", resolved_sha):
        raise SourceValidationError(
            f"rev-parse produced invalid commit SHA '{resolved_sha}' for ref '{ref}'"
        )

    # F6: compare every non-None value, including falsy ''/0/list/empty.
    # Only None means "no expectation". A falsy non-None claim is malformed
    # and must raise, never skip silently.
    if expected_full_sha is not None:
        # Fail closed: a malformed or truncated expected SHA is a broken claim,
        # never a reason to skip the exact-match comparison.
        expected = require_full_sha(expected_full_sha, "expected_full_sha")
        if resolved_sha != expected:
            raise SourceValidationError(
                f"Source commit SHA mismatch: resolved {resolved_sha}, expected {expected}"
            )

    if require_clean:
        status_res = subprocess.run(
            ["git", "-C", str(p), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status_res.returncode != 0 or status_res.stdout.strip():
            dirt = status_res.stdout.strip()[:200]
            raise SourceValidationError(
                f"Worktree at {p} has uncommitted changes (dirty): {dirt}"
            )

    return {
        "resolved_commit": resolved_sha,
        "repo_path": str(p),
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


def resolve_task_bounds(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve effective timeout and memory bounds for a task payload.

    Precedence:
    1. Explicit values in payload (if truthy and > 0, clamped to safety bounds)
    2. Named profile in payload["profile"] (fail-closed if unknown)
    3. Auto-detected profile from payload contents (checking provider, model, command, prompt, goal, id)
    4. General default (300s timeout, 768MB memory)
    """
    profile_name = payload.get("profile")

    if profile_name:
        profile = get_profile(profile_name)
    else:
        # Comprehensive detection (F1 / C3014): inspect provider, model, model_requirements,
        # command, prompt, goal, id. "goal" is included because `launcher request` stores the
        # task prompt under that key (C3048); without it, request-submitted model work would
        # silently fall back to the 300s default profile.
        model_req = payload.get("model_requirements") or {}
        provider = str(payload.get("provider") or model_req.get("provider") or "").lower().strip()
        model = str(payload.get("model") or model_req.get("model") or "").lower().strip()
        cmd = (
            str(payload.get("command") or "")
            + " " + str(payload.get("prompt") or "")
            + " " + str(payload.get("goal") or "")
        ).lower()
        task_id = str(payload.get("id") or "").lower()

        is_model = bool(
            provider in ("zai", "zcode", "grok", "antigravity", "codex", "openai", "anthropic", "claude")
            or any(k in provider for k in ("zai", "grok", "agy", "codex", "gemini", "zcode"))
            or any(k in model for k in ("glm", "grok", "gemini", "claude", "gpt"))
            or any(k in cmd for k in ("zcodex", "agy", "codex", "zcode", "gemini", "glm-5", "grok", "antigravity", "openai", "anthropic"))
        )

        if (
            "root-run" in task_id
            or payload.get("root_run")
            or "root-run" in cmd
            or payload.get("profile") == "root-run"
        ):
            profile_name = "root-run"
        elif "review" in task_id or "review" in cmd:
            profile_name = "model-review"
        elif "cleanup" in task_id:
            profile_name = "model-cleanup" if is_model else "deterministic-cleanup"
        elif is_model:
            profile_name = "model-task"
        else:
            profile_name = "default"

        profile = get_profile(profile_name)

    raw_timeout = payload.get("timeout")
    if raw_timeout is not None and float(raw_timeout) > 0:
        effective_timeout = float(raw_timeout)
    else:
        effective_timeout = float(profile["timeout"])

    # Enforce safe bounds [60.0, 7200.0] on task-units backend (F3), clamped to 1500.0 for root-run
    max_timeout = 1500.0 if profile_name == "root-run" else 7200.0
    effective_timeout = max(60.0, min(max_timeout, effective_timeout))

    raw_mem = payload.get("memory_mb")
    if raw_mem is not None and int(raw_mem) > 0:
        effective_memory = int(raw_mem)
    else:
        effective_memory = int(profile["memory_mb"])

    from launcher.task_resource_limits import hub_headless_memory_mb
    hub_memory = hub_headless_memory_mb(payload)
    effective_memory = hub_memory if hub_memory is not None else max(128, min(1500, effective_memory))

    return {
        "profile": profile_name,
        "timeout": effective_timeout,
        "memory_mb": effective_memory,
    }
