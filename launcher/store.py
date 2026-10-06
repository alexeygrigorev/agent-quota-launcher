import sqlite3
import json
import os
import fcntl
import subprocess
from pathlib import Path
from contextlib import contextmanager
import re

from launcher.tags import run_tag_for

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

CREATE TABLE IF NOT EXISTS review_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    source_commit TEXT,
    source_repo TEXT,
    reviewer_session TEXT NOT NULL,
    reviewer_model TEXT,
    head_session TEXT NOT NULL,
    review_prompt TEXT,
    report_path TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    verdict TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

    def complete_task(self, task_id, reviewer, reason=None):
        """running -> completed-awaiting-review. Cannot complete queued or
        launch-uncertain work; the caller must confirm native process death
        AND hold result evidence before calling."""
        return self.transition_task(
            task_id, "completed-awaiting-review", ("running",),
            reason=reason or "head marked complete; native death confirmed",
            reviewer=reviewer,
        )

    def fail_task(self, task_id, reviewer, reason):
        """running -> failed. Honest close for launches whose native died
        without producing result evidence; caller confirms death first."""
        return self.transition_task(
            task_id, "failed", ("running",), reason=reason, reviewer=reviewer,
        )

    def get_task_paths(self, task_id):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT path FROM task_paths WHERE task_id = ?", (task_id,))
            return [r[0] for r in cursor.fetchall()]

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
        from launcher.task_profiles import resolve_task_bounds, validate_source_commit, SourceValidationError
        
        # 1. Bounds resolution (fail-closed on unknown profile name, auto-detect keywords)
        bounds = resolve_task_bounds(payload)
        if not payload.get("timeout") and bounds.get("timeout"):
            payload["timeout"] = bounds["timeout"]
        if not payload.get("profile") and bounds.get("profile"):
            payload["profile"] = bounds["profile"]

        # 2. Preadmission git source commit verification (C2999, C3002)
        target_commit = payload.get("target_commit") or payload.get("source_commit")
        target_repo = payload.get("target_worktree") or payload.get("cwd")
        if target_commit and target_repo:
            source_receipt = validate_source_commit(
                target_repo,
                target_commit,
                expected_full_sha=target_commit if len(target_commit) == 40 else None,
            )
            payload["source_receipt"] = source_receipt
            payload["resolved_commit"] = source_receipt["resolved_commit"]

        if not payload.get("owner") or not payload.get("cwd") or not payload.get("timeout"):
            raise ValueError("Missing owner/cwd/timeout in payload")

        normalized_paths = []
        if paths:
            for p_arg in paths:
                for p in str(p_arg).split(','):
                    stripped = p.strip()
                    if stripped:
                        normalized_paths.append(stripped)

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
            self.check_path_overlap(normalized_paths, active_paths)

            conn.execute(
                "INSERT INTO tasks (id, idempotency_key, payload, state) VALUES (?, ?, ?, ?)",
                (task_id, idempotency_key, payload_str, "queued")
            )
            for p in normalized_paths:
                conn.execute("INSERT INTO task_paths (task_id, path) VALUES (?, ?)", (task_id, str(Path(p).resolve())))

            # Record reservation in task_resources (explicit payload memory_mb wins, else parameter)
            effective_mem = int(payload["memory_mb"]) if payload.get("memory_mb") else memory_mb
            conn.execute("INSERT INTO task_resources (task_id, memory_mb, disk_mb) VALUES (?, ?, ?)", (task_id, effective_mem, disk_mb))

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
                try:
                    res = subprocess.run(["aplexer", "status", run_tag_for(task_id), "--json"],
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
                except FileNotFoundError:
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

    def add_review_receipt(self, receipt: dict, status: str, details: dict = None):
        """Record review receipt into review_receipts table preserving negative history."""
        reviewer = receipt.get("reviewer") or {}
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_receipts (
                    task_id, source_commit, source_repo, reviewer_session,
                    reviewer_model, head_session, review_prompt, report_path,
                    report_sha256, verdict, status, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.get("task_id"),
                    receipt.get("source_commit"),
                    receipt.get("source_repo"),
                    reviewer.get("session_id", ""),
                    reviewer.get("model", ""),
                    receipt.get("head_session_id", ""),
                    receipt.get("review_prompt", ""),
                    receipt.get("report_path", ""),
                    receipt.get("report_sha256", ""),
                    receipt.get("verdict", ""),
                    status,
                    json.dumps(details or {}),
                ),
            )

    def list_review_receipts(self, task_id: str = None):
        """Retrieve review receipts ordered by creation time."""
        with self.get_conn() as conn:
            if task_id:
                cursor = conn.execute(
                    "SELECT id, task_id, source_commit, reviewer_session, reviewer_model, verdict, status, details, created_at FROM review_receipts WHERE task_id = ? ORDER BY created_at",
                    (task_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, task_id, source_commit, reviewer_session, reviewer_model, verdict, status, details, created_at FROM review_receipts ORDER BY created_at"
                )
            return [
                {
                    "id": r[0],
                    "task_id": r[1],
                    "source_commit": r[2],
                    "reviewer_session": r[3],
                    "reviewer_model": r[4],
                    "verdict": r[5],
                    "status": r[6],
                    "details": json.loads(r[7]) if r[7] else {},
                    "created_at": r[8],
                }
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
