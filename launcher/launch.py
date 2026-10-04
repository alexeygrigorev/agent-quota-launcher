"""Launch adapters (strict allowlist, no shell parsing) and the bounded
launch lifecycle. Provider argv sets are exactly:

- grok:        grok -p --model grok-4.6 --effort high --permission-mode auto <goal>
- antigravity: env -u GEMINI_API_KEY -u GOOGLE_API_KEY agy --model gemini-3.1-pro-high
               --effort high --dangerously-skip-permissions -p --print-timeout 0
               --output-format text <goal>   (OAuth route; ambient API keys stripped)
- zai:         /home/alexey/.local/bin/zcodex exec --model glm-5.3-flash
               --dangerously-bypass-approvals-and-sandbox -c
               check_for_update_on_startup=false --json <goal>
               with ZCODE_CJS=/opt/ZCode/resources/glm/zcode.cjs

Codex is unsupported in v0.1 (protected wrapper scripts/launch-codex.sh not
configured in this repo); `codex-cli -p` and `zai -p` never appear here.
"""
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from launcher.admission import fetch_quse, validate_quse, PROMO_MULTIPLIER, PROMO_CUTOFF_UTC
from launcher.ranking import select_candidate
from launcher.resources import check_resources
from launcher.store import Store, launch_lock, StateTransitionError

ZCODE_CJS = "/opt/ZCode/resources/glm/zcode.cjs"
ZCODEX_BIN = "/home/alexey/.local/bin/zcodex"

ADAPTERS = {
    "grok": {
        "argv": ["grok", "-p", "--model", "grok-4.6", "--effort", "high",
                 "--permission-mode", "auto"],
        "env": {},
    },
    "antigravity": {
        "argv": ["env", "-u", "GEMINI_API_KEY", "-u", "GOOGLE_API_KEY",
                 "agy", "--model", "gemini-3.1-pro-high", "--effort", "high",
                 "--dangerously-skip-permissions", "-p", "--print-timeout", "0",
                 "--output-format", "text"],
        "env": {},
    },
    "zai": {
        "argv": [ZCODEX_BIN, "exec", "--model", "glm-5.3-flash",
                 "--dangerously-bypass-approvals-and-sandbox",
                 "-c", "check_for_update_on_startup=false", "--json"],
        "env": {"ZCODE_CJS": ZCODE_CJS},
    },
}

# First-action artifacts must be whoami-shaped and nothing else. Any of these
# keys marks an engine/wrapper start record (phase/command/schema_version/...),
# which never proves a genuine agent tool action.
FIRST_ACTION_KEYS = {"id", "tag", "workspace", "timestamp"}
FIRST_ACTION_FORBIDDEN_KEYS = {"command", "phase", "parent_session", "schema_version"}

MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 7200


def build_adapter_argv(provider, goal):
    adapter = ADAPTERS.get(provider)
    if not adapter:
        raise ValueError(f"Unsupported provider: {provider}")
    return list(adapter["argv"]) + [str(goal)]


def validate_first_action(fa_path, expected_id, expected_tag, expected_workspace, min_mtime):
    """Content check: exactly whoami-shaped {id, tag, workspace, timestamp}
    with matching values; wrapper/engine start records (containing command,
    phase, parent_session or schema_version) are rejected."""
    try:
        if not os.path.exists(fa_path):
            return False
        if os.path.getmtime(fa_path) < min_mtime - 5:
            return False
        with open(fa_path, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        keys = set(data.keys())
        if keys & FIRST_ACTION_FORBIDDEN_KEYS:
            return False
        if keys != FIRST_ACTION_KEYS:
            return False
        if data.get("id") != expected_id:
            return False
        if data.get("tag") != expected_tag:
            return False
        if data.get("workspace") != expected_workspace:
            return False
        ts = data.get("timestamp")
        if not isinstance(ts, str):
            return False
        text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        datetime.fromisoformat(text)  # must parse as ISO-8601
        return True
    except Exception:
        return False


def _extract_json(text):
    text = (text or "").strip()
    obj, _ = json.JSONDecoder().raw_decode(text)
    return obj


def native_status(tag, timeout=15):
    """Query aplexer for the native session state. Returns
    (alive|dead|unknown, detail)."""
    try:
        res = subprocess.run(["aplexer", "status", tag, "--json"],
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "unknown", f"aplexer status timed out after {timeout}s"
    if res.returncode != 0:
        return "dead", f"aplexer status rc={res.returncode}: {(res.stderr or res.stdout or '').strip()[:200]}"
    try:
        info = _extract_json(res.stdout)
    except Exception as e:
        return "unknown", f"unparsable status JSON: {e}"
    phase = info.get("phase")
    if phase in ("running", "starting", "working", "launching"):
        alive = not info.get("containment_empty", False)
        return ("alive" if alive else "dead"), f"phase={phase} containment_empty={info.get('containment_empty')}"
    if phase in ("exited", "dead", "failed", "completed", "finished"):
        return "dead", f"phase={phase}"
    return "unknown", f"phase={phase!r}"


def _kill_process_group(proc, grace=10.0):
    """Terminate then kill the child's whole process group."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _write_launch_record(cwd, record):
    path = Path(cwd) / ".local" / f"launch-{record.get('task_id')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(record, f, indent=2, sort_keys=True)
    return str(path)


def _bounded_timeout(payload):
    raw = payload.get("timeout")
    try:
        t = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"payload timeout must be a number of seconds, got {raw!r}")
    if not (MIN_TIMEOUT_SECONDS <= t <= MAX_TIMEOUT_SECONDS):
        raise ValueError(f"payload timeout {t}s outside bounded range "
                         f"[{MIN_TIMEOUT_SECONDS}, {MAX_TIMEOUT_SECONDS}]")
    return t


def do_run(store_path, task_id, cwd, tmpdir, lock_path):
    store = Store(store_path)

    task_data = store.get_task(task_id)
    if not task_data:
        # Fail before any quota fetch, resource check or dispatch attempt.
        raise ValueError(f"unsubmitted id reached dispatch: {task_id}")

    payload = task_data["payload"]
    if task_data["state"] != "queued":
        raise ValueError(f"Task {task_id} state is {task_data['state']}, expected queued")

    if not payload.get("owner") or not payload.get("cwd") or not payload.get("timeout"):
        raise ValueError("Missing owner/cwd/timeout in payload")
    timeout_seconds = _bounded_timeout(payload)

    workspace = str(Path(cwd).resolve())
    tmpdir_resolved = str(Path(tmpdir).resolve())

    record = {
        "task_id": task_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "started_at_ts": time.time(),
        "timeout_seconds": timeout_seconds,
        "requested_model": None,
        "observed_model": None,
        "usage": {"input_tokens": None, "output_tokens": None,
                  "cached_tokens": None, "source": "unproven"},
        "quota": {"percent_delta": None, "snapshots": None},
    }

    # --- admission (fresh evidence each launch) ---
    quse_data = fetch_quse()
    record["quse_fetched_at"] = datetime.now(timezone.utc).isoformat()
    valid_routes, rejections = validate_quse(
        quse_data, task_requirements=payload.get("model_requirements"))
    record["rejections"] = rejections

    if not valid_routes:
        record["outcome"] = "no-valid-route"
        _write_launch_record(cwd, record)
        raise ValueError("No valid routes available")

    seed = int(time.time() * 1000)
    chosen, provenance = select_candidate(valid_routes, seed=seed)
    record["ranking"] = provenance
    record["promo_multiplier"] = PROMO_MULTIPLIER
    record["promo_cutoff_utc"] = PROMO_CUTOFF_UTC
    if not chosen:
        record["outcome"] = "no-selectable-candidate"
        _write_launch_record(cwd, record)
        raise ValueError("No eligible candidate chosen (unknown fit/health weight 0)")

    provider = chosen["provider"]
    record["provider"] = provider
    record["requested_model"] = chosen.get("model")

    active_mem, active_disk = store.get_active_resources(exclude_task_id=task_id)
    record["active_mem_mb"] = active_mem
    record["active_disk_mb"] = active_disk
    check_resources(1500, workspace, tmpdir_resolved, active_mem, active_disk,
                    repo_root=workspace)

    run_tag = f"task-{task_id}"
    record["tag"] = run_tag

    cmd = [
        "aplexer", "start",
        "--json",
        "--memory", "1500M",
        "--pids", "100",
        "--startup-timeout-ms", "30000",
        "--workspace", workspace,
        "--cwd", str(Path(payload["cwd"]).resolve()),
        "--tag", run_tag,
    ]
    child_env = dict(ADAPTERS[provider]["env"])
    child_env["TMPDIR"] = tmpdir_resolved
    for key, value in child_env.items():
        cmd.extend(["--env", f"{key}={value}"])
    cmd.extend(["--"])
    cmd.extend(build_adapter_argv(provider, payload.get("goal", "")))
    record["adapter_argv"] = cmd[cmd.index("--") + 1:]

    deadline = time.monotonic() + timeout_seconds

    # --- critical section: reserve + spawn under the exclusive launch lock ---
    with launch_lock(lock_path):
        store.transition_task(task_id, "starting", ("queued",),
                              reason=f"launching via {provider}")
        env = os.environ.copy()
        launch_output_path = Path(cwd) / ".local" / f"launch-{task_id}-start.json"
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            # Timeout enforced on the whole process group. The native child may
            # or may not exist: launch-uncertain, lease retained until native
            # death is confirmed by reconciliation.
            _kill_process_group(proc)
            record["outcome"] = "launch-uncertain"
            record["reason"] = f"aplexer start exceeded payload timeout ({timeout_seconds}s); process group killed"
            _write_launch_record(cwd, record)
            store.transition_task(task_id, "launch-uncertain", ("starting",),
                                  reason=record["reason"])
            return 1

    if proc.returncode != 0:
        record["outcome"] = "launch-uncertain"
        record["reason"] = f"aplexer start rc={proc.returncode}: {(err or out or '').strip()[:400]}"
        record["aplexer_stderr"] = (err or "")[:2000]
        _write_launch_record(cwd, record)
        store.transition_task(task_id, "launch-uncertain", ("starting",),
                              reason=record["reason"])
        return 1

    try:
        start_json = _extract_json(out)
    except Exception as e:
        record["outcome"] = "launch-uncertain"
        record["reason"] = f"ambiguous launch response: {e}"
        _write_launch_record(cwd, record)
        store.transition_task(task_id, "launch-uncertain", ("starting",),
                              reason=record["reason"])
        return 1

    launch_output_path.write_text(json.dumps(start_json, indent=2, sort_keys=True))
    record["wrapper_start_json"] = start_json

    sess_id = start_json.get("id")
    sess_tag = start_json.get("tag")
    sess_ws = start_json.get("workspace")
    record["session_id"] = sess_id
    record["observed_tag"] = sess_tag

    # --- first-action wait happens OUTSIDE the launch lock ---
    fa_path = Path(cwd) / ".local" / f"first-action-{task_id}.json"
    record["first_action_path"] = str(fa_path)
    while time.monotonic() < deadline:
        if validate_first_action(str(fa_path), sess_id, sess_tag, sess_ws,
                                 record["started_at_ts"]):
            store.transition_task(task_id, "running", ("starting",),
                                  reason="genuine first action validated")
            record["outcome"] = "running"
            record["first_action_validated_at"] = datetime.now(timezone.utc).isoformat()
            _write_launch_record(cwd, record)
            return 0
        time.sleep(2)

    # Deadline reached without first action.
    alive, detail = native_status(sess_tag or run_tag)
    record["native_status_at_deadline"] = detail
    if alive == "alive":
        store.transition_task(task_id, "stalled", ("starting",),
                              reason=f"first action missing by deadline; native alive: {detail}")
        record["outcome"] = "stalled"
    else:
        store.transition_task(task_id, "failed", ("starting",),
                              reason=f"first action missing by deadline; native death confirmed: {detail}")
        record["outcome"] = "failed"
    _write_launch_record(cwd, record)
    return 1
