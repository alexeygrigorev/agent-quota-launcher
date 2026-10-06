"""Tests for task admission profiles in agent-quota-launcher."""

import pytest
from pathlib import Path
from launcher.task_profiles import TASK_PROFILES, get_profile, resolve_task_bounds
from launcher.store import Store


def test_get_profile_known():
    prof = get_profile("model-review")
    assert prof["timeout"] == 600.0
    assert prof["memory_mb"] == 768

    det = get_profile("deterministic-check")
    assert det["timeout"] == 60.0
    assert det["memory_mb"] == 256


def test_get_profile_unknown_or_none():
    assert get_profile(None)["timeout"] == 300.0
    assert get_profile("nonexistent-profile")["timeout"] == 300.0


def test_resolve_task_bounds_explicit_override():
    payload = {"profile": "model-review", "timeout": 450.0, "memory_mb": 512}
    bounds = resolve_task_bounds(payload)
    assert bounds["timeout"] == 450.0
    assert bounds["memory_mb"] == 512
    assert bounds["profile"] == "model-review"


def test_resolve_task_bounds_named_profile():
    payload = {"profile": "deterministic-check"}
    bounds = resolve_task_bounds(payload)
    assert bounds["timeout"] == 60.0
    assert bounds["memory_mb"] == 256
    assert bounds["profile"] == "deterministic-check"


def test_resolve_task_bounds_autodetect_review():
    payload = {"id": "t-ql-review-something", "command": "python3 review.py"}
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "model-review"
    assert bounds["timeout"] == 600.0


def test_resolve_task_bounds_autodetect_model_exec():
    payload = {"id": "t-custom-exec", "command": "zcodex exec --model glm-5.3-flash"}
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "model-task"
    assert bounds["timeout"] == 600.0


def test_resolve_task_bounds_autodetect_cleanup():
    payload = {"id": "disk-pressure-cleanup-2"}
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "cleanup"
    assert bounds["timeout"] == 120.0
    assert bounds["memory_mb"] == 256


def test_submit_task_auto_populates_profile_timeout(tmp_path):
    db_path = tmp_path / "state.db"
    store = Store(db_path)

    # Submitting task without timeout but with profile
    payload = {
        "owner": "test-owner",
        "cwd": str(tmp_path),
        "profile": "model-review",
    }
    task_id = store.submit_task("t-test-prof", "key-1", payload, paths=[])
    assert task_id == "t-test-prof"

    # Verify stored payload has timeout auto-populated
    stored = store.get_task("t-test-prof")
    stored_payload = stored["payload"]
    if isinstance(stored_payload, str):
        import json
        stored_payload = json.loads(stored_payload)
    assert stored_payload["timeout"] == 600.0
    assert stored_payload["profile"] == "model-review"
