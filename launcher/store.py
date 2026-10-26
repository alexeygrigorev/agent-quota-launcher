import sqlite3
import json
import os
from launcher.portable_lock import file_lock
import subprocess
from pathlib import Path
from contextlib import contextmanager
import re
import time
import math

from launcher.memory_accounting import resident_credit_mb
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

CREATE TABLE IF NOT EXISTS memory_samples (
    task_id TEXT NOT NULL,
    native_id TEXT NOT NULL,
    incarnation INTEGER NOT NULL,
    bucket INTEGER NOT NULL,
    resident_mb INTEGER NOT NULL,
    PRIMARY KEY(task_id, native_id, incarnation, bucket)
);

CREATE TABLE IF NOT EXISTS cpu_starts (
    task_id TEXT PRIMARY KEY,
    expected_cpu_cores REAL NOT NULL,
    expected_cpu_source TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS cpu_samples (
    task_id TEXT PRIMARY KEY,
    current_cpu_cores REAL NOT NULL,
    observed_at TEXT NOT NULL,
    identity TEXT NOT NULL,
    counter REAL NOT NULL,
    observed_ms INTEGER NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS cpu_choking_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cpu_oom_baseline (
    proc_root TEXT PRIMARY KEY,
    counter INTEGER NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
    with open(lock_path, 'a') as f:
        with file_lock(f, exclusive=True):
            yield

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

                # Reserve a known route at submit time; do_run rebinds the
                # reservation to its exact selected route under launch.lock.
                from launcher.resources import native_memory_request_for
                from launcher.admission import is_managed_profile_payload
                requirements = payload.get('model_requirements') or {}
                providers = requirements.get('providers', []) if isinstance(requirements, dict) else []
                provider = payload.get('provider') or (requirements.get('provider') if isinstance(requirements, dict) else None)
                if provider is None and isinstance(providers, list) and len(providers) == 1:
                    provider = providers[0]
                explicit = payload.get('memory_mb')
                if explicit is None and memory_mb != 1500:
                    explicit = memory_mb  # Preserve the existing caller override.
                effective_mem = native_memory_request_for(provider, is_managed_profile_payload(payload), explicit)
                conn.execute("INSERT INTO task_resources (task_id, memory_mb, disk_mb) VALUES (?, ?, ?)", (task_id, effective_mem, disk_mb))

        return task_id


    def reserve_native_memory(self, task_id, memory_mb):
        """Called under launch.lock immediately before admission and spawn."""
        with self.get_conn() as conn:
            changed = conn.execute("UPDATE task_resources SET memory_mb = ? WHERE task_id = ? AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND state = 'queued')", (memory_mb, task_id, task_id)).rowcount
            if changed != 1:
                raise StateTransitionError('Native memory reservation requires a queued task')

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
        if task is None:
            raise ValueError("Native binding requires an existing submitted task")
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
                prov = payload.get("provider")
                is_managed = (
                    payload.get("account_profile")
                    or payload.get("provider_profile")
                    or (isinstance(payload.get("profile"), str) and payload.get("profile").startswith("hub-"))
                    or payload.get("is_managed")
                )
                if prov == "antigravity":
                    valid_engines = ("antigravity",)
                elif prov == "gemini":
                    valid_engines = ("gemini",)
                elif prov == "claude":
                    valid_engines = ("claude",)
                elif prov == "opencode":
                    valid_engines = ("opencode",)
                elif prov == "zai":
                    valid_engines = ("codex",) if is_managed else ("zcodex",)
                elif prov == "codex":
                    valid_engines = ("codex",)
                else:
                    valid_engines = (payload.get("engine"),) if payload.get("engine") else ()

                requested_engine = payload.get("engine")
                if requested_engine and valid_engines and requested_engine not in valid_engines:
                    raise ValueError(
                        f"Requested engine {requested_engine!r} mismatches provider {prov!r} (expected {valid_engines[0]!r})"
                    )

                if valid_engines and validated["engine"] not in valid_engines:
                    raise ValueError(
                        f"Binding engine {validated['engine']!r} does not match expected engine {valid_engines[0]!r}"
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

        if type(incarnation) is not int or incarnation <= 0:
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

    def _record_resident_sample(self, task_id, binding, resident, dry_run=False):
        """Record one exact-incarnation resident observation for the p90 estimate.

        Keeps 15 minutes of samples (one maximum per 30s bucket). The samples
        feed ``memory_samples_p90``; the reservation itself is decided by the
        caller (stable measured residents hold no additional RAM, young ones
        hold estimate-minus-current). Writes are skipped on dry_run.
        """
        if resident <= 0:
            return
        bucket = int(time.time() // 30)
        identity = (task_id, binding['native_id'], binding['incarnation'])
        with self.get_conn() as conn:
            if not dry_run:
                conn.execute("DELETE FROM memory_samples WHERE bucket < ? OR bucket > ?", (bucket - 29, bucket))
                conn.execute("INSERT INTO memory_samples VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id,native_id,incarnation,bucket) DO UPDATE SET resident_mb=MAX(resident_mb,excluded.resident_mb)", (*identity, bucket, resident))

    def memory_samples_p90(self, percentile=90):
        """Measured placement estimate: percentile over resident samples.

        Returns an int MiB, or None when nothing has been measured yet
        (caller falls back to the configured ramp default). Samples are
        exact-incarnation resident observations written by
        _record_resident_sample.
        """
        from launcher.resource_policy import percentile_of
        with self.get_conn() as conn:
            rows = conn.execute("SELECT resident_mb FROM memory_samples").fetchall()
        return percentile_of([r[0] for r in rows], percentile)

    # ---- CPU parity (t1511u V2, choking-only) ----

    def freeze_cpu_start(self, task_id, expected_cpu_cores, expected_cpu_source,
                         dry_run=False):
        """Freeze the authoritative expected CPU per task (INSERT OR IGNORE).

        Called under the existing launch lock at placement. Never overwrites
        an existing freeze. Writes are skipped on dry_run (readonly).
        """
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT expected_cpu_cores, expected_cpu_source FROM cpu_starts WHERE task_id = ?",
                (task_id,)).fetchone()
            if row:
                return {"expected_cpu_cores": row[0], "expected_cpu_source": row[1]}
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO cpu_starts (task_id, expected_cpu_cores, expected_cpu_source) VALUES (?, ?, ?)",
                    (task_id, float(expected_cpu_cores), expected_cpu_source))
            return {"expected_cpu_cores": float(expected_cpu_cores),
                    "expected_cpu_source": expected_cpu_source}

    def get_cpu_start(self, task_id):
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT expected_cpu_cores, expected_cpu_source FROM cpu_starts WHERE task_id = ?",
                (task_id,)).fetchone()
        if not row:
            return None
        return {"expected_cpu_cores": row[0], "expected_cpu_source": row[1]}

    def record_cpu_sample(self, task_id, current_cpu_cores, observed_at,
                          identity, counter, observed_ms, dry_run=False):
        """Persist exact-incarnation current CPU. Skipped on dry_run."""
        if dry_run:
            return
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO cpu_samples (task_id, current_cpu_cores, observed_at, identity, counter, observed_ms) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET current_cpu_cores=excluded.current_cpu_cores, observed_at=excluded.observed_at, "
                "identity=excluded.identity, counter=excluded.counter, observed_ms=excluded.observed_ms",
                (task_id, float(current_cpu_cores), observed_at, str(identity),
                 float(counter), int(observed_ms)))

    def get_cpu_sample(self, task_id):
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT current_cpu_cores, observed_at, identity, counter, observed_ms FROM cpu_samples WHERE task_id = ?",
                (task_id,)).fetchone()
        if not row:
            return None
        return {"current_cpu_cores": row[0], "observed_at": row[1],
                "identity": row[2], "counter": row[3], "observed_ms": row[4]}

    def record_choking_event(self, reason, observed_at, dry_run=False):
        """Keep positive choking cooldown evidence across cycles.

        Writes are skipped on dry_run (readonly). Callers must pass only
        validated fresh reasons (host_oom/host_runtime_choking).
        """
        if dry_run:
            return
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO cpu_choking_events (reason, observed_at) VALUES (?, ?)",
                (reason, observed_at))

    def get_fresh_choking(self, cooldown_s=300, now_ms=None):
        """Latest choking event within cooldown, or None. Read-only."""
        import time as _time
        from datetime import datetime, timezone
        if now_ms is None:
            now_ms = int(_time.time() * 1000)
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT reason, observed_at FROM cpu_choking_events ORDER BY id DESC LIMIT 20").fetchall()
        for reason, observed_at in rows:
            if reason not in ("host_oom", "host_runtime_choking"):
                continue
            try:
                text = str(observed_at).replace("Z", "+00:00")
                at = int(datetime.fromisoformat(text).timestamp() * 1000)
            except (ValueError, AttributeError, TypeError):
                continue
            if at > now_ms + 30000:
                continue
            if 0 <= now_ms - at <= cooldown_s * 1000:
                return {"reason": reason, "observed_at": observed_at}
        return None

    def get_oom_baseline(self, proc_root="/proc"):
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT counter FROM cpu_oom_baseline WHERE proc_root = ?",
                (str(proc_root),)).fetchone()
        return row[0] if row else None

    def set_oom_baseline(self, proc_root, counter, dry_run=False):
        if dry_run:
            return
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO cpu_oom_baseline (proc_root, counter) VALUES (?, ?) "
                "ON CONFLICT(proc_root) DO UPDATE SET counter=excluded.counter",
                (str(proc_root), int(counter)))

    def cpu_ramping_entries(self, exclude_task_id=None, ramp_seconds=180,
                            default_expected=1.0, dry_run=False):
        """Build advisory ramping entries for live tasks.

        Queued entries contribute 0; stable residents at/after the ramp
        contribute 0 (host busy already includes them); young/unknown-age
        non-queued tasks retain max(0, frozen_expected - fresh_current).
        Frozen expected CPU comes from cpu_starts, else the task payload
        authoritative fields, else the conservative default. Current CPU
        comes from exact-incarnation cpu_samples only when fresh (<=180s).
        Read-only except for resolving payload defaults (no writes here).
        """
        import json as _json
        import time as _time
        from launcher.cpu_resources import resolve_expected_cpu
        now_ms = int(_time.time() * 1000)
        entries = []
        with self.get_conn() as conn:
            placeholders = ",".join("?" for _ in RESOURCE_HOLDING_STATES)
            query = (f"SELECT t.id, t.payload, t.state FROM tasks t "
                     f"WHERE t.state IN ({placeholders})")
            params = list(RESOURCE_HOLDING_STATES)
            if exclude_task_id:
                query += " AND t.id != ?"
                params.append(exclude_task_id)
            rows = conn.execute(query, params).fetchall()
        for task_id, raw_payload, state in rows:
            try:
                payload = _json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            except (ValueError, TypeError):
                payload = None
            if not isinstance(payload, dict):
                payload = {}
            frozen = self.get_cpu_start(task_id)
            if frozen is not None:
                expected = frozen["expected_cpu_cores"]
            else:
                try:
                    expected, _source = resolve_expected_cpu(
                        payload, policy={"cpu_default_cores": default_expected})
                except ValueError:
                    continue
            binding = self.get_native_binding(task_id)
            age = self._task_age_seconds(task_id, binding)
            current = None
            sample = self.get_cpu_sample(task_id)
            if sample is not None:
                try:
                    from datetime import datetime, timezone
                    text = str(sample["observed_at"]).replace("Z", "+00:00")
                    at = int(datetime.fromisoformat(text).timestamp() * 1000)
                    if 0 <= now_ms - at <= 180000 and at >= now_ms - 180000:
                        # Only credit exact-incarnation samples.
                        if binding and sample.get("identity") == \
                                f"{binding.get('native_id')}:{binding.get('incarnation')}":
                            current = sample.get("current_cpu_cores")
                        elif not binding:
                            current = None
                except (ValueError, AttributeError, TypeError):
                    current = None
            entries.append({"task_id": task_id, "state": state,
                            "expected_cpu_cores": expected,
                            "current_cpu_cores": current,
                            "age_seconds": age})
        return entries

    def _task_age_seconds(self, task_id, binding=None):
        """Seconds since the task/binding was last updated; None if unknown."""
        from datetime import datetime, timezone
        stamp = None
        if binding:
            with self.get_conn() as conn:
                row = conn.execute(
                    "SELECT updated_at FROM task_native_bindings WHERE task_id = ?",
                    (task_id,)).fetchone()
                if row:
                    stamp = row[0]
        if stamp is None:
            with self.get_conn() as conn:
                row = conn.execute(
                    "SELECT updated_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row:
                    stamp = row[0]
        if not stamp or not isinstance(stamp, str):
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(stamp[:19], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
        import time as _time
        return max(0.0, _time.time() - dt.timestamp())

    def resource_breakdown(self, exclude_task_id=None, *, dry_run=False,
                           reconciliation=None, ramp_reserve_mb=512,
                           estimate_percentile=90, ramp_seconds=180,
                           estimate_multiplier=1.25):
        """Per-task reservation rows under the placement policy.

        Each row is {task_id, state, reserve_mb, basis} where basis is one
        of: queued-estimate, start-ramp, resident-settled, full-unknown,
        settled-dead. Queued tasks hold no RAM before starting (reserve 0;
        the gate only counts active starting ramps). Live residents with an
        exact measurement past the ramp window hold no additional RAM (their
        usage is already inside MemAvailable); young ones hold the measured
        estimate minus proven current usage, mirroring the Hub headroom
        credit. Unknown measurements hold the full allowance (fail
        closed). Dead tasks settle (dry_run only reports them).
        """
        from launcher.resource_policy import resolve_memory_estimate_mb
        if type(ramp_reserve_mb) is not int or ramp_reserve_mb <= 0:
            raise ValueError(f"invalid ramp_reserve_mb: {ramp_reserve_mb!r}")
        if isinstance(estimate_multiplier, bool) \
                or not isinstance(estimate_multiplier, (int, float)) \
                or not math.isfinite(estimate_multiplier) \
                or not (1 <= estimate_multiplier <= 4):
            raise ValueError(f"invalid estimate_multiplier: {estimate_multiplier!r}")
        mini_policy = {"ramp_reserve_default_mb": ramp_reserve_mb,
                       "estimate_multiplier": estimate_multiplier}
        measured = self.memory_samples_p90(estimate_percentile)
        rows_out = []
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
        # to prevent SQLite deadlocks.
        for row in rows:
            task_id, mem, disk, raw_payload, state = row
            allowance = mem or 0
            payload = None
            if isinstance(raw_payload, str):
                try:
                    payload = json.loads(raw_payload)
                except (json.JSONDecodeError, ValueError):
                    payload = None
            elif isinstance(raw_payload, dict):
                payload = raw_payload
            if not isinstance(payload, dict):
                payload = {}
            estimate = resolve_memory_estimate_mb(
                payload, measured, mini_policy)
            estimate = min(max(1, estimate), max(1, allowance)) if allowance else estimate

            if state == "queued":
                # Queued tasks hold no RAM before starting: only active
                # starting ramps gate placement (Hub headroom contract).
                rows_out.append({"task_id": task_id, "state": state,
                                 "reserve_mb": 0, "disk_mb": disk or 0,
                                 "basis": "queued-estimate"})
                continue

            binding = self.get_native_binding(task_id)
            if not binding and payload and not dry_run:
                try:
                    binding = self.recover_binding_from_artifacts(task_id, payload)
                except Exception:
                    binding = None

            if binding:
                live_info = {}
                status, detail = probe_native_lifecycle(binding, live_info=live_info)
                if status in ("dead", "retired-finished"):
                    # Persist confirmed death as well as releasing accounting.
                    # No result/reviewer evidence is manufactured by a resource scan.
                    if reconciliation is not None:
                        reconciliation.append({"task_id": task_id, "native_id": binding["native_id"],
                                               "previous_state": state, "native_status": status, "memory_mb": allowance})
                    if not dry_run:
                        evidence = False
                        for path in self.get_task_paths(task_id):
                            try:
                                evidence |= Path(path).is_file() and Path(path).stat().st_size > 0
                            except OSError:
                                pass
                        try:
                            self.transition_task(task_id, "completed-awaiting-review" if evidence else "failed", (state,),
                                reason="resource reconciliation: confirmed native exit; no acceptance inferred",
                                reviewer="resource-reconciler")
                        except StateTransitionError:
                            pass  # A concurrent lifecycle writer owns the newer state.
                    rows_out.append({"task_id": task_id, "state": state,
                                     "reserve_mb": 0, "disk_mb": 0,
                                     "basis": "settled-dead"})
                    continue
                else:
                    # MemAvailable already excludes verified resident anonymous
                    # memory. Stable measured residents (past the ramp window)
                    # hold no additional reserve; young ones hold the estimate
                    # minus proven current usage, mirroring the Hub headroom
                    # credit. Unknown holds the full allowance (fail closed).
                    credit = resident_credit_mb(binding, live_info, allowance) if status == "alive" else 0
                    if status == "alive" and credit > 0:
                        self._record_resident_sample(task_id, binding, credit, dry_run)
                        age = self._task_age_seconds(task_id, binding)
                        if age is not None and age >= ramp_seconds:
                            rows_out.append({"task_id": task_id, "state": state,
                                             "reserve_mb": 0, "disk_mb": disk or 0,
                                             "basis": "resident-settled"})
                        else:
                            rows_out.append({"task_id": task_id, "state": state,
                                             "reserve_mb": max(0, estimate - credit),
                                             "disk_mb": disk or 0,
                                             "basis": "start-ramp"})
                    elif status == "alive" and (
                            state in ("starting", "launch-uncertain", "stalled")
                            or (state == "running"
                                and (self._task_age_seconds(task_id, binding) or 0) < ramp_seconds)):
                        # Inflight start (or young running worker) before
                        # samples land: hold the measured estimate.
                        rows_out.append({"task_id": task_id, "state": state,
                                         "reserve_mb": estimate, "disk_mb": disk or 0,
                                         "basis": "start-ramp"})
                    else:
                        # Legacy or task-units without native binding proof, or
                        # a long-running worker with no measurements:
                        # stays conservative under actual ownership proof.
                        rows_out.append({"task_id": task_id, "state": state,
                                         "reserve_mb": allowance, "disk_mb": disk or 0,
                                         "basis": "full-unknown"})
            else:
                # No binding even after recovery: starting states are proven
                # inflight starts by lifecycle state alone, so young ones hold
                # the ramp estimate. Stale or clock-unknown starts hold the
                # full allowance (fail closed). Running/legacy rows without
                # proof stay conservative under actual ownership proof.
                if state in ("starting", "launch-uncertain", "stalled"):
                    age = self._task_age_seconds(task_id, None)
                    if age is not None and age < ramp_seconds:
                        rows_out.append({"task_id": task_id, "state": state,
                                         "reserve_mb": estimate, "disk_mb": disk or 0,
                                         "basis": "start-ramp"})
                    else:
                        rows_out.append({"task_id": task_id, "state": state,
                                         "reserve_mb": allowance, "disk_mb": disk or 0,
                                         "basis": "full-unknown"})
                else:
                    # Legacy or task-units without native binding:
                    # Stays conservative under actual ownership proof
                    rows_out.append({"task_id": task_id, "state": state,
                                     "reserve_mb": allowance, "disk_mb": disk or 0,
                                     "basis": "full-unknown"})

        return rows_out

    def get_active_resources(self, exclude_task_id=None, *, dry_run=False,
                             reconciliation=None, ramp_reserve_mb=512,
                             estimate_percentile=90, ramp_seconds=180,
                             estimate_multiplier=1.25):
        """Sum outstanding ramp/growth reservations and disk leases.

        Totals the per-task rows from resource_breakdown: queued tasks hold
        nothing, inflight starts hold the measured estimate (payload
        ``memory_estimate_mb`` first, then the sample p90 scaled by
        ``estimate_multiplier``, then ``ramp_reserve_mb``) minus proven
        current resident usage, stable measured residents hold nothing,
        and unknown measurements hold the full allowance.
        ``exclude_task_id`` removes the launching task's own reservation.
        Dead tasks settle unless ``dry_run``.
        """
        rows = self.resource_breakdown(
            exclude_task_id, dry_run=dry_run, reconciliation=reconciliation,
            ramp_reserve_mb=ramp_reserve_mb,
            estimate_percentile=estimate_percentile, ramp_seconds=ramp_seconds,
            estimate_multiplier=estimate_multiplier)
        return (sum(r["reserve_mb"] for r in rows),
                sum(r["disk_mb"] for r in rows))

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
    with open(p, 'a') as f:
        with file_lock(f, exclusive=True):
            yield
