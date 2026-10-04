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

class Store:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path).resolve())
        self._init_db()

    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.get_conn() as conn:
            conn.executescript(SCHEMA)

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

    def get_active_paths(self, conn, exclude_task_id=None):
        query = """
            SELECT tp.path, t.id, t.state
            FROM task_paths tp
            JOIN tasks t ON t.id = tp.task_id
            WHERE t.state NOT IN ('accepted', 'failed')
        """
        params = []
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
            
    def _extract_json(self, text):
        match = re.search(r'(\{.*\})', text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        return json.loads(text)
            
    def get_active_resources(self):
        total_mem = 0
        total_disk = 0
        with self.get_conn() as conn:
            cursor = conn.execute("""
                SELECT t.id, tr.memory_mb, tr.disk_mb
                FROM task_resources tr
                JOIN tasks t ON t.id = tr.task_id
                WHERE t.state NOT IN ('accepted', 'failed')
            """)
            for row in cursor.fetchall():
                task_id, mem, disk = row
                res = subprocess.run(["aplexer", "status", f"task-{task_id}", "--json"], capture_output=True, text=True)
                if res.returncode == 0:
                    try:
                        info = self._extract_json(res.stdout)
                        if info.get("phase") in ["running", "starting", "working"]:
                            total_mem += (mem or 0)
                            total_disk += (disk or 0)
                    except Exception:
                        total_mem += (mem or 0)
                        total_disk += (disk or 0)
                else:
                    cursor2 = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
                    tstate = cursor2.fetchone()[0]
                    if tstate in ("queued", "starting", "launch-uncertain"):
                        total_mem += (mem or 0)
                        total_disk += (disk or 0)
                        
        return total_mem, total_disk

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
