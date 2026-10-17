"""Authoritative native lifecycle verification and tombstone reconciliation.

Provides exact native binding schema, validation, and authoritative lifecycle
probing against active aplexer sessions and retired-session tombstones.
"""
import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
MAX_TOMBSTONE_BYTES = 65536
MAX_ARTIFACT_JSON_BYTES = 1048576

ACTIVE_PHASES = ("running", "starting", "working", "launching")
TERMINAL_PHASES = ("exited", "dead", "failed", "completed", "finished")

FORBIDDEN_DETAIL_KEYS = {
    "env", "argv", "prompt", "start_json", "command", "raw_env",
    "adapter_argv", "child_env", "env_unset",
}


def sanitize_binding_details(details: dict | None) -> dict:
    """Sanitize and compact details, stripping raw env/argv/prompt/start_json."""
    if not details or not isinstance(details, dict):
        return {}
    compact = {}
    for k, v in details.items():
        if k in FORBIDDEN_DETAIL_KEYS:
            if k == "start_json" and isinstance(v, dict):
                phase = v.get("phase")
                if isinstance(phase, str) and phase.strip():
                    compact["start_phase"] = phase.strip()
                w_pid = v.get("worker_pid")
                if isinstance(w_pid, int) and not isinstance(w_pid, bool) and w_pid > 0:
                    compact["worker_pid"] = w_pid
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            if isinstance(v, str) and len(v) > 500:
                compact[k] = v[:500]
            else:
                compact[k] = v
        elif isinstance(v, dict):
            sub = sanitize_binding_details(v)
            if sub:
                compact[k] = sub
    return compact


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
    borrowed UUID, or caller PID. Enforces non-empty absolute workspace,
    positive non-bool incarnation, and compact details.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be a non-empty string")
    canonical_id = validate_native_id(native_id)

    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("tag must be a non-empty string")

    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty string")
    ws_str = workspace.strip()
    if not os.path.isabs(ws_str):
        raise ValueError(f"workspace must be an absolute path, got {workspace!r}")
    ws_resolved = str(Path(ws_str).resolve())

    if not isinstance(engine, str) or not engine.strip():
        raise ValueError("engine must be a non-empty string")

    if isinstance(incarnation, bool) or not isinstance(incarnation, (int, float)) or incarnation <= 0:
        raise ValueError(f"incarnation must be a positive timestamp (epoch ms), got {incarnation!r}")
    incarnation_int = int(incarnation)

    det = sanitize_binding_details(details)

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


def read_bounded_json_file(file_path: Path | str, max_bytes: int = 65536) -> dict | None:
    """Read a JSON dictionary from a bounded, regular, nofollow file.

    Rejects:
    - Non-existent files
    - Symlinks (nofollow)
    - Non-regular files (directories, FIFOs, devices)
    - Files exceeding max_bytes
    - Malformed JSON or non-dict root
    """
    p = Path(file_path)
    try:
        st = os.lstat(p)
    except (OSError, ValueError):
        return None

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
        return None

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        fd = os.open(str(p), flags)
    except (OSError, ValueError):
        return None

    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or fst.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb") as f:
            raw = f.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return None
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
            return None
    except Exception:
        return None


def read_tombstone(native_id: str, state_dir: Path | None = None) -> dict | None:
    """Bounded, explicit read of retired-sessions/<native_id>/tombstone.json.

    Never scans every directory; reads only the explicit native_id file.
    Rejects symlinks, oversize files, and malformed JSON.
    """
    try:
        canonical_id = validate_native_id(native_id)
    except ValueError:
        return None

    base = state_dir if state_dir is not None else get_aplexer_state_dir()
    tombstone_file = base / "retired-sessions" / canonical_id / "tombstone.json"
    return read_bounded_json_file(tombstone_file, max_bytes=MAX_TOMBSTONE_BYTES)


def verify_tombstone(
    tombstone: dict,
    expected_id: str,
    expected_tag: str,
    expected_workspace: str,
    expected_incarnation: int | None = None,
) -> tuple[bool, str]:
    """Verify tombstone against expected UUID, tag, workspace, cause, and creation ordering.

    Requires cause == "finished". Missing workspace never resolves cwd.
    Native tombstones lack engine/creation so does not demand absent fields or fabricate.
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

    # 3. Exact workspace match (absolute; missing workspace must never resolve cwd)
    t_ws = tombstone.get("workspace")
    if not isinstance(t_ws, str) or not t_ws.strip() or not os.path.isabs(t_ws.strip()):
        return False, f"tombstone workspace is missing or not absolute: {t_ws!r}"
    t_ws_clean = t_ws.strip()
    try:
        if Path(t_ws_clean).resolve() != Path(expected_workspace).resolve():
            return False, f"workspace mismatch: tombstone={t_ws_clean!r}, expected={expected_workspace!r}"
    except Exception:
        return False, f"workspace resolution error for {t_ws_clean!r}"

    # 4. Cause finished
    cause = str(tombstone.get("cause") or "").strip()
    if cause != "finished":
        return False, f"cause is {cause!r}, expected 'finished'"

    # 5. Creation ordering: finished_at_ms >= incarnation
    if expected_incarnation is not None:
        if isinstance(expected_incarnation, bool) or not isinstance(expected_incarnation, (int, float)) or expected_incarnation <= 0:
            return False, f"invalid expected_incarnation: {expected_incarnation!r}"
        finished_at = tombstone.get("finished_at_ms")
        if isinstance(finished_at, bool) or not isinstance(finished_at, (int, float)) or finished_at <= 0:
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

    Requires actual exact UUID, tag, absolute workspace, engine, and created_at_ms
    matching the persisted binding.
    Active/starting is always reserved even if containment_empty is true.
    Live terminal requires full identity and explicit containment_empty is True;
    otherwise unknown.
    """
    native_id = binding.get("native_id")
    expected_tag = binding.get("tag")
    expected_workspace = binding.get("workspace")
    expected_engine = binding.get("engine")
    expected_incarnation = binding.get("incarnation")

    if not native_id:
        return "unknown", "binding missing native_id"
    if not expected_tag:
        return "unknown", "binding missing tag"
    if not expected_workspace:
        return "unknown", "binding missing workspace"
    if not expected_engine:
        return "unknown", "binding missing engine"
    if isinstance(expected_incarnation, bool) or not isinstance(expected_incarnation, (int, float)) or expected_incarnation <= 0:
        return "unknown", "binding missing or invalid incarnation"

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

            if not isinstance(info, dict):
                return "unknown", "status output is not a JSON dictionary"

            # 1. Exact UUID match
            info_id = str(info.get("id") or "").strip().lower()
            if not info_id or info_id != native_id.lower():
                return "unknown", f"aplexer status returned mismatched id: {info_id!r} != {native_id!r}"

            # 2. Exact tag match
            info_tag = str(info.get("tag") or "").strip()
            if not info_tag or info_tag != expected_tag.strip():
                return "unknown", f"aplexer status returned mismatched tag: {info_tag!r} != {expected_tag!r}"

            # 3. Exact workspace match (absolute)
            info_ws = info.get("workspace")
            if not isinstance(info_ws, str) or not info_ws.strip() or not os.path.isabs(info_ws.strip()):
                return "unknown", f"aplexer status returned missing or non-absolute workspace: {info_ws!r}"
            try:
                if Path(info_ws.strip()).resolve() != Path(expected_workspace).resolve():
                    return "unknown", f"aplexer status returned mismatched workspace: {info_ws!r} != {expected_workspace!r}"
            except Exception:
                return "unknown", f"workspace resolution error for {info_ws!r}"

            # 4. Exact engine match
            info_engine = str(info.get("engine") or "").strip()
            if not info_engine or info_engine != expected_engine.strip():
                return "unknown", f"aplexer status returned mismatched engine: {info_engine!r} != {expected_engine!r}"

            # 5. Exact incarnation match (created_at_ms)
            info_created = info.get("created_at_ms")
            if isinstance(info_created, bool) or not isinstance(info_created, (int, float)) or int(info_created) != int(expected_incarnation):
                return "unknown", f"aplexer status returned mismatched created_at_ms: {info_created!r} != {expected_incarnation!r}"

            # Full identity verified! Check phase and containment
            phase = info.get("phase")
            c_empty = info.get("containment_empty")

            if phase in ACTIVE_PHASES:
                # Active/starting always reserved even containment_empty true;
                # malformed/truthy string false cannot release.
                return "alive", f"phase={phase} containment_empty={c_empty}"

            if phase in TERMINAL_PHASES:
                # Live terminal requires full identity and explicit containment_empty is True; otherwise unknown.
                if c_empty is True:
                    return "dead", f"phase={phase} containment_empty=True"
                return "unknown", f"terminal phase={phase} but containment_empty is not explicitly True (got {c_empty!r})"

            return "unknown", f"unrecognized phase: {phase!r}"

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
