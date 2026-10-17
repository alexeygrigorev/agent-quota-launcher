"""Authoritative native lifecycle verification and tombstone reconciliation.

Provides exact native binding schema, validation, and authoritative lifecycle
probing against active aplexer sessions and retired-session tombstones.
"""
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def validate_native_id(native_id: str) -> str:
    """Validate that native_id is a canonical non-empty UUID string.

    Rejects non-UUID strings, caller PIDs, dummy values, and nil UUIDs.
    """
    if not isinstance(native_id, str):
        raise ValueError(f"native_id must be a string, got {type(native_id).__name__}")
    val = native_id.strip().lower()
    if not UUID_RE.match(val):
        raise ValueError(f"native_id {val!r} is not a valid canonical UUID")
    # Disallow nil UUID (00000000-0000-0000-0000-000000000000)
    if val == "00000000-0000-0000-0000-000000000000":
        raise ValueError("nil UUID is not an authoritative native session ID")
    # Verify caller PID is never passed as a native UUID
    if val == str(os.getpid()):
        raise ValueError("caller PID cannot be used as native_id")
    return val


def validate_native_binding(
    task_id: str,
    native_id: str,
    tag: str,
    workspace: str,
    engine: str,
    incarnation: int,
    details: dict | None = None,
) -> dict:
    """Validate all fields of an exact native binding.

    Never creates/binds an agent registry entry, fabricates whoami,
    borrowed UUID, or caller PID.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be a non-empty string")
    canonical_id = validate_native_id(native_id)

    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("tag must be a non-empty string")

    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty string")
    ws_resolved = str(Path(workspace).resolve())

    if not isinstance(engine, str) or not engine.strip():
        raise ValueError("engine must be a non-empty string")

    if not isinstance(incarnation, (int, float)) or isinstance(incarnation, bool) or incarnation <= 0:
        raise ValueError(f"incarnation must be a positive timestamp (epoch ms), got {incarnation!r}")
    incarnation_int = int(incarnation)

    det = details if (details is not None and isinstance(details, dict)) else {}

    return {
        "task_id": task_id.strip(),
        "native_id": canonical_id,
        "tag": tag.strip(),
        "workspace": ws_resolved,
        "engine": engine.strip(),
        "incarnation": incarnation_int,
        "details": det,
    }


def get_aplexer_state_dir() -> Path:
    """Get authoritative aplexer state directory."""
    custom = os.environ.get("APLEXER_STATE_DIR")
    if custom and custom.strip():
        return Path(custom).resolve()
    return Path(os.path.expanduser("~/.local/state/aplexer")).resolve()


def read_tombstone(native_id: str, state_dir: Path | None = None) -> dict | None:
    """Bounded, explicit read of retired-sessions/<native_id>/tombstone.json.

    Never scans every directory; reads only the explicit native_id file.
    """
    try:
        canonical_id = validate_native_id(native_id)
    except ValueError:
        return None

    base = state_dir if state_dir is not None else get_aplexer_state_dir()
    tombstone_file = base / "retired-sessions" / canonical_id / "tombstone.json"

    if not tombstone_file.is_file():
        return None

    try:
        content = tombstone_file.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def verify_tombstone(
    tombstone: dict,
    expected_id: str,
    expected_tag: str,
    expected_workspace: str,
    expected_incarnation: int | None = None,
) -> tuple[bool, str]:
    """Verify tombstone against expected UUID, tag, workspace, cause, and creation ordering.

    Requires cause == "finished".
    """
    if not isinstance(tombstone, dict):
        return False, "tombstone is not a dictionary"

    # 1. Exact UUID match
    t_id = str(tombstone.get("id") or "").strip().lower()
    if t_id != expected_id.strip().lower():
        return False, f"UUID mismatch: tombstone={t_id!r}, expected={expected_id!r}"

    # 2. Exact tag match
    t_tag = str(tombstone.get("tag") or "").strip()
    if t_tag != expected_tag.strip():
        return False, f"tag mismatch: tombstone={t_tag!r}, expected={expected_tag!r}"

    # 3. Exact workspace match
    t_ws = str(tombstone.get("workspace") or "").strip()
    try:
        if Path(t_ws).resolve() != Path(expected_workspace).resolve():
            return False, f"workspace mismatch: tombstone={t_ws!r}, expected={expected_workspace!r}"
    except Exception:
        if t_ws != expected_workspace:
            return False, f"workspace mismatch: tombstone={t_ws!r}, expected={expected_workspace!r}"

    # 4. Cause finished
    cause = str(tombstone.get("cause") or "").strip()
    if cause != "finished":
        return False, f"cause is {cause!r}, expected 'finished'"

    # 5. Creation ordering: finished_at_ms >= incarnation
    if expected_incarnation is not None and expected_incarnation > 0:
        finished_at = tombstone.get("finished_at_ms")
        if not isinstance(finished_at, (int, float)) or isinstance(finished_at, bool):
            return False, f"invalid or missing finished_at_ms in tombstone: {finished_at!r}"
        if int(finished_at) < int(expected_incarnation):
            return False, f"stale incarnation: finished_at_ms ({finished_at}) < expected_incarnation ({expected_incarnation})"

    return True, "matching finished tombstone verified"


def probe_native_lifecycle(
    binding: dict,
    state_dir: Path | None = None,
    timeout: float = 15.0,
) -> tuple[str, str]:
    """Authoritative lifecycle probe for an exact native session binding.

    Returns (status, detail) where status is one of:
      - 'alive': session is currently active in aplexer
      - 'dead': live session reported dead/exited phase or empty containment
      - 'retired-finished': exact matching tombstone verified with cause=finished
      - 'unknown': missing, transport/timeout error, corrupt/mismatched tombstone

    Conservative: 'unknown' never frees reservations.
    """
    native_id = binding.get("native_id")
    expected_tag = binding.get("tag")
    expected_workspace = binding.get("workspace")
    expected_incarnation = binding.get("incarnation")

    if not native_id:
        return "unknown", "binding missing native_id"

    # Step 1: Probe live aplexer session by global UUID
    res = None
    try:
        res = subprocess.run(
            ["aplexer", "status", native_id, "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "unknown", f"aplexer status timed out after {timeout}s"
    except FileNotFoundError:
        # In test environments without aplexer CLI, fall through to tombstone check
        res = None
    except Exception as e:
        return "unknown", f"aplexer status error: {e}"

    if res is not None:
        out_err = (res.stderr or "") + " " + (res.stdout or "")
        out_err_lower = out_err.lower()
        if "stale" in out_err_lower and "socket" in out_err_lower:
            return "unknown", "aplexer status stale socket"
        if "transport" in out_err_lower:
            return "unknown", "aplexer status transport failure"

        if res.returncode == 0:
            try:
                info = json.loads(res.stdout) if res.stdout.strip().startswith("{") else None
            except Exception as e:
                return "unknown", f"unparsable status JSON: {e}"

            if isinstance(info, dict):
                info_id = str(info.get("id") or "").strip().lower()
                if info_id and info_id != native_id.lower():
                    return "unknown", f"aplexer status returned mismatched id: {info_id}"

                phase = info.get("phase")
                if phase in ("running", "starting", "working", "launching"):
                    if info.get("containment_empty", False):
                        return "dead", f"containment empty (phase={phase})"
                    return "alive", f"phase={phase} containment_empty={info.get('containment_empty')}"
                if phase in ("exited", "dead", "failed", "completed", "finished"):
                    return "dead", f"phase={phase}"
                return "unknown", f"unrecognized phase: {phase!r}"
            return "unknown", "status output is not a JSON dictionary"

    # Step 2: Session is not in active live sessions (or aplexer returned non-zero / not found).
    # Check bounded explicit retired tombstone!
    tombstone = read_tombstone(native_id, state_dir=state_dir)
    if tombstone is None:
        rc = res.returncode if res is not None else -1
        err = ((res.stderr or res.stdout or "").strip()[:100]) if res is not None else "aplexer unavailable"
        return "unknown", f"aplexer status rc={rc} ({err}); no matching retired tombstone"

    ok, msg = verify_tombstone(
        tombstone,
        expected_id=native_id,
        expected_tag=expected_tag,
        expected_workspace=expected_workspace,
        expected_incarnation=expected_incarnation,
    )
    if ok:
        return "retired-finished", f"proven native exit: {msg}"
    return "unknown", f"retired tombstone rejected: {msg}"
