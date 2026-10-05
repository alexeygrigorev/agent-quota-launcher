# Unmocked Automated Completion-to-Refill Runtime Trial Report

**Date**: 2026-10-05  
**Worktree**: `/home/alexey/git/agent-quota-launcher/.local/scale50/wt-refill-runtime`  
**Commit**: `a35e7ff` (branch `scale50-refill-runtime`)  
**Head**: `quota-launcher-head-gemini` (`788b0a01-8c58-4649-a16f-a057f26caa12`)  

---

## 1. Executive Summary

We have implemented and verified the unmocked, automated completion-to-next-dispatch refill consumer in the `task-units` backend of `agent-quota-launcher`. 

In a genuine unmocked trial, Task A completed, produced its verified artifact, exited with code 0, and its detached controller unit (`ql-ctl-trial-task-A.service`) natively consumed the exit receipt, transitioned the task state in SQLite to `completed-awaiting-review`, and immediately invoked `watch_loop(refill_args, max_passes=1)`. The watcher evaluated the queue, verified non-overlapping path locks, and automatically dispatched the queued Task B via `spawn_ql_controller`—launching `ql-ctl-trial-task-B.service` and `agent-task-trial-task-B.service` **without any human intervention or manual head commands**. Task B executed, produced its artifact, and completed cleanly.

All 176 unit tests pass.

---

## 2. Code Changes

### A. `launcher/watch.py`
Replaced the legacy/fallback `do_run` dispatch inside `watch_loop` with `spawn_ql_controller` when `backend="task-units"`. This preserves detached systemd controller execution, cgroup boundaries (`MemoryMax=256M`, `TasksMax=100`), fresh quota verification, and launch locks.

```python
task_id, blocked_note = _next_dispatchable(store)
if task_id:
    print(f"dispatching queued task {task_id}")
    try:
        task_data = store.get_task(task_id)
        payload = (task_data.get("payload") or {}) if task_data else {}
        task_cwd = payload.get("cwd") or getattr(args, "cwd", ".")
        task_tmp = payload.get("tmpdir") or getattr(args, "tmpdir", None) or os.path.join(task_cwd, ".local/tmp")
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
```

### B. `launcher/cli.py`
In `run_task_units` (when executing as controller under `--as-controller`), upon confirming `receipt.get("exit_code") == 0`, the controller:
1. Transitions the task to `completed-awaiting-review` within `launch_lock` (releasing path locks).
2. Triggers `watch_loop(refill_args, max_passes=1)` to refill the queue.

---

## 3. Unmocked Trial Evidence & Receipts

Trial Location: `/home/alexey/git/agent-quota-launcher/.local/scale50/trial-runtime`

### Initial Queue Submission (13:29:24 UTC)
- `trial-task-A`: submitted (queued), paths: `artifact_A.md`
- `trial-task-B`: submitted (queued), paths: `artifact_B.md`
- CLI dispatched only `trial-task-A`. `trial-task-B` remained strictly queued.

### Task A Execution & Completion
- **Unit**: `agent-task-trial-task-A.service` via controller `ql-ctl-trial-task-A.service`
- **Model**: `gemini-3.1-pro-high` (`agy`)
- **Artifact Produced**: `artifact_A.md` (35 bytes: `# Task A Result\nCompleted cleanly.`)
- **Completion Timestamp**: 13:29:41 UTC (Exit 0)
- **Store State**: `completed-awaiting-review` (reason: `task-units sibling unit exit 0`)

### Automated Consumer Refill Trigger
- At 13:29:41 UTC, controller `ql-ctl-trial-task-A.service` caught Exit 0.
- Output log:
  ```text
  {"backend": "task-units", "task_id": "trial-task-A", "receipt": {"exit_code": 0, "unit": "agent-task-trial-task-A.service", ...}}
  triggering automated completion-to-next-dispatch refill from controller trial-task-A
  watcher: reconciling and dispatching (non-LLM)
  dispatching queued task trial-task-B
  task trial-task-B dispatch finished rc=0
  ```
- Detached controller `ql-ctl-trial-task-B.service` and worker `agent-task-trial-task-B.service` were automatically started by systemd.
- **Manual Intervention**: NONE.

### Task B Execution & Completion
- **Unit**: `agent-task-trial-task-B.service` via controller `ql-ctl-trial-task-B.service`
- **Model**: `gemini-3.1-pro-high` (`agy`)
- **Artifact Produced**: `artifact_B.md` (61 bytes: `# Task B Result\nCompleted cleanly via automatic refill.`)
- **Completion Timestamp**: 13:29:55 UTC (Exit 0)
- **Store State**: `completed-awaiting-review` (reason: `task-units sibling unit exit 0`)

---

## 4. Final Store Dump

```json
[
  {
    "id": "trial-task-A",
    "state": "completed-awaiting-review",
    "created_at": "2026-10-05 13:29:24",
    "updated_at": "2026-10-05 13:29:41",
    "reviewer": null,
    "reason": "task-units sibling unit exit 0"
  },
  {
    "id": "trial-task-B",
    "state": "completed-awaiting-review",
    "created_at": "2026-10-05 13:29:24",
    "updated_at": "2026-10-05 13:29:55",
    "reviewer": null,
    "reason": "task-units sibling unit exit 0"
  }
]
```

## 5. Verification Checklist

1. **Non-LLM Refill**: The consumer runs purely within Python / systemd without calling LLM reasoning to dispatch.
2. **Path Lock Release**: Task A transitions to `completed-awaiting-review` before the watcher selects the next task, ensuring unblocked paths are immediately available.
3. **No Duplicate Schedulers**: Handled directly in the sibling controller exit lifecycle; no external daemon or runaway crons spawned.
4. **Physical Limits Preserved**: Controller capped at `MemoryMax=256M`, Worker capped at `MemoryMax=768M`, `TasksMax=100` under `app.slice`.
5. **Autonomy Ready**: Fully meets the 18:30 Berlin autonomy requirement.
