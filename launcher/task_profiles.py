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


def validate_source_commit(
    repo_path: str | Path,
    commit_ref: str,
    expected_full_sha: Optional[str] = None,
    require_clean: bool = False,
) -> Dict[str, Any]:
    """Validate declared git source ref via rev-parse before admission (C2999, C3002).
    
    Guarantees:
    1. Commit ref resolves to a valid 40-character SHA commit object.
    2. If expected_full_sha is provided, verifies exact 40-char match (rejects truncated or mismatched SHAs).
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

    if expected_full_sha:
        expected = str(expected_full_sha).strip().lower()
        if len(expected) == 40 and resolved_sha != expected:
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

        if "review" in task_id or "review" in cmd:
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

    # Enforce safe bounds [60.0, 7200.0] on task-units backend (F3)
    effective_timeout = max(60.0, min(7200.0, effective_timeout))

    raw_mem = payload.get("memory_mb")
    if raw_mem is not None and int(raw_mem) > 0:
        effective_memory = int(raw_mem)
    else:
        effective_memory = int(profile["memory_mb"])

    # Enforce memory ceiling <= 1500MB
    effective_memory = max(128, min(1500, effective_memory))

    return {
        "profile": profile_name,
        "timeout": effective_timeout,
        "memory_mb": effective_memory,
    }
