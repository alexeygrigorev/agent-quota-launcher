"""Non-LLM queue watcher: reconcile uncertain launches against native session
death, then dispatch the next queued independently owned task with fresh
quota/resource checks (do_run re-checks everything at launch boundary).
"""
import json
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


def _next_dispatchable(store):
    """Oldest queued task whose owned paths don't overlap a live lease.
    Returns (task_id | None, blocked_note | None)."""
    with store.get_conn() as conn:
        cursor = conn.execute(
            "SELECT id FROM tasks WHERE state = 'queued' ORDER BY created_at ASC")
        queued = [r[0] for r in cursor.fetchall()]
        owned = {}
        if queued:
            marks = ",".join("?" for _ in queued)
            for tid, path in conn.execute(
                    f"SELECT task_id, path FROM task_paths WHERE task_id IN ({marks})",
                    queued):
                owned.setdefault(tid, []).append(path)

    for task_id in queued:
        blocked = None
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

    store = Store(store_path)
    print("watcher: reconciling and dispatching (non-LLM)")

    passes = 0
    while max_passes is None or passes < max_passes:
        passes += 1
        for task_id, detail in _reconcile(store):
            print(f"reconciled: task {task_id} -> failed ({detail})")

        task_id, blocked_note = _next_dispatchable(store)
        if task_id:
            print(f"dispatching queued task {task_id}")
            try:
                rc = do_run(store_path, task_id, args.cwd, args.tmpdir, lock_path)
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
