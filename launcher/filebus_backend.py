"""Headless FileBus backend (C2437). Separate from launch.py/cli.py.

Native aplexer whoami is not worker identity here. A genuine FileBus worker
proves task_id + bus_identity + original task/readACK/reply/outcome. This
module does not spawn `aplexer start` and does not run systemd-run until
source tests of transient task units outside the head aplexer cgroup exist.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from launcher.admission import validate_quse
from launcher.resources import check_resources
from launcher.store import Store

FILEBUS_LIVE_IDENTITY_REQUIRED = (
    "task_id",
    "bus_identity",
    "task_message_id",
    "provider",
)
FILEBUS_TERMINAL_RECEIPT_REQUIRED = (
    "ack_id",
    "reply_id",
    "outcome",
)
# Backward-compatible alias: full record is live + terminal, never invented at start.
FILEBUS_IDENTITY_REQUIRED = FILEBUS_LIVE_IDENTITY_REQUIRED + FILEBUS_TERMINAL_RECEIPT_REQUIRED

NATIVE_WHOAMI_MARKERS = ("worker_cgroup", "workload_cgroup", "socket_path")

# C2506/C2512: same-UID privacy check, not a security boundary (rename/symlink
# still share the host UID). Catch the exact inheritance defect plus head role.
HEAD_CRED_GRANT_RE = re.compile(
    r"--cred(?:\s+|=)\S*filebus/head\.cred\b",
    re.IGNORECASE,
)
HEAD_CRED_FIELD_KEYS = ("cred", "cred_path", "credential", "filebus_cred")
HEAD_BUS_IDENTITY_ID = "ad6251d7-49f8-4f20-b8a1-d5af62412c5c"
HEAD_BUS_AGENT = "quota-launcher-head"


class FileBusBackendError(Exception):
    pass


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, val in obj.items():
            yield str(key)
            yield from _walk_strings(val)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _walk_strings(item)


def payload_grants_head_cred(payload) -> bool:
    """True when a worker payload would inherit the head FileBus credential."""
    if not isinstance(payload, dict):
        return False
    worker_agent = payload.get("bus_identity") or payload.get("agent_name")
    if worker_agent == HEAD_BUS_AGENT:
        return True
    worker_id = payload.get("identity_id") or payload.get("bus_identity_id")
    if worker_id == HEAD_BUS_IDENTITY_ID:
        return True
    for key in HEAD_CRED_FIELD_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and "filebus/head.cred" in val.replace("\\", "/"):
            return True
    for text in _walk_strings(payload):
        if HEAD_CRED_GRANT_RE.search(text.replace("\\", "/")):
            return True
    return False


def reject_head_cred_inheritance(payload) -> None:
    """Fail closed if payload/goal would pass head.cred to a worker (C2506)."""
    if payload_grants_head_cred(payload):
        raise FileBusBackendError(
            "head.cred must stay private from workers; payload/goal grants HEAD mailbox"
        )


def is_native_whoami(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    return any(k in record for k in NATIVE_WHOAMI_MARKERS)


def _nonempty_str(record: dict, key: str) -> bool:
    val = record.get(key)
    return isinstance(val, str) and bool(val.strip())


def validate_filebus_live_identity(record: dict) -> bool:
    """Start/enrollment identity. Must not require future ack/reply/outcome."""
    if not isinstance(record, dict):
        return False
    if is_native_whoami(record):
        return False
    if any(_nonempty_str(record, k) for k in FILEBUS_TERMINAL_RECEIPT_REQUIRED):
        return False
    return all(_nonempty_str(record, k) for k in FILEBUS_LIVE_IDENTITY_REQUIRED)


def validate_filebus_terminal_receipt(record: dict) -> bool:
    """Terminal receipt, distinct from live identity. Owned bus events only."""
    if not isinstance(record, dict):
        return False
    if is_native_whoami(record):
        return False
    if not all(_nonempty_str(record, k) for k in FILEBUS_IDENTITY_REQUIRED):
        return False
    if record.get("outcome") not in ("accepted", "completed", "failed", "blocked"):
        return False
    return True


def validate_filebus_identity(record: dict) -> bool:
    """Full correlated record (live + terminal). Not for enrollment-at-start."""
    return validate_filebus_terminal_receipt(record)


def admit_headless(quse_json, memory_mb, workspace, tmpdir, active_mem_mb=0, active_disk_mb=0):
    """Reuse canonical admission. Fail-closed. Does not raise pids.max."""
    validate_quse(quse_json)
    if int(memory_mb) > 1500:
        raise FileBusBackendError("worker memory exceeds 1500M ceiling")
    check_resources(int(memory_mb), workspace, tmpdir, active_mem_mb, active_disk_mb,
                    repo_root=workspace)
    tmp = Path(tmpdir).resolve()
    root = Path(workspace).resolve() / ".local" / "tmp"
    if not tmp.is_relative_to(root):
        raise FileBusBackendError("TMPDIR must be under repo .local/tmp")
    return True


def dispatch_headless_task(payload: dict) -> dict:
    """Fail-closed until transient task-unit path is source-tested.

    Nested `aplexer start` from quota-launcher-head inherits the head
    100-pid/1500M cgroup (C2438). Sidecar/coordinator already live in
    session-10825.scope outside that unit; this function still refuses
    live spawn until a tested systemd-run --user transient unit recipe
    exists that does not raise parent limits.
    """
    if payload.get("backend") != "filebus":
        raise FileBusBackendError("backend must be filebus")
    reject_head_cred_inheritance(payload)
    raise FileBusBackendError(
        "live filebus dispatch held: missing source-tested transient "
        "task unit outside aplexer-workload-6be4c247; sidecar session-10825 "
        "is outside head cgroup but has no documented spawn API"
    )


BLOCKED_REASON = (
    "filebus queued; live dispatch held until launcher/task_units.py "
    "source-tests a manager-spawned sibling TASK unit outside the head cgroup"
)


def plan_filebus_task(store: Store, task_id: str, idempotency_key: str, payload: dict,
                      paths, memory_mb=1500, disk_mb=512) -> str:
    """Queue a FileBus task in the canonical Store. Does not spawn a worker."""
    if payload.get("backend") != "filebus":
        raise FileBusBackendError("backend must be filebus")
    reject_head_cred_inheritance(payload)
    if int(memory_mb) > 1500:
        raise FileBusBackendError("worker memory exceeds 1500M ceiling")
    queued = store.submit_task(task_id, idempotency_key, payload, paths, memory_mb, disk_mb)
    store.record_reason(queued, BLOCKED_REASON)
    return queued


def attach_filebus_live_identity(store: Store, task_id: str, identity: dict, receipt_path: str) -> dict:
    """Persist live enrollment identity. Rejects invented ack/reply/outcome."""
    if not validate_filebus_live_identity(identity):
        raise FileBusBackendError("invalid FileBus live identity")
    task = store.get_task(task_id)
    if task is None:
        raise FileBusBackendError("unknown task_id")
    if identity["task_id"] != task_id:
        raise FileBusBackendError("identity.task_id mismatch")
    path = Path(receipt_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    return identity


def attach_filebus_identity(store: Store, task_id: str, identity: dict, receipt_path: str) -> dict:
    """Persist a terminal FileBus receipt. Native whoami is rejected."""
    if not validate_filebus_identity(identity):
        raise FileBusBackendError("invalid FileBus identity")
    task = store.get_task(task_id)
    if task is None:
        raise FileBusBackendError("unknown task_id")
    if identity["task_id"] != task_id:
        raise FileBusBackendError("identity.task_id mismatch")
    path = Path(receipt_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    return identity
