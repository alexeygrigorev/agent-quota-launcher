"""Manager-spawned bounded sibling systemd TASK units (C2438 / C2441).

Spawns detached transient task service units outside the interactive head's
cgroup (aplexer-workload-6be4c247-4410-4bdb-968e-7fc2d5844941.scope).
Each task unit receives its own independent MemoryMax (<= 1500M) and TasksMax (100)
under app.slice, preventing parent-bound 100-PID exhaustion and allowing
up to 50 concurrent headless workers across host memory.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from launcher.admission import validate_quse
from launcher.resources import check_resources

MAX_MEMORY_MB = 1500
TASKS_MAX = 100
MAX_DISK_LOG_BYTES = 64 * 1024  # 64 KiB bounded disk log
CLEANUP_POLL_INTERVAL_SEC = 0.05
CLEANUP_TIMEOUT_SEC = 5.0

HEAD_SCOPE_FORBIDDEN_MARKERS = (
    "aplexer-workload-6be4c247-4410-4bdb-968e-7fc2d5844941",
    "aplexer-workload-",
)


class TaskUnitError(Exception):
    """Base exception for task unit operations."""
    pass


class TaskUnitAdmissionError(TaskUnitError):
    """Raised when task unit violates quota or resource floors."""
    pass


class TaskUnitExecutionError(TaskUnitError):
    """Raised when task unit execution fails or encounters cgroup errors."""
    pass


class TaskUnitCleanupError(TaskUnitError):
    """Raised when task unit fails to cleanly dissolve lingering PIDs."""
    pass


class TaskUnitTimeoutError(TaskUnitExecutionError):
    """Timeout without claiming foreign unit ownership."""

    def __init__(self, message: str, identity: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.identity = identity or {}


def invocation_ids_match(expected: Optional[str], live: Optional[str]) -> bool:
    """Kill/stop requires nonempty expected Invocation AND nonempty live match."""
    exp = (expected or "").strip()
    liv = (live or "").strip()
    return bool(exp) and bool(liv) and exp == liv


def show_unit_props(
    unit_name: str,
    properties: Tuple[str, ...] = (
        "InvocationID",
        "ControlGroup",
        "MainPID",
        "ExecMainPID",
        "LoadState",
        "ActiveState",
    ),
) -> Dict[str, str]:
    cmd = ["systemctl", "--user", "show", unit_name]
    for prop in properties:
        cmd.extend(["-p", prop])
    chk = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=False)
    props: Dict[str, str] = {}
    for line in (chk.stdout or "").splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            props[key] = val
    return props


def timeout_output_text(value) -> str:
    """Popen._check_timeout yields bytes stdout/stderr even when text=True (C2513)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_invocation_id(text: str) -> Optional[str]:
    match = re.search(r"invocation ID:\s*([a-zA-Z0-9_\-]+)", text or "", re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip() or None


def signal_owned_unit(unit_name: str, expected_inv: Optional[str], verb: str) -> bool:
    """Issue kill/stop only when nonempty expected Invocation matches nonempty live Invocation."""
    if verb not in ("kill", "stop"):
        raise ValueError("verb must be kill or stop")
    live = show_unit_props(unit_name)
    if not invocation_ids_match(expected_inv, live.get("InvocationID")):
        return False
    if verb == "kill":
        cmd = ["systemctl", "--user", "kill", "--signal=SIGKILL", unit_name]
    else:
        cmd = ["systemctl", "--user", "stop", unit_name]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return True


def sanitize_unit_name(task_id: str) -> str:
    """Derive a safe systemd unit name from a task identifier."""
    if not task_id or not isinstance(task_id, str):
        raise TaskUnitAdmissionError("task_id must be a non-empty string")
    clean = task_id.strip()
    if not clean:
        raise TaskUnitAdmissionError("task_id contains no valid unit characters")
    if "/" in clean or "\\" in clean or ".." in clean:
        raise TaskUnitAdmissionError(f"path traversal or directory separator forbidden in task_id: {task_id}")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", clean):
        raise TaskUnitAdmissionError(
            f"task_id contains invalid characters (only alphanumeric, _, - allowed): {task_id}"
        )
    return f"agent-task-{clean}.service"


def assert_cgroup_outside_head(cgroup_path: str) -> bool:
    """Verify that a cgroup path is not nested under any head scope."""
    if not cgroup_path or not isinstance(cgroup_path, str):
        return True
    for marker in HEAD_SCOPE_FORBIDDEN_MARKERS:
        if marker in cgroup_path:
            raise TaskUnitExecutionError(
                f"cgroup '{cgroup_path}' is nested under forbidden head scope marker '{marker}'"
            )
    return True


def admit_task_unit(
    task_id: str,
    memory_mb: int,
    workspace: str,
    tmpdir: Optional[str] = None,
    quse_json: Optional[Dict[str, Any]] = None,
    provider: Optional[str] = None,
    active_mem_mb: int = 0,
    active_disk_mb: int = 0,
    check_capacity: bool = False,
    config_dir: Optional[Path] = None,
) -> bool:
    """Pre-execution admission check enforcing memory, floors, and quota."""
    if int(memory_mb) > MAX_MEMORY_MB:
        raise TaskUnitAdmissionError(
            f"worker memory {memory_mb}M exceeds maximum allowed ceiling of {MAX_MEMORY_MB}M"
        )
    if int(memory_mb) <= 0:
        raise TaskUnitAdmissionError("worker memory must be a positive integer")

    # Fail closed on missing quota evidence (C2447)
    if quse_json is None:
        raise TaskUnitAdmissionError(
            "fresh quse_json evidence is required for task unit admission (fail closed)"
        )
    try:
        valid_routes, rejections = validate_quse(
            quse_json, check_capacity=check_capacity, config_dir=config_dir
        )
        if not valid_routes:
            raise TaskUnitAdmissionError(f"no valid quota route available: {rejections}")
        if provider:
            matching = [
                r for r in valid_routes
                if r.get("provider") == provider or r.get("name") == provider
            ]
            if not matching:
                reason = rejections.get(provider, "provider route not accepted or exhausted")
                raise TaskUnitAdmissionError(
                    f"requested provider '{provider}' quota rejected: {reason}"
                )
    except TaskUnitAdmissionError:
        raise
    except Exception as e:
        raise TaskUnitAdmissionError(f"quota admission validation failed: {e}") from e

    # Check host floors (MemAvailable >= 10 GiB, root disk >= 50 GiB, deny /data)
    try:
        check_resources(
            int(memory_mb),
            workspace,
            tmpdir,
            active_mem_mb=active_mem_mb,
            active_disk_mb=active_disk_mb,
            repo_root=workspace,
        )
    except Exception as e:
        raise TaskUnitAdmissionError(f"resource floor check failed: {e}") from e

    # Enforce tmpdir strictly under repo .local/tmp
    repo_path = Path(workspace).resolve()
    if tmpdir is None:
        tmpdir = str(repo_path / ".local" / "tmp" / task_id)
    tmp_path = Path(tmpdir).resolve()
    allowed_tmp_root = repo_path / ".local" / "tmp"
    if not tmp_path.is_relative_to(allowed_tmp_root):
        raise TaskUnitAdmissionError(
            f"TMPDIR '{tmp_path}' must be located under '{allowed_tmp_root}'"
        )

    # Deny /data destination explicitly (18.9 GiB free, below 50 GiB floor)
    if str(tmp_path).startswith("/data") or str(repo_path).startswith("/data"):
        raise TaskUnitAdmissionError("destination under /data is denied due to floor deficit")

    return True


def build_systemd_run_argv(
    unit_name: str,
    command_argv: List[str],
    memory_mb: int,
    tmpdir: str,
    workspace: Optional[str] = None,
    stdout_path: Optional[str] = None,
    stderr_path: Optional[str] = None,
    wait: bool = False,
    slice_name: str = "app.slice",
    extra_env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Construct systemd-run invocation for a detached transient service unit.

    Notice: Does NOT pass `--scope`. Runs as a standalone transient `.service`
    unit directly under the user manager, ensuring it does not inherit the
    head's 100-task or 1500M cgroup ceiling.
    """
    if not unit_name.endswith(".service"):
        raise TaskUnitAdmissionError(f"transient unit must end with .service, got: {unit_name}")

    argv = [
        "systemd-run",
        "--user",
        f"--unit={unit_name}",
        f"--slice={slice_name}",
        "--collect",
    ]
    if wait:
        argv.append("--wait")
    argv.extend([
        "-p", f"MemoryMax={int(memory_mb)}M",
        "-p", f"TasksMax={TASKS_MAX}",
        "-E", f"TMPDIR={tmpdir}",
        "-E", f"TEMP={tmpdir}",
        "-E", f"TMP={tmpdir}",
    ])

    # Propagate strict whitelisted execution PATH, HOME, USER to transient unit
    path_entries = [
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".nvm" / "versions" / "node" / "v24.13.1" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    valid_paths = [p for p in path_entries if os.path.isdir(p)]
    unit_path = ":".join(valid_paths)

    argv.extend([
        "-E", f"PATH={unit_path}",
        "-E", f"HOME={os.environ.get('HOME', str(Path.home()))}",
        "-E", f"USER={os.environ.get('USER', 'alexey')}",
    ])

    # Propagate ZCode runtime environment if present
    zcode_cjs = os.environ.get("ZCODE_CJS", "/opt/ZCode/resources/glm/zcode.cjs")
    if os.path.exists(zcode_cjs):
        argv.extend(["-E", f"ZCODE_CJS={zcode_cjs}"])

    if extra_env:
        # Securely pass extra_env via temporary EnvironmentFile (0600) to prevent cmdline secret leaks
        tmp_p = Path(tmpdir)
        tmp_p.mkdir(parents=True, exist_ok=True)
        env_file_path = tmp_p / f"{unit_name}.env"
        lines = [f"{k}={v}\n" for k, v in sorted(extra_env.items()) if k and v is not None]
        env_file_path.write_text("".join(lines), encoding="utf-8")
        env_file_path.chmod(0o600)
        argv.extend(["-p", f"EnvironmentFile={env_file_path}"])
    if workspace:
        ws_path = Path(workspace).resolve()
        if not ws_path.is_dir():
            raise TaskUnitAdmissionError(f"workspace path does not exist or is not a directory: {workspace}")
        argv.extend(["-p", f"WorkingDirectory={ws_path}"])
    if stdout_path:
        argv.extend(["-p", f"StandardOutput=file:{stdout_path}"])
    if stderr_path:
        argv.extend(["-p", f"StandardError=file:{stderr_path}"])
    if not stdout_path and not stderr_path and not wait:
        argv.append("--pipe")

    argv.append("--")
    argv.extend(list(command_argv))
    return argv


def generate_prelude_code(expected_unit_name: str, expected_workspace: Optional[str] = None) -> str:
    """Generate inline Python prelude verifying cgroup isolation and working directory before execution."""
    ws_check_code = ""
    if expected_workspace:
        ws_resolved = str(Path(expected_workspace).resolve())
        ws_check_code = f"""
# 4. Verify working directory matches expected workspace (fail-closed per C2465/C2468)
expected_ws = {repr(ws_resolved)}
current_cwd = os.path.realpath(os.getcwd())
if current_cwd != os.path.realpath(expected_ws):
    sys.stderr.write(f"FATAL: Service cwd '{{current_cwd}}' does not match expected workspace '{{expected_ws}}'\\n")
    sys.exit(99)
"""

    return f"""# Auto-generated task unit prelude
import os, sys

# 1. Inspect own cgroup
try:
    with open('/proc/self/cgroup', 'r') as f:
        cg_content = f.read().strip()
except Exception as e:
    sys.stderr.write(f"Prelude error reading /proc/self/cgroup: {{e}}\\n")
    sys.exit(95)

# 2. Verify NOT in head scope
forbidden = {repr(HEAD_SCOPE_FORBIDDEN_MARKERS)}
for mark in forbidden:
    if mark in cg_content:
        sys.stderr.write(f"FATAL: Process running inside forbidden head scope {{mark}}\\n")
        sys.exit(96)

# 3. Verify in expected transient unit and app.slice (fail-closed per C2447)
unit_base = {repr(expected_unit_name.replace('.service', ''))}
if unit_base not in cg_content:
    sys.stderr.write(f"FATAL: cgroup '{{cg_content}}' does not match expected unit '{{unit_base}}'\\n")
    sys.exit(97)
if 'app.slice' not in cg_content:
    sys.stderr.write(f"FATAL: cgroup '{{cg_content}}' is not placed under app.slice\\n")
    sys.exit(98)
{ws_check_code}
# Proceed to execute wrapped command
if len(sys.argv) > 1:
    os.execvp(sys.argv[1], sys.argv[1:])
"""


def _is_cgroup_dissolved_or_empty(cg_rel_path: str) -> bool:
    """Check cgroup.events for populated 0 and verify all descendant cgroup.procs empty."""
    cgroup_root = Path("/sys/fs/cgroup")
    clean_rel = cg_rel_path.strip("/")
    target = cgroup_root / clean_rel

    if not target.exists():
        return True

    events_file = target / "cgroup.events"
    if events_file.exists():
        try:
            content = events_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("populated"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] != "0":
                        return False
        except Exception:
            pass

    for procs_file in target.glob("**/cgroup.procs"):
        try:
            pids = procs_file.read_text(encoding="utf-8").strip()
            if pids:
                return False
        except Exception:
            pass

    return True


def verify_task_unit_cleanup(
    unit_name: str,
    timeout_sec: float = CLEANUP_TIMEOUT_SEC,
    systemctl_cmd: Optional[List[str]] = None,
    expected_invocation_id: Optional[str] = None,
    expected_cgroup: Optional[str] = None,
    expected_pid: Optional[int] = None,
) -> Tuple[bool, Dict[str, str]]:
    """Verify that a transient unit has stopped and its cgroup is fully dissolved.

    Handles systemd --collect metadata erasure cleanly: when a unit completes and
    systemd garbage-collects its properties (ActiveState=inactive, SubState=dead,
    ControlGroup="", InvocationID=""), verifies that the known prior cgroup
    and PID are fully dead. Never touches foreign or reused active units.
    """
    cmd_base = systemctl_cmd or ["systemctl", "--user"]
    deadline = time.time() + timeout_sec
    props: Dict[str, str] = {}

    while time.time() < deadline:
        try:
            res = subprocess.run(
                cmd_base + [
                    "show",
                    unit_name,
                    "-p", "ActiveState",
                    "-p", "SubState",
                    "-p", "ControlGroup",
                    "-p", "InvocationID",
                    "-p", "ExecMainPID",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            props = {}
            for line in res.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v

            active_state = props.get("ActiveState", "unknown")
            sub_state = props.get("SubState", "unknown")
            cg = props.get("ControlGroup", "")
            inv_id = props.get("InvocationID", "")
            pid_str = props.get("ExecMainPID", "0")
            current_pid = int(pid_str) if pid_str.isdigit() else 0

            # If expected invocation was given and a different or unverified active invocation is present, do not touch it
            if expected_invocation_id:
                if inv_id and inv_id != expected_invocation_id:
                    return False, props
                if active_state not in ("inactive", "failed") and not inv_id:
                    return False, props

            # If inactive or dead, verify cgroup dissolution and PID termination
            if active_state in ("inactive", "failed") and sub_state in ("dead", "failed", ""):
                # 1. Verify tracked PID is dead
                target_pid = expected_pid or (current_pid if current_pid > 0 else None)
                if target_pid and os.path.exists(f"/proc/{target_pid}"):
                    return False, props

                # 2. Verify cgroup dissolution (either current cg, expected cg, or slice path)
                target_cg = cg or expected_cgroup or f"app.slice/{unit_name}"
                if _is_cgroup_dissolved_or_empty(target_cg):
                    return True, props

        except Exception:
            pass
        time.sleep(CLEANUP_POLL_INTERVAL_SEC)

    # Final attempt to stop unit if still running and verified owned
    try:
        inv_id = props.get("InvocationID", "")
        active_state = props.get("ActiveState", "unknown")
        # Only stop when nonempty expected Invocation matches nonempty live Invocation.
        if not invocation_ids_match(expected_invocation_id, inv_id):
            return False, props

        subprocess.run(
            cmd_base + ["stop", unit_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass

    return False, props


def _bounded_pipe_pump(pipe, target_path: Path, max_bytes: int):
    """Pump subprocess stream to disk with strict byte bounding."""
    bytes_written = 0
    try:
        with open(target_path, "wb") as f:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                if bytes_written < max_bytes:
                    allowed = min(len(chunk), max_bytes - bytes_written)
                    f.write(chunk[:allowed])
                    bytes_written += allowed
                    f.flush()
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def execute_transient_task_unit(
    task_id: str,
    command_argv: List[str],
    memory_mb: int,
    workspace: str,
    tmpdir: Optional[str] = None,
    timeout_sec: float = 1200.0,
    quse_json: Optional[Dict[str, Any]] = None,
    provider: Optional[str] = None,
    log_dir: Optional[str] = None,
    check_capacity: bool = False,
    config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute a task in a manager-spawned sibling systemd unit.

    Returns an execution receipt detailing unit, cgroup, timing, and exit code.
    Fails closed if admission fails, unit fails to start, or lingering PIDs remain.
    """
    # 0. Calculate loaded module digest BEFORE execution per C2469
    try:
        module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except Exception:
        module_sha256 = "unknown"

    # 1. Admission check
    admit_task_unit(
        task_id,
        memory_mb,
        workspace,
        tmpdir,
        quse_json=quse_json,
        provider=provider,
        check_capacity=check_capacity,
        config_dir=config_dir,
    )
    unit_name = sanitize_unit_name(task_id)

    repo_dir = Path(workspace).resolve()
    if not repo_dir.is_dir():
        raise TaskUnitAdmissionError(f"workspace directory does not exist: {workspace}")
    if tmpdir is None:
        tmpdir = str(repo_dir / ".local" / "tmp" / task_id)
    tmpdir_path = Path(tmpdir).resolve()
    tmpdir_path.mkdir(mode=0o700, parents=True, exist_ok=True)

    out_dir = Path(log_dir).resolve() if log_dir else repo_dir / ".local"
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = out_dir / f"{task_id}-stdout.log"
    stderr_log = out_dir / f"{task_id}-stderr.log"
    stdout_log.touch(mode=0o600, exist_ok=True)
    stderr_log.touch(mode=0o600, exist_ok=True)

    # 2. Write prelude with verified expected workspace (fail-closed cwd check)
    prelude_script = tmpdir_path / f"prelude_{task_id}.py"
    prelude_code = generate_prelude_code(unit_name, expected_workspace=str(repo_dir))
    prelude_script.write_text(prelude_code, encoding="utf-8")
    prelude_script.chmod(0o700)

    # 3. Construct systemd-run invocation with file redirection (0 pump threads) and explicit WorkingDirectory
    wrapped_command = [sys.executable, str(prelude_script)] + list(command_argv)
    systemd_cmd = build_systemd_run_argv(
        unit_name=unit_name,
        command_argv=wrapped_command,
        memory_mb=memory_mb,
        tmpdir=str(tmpdir_path),
        workspace=str(repo_dir),
        stdout_path=str(stdout_log),
        stderr_path=str(stderr_log),
        wait=True,
    )

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    clean_env = dict(os.environ)
    clean_env["TMPDIR"] = str(tmpdir_path)
    clean_env["TEMP"] = str(tmpdir_path)
    clean_env["TMP"] = str(tmpdir_path)

    assigned_inv_id = None
    proc = None

    try:
        proc = subprocess.Popen(
            systemd_cmd,
            cwd=str(repo_dir),
            env=clean_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        # Expected identity is the systemd-run launch witness, not an immediate
        # systemctl show (same-name unit can still be live from a prior run).

        captured_cg = ""
        captured_mem_peak = None
        captured_cpu_usage = None
        
        def _poll_telemetry():
            nonlocal captured_cg, captured_mem_peak, captured_cpu_usage
            while proc.poll() is None:
                try:
                    props = show_unit_props(unit_name, properties=("ControlGroup", "MemoryPeak", "CPUUsageNSec"))
                    cg_val = props.get("ControlGroup")
                    if cg_val and cg_val.strip() and cg_val.strip() != "[not set]":
                        captured_cg = cg_val.strip()
                    
                    mp_val = props.get("MemoryPeak")
                    if mp_val and mp_val.isdigit():
                        captured_mem_peak = int(mp_val)
                        
                    cpu_val = props.get("CPUUsageNSec")
                    if cpu_val and cpu_val.isdigit():
                        captured_cpu_usage = int(cpu_val)
                except Exception:
                    pass
                time.sleep(CLEANUP_POLL_INTERVAL_SEC)

        t = threading.Thread(target=_poll_telemetry, daemon=True)
        t.start()

        try:
            out, err = proc.communicate(timeout=timeout_sec)
            combined_run_output = timeout_output_text(out) + "\n" + timeout_output_text(err)
            assigned_inv_id = parse_invocation_id(combined_run_output)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as timed_out:
            partial = (
                timeout_output_text(getattr(timed_out, "stdout", None))
                + "\n"
                + timeout_output_text(getattr(timed_out, "stderr", None))
            )
            assigned_inv_id = parse_invocation_id(partial)
            live = show_unit_props(unit_name)
            killed = signal_owned_unit(unit_name, assigned_inv_id, "kill")
            try:
                proc.kill()
            except Exception:
                pass
            identity = {
                "expected_invocation_id": assigned_inv_id or "",
                "live_invocation_id": (live.get("InvocationID") or "").strip(),
                "live_cgroup": (live.get("ControlGroup") or "").strip(),
                "live_pid": (live.get("MainPID") or live.get("ExecMainPID") or "").strip(),
                "load_state": (live.get("LoadState") or "").strip(),
                "killed": killed,
                "witness_source": "systemd-run-timeout-output",
            }
            raise TaskUnitTimeoutError(
                f"task unit {unit_name} exceeded timeout of {timeout_sec}s identity={identity}",
                identity=identity,
            )

    except TaskUnitTimeoutError:
        raise
    except Exception as e:
        signal_owned_unit(unit_name, assigned_inv_id, "stop")
        verify_task_unit_cleanup(
            unit_name,
            timeout_sec=2.0,
            expected_invocation_id=assigned_inv_id,
        )
        raise TaskUnitExecutionError(f"execution failed: {e}") from e

    finally:
        # Remove prelude script
        try:
            if prelude_script.exists():
                prelude_script.unlink()
        except Exception:
            pass

    # 4. Verify post-stop cleanup & cgroup dissolution
    cleaned_up, props = verify_task_unit_cleanup(
        unit_name,
        timeout_sec=CLEANUP_TIMEOUT_SEC,
        expected_invocation_id=assigned_inv_id,
    )
    if not cleaned_up:
        raise TaskUnitCleanupError(
            f"unit {unit_name} did not cleanly dissolve lingering PIDs or cgroup: {props}"
        )

    # 5. Verify cgroup was outside head scope
    cg = captured_cg or props.get("ControlGroup", "")
    if cg:
        assert_cgroup_outside_head(cg)

    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 6. Extract genuine structured telemetry events (no synthesized events)
    events = extract_telemetry_events(stdout_log)
    tool_calls = parse_tool_events(events)
    first_tool = tool_calls[0] if tool_calls else None

    result_info = None
    for ev in events:
        if ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            result_info = ev["result"]
            break

    # Preserve genuine structured events alongside final artifact in workspace
    events_artifact = repo_dir / f"{task_id}-telemetry.jsonl"
    log_events_path = out_dir / f"{task_id}-telemetry.jsonl"
    if events:
        try:
            with open(events_artifact, "w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")
        except Exception:
            pass
        try:
            with open(log_events_path, "w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")
        except Exception:
            pass

    return {
        "task_id": task_id,
        "unit_name": unit_name,
        "invocation_id": assigned_inv_id,
        "exit_code": exit_code,
        "cgroup": cg,
        "memory_peak_bytes": captured_mem_peak,
        "cpu_usage_nsec": captured_cpu_usage,
        "workspace": str(repo_dir),
        "working_directory": str(repo_dir),
        "module_sha256": module_sha256,
        "provider": provider,
        "timeout_sec": timeout_sec,
        "started_at": started_at,
        "finished_at": finished_at,
        "cleanup_verified": True,
        "memory_max_mb": memory_mb,
        "tasks_max": TASKS_MAX,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "telemetry_events_path": str(events_artifact) if events else None,
        "log_telemetry_path": str(log_events_path) if events else None,
        "tool_calls_count": len(tool_calls),
        "first_tool": first_tool,
        "model_status": result_info.get("status") if result_info else None,
        "usage": result_info.get("usage") if result_info else None,
    }


def extract_telemetry_events(stdout_path: Path) -> List[Dict[str, Any]]:
    """Extract genuine structured invocation/tool events from stdout log.
    Never synthesizes events: returns only lines that are valid JSON objects
    representing genuine structured events from adapters."""
    events = []
    if not stdout_path.is_file():
        return events
    try:
        with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not (line.startswith("{") and line.endswith("}")):
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        # Matches agy stream-json, grok streaming-json, or zcodex json
                        if (
                            "event" in obj
                            or "type" in obj
                            or "tool_name" in obj
                            or "step_update" in obj
                            or "result" in obj
                        ):
                            events.append(obj)
                except Exception:
                    continue
    except Exception:
        pass
    return events


def parse_tool_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract genuine tool-call events from structured event stream."""
    tool_calls = []
    for ev in events:
        step = ev.get("step_update") if isinstance(ev.get("step_update"), dict) else ev
        if (
            step.get("step_type") == "tool"
            or "tool_name" in step
            or ("tool_info" in step and isinstance(step.get("tool_info"), dict))
        ):
            tool_name = step.get("tool_name") or (step.get("tool_info") or {}).get("name")
            tool_entry = {
                "tool_name": tool_name,
                "state": step.get("state"),
                "step_index": step.get("step_index"),
                "duration_seconds": step.get("duration_seconds"),
                "tool_info": step.get("tool_info"),
            }
            tool_calls.append(tool_entry)
        elif ev.get("type") in ("tool_use", "tool_call"):
            tool_entry = {
                "tool_name": ev.get("name") or ev.get("tool_name"),
                "state": ev.get("state", "DONE"),
                "parameters": ev.get("parameters") or ev.get("input"),
            }
            tool_calls.append(tool_entry)
    return tool_calls


def spawn_transient_task_unit(
    task_id: str,
    command_argv: List[str],
    memory_mb: int,
    workspace: str,
    tmpdir: Optional[str] = None,
    quse_json: Optional[Dict[str, Any]] = None,
    provider: Optional[str] = None,
    log_dir: Optional[str] = None,
    check_capacity: bool = False,
    config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Asynchronously spawn a manager-spawned sibling systemd unit.

    Returns immediately with unit details and invocation ID.
    0 PIDs and 0 threads remain in the calling controller process during execution.
    """
    try:
        module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except Exception:
        module_sha256 = "unknown"

    admit_task_unit(
        task_id,
        memory_mb,
        workspace,
        tmpdir,
        quse_json=quse_json,
        provider=provider,
        check_capacity=check_capacity,
        config_dir=config_dir,
    )
    unit_name = sanitize_unit_name(task_id)

    repo_dir = Path(workspace).resolve()
    if not repo_dir.is_dir():
        raise TaskUnitAdmissionError(f"workspace directory does not exist: {workspace}")
    if tmpdir is None:
        tmpdir = str(repo_dir / ".local" / "tmp" / task_id)
    tmpdir_path = Path(tmpdir).resolve()
    tmpdir_path.mkdir(mode=0o700, parents=True, exist_ok=True)

    out_dir = Path(log_dir).resolve() if log_dir else repo_dir / ".local"
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = out_dir / f"{task_id}-stdout.log"
    stderr_log = out_dir / f"{task_id}-stderr.log"
    stdout_log.touch(mode=0o600, exist_ok=True)
    stderr_log.touch(mode=0o600, exist_ok=True)

    prelude_script = tmpdir_path / f"prelude_{task_id}.py"
    prelude_code = generate_prelude_code(unit_name, expected_workspace=str(repo_dir))
    prelude_script.write_text(prelude_code, encoding="utf-8")
    prelude_script.chmod(0o700)

    wrapped_command = [sys.executable, str(prelude_script)] + list(command_argv)
    systemd_cmd = build_systemd_run_argv(
        unit_name=unit_name,
        command_argv=wrapped_command,
        memory_mb=memory_mb,
        tmpdir=str(tmpdir_path),
        workspace=str(repo_dir),
        stdout_path=str(stdout_log),
        stderr_path=str(stderr_log),
        wait=False,
    )

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    clean_env = dict(os.environ)
    clean_env["TMPDIR"] = str(tmpdir_path)
    clean_env["TEMP"] = str(tmpdir_path)
    clean_env["TMP"] = str(tmpdir_path)

    res = subprocess.run(
        systemd_cmd,
        cwd=str(repo_dir),
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise TaskUnitExecutionError(
            f"failed to spawn unit {unit_name}: rc={res.returncode}, stderr={res.stderr}"
        )

    combined = (res.stdout or "") + "\n" + (res.stderr or "")
    m = re.search(r"invocation ID:\s*([a-zA-Z0-9_\-]+)", combined, re.IGNORECASE)
    inv_id = m.group(1) if m else None

    return {
        "task_id": task_id,
        "unit_name": unit_name,
        "invocation_id": inv_id,
        "workspace": str(repo_dir),
        "working_directory": str(repo_dir),
        "module_sha256": module_sha256,
        "provider": provider,
        "started_at": started_at,
        "memory_max_mb": memory_mb,
        "tasks_max": TASKS_MAX,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
