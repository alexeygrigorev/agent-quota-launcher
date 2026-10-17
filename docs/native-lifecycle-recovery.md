# Native Lifecycle & Resource Accounting Contract

## Overview

This document specifies the authoritative native lifecycle and resource accounting recovery contract for Agent Quota Launcher (M8a/M15 reliability).

The contract eliminates false-positive resource starvation (e.g. `host MemAvailable < 10GiB` caused by retired workers holding reservations across deleted worktrees or foreign workspaces) while preserving strict conservative guarantees: unknown status retains reservations, and process exit alone never fabricates task success, review, or acceptance.

## 1. Durable Exact Native Binding

At real successful native start and validated first action, the launcher persists an exact native binding in the task store (`task_native_bindings` table):

```sql
CREATE TABLE IF NOT EXISTS task_native_bindings (
    task_id TEXT PRIMARY KEY,
    native_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    workspace TEXT NOT NULL,
    engine TEXT NOT NULL,
    incarnation INTEGER NOT NULL,
    details TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_native_bindings_native_id ON task_native_bindings(native_id);
```

### Validation Rules
- `native_id`: Canonical lowercase UUID v4 string. Rejects nil UUID, non-UUID strings, dummy identifiers, and caller PIDs.
- `tag`: Authoritative task run tag (e.g. `task-<task_id>`).
- `workspace`: Resolved canonical directory path of the session workspace.
- `engine`: Engine or profile name (e.g. `gemini`, `opencode`, `zai`, `grok`).
- `incarnation`: Positive integer epoch-millisecond timestamp (`created_at_ms`) when the native session was created.
- Never creates or binds an agent registry entry, fabricates whoami, or borrows external identities.

## 2. Resource Accounting Projection (`get_active_resources`)

`Store.get_active_resources(exclude_task_id=None)` evaluates tasks in resource-holding states (`queued`, `starting`, `launch-uncertain`, `stalled`, `running`):
- `queued` tasks always hold their resource reservations awaiting dispatch.
- Active tasks with exact native bindings are probed authoritatively:
  1. **Authoritative live probe**: Queries `aplexer status <native_id> --json` by global session UUID.
     - Phase in `running`, `starting`, `working`, `launching` with non-empty containment: classified as `alive` (reservation held).
     - Phase in `exited`, `dead`, `failed`, `completed`, `finished`, or empty containment: classified as `dead` (proven exit; process reservation released).
  2. **Authoritative retired tombstone probe**: If the session is not in live session tables, explicit single-file lookup is performed at `<APLEXER_STATE_DIR>/retired-sessions/<native_id>/tombstone.json`.
     - Matching criteria:
       - `tombstone.id == native_id`
       - `tombstone.tag == tag`
       - `Path(tombstone.workspace).resolve() == Path(workspace).resolve()`
       - `tombstone.cause == "finished"`
       - `tombstone.finished_at_ms >= incarnation` (creation ordering)
     - When all matching criteria pass: classified as `retired-finished` (proven exit; process RAM/disk reservation released).
  3. **Conservative fallback**:
     - Mismatched UUID, mismatched tag (reused tag), mismatched workspace, malformed JSON, cause other than "finished", or stale incarnation (`finished_at_ms < incarnation`) -> classified as `unknown` (reservation held).
     - Transport errors, timeouts, stale sockets, missing tombstones -> classified as `unknown` (reservation held).
     - Legacy records or tasks without native bindings -> fail conservative (reservation held).

Releasing process RAM/disk reservations is a pure projection and does not mutate task lifecycle state or invent task completion.

## 3. Global UUID Resolution & Targeted Reconciliation

### Global Session UUID Resolution
CLI commands (`complete`, `fail`, `status`, `reconcile`) and store queries accept either a task ID or a native session UUID. When a native UUID is supplied, `store.resolve_task_id(id_or_uuid)` maps it to its associated task ID across workspaces without relying on local directory context.

### Lifecycle Outcome Distinction
- **Native Exit**: Confirms the worker process has terminated. Releases process RAM/disk reservations in resource projections.
- **Task Completion (`complete`)**: Requires BOTH proven native exit (`dead` or `retired-finished`) AND non-empty result evidence artifact in owned paths. Native exit alone is refused.
- **Task Failure (`fail`)**: Requires proven native exit and an explicit reason. Refuses to fail a possibly-live worker.
- **Targeted Reconciliation (`reconcile --id <id_or_uuid>`)**:
  - Reconciles only the specified target task.
  - If native exit is proven and result evidence exists: transitions to `completed-awaiting-review`.
  - If native exit is proven and no result evidence on starting/stalled/launch-uncertain: transitions to `failed`.
  - Never dispatches queued work or invokes full queue loops.
- **Task Acceptance (`accept`)**: Permitted ONLY from `completed-awaiting-review` after independent review. Native exit alone never permits acceptance.
