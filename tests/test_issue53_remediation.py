import pytest
import os
import subprocess
import time
import json
import sqlite3
import concurrent.futures
from pathlib import Path
from argparse import Namespace

import launcher.store as l_store
from launcher.store import Store, host_fencing_lock, get_host_registry_conn
from launcher.cli import config_dir_for


def test_config_dir_fail_closed(monkeypatch):
    monkeypatch.delenv("LAUNCHER_CONFIG_DIR", raising=False)
    args = Namespace(config_dir=None)
    with pytest.raises(ValueError, match="Configuration error.*environment variable must be set"):
        config_dir_for(args)

    args = Namespace(config_dir="   ")
    with pytest.raises(ValueError, match="Configuration error.*environment variable must be set"):
        config_dir_for(args)


def test_cli_invocation_fail_closed(tmp_path, monkeypatch):
    env = os.environ.copy()
    env.pop("LAUNCHER_CONFIG_DIR", None)
    launcher_dir = Path(l_store.__file__).resolve().parent.parent
    res = subprocess.run(
        ["python3", "-m", "launcher", "init"],
        cwd=str(launcher_dir),
        env=env,
        capture_output=True,
        text=True
    )
    assert res.returncode != 0
    assert "error: Configuration error" in res.stderr
    assert "LAUNCHER_CONFIG_DIR" in res.stderr


def test_host_level_path_fencing_across_stores(tmp_path, monkeypatch):
    store_a_dir = tmp_path / "store_a"
    store_b_dir = tmp_path / "store_b"
    
    test_host_fencing_dir = tmp_path / "host_fencing"
    monkeypatch.setenv('LAUNCHER_HOST_FENCING_DIR', str(test_host_fencing_dir))
    
    store_a = Store(store_a_dir / "state.db")
    store_b = Store(store_b_dir / "state.db")
    
    payload_a = {"owner": "test", "cwd": "/tmp/testcwd1", "timeout": 10}
    payload_b = {"owner": "test", "cwd": "/tmp/testcwd2", "timeout": 10}
    
    task_a = store_a.submit_task("task-a", "key-a", payload_a, ["/tmp/shared_path"])
    assert task_a == "task-a"
    
    with pytest.raises(ValueError, match="Path overlap.*in store.*store_a"):
        store_b.submit_task("task-b-overlap", "key-b-overlap", payload_b, ["/tmp/shared_path/sub"])
        
    task_b = store_b.submit_task("task-b-indep", "key-b-indep", payload_b, ["/tmp/independent_path"])
    assert task_b == "task-b-indep"
    
    with pytest.raises(ValueError, match="Path overlap.*in store.*store_b"):
        store_a.submit_task("task-a-2", "key-a-2", payload_a, ["/tmp/independent_path/sub"])


def test_lifecycle_release(tmp_path, monkeypatch):
    store_a_dir = tmp_path / "store_a"
    store_b_dir = tmp_path / "store_b"
    test_host_fencing_dir = tmp_path / "host_fencing"
    monkeypatch.setenv('LAUNCHER_HOST_FENCING_DIR', str(test_host_fencing_dir))
    
    store_a = Store(store_a_dir / "state.db")
    store_b = Store(store_b_dir / "state.db")
    payload = {"owner": "test", "cwd": "/tmp/testcwd", "timeout": 10}
    
    store_a.submit_task("task-1", "key-1", payload, ["/tmp/shared"])
    
    with pytest.raises(ValueError, match="Path overlap"):
        store_b.submit_task("task-2", "key-2", payload, ["/tmp/shared"])
        
    # Complete and reject task-1 to release lease
    store_a.transition_task("task-1", "starting", ("queued",))
    store_a.transition_task("task-1", "running", ("starting",))
    store_a.complete_task("task-1", "reviewer")
    store_a.reject_task("task-1", "reviewer", "reason")
    
    # Store B can now submit
    store_b.submit_task("task-2", "key-2", payload, ["/tmp/shared"])


def test_locked_foreign_store(tmp_path, monkeypatch):
    store_a_dir = tmp_path / "store_a"
    store_b_dir = tmp_path / "store_b"
    test_host_fencing_dir = tmp_path / "host_fencing"
    monkeypatch.setenv('LAUNCHER_HOST_FENCING_DIR', str(test_host_fencing_dir))
    
    store_a = Store(store_a_dir / "state.db")
    store_b = Store(store_b_dir / "state.db")
    payload = {"owner": "test", "cwd": "/tmp/testcwd", "timeout": 10}
    
    # Lock store B with an exclusive transaction directly
    with store_b.get_conn() as conn:
        conn.execute("BEGIN EXCLUSIVE")
        
        # Store A tries to submit while B is locked
        with pytest.raises(ValueError, match="Cannot verify path exclusivity: store.*is locked or unavailable"):
            store_a.submit_task("task-a", "key-a", payload, ["/tmp/path"])


def test_concurrency_overlap_prevented(tmp_path, monkeypatch):
    store_a_dir = tmp_path / "store_a"
    store_b_dir = tmp_path / "store_b"
    test_host_fencing_dir = tmp_path / "host_fencing"
    monkeypatch.setenv('LAUNCHER_HOST_FENCING_DIR', str(test_host_fencing_dir))
    
    store_a = Store(store_a_dir / "state.db")
    store_b = Store(store_b_dir / "state.db")
    payload = {"owner": "test", "cwd": "/tmp/testcwd", "timeout": 10}
    
    def submit_a():
        try:
            store_a.submit_task("task-a", "key-a", payload, ["/tmp/concurrent_shared"])
            return True
        except ValueError:
            return False

    def submit_b():
        try:
            store_b.submit_task("task-b", "key-b", payload, ["/tmp/concurrent_shared"])
            return True
        except ValueError:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(submit_a)
        f2 = executor.submit(submit_b)
        res1 = f1.result()
        res2 = f2.result()
        
    assert res1 != res2
    assert res1 or res2
