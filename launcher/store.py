import sqlite3
import json
import os
import fcntl
import subprocess
from pathlib import Path
from contextlib import contextmanager
import re

from launcher.tags import run_tag_for
from launcher.review_receipt import ReviewValidationError
from launcher.native_lifecycle import validate_native_binding, probe_native_lifecycle

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


def get_host_fencing_dir():
    return Path(os.environ.get(
        'LAUNCHER_HOST_FENCING_DIR',
        os.path.expanduser('~/.local/share/agent-quota-launcher/host_fencing')
    ))

@contextmanager
def host_fencing_lock():
    fencing_dir = get_host_fencing_dir()
    fencing_dir.mkdir(parents=True, exist_ok=True)
    lock_path = fencing_dir / "host_registry.lock"
    with open(lock_path, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

@contextmanager
def get_host_registry_conn():
    fencing_dir = get_host_fencing_dir()
    fencing_dir.mkdir(parents=True, exist_ok=True)
    db_path = fencing_dir / "host_registry.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        try:
            conn.execute("SELECT 1 FROM stores LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("CREATE TABLE IF NOT EXISTS stores (db_path TEXT PRIMARY KEY)")
        yield conn
    finally:
        conn.close()


class StateTransitionError(ValueError):
    pass


class Store:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path).resolve())
        self._init_db()
        self._register_store()

    def _register_store(self):
        with host_fencing_lock():
            with get_host_registry_conn() as reg_conn:
                reg_conn.execute("INSERT OR IGNORE INTO stores (db_path) VALUES (?)", (self.db_path,))

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

    def reject_task(self, task_id, reviewer, reason):
        """completed-awaiting-review -> rejected. Releases path lease."""
        return self.transition_task(
            task_id, "rejected", ("completed-awaiting-review",),
            reason=reason, reviewer=reviewer,
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

    def check_host_wide_path_overlap(self, current_conn, requested_paths):
        with get_host_registry_conn() as reg_conn:
            cursor = reg_conn.execute("SELECT db_path FROM stores")
            store_dbs = [row[0] for row in cursor.fetchall()]

        for s_db in store_dbs:
            if not os.path.exists(s_db):
                with get_host_registry_conn() as reg_conn:
                    reg_conn.execute("DELETE FROM stores WHERE db_path = ?", (s_db,))
                continue
            if s_db == self.db_path:
                s_conn = current_conn
                close_conn = False
            else:
                s_conn = sqlite3.connect(f"file:{s_db}?mode=ro", uri=True)
                close_conn = True

            try:
                import time
                start_time = time.time()
                active_paths = None
                while True:
                    try:
                        active_paths = self.get_active_paths(s_conn)
                        break
                    except sqlite3.OperationalError as e:
                        if "locked" in str(e).lower() and time.time() - start_time < 1.0:
                            time.sleep(0.1)
                            continue
                        raise ValueError(f"Cannot verify path exclusivity: store {s_db} is locked or unavailable") from e
                self.check_path_overlap(requested_paths, active_paths)
            except ValueError as e:
                if "Cannot verify" not in str(e):
                    raise ValueError(f"{str(e)} in store {s_db}")
                raise
            finally:
                if close_conn:
                    s_conn.close()


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

        with host_fencing_lock():
            with self.transaction() as conn:
                cursor = conn.execute("SELECT id, payload FROM tasks WHERE idempotency_key = ?", (idempotency_key,))
                row = cursor.fetchone()
                if row:
                    existing_id, existing_payload_str = row
                    if existing_payload_str == payload_str:
                        return existing_id
                    else:
                        raise ValueError("Conflicting payload for idempotency key")

                self.check_host_wide_path_overlap(conn, normalized_paths)

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

    def record_native_binding(self, task_id, native_id, tag, workspace, engine, incarnation, details=None):
        """Durably persist compact exact native binding evidence in task store.

        Validates native UUID, task tag, workspace, engine, and incarnation.
        Enforces one UUID per task and immutable identity: no rebinding to different UUID/incarnation.
        Matches submitted task id/run_tag_for(task), cwd, and expected engine where known.
        Compact details only, no raw env/argv/prompt/start_json.
        """
        validated = validate_native_binding(
            task_id=task_id,
            native_id=native_id,
            tag=tag,
            workspace=workspace,
            engine=engine,
            incarnation=incarnation,
            details=details,
        )

        task = self.get_task(task_id)
        if task:
            expected_tag = run_tag_for(task_id)
            if validated["tag"] != expected_tag:
                raise ValueError(
                    f"Binding tag {validated['tag']!r} does not match expected task tag {expected_tag!r}"
                )
            payload = task.get("payload", {})
            if isinstance(payload, dict):
                expected_cwd = payload.get("cwd")
                if expected_cwd:
                    if not os.path.isabs(expected_cwd):
                        raise ValueError(f"Task payload cwd {expected_cwd!r} is not absolute")
                    if validated["workspace"] != str(Path(expected_cwd).resolve()):
                        raise ValueError(
                            f"Binding workspace {validated['workspace']!r} does not match task cwd {str(Path(expected_cwd).resolve())!r}"
                        )
                expected_engine = payload.get("engine")
                if not expected_engine:
                    prov = payload.get("provider")
                    if prov == "antigravity":
                        expected_engine = "gemini"
                    elif prov == "opencode":
                        expected_engine = "opencode"
                if expected_engine and validated["engine"] != expected_engine:
                    raise ValueError(
                        f"Binding engine {validated['engine']!r} does not match expected engine {expected_engine!r}"
                    )

        with self.transaction() as conn:
            # Check 1: One UUID per task across all tasks
            cursor = conn.execute(
                "SELECT task_id FROM task_native_bindings WHERE native_id = ? AND task_id != ?",
                (validated["native_id"], task_id),
            )
            row = cursor.fetchone()
            if row:
                raise ValueError(
                    f"Native session {validated['native_id']} is already bound to task {row[0]}; one UUID per task"
                )

            # Check 2: Immutable identity per task (no rebinding to different UUID or incarnation)
            cursor = conn.execute(
                """
                SELECT native_id, tag, workspace, engine, incarnation, details
                FROM task_native_bindings WHERE task_id = ?
                """,
                (task_id,),
            )
            row = cursor.fetchone()
            if row:
                existing_id, existing_tag, existing_ws, existing_engine, existing_inc, existing_det = row
                if existing_id.lower() != validated["native_id"].lower():
                    raise ValueError(
                        f"Task {task_id} has immutable binding to {existing_id}; cannot rebind to {validated['native_id']}"
                    )
                if int(existing_inc) != int(validated["incarnation"]):
                    raise ValueError(
                        f"Task {task_id} has immutable incarnation {existing_inc}; cannot rebind to {validated['incarnation']}"
                    )
                if existing_tag != validated["tag"]:
                    raise ValueError(
                        f"Task {task_id} has immutable tag {existing_tag}; cannot change to {validated['tag']}"
                    )
                if existing_ws != validated["workspace"]:
                    raise ValueError(
                        f"Task {task_id} has immutable workspace {existing_ws}; cannot change to {validated['workspace']}"
                    )
                if existing_engine != validated["engine"]:
                    raise ValueError(
                        f"Task {task_id} has immutable engine {existing_engine}; cannot change to {validated['engine']}"
                    )

                # Merge details compactly
                merged_det = {}
                if existing_det:
                    try:
                        merged_det = json.loads(existing_det)
                    except Exception:
                        pass
                merged_det.update(validated["details"])
                conn.execute(
                    "UPDATE task_native_bindings SET details = ?, updated_at = CURRENT_TIMESTAMP WHERE task_id = ?",
                    (json.dumps(merged_det, sort_keys=True), task_id),
                )
                validated["details"] = merged_det
            else:
                conn.execute(
                    """
                    INSERT INTO task_native_bindings (
                        task_id, native_id, tag, workspace, engine, incarnation, details, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        validated["task_id"],
                        validated["native_id"],
                        validated["tag"],
                        validated["workspace"],
                        validated["engine"],
                        validated["incarnation"],
                        json.dumps(validated["details"], sort_keys=True),
                    ),
                )
        return validated

    def get_native_binding(self, task_id):
        """Retrieve exact native binding for a task, or None if unbound."""
        with self.get_conn() as conn:
            cursor = conn.execute(
                """
                SELECT task_id, native_id, tag, workspace, engine, incarnation, details, created_at, updated_at
                FROM task_native_bindings
                WHERE task_id = ?
                """,
                (task_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            det = {}
            if row[6]:
                try:
                    det = json.loads(row[6])
                except Exception:
                    det = {"raw": row[6]}
            return {
                "task_id": row[0],
                "native_id": row[1],
                "tag": row[2],
                "workspace": row[3],
                "engine": row[4],
                "incarnation": row[5],
                "details": det,
                "created_at": row[7],
                "updated_at": row[8],
            }

    def get_task_by_native_id(self, native_id):
        """Look up task by global native session UUID across all workspaces."""
        if not native_id or not isinstance(native_id, str):
            return None
        canon_id = native_id.strip().lower()
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT task_id FROM task_native_bindings WHERE native_id = ?",
                (canon_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            task_id = row[0]
            task = self.get_task(task_id)
            binding = self.get_native_binding(task_id)
            return {"task_id": task_id, "task": task, "binding": binding}

    def resolve_task_id(self, id_or_uuid):
        """Resolve either task_id or global native UUID to task_id."""
        if not id_or_uuid or not isinstance(id_or_uuid, str):
            return None
        target = id_or_uuid.strip()
        if self.get_task(target):
            return target
        by_native = self.get_task_by_native_id(target)
        if by_native:
            return by_native["task_id"]
        return None

    def recover_binding_from_artifacts(self, task_id, payload=None):
        """Best-effort recovery of genuine native binding from local launch artifacts.

        Full observed start identity only. Does not fallback to engine gemini/provider,
        cwd/workspace, or caller timestamps as observed facts.
        Checks files within bounded expected path only; no history scan.
        """
        if payload is None:
            task = self.get_task(task_id)
            if task:
                payload = task.get("payload")
        if not payload or not isinstance(payload, dict):
            return None
        cwd = payload.get("cwd")
        if not cwd or not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        cwd_p = Path(cwd).resolve()
        start_p = cwd_p / ".local" / f"launch-{task_id}-start.json"
        record_p = cwd_p / ".local" / f"launch-{task_id}.json"

        from launcher.native_lifecycle import read_bounded_json_file

        data = None
        recovered_from = None
        for p in (start_p, record_p):
            try:
                # Ensure bounded expected path within cwd_p / ".local"
                if not p.resolve().is_relative_to(cwd_p / ".local"):
                    continue
            except Exception:
                continue
            doc = read_bounded_json_file(p, max_bytes=1048576)
            if isinstance(doc, dict):
                data = doc
                recovered_from = p.name
                break
        if not isinstance(data, dict):
            return None

        # Prefer start record or wrapper_start_json if present
        start_doc = data.get("wrapper_start_json") if isinstance(data.get("wrapper_start_json"), dict) else data

        # Full observed start identity only: NO fallbacks to provider, cwd, or caller timestamps
        native_id = start_doc.get("id")
        tag = start_doc.get("tag")
        workspace = start_doc.get("workspace")
        engine = start_doc.get("engine")
        incarnation = start_doc.get("created_at_ms")

        if not native_id or not tag or not workspace or not engine or incarnation is None:
            return None

        if not isinstance(workspace, str) or not os.path.isabs(workspace.strip()):
            return None

        if isinstance(incarnation, bool) or not isinstance(incarnation, (int, float)) or incarnation <= 0:
            return None

        try:
            return self.record_native_binding(
                task_id=task_id,
                native_id=native_id,
                tag=tag,
                workspace=workspace,
                engine=engine,
                incarnation=int(incarnation),
                details={"recovered_from": recovered_from},
            )
        except Exception:
            return None

    def get_active_resources(self, exclude_task_id=None):
        """Sum live/uncertain reservations. exclude_task_id removes the task
        being launched so its own reservation is not double-counted.

        Uses exact native identity and current authoritative live phase or
        exact retired-sessions/<UUID>/tombstone.json (matching UUID, tag,
        workspace, creation ordering, and cause finished). Proven exit
        releases process RAM/disk reservation without inventing task success/
        review/acceptance. Unknown/missing/transport error remains conservative.
        Materializes SQL rows and closes read connection BEFORE recovery writes
        or probes to prevent SQLite deadlocks.
        """
        total_mem = 0
        total_disk = 0
        with self.get_conn() as conn:
            placeholders = ",".join("?" for _ in RESOURCE_HOLDING_STATES)
            query = f"""
                SELECT t.id, tr.memory_mb, tr.disk_mb, t.payload, t.state
                FROM task_resources tr
                JOIN tasks t ON t.id = tr.task_id
                WHERE t.state IN ({placeholders})
            """
            params = list(RESOURCE_HOLDING_STATES)
            if exclude_task_id:
                query += " AND t.id != ?"
                params.append(exclude_task_id)
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

        # Connection is CLOSED before performing recovery writes or probes
        for row in rows:
            task_id, mem, disk, raw_payload, state = row
            if state == "queued":
                total_mem += (mem or 0)
                total_disk += (disk or 0)
                continue

            binding = self.get_native_binding(task_id)
            if not binding and raw_payload:
                try:
                    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                    binding = self.recover_binding_from_artifacts(task_id, payload)
                except Exception:
                    binding = None

            if binding:
                status, detail = probe_native_lifecycle(binding)
                if status in ("dead", "retired-finished"):
                    # Proven exit releases process RAM/disk reservation
                    continue
                else:
                    # Alive or unknown remains conservative
                    total_mem += (mem or 0)
                    total_disk += (disk or 0)
            else:
                # Legacy or task-units without native binding:
                # Stays conservative under actual ownership proof
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
        report_path = receipt.get("report_path")
        report_sha = receipt.get("report_sha256")
        with self.transaction() as conn:
            if report_path and report_sha:
                cursor = conn.execute("SELECT id, report_sha256 FROM review_receipts WHERE report_path = ?", (report_path,))
                for row in cursor.fetchall():
                    if row[1] != report_sha:
                        raise ReviewValidationError(
                            f"Report path collision: '{report_path}' already recorded in receipt #{row[0]} with hash '{row[1]}'",
                            status="rejected_report_path_collision",
                        )
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

    def relocate_historical_report_pointer(self, receipt_id: int, new_path: str, expected_sha: str) -> bool:
        """Audit-preserving relocation of historical report pointer to an immutable frozen path."""
        p = Path(new_path)
        if not p.is_file() or p.is_symlink():
            raise ValueError(f"Target report file '{new_path}' does not exist or is a symlink")
        import hashlib
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        actual_sha = h.hexdigest().lower()
        if actual_sha != expected_sha.lower():
            raise ValueError(f"SHA mismatch for relocated report: expected {expected_sha}, computed {actual_sha}")
        with self.transaction() as conn:
            cursor = conn.execute("SELECT id, report_sha256 FROM review_receipts WHERE id = ?", (receipt_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Review receipt #{receipt_id} not found")
            if row[1].lower() != expected_sha.lower():
                raise ValueError(f"Receipt #{receipt_id} has recorded SHA '{row[1]}', does not match expected '{expected_sha}'")
            conn.execute("UPDATE review_receipts SET report_path = ? WHERE id = ?", (str(p), receipt_id))
        return True

    def list_review_receipts(self, task_id: str = None):
        """Retrieve review receipts ordered by creation time."""
        def _parse_details(raw):
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"raw": raw}

        with self.get_conn() as conn:
            if task_id:
                cursor = conn.execute(
                    "SELECT id, task_id, source_commit, reviewer_session, reviewer_model, verdict, status, details, created_at, report_path, report_sha256 FROM review_receipts WHERE task_id = ? ORDER BY created_at",
                    (task_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, task_id, source_commit, reviewer_session, reviewer_model, verdict, status, details, created_at, report_path, report_sha256 FROM review_receipts ORDER BY created_at"
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
                    "details": _parse_details(r[7]),
                    "created_at": r[8],
                    "report_path": r[9],
                    "report_sha256": r[10],
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
