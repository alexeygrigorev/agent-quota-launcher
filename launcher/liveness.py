"""Bounded, read-only observations of one native session; no recovery actions."""
import argparse
import datetime
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
import stat

MAX_EVENTS_FILE_SIZE = 1024 * 1024  # 1 MiB

def parse_args(args_list=None):
    parser = argparse.ArgumentParser(description="Liveness observer")
    parser.add_argument("--session", required=True, help="UUID of the session to observe")
    parser.add_argument("--output-dir", required=True, help="Directory to store observations")
    parser.add_argument("--interval", type=float, default=10.0, help="Interval between probes")
    parser.add_argument("--duration", type=float, default=300.0, help="Maximum duration to run")
    parser.add_argument("--probe-timeout", type=float, default=5.0, help="Timeout for probe commands")
    parser.add_argument("--silence-timeout", type=float, default=60.0, help="Threshold to consider session silent")
    parser.add_argument("--once", action="store_true", help="Run only one probe")
    
    args = parser.parse_args(args_list)
    
    try:
        parsed = str(uuid.UUID(args.session))
        if parsed != args.session:
            raise ValueError("Full canonical UUID required")
    except ValueError:
        parser.error("Session must be a valid canonical UUID")

    bounds = {
        "duration": 3600.0,
        "interval": 60.0,
        "probe_timeout": 60.0,
        "silence_timeout": 86400.0
    }
    for attr, max_val in bounds.items():
        val = getattr(args, attr)
        if not math.isfinite(val) or val <= 0 or val > max_val:
            parser.error(f"--{attr.replace('_', '-')} must be finite, positive, and <= {max_val}")

    return args

def acquire_lock(output_dir):
    if os.path.islink(output_dir):
        raise ValueError("Output directory must not be a symlink")
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    os.chmod(output_dir, 0o700)
    
    lock_file = os.path.join(output_dir, ".observer.lock")
    try:
        # lock inode MUST NOT unlink at shutdown or concurrent writer can split; close failed lock fd
        fd = os.open(lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("Lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, lock_file
        except BlockingIOError:
            os.close(fd)
            sys.exit(f"Duplicate observer running for output dir: {output_dir}")
    except OSError as e:
        sys.exit(f"Failed to acquire lock: {e}")

def run_probe(cmd, timeout):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout, result.stderr, result.returncode, None
    except subprocess.TimeoutExpired as e:
        return "", "", -1, "timeout"
    except (OSError, UnicodeError):
        return "", "", -1, "probe-unavailable"

def parse_status(session_uuid, status_stdout, status_stderr, status_code, error_str, current_time_ms):
    if error_str or status_code != 0:
        return {"error": "timeout" if error_str == "timeout" else "nonzero status code" if status_code != 0 and not error_str else "probe-unavailable", "available": False, "dead": None}
        
    combined = status_stdout + "\n" + status_stderr
    if not combined.strip():
        return {"error": "empty output", "available": False, "dead": None}
        
    try:
        data = json.loads(status_stdout)
        if not isinstance(data, dict):
            return {"error": "malformed status", "available": False, "dead": None}
        found_uuid = data.get("id")
        if str(found_uuid) != str(session_uuid):
            return {"error": "identity mismatch", "available": False, "dead": None}
            
        # status parse JSON object only native id + phase/state/reported_state
        phase = data.get("phase")
        state = data.get("state")
        reported_state = data.get("reported_state")
        
        dead = phase == "exited" or state in ("exited", "dead")
        
        last_activity_ms = data.get("last_activity_ms")
        if last_activity_ms is not None:
            if isinstance(last_activity_ms, bool) or not isinstance(last_activity_ms, int):
                return {"error": "malformed last_activity_ms", "available": False, "dead": None}
            if last_activity_ms < 0 or last_activity_ms > current_time_ms:
                return {"error": "future activity", "available": False, "dead": None}
                
        # Native placement and pid warnings normalized booleans/reason codes only no raw hostpaths
        def placement_risk(key):
            value = data.get(key)
            return value.get("placement") == "login_session" if isinstance(value, dict) else None
        placements = [placement_risk("worker_placement"), placement_risk("workload_placement")]
        login_session_risk = True if True in placements else False if all(v is False for v in placements) else None
        limits = data.get("limits")
        pids = limits.get("pids") if isinstance(limits, dict) else None
        pids_limit_risk = pids <= 100 if isinstance(pids, int) and not isinstance(pids, bool) and pids > 0 else None
        warning_text = str(data.get("warning") or "").lower() + status_stderr.lower()
        warning_task_limit = any(x in warning_text for x in ("task-limit", "pids", "pid exhaustion"))
        warning_sighup = "sighup" in warning_text

        return {
            "available": True,
            "dead": True if dead else False if phase == "running" else None,
            "phase": phase,
            "state": state,
            "reported_state": reported_state if reported_state in ("idle", "working", "waiting") else None,
            "worker_alive": data.get("worker_alive") if isinstance(data.get("worker_alive"), bool) else None,
            "last_activity_ms": last_activity_ms,
            "login_session_risk": login_session_risk,
            "pids_limit_risk": pids_limit_risk,
            "warning_task_limit": warning_task_limit,
            "warning_sighup": warning_sighup
        }
    except json.JSONDecodeError:
        return {"error": "malformed status", "available": False, "dead": None}

def classify_state(screen_text, status_info, silence_timeout_ms, current_time_ms, sc_code):
    if not status_info.get("available"):
        return "unknown"
        
    if sc_code != 0:
        screen_text = ""

    text_lower = screen_text.lower()
    
    # Restrict matching to actual card markers not generic stalled in arbitrary prose
    # Using exact recognizable fresh UI cards or standard exact matches
    if "==== content filter ====" in text_lower or "[content filter]" in text_lower or "safety system triggered content filter" in text_lower:
        return "content-filter"
    if "==== quota exhausted ====" in text_lower or "[quota exhausted]" in text_lower or "[quota held]" in text_lower or "quota limit reached" in text_lower:
        return "quota-held"
    if "==== menu ====" in text_lower or "[menu]" in text_lower or "please select from the menu" in text_lower:
        return "menu"
    if "==== goal stalled ====" in text_lower or "[goal stalled]" in text_lower or re.search(r"^\s*goal stalled\s*$", text_lower, re.MULTILINE):
        return "goal-stalled"
        
    rep_state = status_info.get("reported_state")
    if rep_state in ("idle", "working", "waiting"):
        return rep_state
    # "unknown otherwise"
    return "unknown"

def private_open(filepath, flags):
    fd = os.open(filepath, flags | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Evidence path must be a regular file")
    os.fchmod(fd, 0o600)
    return fd


def write_atomic_json(filepath, data):
    tmp_path = filepath + ".tmp"
    fd = private_open(tmp_path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)


def append_events_jsonl(filepath, data):
    record = (json.dumps(data, allow_nan=False) + "\n").encode("utf-8")
    if len(record) > MAX_EVENTS_FILE_SIZE:
        raise ValueError("Observation exceeds event bound")
    fd = private_open(filepath, os.O_WRONLY | os.O_APPEND)
    if os.fstat(fd).st_size + len(record) > MAX_EVENTS_FILE_SIZE:
        os.close(fd)
        os.replace(filepath, filepath + ".1")
        fd = private_open(filepath, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "ab") as f:
        f.write(record)
        f.flush()
        os.fsync(f.fileno())


def run_observer(args_list=None):
    args = parse_args(args_list)
    lock_fd, lock_file = acquire_lock(args.output_dir)
    
    status_file = os.path.join(args.output_dir, "status.json")
    events_file = os.path.join(args.output_dir, "events.jsonl")
    
    start_time = time.monotonic()
    deadline = start_time + args.duration
    silence_timeout_ms = args.silence_timeout * 1000
    
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
                
            budget = deadline - now
            probe_timeout = min(args.probe_timeout, budget)
            if probe_timeout <= 0:
                break
                
            current_time = time.time()
            now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            current_time_ms = int(current_time * 1000)
            
            # Read status
            status_cmd = ["aplexer", "status", args.session, "--json"]
            st_out, st_err, st_code, st_probe_err = run_probe(status_cmd, probe_timeout)
            current_time_ms = int(time.time() * 1000)
            status_info = parse_status(args.session, st_out, st_err, st_code, st_probe_err, current_time_ms)
            
            # Read screen
            screen_cmd = ["aplexer", "capture", args.session, "--screen", "--plain"]
            remaining = deadline - time.monotonic()
            if remaining > 0:
                sc_out, sc_err, sc_code, sc_probe_err = run_probe(screen_cmd, min(args.probe_timeout, remaining))
            else:
                sc_out, sc_err, sc_code, sc_probe_err = "", "", -1, "deadline"
            
            # Classify state
            state = classify_state(sc_out, status_info, silence_timeout_ms, current_time_ms, sc_code)
            
            # Quota/menu/idle protected: would_escalate false, action none; goal-stalled/content-filter can annotate diagnostic only.
            would_escalate = False
            if state in ("goal-stalled", "content-filter") or status_info.get("dead"):
                would_escalate = True
            elif state in ("quota-held", "menu", "idle", "working", "suspected-silence"):
                would_escalate = False
                
            # source schema_version=1 mode=shadow heartbeat UTC observer_id if available; explicit nullable tool telemetry source unavailable; last PTY activity named precisely.
            observation = {
                "schema_version": 1,
                "mode": "shadow",
                "heartbeat": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "observer_id": os.environ.get("APLEXER_SESSION_ID"),
                "session_uuid": args.session,
                "state": state,
                "last_pty_activity_ms": status_info.get("last_activity_ms"),
                "tool_telemetry_source": "unavailable",
                "last_tool_output_time": None,
                "current_tool_start": None,
                "command": None,
                "screen_available": sc_code == 0 and sc_probe_err is None,
                "screen_error": "timeout" if sc_probe_err == "timeout" else "unavailable" if sc_code != 0 else None,
                "silence_suspected": current_time_ms - status_info["last_activity_ms"] > silence_timeout_ms if status_info.get("last_activity_ms") is not None else None,
                "proposed_action": "diagnose" if would_escalate else None,
                "would_escalate": would_escalate,
                "probe_available": status_info.get("available"),
                "session_dead": status_info.get("dead"),
                "worker_alive": status_info.get("worker_alive"),
                "probe_error": status_info.get("error"),
                "login_session_risk": status_info.get("login_session_risk"),
                "pids_limit_risk": status_info.get("pids_limit_risk"),
                "warning_task_limit": status_info.get("warning_task_limit", False),
                "warning_sighup": status_info.get("warning_sighup", False)
            }
            
            # Do not persist raw_data/screens/env/exception strings etc.
            
            # write progress immediately and result JSON with meaningful checks
            print(json.dumps(observation), flush=True)
            
            write_atomic_json(status_file, observation)
            append_events_jsonl(events_file, observation)
            
            if args.once:
                break
                
            now_after = time.monotonic()
            if now_after >= deadline:
                break
                
            sleep_time = min(args.interval, deadline - now_after)
            if sleep_time > 0:
                time.sleep(sleep_time)
            
    finally:
        os.close(lock_fd)
        # persistent lock inode MUST NOT unlink at shutdown or concurrent writer can split
        # so DO NOT os.remove(lock_file)

if __name__ == "__main__":
    run_observer()
