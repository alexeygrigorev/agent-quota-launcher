"""Task admission profiles for agent-quota-launcher.

Formalizes timeout and resource bounds per task category to prevent
premature timeouts on model code reviews and long-running test suites,
while maintaining fast fail-fast deadlines on deterministic checks.
"""

from typing import Dict, Any, Optional

DEFAULT_TIMEOUT_SEC: float = 300.0
DEFAULT_MEMORY_MB: int = 768

TASK_PROFILES: Dict[str, Dict[str, Any]] = {
    "model-review": {
        "timeout": 600.0,
        "memory_mb": 768,
        "description": "Full-suite test execution and independent LLM code review",
    },
    "model-task": {
        "timeout": 600.0,
        "memory_mb": 768,
        "description": "Autonomous model agent reasoning, tool use, and artifact drafting",
    },
    "deterministic-check": {
        "timeout": 60.0,
        "memory_mb": 256,
        "description": "Fast deterministic test runner, linter, or syntax verification",
    },
    "cleanup": {
        "timeout": 120.0,
        "memory_mb": 256,
        "description": "Disk pressure mitigation, cache pruning, or artifact cleanup unit",
    },
    "default": {
        "timeout": DEFAULT_TIMEOUT_SEC,
        "memory_mb": DEFAULT_MEMORY_MB,
        "description": "Standard general task execution profile",
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
    3. Auto-detected profile from payload contents (e.g. review keywords/command)
    4. General default (300s timeout, 768MB memory)
    """
    profile_name = payload.get("profile")

    # Auto-detection heuristic if profile is not explicitly specified
    if not profile_name:
        cmd = str(payload.get("command") or "") + " " + str(payload.get("prompt") or "")
        task_id = str(payload.get("id") or "")
        if "review" in task_id or "review" in cmd:
            profile_name = "model-review"
        elif any(k in cmd for k in ("zcodex", "agy", "codex exec", "zcode")):
            profile_name = "model-task"
        elif "cleanup" in task_id:
            profile_name = "cleanup"
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
