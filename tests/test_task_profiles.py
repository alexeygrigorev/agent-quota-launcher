"""Tests for task admission profiles in agent-quota-launcher."""

import pytest
import subprocess
from pathlib import Path
from launcher.task_profiles import (
    TASK_PROFILES,
    SourceValidationError,
    get_profile,
    resolve_task_bounds,
    validate_source_commit,
)
from launcher.store import Store


def test_get_profile_known():
    prof = get_profile("model-review")
    assert prof["timeout"] == 900.0
    assert prof["memory_mb"] == 768

    det = get_profile("deterministic-check")
    assert det["timeout"] == 60.0
    assert det["memory_mb"] == 256

    cln = get_profile("model-cleanup")
    assert cln["timeout"] == 600.0
    assert cln["memory_mb"] == 768


def test_get_profile_none():
    assert get_profile(None)["timeout"] == 300.0
    assert get_profile("")["timeout"] == 300.0


def test_get_profile_unknown_raises_fail_closed():
    with pytest.raises(ValueError, match="Unknown task profile"):
        get_profile("nonexistent-profile")
    with pytest.raises(ValueError, match="Unknown task profile"):
        get_profile("model-revew")  # Typo must fail closed (F2)


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
    assert bounds["timeout"] == 900.0


def test_resolve_task_bounds_autodetect_provider_grok():
    # grok is backend default provider; must detect as model-task (F1)
    payload = {"id": "t-custom-task", "provider": "grok"}
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "model-task"
    assert bounds["timeout"] == 600.0


def test_resolve_task_bounds_autodetect_provider_antigravity():
    # antigravity provider must detect as model-task (F1)
    payload = {"id": "t-custom-task", "provider": "antigravity"}
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "model-task"
    assert bounds["timeout"] == 600.0


def test_resolve_task_bounds_autodetect_model_requirements():
    payload = {"id": "t-req-task", "model_requirements": {"provider": "zai"}}
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "model-task"
    assert bounds["timeout"] == 600.0


def test_resolve_task_bounds_timeout_clamping():
    # Clamping below 60.0s (F3)
    p_low = {"profile": "deterministic-check", "timeout": 10.0}
    b_low = resolve_task_bounds(p_low)
    assert b_low["timeout"] == 60.0

    # Clamping above 7200.0s (F3)
    p_high = {"profile": "model-task", "timeout": 10000.0}
    b_high = resolve_task_bounds(p_high)
    assert b_high["timeout"] == 7200.0


def test_validate_source_commit_valid():
    repo_dir = Path(__file__).resolve().parent.parent
    res = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True)
    head_sha = res.stdout.strip()
    
    receipt = validate_source_commit(repo_dir, "HEAD", expected_full_sha=head_sha)
    assert receipt["resolved_commit"] == head_sha
    assert len(receipt["resolved_commit"]) == 40
    assert "validated_at" in receipt


def test_validate_source_commit_mismatch_raises():
    repo_dir = Path(__file__).resolve().parent.parent
    fake_sha = "0000000000000000000000000000000000000000"
    with pytest.raises(SourceValidationError, match="Source commit SHA mismatch"):
        validate_source_commit(repo_dir, "HEAD", expected_full_sha=fake_sha)


def test_validate_source_commit_invalid_ref_raises():
    repo_dir = Path(__file__).resolve().parent.parent
    with pytest.raises(SourceValidationError, match="Invalid or unresolvable commit ref"):
        validate_source_commit(repo_dir, "nonexistent-branch-or-commit-ref-12345")


def test_submit_task_auto_populates_profile_and_source_receipt(tmp_path):
    repo_dir = Path(__file__).resolve().parent.parent
    res = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True)
    head_sha = res.stdout.strip()

    db_path = tmp_path / "state.db"
    store = Store(db_path)

    payload = {
        "owner": "test-owner",
        "cwd": str(tmp_path),
        "target_worktree": str(repo_dir),
        "target_commit": head_sha,
        "provider": "grok",
    }
    task_id = store.submit_task("t-test-prof", "key-1", payload, paths=[])
    assert task_id == "t-test-prof"

    stored = store.get_task("t-test-prof")
    stored_payload = stored["payload"]
    if isinstance(stored_payload, str):
        import json
        stored_payload = json.loads(stored_payload)
    
    assert stored_payload["profile"] == "model-task"
    assert stored_payload["timeout"] == 600.0
    assert "source_receipt" in stored_payload
    assert stored_payload["source_receipt"]["resolved_commit"] == head_sha


def test_submit_task_records_profile_memory_in_task_resources(tmp_path):
    db_path = tmp_path / "state.db"
    store = Store(db_path)

    # Explicit memory_mb in payload must be recorded in task_resources
    payload = {
        "owner": "test-owner",
        "cwd": str(tmp_path),
        "profile": "deterministic-check",
        "memory_mb": 256,
    }
    store.submit_task("t-test-mem", "key-2", payload, paths=[])

    with store.get_conn() as conn:
        cursor = conn.execute("SELECT memory_mb, disk_mb FROM task_resources WHERE task_id = 't-test-mem'")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 256
        assert row[1] == 512



def test_resolve_task_bounds_autodetect_sees_goal_key():
    # C3048 regression (review probe P3): `launcher request` stores the prompt
    # under payload["goal"]; auto-detection must see it, or review goals resolve
    # to the 300s default instead of model-review (900s).
    payload = {
        "id": "task-abcd1234",
        "goal": "perform full code review of the diff and report findings",
        "cwd": "/tmp/request-repo",
        "owner": "alexey",
    }
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "model-review"
    assert bounds["timeout"] == 900.0


def test_resolve_task_bounds_goal_model_work_detects_model_task():
    # Non-review model goal submitted via request() must land on model-task.
    payload = {"id": "task-abcd1234", "goal": "use zcodex to draft the migration script"}
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "model-task"
    assert bounds["timeout"] == 600.0


def test_resolve_task_bounds_goal_without_model_keywords_stays_default():
    payload = {"id": "task-abcd1234", "goal": "run the linter and report whitespace issues"}
    bounds = resolve_task_bounds(payload)
    assert bounds["profile"] == "default"
    assert bounds["timeout"] == 300.0
