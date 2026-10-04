import sqlite3
import json
import os
import fcntl
import subprocess
from pathlib import Path
from contextlib import contextmanager
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    payload TEXT,
    state TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_paths (
    task_id TEXT,
    path TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_resources (
    task_id TEXT PRIMARY KEY,
    memory_mb INTEGER,
    disk_mb INTEGER,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
"""

# States that hold RAM/disk reservations: live or possibly-live launches.
RESOURCE_HOLDING_STATES = ("queued", "starting", "launch-uncertain", "stalled", "running")
# States that hold path/evidence leases (through completed-awaiting-review).
LEASED_STATES = ("queued", "starting", "launch-uncertain", "stalled",
                 "running", "completed-awaiting-review")


class StateTransitionError(ValueError):
    pass


class Store:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path).resolve())
        self._init_db()

    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.get_conn() as conn:
            conn.executescript(SCHEMA)
            for col, decl in (("reviewer", "TEXT"), ("reason", "TEXT")):
                try:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:
                    pass  # column already exists

    @contextmanager
    def get_conn(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.get_conn() as conn:
            conn.execute("BEGIN EXCLUSIVE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def transition_task(self, task_id, new_state, expected_states, reason=None, reviewer=None):
        """Guarded state transition. Only moves the row when its current state
        is one of expected_states; raises StateTransitionError otherwise."""
        placeholders = ",".join("?" for _ in expected_states)
        sets = ["state = ?", "updated_at = CURRENT_TIMESTAMP"]
        params = [new_state]
        if reviewer is not None:
            sets.append("reviewer = ?")
            params.append(reviewer)
        if reason is not None:
            sets.append("reason = ?")
            params.append(reason)
        params.append(task_id)
        params.extend(expected_states)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND state IN ({placeholders})",
                params,
            )
            if cursor.rowcount == 0:
                raise StateTransitionError(
                    f"Task {task_id}: cannot transition to {new_state} "
                    f"(expected state in {list(expected_states)})"
                )
        return True

    def complete_task(self, task_id, reviewer):
        """running -> completed-awaiting-review. Cannot complete queued or
        launch-uncertain work; caller confirms native process death first."""
        return self.transition_task(
            task_id, "completed-awaiting-review", ("running",),
            reason="head marked complete; native death confirmed", reviewer=reviewer,
        )

    def accept_task(self, task_id, reviewer):
        """completed-awaiting-review -> accepted only. Queued, launch-uncertain
        or running tasks cannot be accepted past review."""
        return self.transition_task(
            task_id, "accepted", ("completed-awaiting-review",),
            reason="head accepted reviewed artifacts", reviewer=reviewer,
        )

    def get_active_paths(self, conn, exclude_task_id=None):
        placeholders = ",".join("?" for _ in LEASED_STATES)
        query = f"""
            SELECT tp.path, t.id
            FROM task_paths tp
            JOIN tasks t ON t.id = tp.task_id
            WHERE t.state IN ({placeholders})
        """
        params = list(LEASED_STATES)
        if exclude_task_id:
            query += " AND t.id != ?"
            params.append(exclude_task_id)

        cursor = conn.execute(query, params)
        return [(row[0], row[1]) for row in cursor.fetchall()]

    def check_path_overlap(self, requested_paths, active_paths):
        req_resolved = [Path(p).resolve() for p in requested_paths]
        for act_p_str, task_id in active_paths:
            act_p = Path(act_p_str).resolve()
            for req_p in req_resolved:
                if req_p.is_relative_to(act_p) or act_p.is_relative_to(req_p):
                    raise ValueError(f"Path overlap: {req_p} overlaps with {act_p} (task {task_id})")

    def submit_task(self, task_id, idempotency_key, payload, paths, memory_mb=1500, disk_mb=512):
        if not payload.get("owner") or not payload.get("cwd") or not payload.get("timeout"):
            raise ValueError("Missing owner/cwd/timeout in payload")

        payload_str = json.dumps(payload, sort_keys=True)

        with self.transaction() as conn:
            cursor = conn.execute("SELECT id, payload FROM tasks WHERE idempotency_key = ?", (idempotency_key,))
            row = cursor.fetchone()
            if row:
                existing_id, existing_payload_str = row
                if existing_payload_str == payload_str:
                    return existing_id
                else:
                    raise ValueError("Conflicting payload for idempotency key")

            active_paths = self.get_active_paths(conn)
            self.check_path_overlap(paths, active_paths)

            conn.execute(
                "INSERT INTO tasks (id, idempotency_key, payload, state) VALUES (?, ?, ?, ?)",
                (task_id, idempotency_key, payload_str, "queued")
            )
            for p in paths:
                conn.execute("INSERT INTO task_paths (task_id, path) VALUES (?, ?)", (task_id, str(Path(p).resolve())))

            conn.execute("INSERT INTO task_resources (task_id, memory_mb, disk_mb) VALUES (?, ?, ?)", (task_id, memory_mb, disk_mb))

        return task_id

    def get_task(self, task_id):
        with self.get_conn() as conn:
            cursor = conn.execute("SELECT payload, state FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "payload": json.loads(row[0]),
                "state": row[1]
            }

    def record_reason(self, task_id, reason):
        """Best-effort durable note (e.g. watcher blocked reason); never
        changes lifecycle state."""
        with self.transaction() as conn:
            conn.execute("UPDATE tasks SET reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (reason, task_id))

    def _extract_json(self, text):
        text = text.strip()
        try:
            obj, _ = json.JSONDecoder().raw_decode(text)
            return obj
        except json.JSONDecodeError:
            match = re.search(r'(\{.*\})', text, re.DOTALL)
            if match:
                return json.loads(match.group(1))
            raise

    def get_active_resources(self, exclude_task_id=None):
        """Sum live/uncertain reservations. exclude_task_id removes the task
        being launched so its own reservation is not double-counted. RAM/disk
        for completed-awaiting-review tasks is not held (process death is
        confirmed before complete); their path leases stay."""
        total_mem = 0
        total_disk = 0
        with self.get_conn() as conn:
            placeholders = ",".join("?" for _ in RESOURCE_HOLDING_STATES)
            query = f"""
                SELECT t.id, tr.memory_mb, tr.disk_mb
                FROM task_resources tr
                JOIN tasks t ON t.id = tr.task_id
                WHERE t.state IN ({placeholders})
            """
            params = list(RESOURCE_HOLDING_STATES)
            if exclude_task_id:
                query += " AND t.id != ?"
                params.append(exclude_task_id)
            cursor = conn.execute(query, params)
            for row in cursor.fetchall():
                task_id, mem, disk = row
                res = subprocess.run(["aplexer", "status", f"task-{task_id}", "--json"],
                                     capture_output=True, text=True, timeout=15)
                if res.returncode == 0:
                    try:
                        info = self._extract_json(res.stdout)
                        if info.get("phase") in ["running", "starting", "working", "launching"]:
                            total_mem += (mem or 0)
                            total_disk += (disk or 0)
                    except Exception:
                        total_mem += (mem or 0)
                        total_disk += (disk or 0)
                else:
                    total_mem += (mem or 0)
                    total_disk += (disk or 0)

        return total_mem, total_disk

    def list_tasks(self):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT id, state, created_at, updated_at, reviewer, reason FROM tasks ORDER BY created_at"
            )
            return [
                {"id": r[0], "state": r[1], "created_at": r[2], "updated_at": r[3],
                 "reviewer": r[4], "reason": r[5]}
                for r in cursor.fetchall()
            ]


@contextmanager
def launch_lock(lock_path):
    p = Path(lock_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
