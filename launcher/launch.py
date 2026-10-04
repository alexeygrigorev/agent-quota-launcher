import subprocess
import json
import time
import os
import re
from pathlib import Path
from launcher.admission import fetch_quse, validate_quse
from launcher.ranking import select_candidate
from launcher.resources import check_resources
from launcher.store import Store

def validate_first_action(fa_path, expected_id, expected_tag, expected_workspace, min_mtime):
    if not os.path.exists(fa_path):
        return False
    if os.path.getmtime(fa_path) < min_mtime:
        return False
    try:
        with open(fa_path, 'r') as f:
            data = json.load(f)
        if data.get("id") != expected_id: return False
        if data.get("tag") != expected_tag: return False
        if data.get("workspace") != expected_workspace: return False
        return True
    except Exception:
        return False

def _extract_json(text):
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return json.loads(text)

def do_run(store_path, task_id, cwd, tmpdir):
    store = Store(store_path)
    
    task_data = store.get_task(task_id)
    if not task_data:
        raise ValueError(f"unsubmitted id reached dispatch: {task_id}")
        
    payload = task_data["payload"]
    state = task_data["state"]
    
    if state != "queued":
        raise ValueError(f"Task {task_id} state is {state}, expected queued")
    
    if not payload.get("owner") or not payload.get("cwd") or not payload.get("timeout"):
        raise ValueError("Missing owner/cwd/timeout in payload")
        
    quse_data = fetch_quse()
    valid_routes, _ = validate_quse(quse_data)
    
    if not valid_routes:
        raise ValueError("No valid routes available")
        
    chosen, eligible, weights = select_candidate(valid_routes, seed=int(time.time()))
    if not chosen:
        raise ValueError("No eligible candidate chosen")
        
    provider = chosen.get("provider")
    
    active_mem, active_disk = store.get_active_resources()
    check_resources(1500, cwd, tmpdir, active_mem, active_disk)
    
    run_tag = f"task-{task_id}"
    
    cmd = [
        "aplexer",
        "start",
        "--fresh",
        "--json",
        "--memory", "1500M",
        "--pids", "100",
        "--startup-timeout-ms", "30000",
        "--workspace", str(Path(cwd).resolve()),
        "--cwd", str(Path(payload.get("cwd", cwd)).resolve()),
        "--tag", run_tag,
        "--env", f"TMPDIR={tmpdir}"
    ]
    
    if provider in ["antigravity", "gemini"]:
        cmd.extend(["--engine", "antigravity", "--", "sh", "-c", "agy --model gemini-3.1-pro-high --effort high --dangerously-skip-permissions --print-timeout 0 --output-format text -p \"" + payload.get("goal", "").replace("\"", "\\\"") + "\""])
        # cmd.extend(["--engine", "antigravity", "--", "agy", "--model", "gemini-3.1-pro-high", "--effort", "high", "--dangerously-skip-permissions", "-p", "--print-timeout", "0", "--output-format", "text", payload.get("goal", "")])
    elif provider == "grok":
        cmd.extend(["--engine", "grok", "--", "grok", "-p", "--model", "grok-4.6", "--effort", "high", "--permission-mode", "auto", payload.get("goal", "")])
    elif provider == "zai":
        cmd.extend(["--engine", "zai", "--", "zai", "-p", payload.get("goal", "")])
    elif provider == "codex":
        cmd.extend(["--engine", "codex", "--", "codex-cli", "-p", payload.get("goal", "")])
    else:
        raise ValueError(f"Unsupported provider: {provider}")
        
    with store.transaction() as conn:
        cursor = conn.execute("UPDATE tasks SET state = 'starting' WHERE id = ? AND state = 'queued'", (task_id,))
        if cursor.rowcount == 0:
            raise ValueError("Rowcount 0 on UPDATE")
            
    env = os.environ.copy()
        
    launch_output_path = Path(cwd) / ".local" / f"launch-{task_id}.json"
    launch_output_path.parent.mkdir(parents=True, exist_ok=True)
    
    start_time = time.time()
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    
    if res.returncode == 0:
        try:
            start_json = _extract_json(res.stdout)
            with open(launch_output_path, 'w') as f:
                json.dump(start_json, f)
                
            sess_id = start_json.get("id")
            sess_tag = start_json.get("tag")
            sess_ws = start_json.get("workspace")
            
            fa_path = Path(cwd) / ".local" / f"first-action-{task_id}.json"
            wait_until = time.time() + 180
            print(f"Waiting for first-action until {wait_until}")
            is_valid = False
            while time.time() < wait_until:
                if validate_first_action(str(fa_path), sess_id, sess_tag, sess_ws, start_time):
                    is_valid = True
                    break
                time.sleep(1)
                
            if is_valid:
                print("First action valid")
                with store.transaction() as conn:
                    conn.execute("UPDATE tasks SET state = 'running' WHERE id = ?", (task_id,))
                return 0
            else:
                print("First action timeout")
                with store.transaction() as conn:
                    conn.execute("UPDATE tasks SET state = 'stalled' WHERE id = ?", (task_id,))
                return 1
        except Exception as e:
            print(f"Exception during post-launch: {e}")
            with store.transaction() as conn:
                conn.execute("UPDATE tasks SET state = 'launch-uncertain' WHERE id = ?", (task_id,))
            return 1
    else:
        print(f"res.returncode={res.returncode}")
        with store.transaction() as conn:
            conn.execute("UPDATE tasks SET state = 'launch-uncertain' WHERE id = ?", (task_id,))
        return res.returncode
