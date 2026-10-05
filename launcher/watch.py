"""Non-LLM queue watcher: reconcile uncertain launches against native session
death, then dispatch the next queued independently owned task with fresh
quota/resource checks (do_run re-checks everything at launch boundary).
"""
import json
import os
import time
from pathlib import Path

from launcher.launch import do_run, native_status
from launcher.store import Store, launch_lock
from launcher.tags import run_tag_for


def _reconcile(store):
    """Confirm native death for uncertain/stalled launches before releasing
    their leases. A time limit alone never proves the lease safe to steal."""
    reconciled = []
    with store.get_conn() as conn:
        cursor = conn.execute(
            "SELECT id FROM tasks WHERE state IN ('launch-uncertain', 'stalled')")
        uncertain = [r[0] for r in cursor.fetchall()]

    for task_id in uncertain:
        alive, detail = native_status(run_tag_for(task_id))
        if alive == "dead":
            try:
                store.transition_task(
                    task_id, "failed", ("launch-uncertain", "stalled"),
                    reason=f"native death confirmed during reconciliation: {detail}")
                reconciled.append((task_id, detail))
            except Exception:
                pass  # state moved concurrently; next pass re-checks
        else:
            print(f"reconcile: task {task_id} native {alive} ({detail}); lease retained")
    return reconciled


def derive_task_tmpdir(task_cwd: str, payload: dict) -> str:
    """Derive a task-local contained TMPDIR under <task_cwd>/.local/tmp.
    Never inherit a foreign tmpdir from CLI args or sibling tasks."""
    cwd_path = Path(task_cwd).resolve()
    allowed_root = cwd_path / ".local" / "tmp"
    specified = payload.get("tmpdir")
    if specified:
        cand_path = Path(specified).resolve()
        if cand_path.is_relative_to(allowed_root):
            task_tmp = str(cand_path)
        else:
            task_tmp = str(allowed_root)
    else:
        task_tmp = str(allowed_root)
    os.makedirs(task_tmp, exist_ok=True)
    return task_tmp


def _unreviewed_task_ids(store):
    with store.get_conn() as conn:
        cursor = conn.execute(
            "SELECT id FROM tasks WHERE state = 'completed-awaiting-review'")
        return [r[0] for r in cursor.fetchall()]


def _next_dispatchable(store, wait_for_review="dependencies"):
    """Oldest queued task whose owned paths don't overlap a live lease.
    Returns (task_id | None, blocked_note | None)."""
    if wait_for_review == "global":
        unreviewed = _unreviewed_task_ids(store)
        if unreviewed:
            return None, (f"automatic refill waiting for distinct independent "
                          f"review acceptance of: {', '.join(unreviewed)}")

    check_dependencies = wait_for_review in (True, "dependencies", "per-task")

    with store.get_conn() as conn:
        cursor = conn.execute(
            "SELECT id, payload, reason FROM tasks WHERE state = 'queued' ORDER BY created_at ASC")
        queued_rows = cursor.fetchall()
        queued = [r[0] for r in queued_rows]
        payloads = {}
        reasons = {}
        for tid, p_raw, r_raw in queued_rows:
            try:
                payloads[tid] = json.loads(p_raw) if isinstance(p_raw, str) else (p_raw or {})
            except Exception:
                payloads[tid] = {}
            reasons[tid] = r_raw or ""

        owned = {}
        if queued:
            marks = ",".join("?" for _ in queued)
            for tid, path in conn.execute(
                    f"SELECT task_id, path FROM task_paths WHERE task_id IN ({marks})",
                    queued):
                owned.setdefault(tid, []).append(path)

    for task_id in queued:
        blocked = None
        payload = payloads.get(task_id, {})

        raw_goal = payload.get("goal")
        goal = (raw_goal if isinstance(raw_goal, str) else str(raw_goal or "")).strip()
        if not goal or goal == task_id:
            store.record_reason(task_id, "watch: blocked: bare proposal without substantive prompt")
            continue

        if "admission:" in reasons.get(task_id, ""):
            continue

        if check_dependencies:
            raw_deps = payload.get("depends_on") or payload.get("dependencies") or []
            if isinstance(raw_deps, str):
                deps = [raw_deps]
            elif isinstance(raw_deps, (list, tuple, set)):
                deps = list(raw_deps)
            else:
                deps = []

            for dep_id in deps:
                dep_id_str = str(dep_id)
                dep_task = store.get_task(dep_id_str)
                if not dep_task:
                    blocked = f"dependency {dep_id_str} not accepted (not found)"
                    break
                dep_state = dep_task.get("state")
                if dep_state != "accepted":
                    blocked = f"dependency {dep_id_str} not accepted (state: {dep_state})"
                    break

        if blocked:
            store.record_reason(task_id, f"watch: blocked: {blocked}")
            print(f"task {task_id} blocked: {blocked}")
            continue

        with store.get_conn() as conn:
            active_paths = store.get_active_paths(conn, exclude_task_id=task_id)
        for req in owned.get(task_id, []):
            req_p = Path(req).resolve()
            for act_p_str, act_id in active_paths:
                act_p = Path(act_p_str).resolve()
                if req_p.is_relative_to(act_p) or act_p.is_relative_to(req_p):
                    blocked = f"path overlap with task {act_id} on {req_p}"
                    break
            if blocked:
                break
        if blocked:
            store.record_reason(task_id, f"watch: blocked: {blocked}")
            print(f"task {task_id} blocked: {blocked}")
            continue
        return task_id, None
    return None, (f"{len(queued)} queued, all blocked" if queued else None)


def watch_loop(args, max_passes=None):
    config_dir = Path(getattr(args, 'config_dir', None) or
                      __import__('os').path.expanduser('~/.config/agent-quota-launcher'))
    store_path = str(config_dir / 'state.db')
    lock_path = str(config_dir / 'launch.lock')
    interval = float(getattr(args, 'interval', 10.0) or 10.0)
    once = bool(getattr(args, 'once', False))
    wait_for_review = getattr(args, 'wait_for_review', "dependencies")

    store = Store(store_path)
    print("watcher: reconciling and dispatching (non-LLM)")

    passes = 0
    while max_passes is None or passes < max_passes:
        passes += 1
        for task_id, detail in _reconcile(store):
            print(f"reconciled: task {task_id} -> failed ({detail})")

        task_id, blocked_note = _next_dispatchable(store, wait_for_review=wait_for_review)
        if task_id:
            print(f"dispatching queued task {task_id}")
            try:
                task_data = store.get_task(task_id)
                payload = (task_data.get("payload") or {}) if task_data else {}
                task_cwd = payload.get("cwd") or getattr(args, "cwd", None) or os.getcwd()
                task_cwd = str(Path(task_cwd).resolve())
                task_tmp = derive_task_tmpdir(task_cwd, payload)
                backend = getattr(args, "backend", "task-units")
                if backend == "task-units":
                    from types import SimpleNamespace
                    from launcher.cli import spawn_ql_controller
                    dispatch_args = SimpleNamespace(
                        id=task_id,
                        cwd=task_cwd,
                        tmpdir=task_tmp,
                        config_dir=config_dir,
                    )
                    rc = spawn_ql_controller(dispatch_args)
                else:
                    rc = do_run(store_path, task_id, task_cwd, task_tmp, lock_path)
                print(f"task {task_id} dispatch finished rc={rc}")
            except Exception as e:
                store.record_reason(task_id, f"watch: dispatch attempt failed: {e}")
                print(f"task {task_id} dispatch failed: {e}")
            if once:
                return 0
        elif once:
            print(f"nothing dispatchable ({blocked_note or 'queue empty'})")
            return 0

        time.sleep(interval)
    return 0
