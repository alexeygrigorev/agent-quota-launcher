"""Task admission profiles for agent-quota-launcher.

Formalizes timeout and resource bounds per task category to prevent
premature timeouts on model code reviews and long-running test suites,
while maintaining fast fail-fast deadlines on deterministic checks.
Incorporates measured historical durations (e.g. model cleanups taking 173-346s).
"""

from typing import Dict, Any, Optional

DEFAULT_TIMEOUT_SEC: float = 300.0
DEFAULT_MEMORY_MB: int = 768

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
    """Retrieve profile definition by name, falling back to 'default'."""
    if not name:
        return TASK_PROFILES["default"]
    return TASK_PROFILES.get(name, TASK_PROFILES["default"])


def resolve_task_bounds(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve effective timeout and memory bounds for a task payload.

    Precedence:
    1. Explicit values in payload (if truthy and > 0)
    2. Named profile in payload["profile"]
    3. Auto-detected profile from payload contents (e.g. model keywords, review, cleanup)
    4. General default (300s timeout, 768MB memory)
    """
    profile_name = payload.get("profile")

    # Auto-detection heuristic if profile is not explicitly specified
    if not profile_name:
        cmd = str(payload.get("command") or "") + " " + str(payload.get("prompt") or "")
        task_id = str(payload.get("id") or "")
        is_model = any(k in cmd for k in ("zcodex", "agy", "codex", "zcode", "gemini", "glm-5"))
        
        if "review" in task_id or "review" in cmd:
            profile_name = "model-review"
        elif "cleanup" in task_id:
            profile_name = "model-cleanup" if is_model else "deterministic-cleanup"
        elif is_model:
            profile_name = "model-task"
        else:
            profile_name = "default"

    profile = get_profile(profile_name)

    effective_timeout = float(payload.get("timeout") or profile["timeout"])
    effective_memory = int(payload.get("memory_mb") or profile["memory_mb"])

    return {
        "profile": profile_name,
        "timeout": effective_timeout,
        "memory_mb": effective_memory,
    }
