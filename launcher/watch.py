import time
import os
import json
from pathlib import Path
from launcher.store import Store
from launcher.launch import do_run
from launcher.cli import launch_lock

def watch(args):
    config_dir = Path(getattr(args, 'config_dir', os.path.expanduser('~/.config/agent-quota-launcher')))
    store_path = str(config_dir / 'state.db')
    lock_path = config_dir / 'launch.lock'
    
    print("Starting watcher...")
    
    store = Store(store_path)
    
    while True:
        with launch_lock(str(lock_path)):
            with store.transaction() as conn:
                # Find the next queued task
                cursor = conn.execute("SELECT id, paths, payload FROM tasks WHERE state = 'queued' ORDER BY created_at ASC")
                queued_tasks = cursor.fetchall()
                
                active_paths = store.get_active_paths()
                
            dispatched = False
            
            for row in queued_tasks:
                task_id, paths_json, payload_json = row
                paths = json.loads(paths_json) if paths_json else []
                payload = json.loads(payload_json) if payload_json else {}
                
                blocked_reason = None
                blocked_owner = None
                
                # Check path overlap manually to record reason
                for req_p in paths:
                    req_p = os.path.abspath(req_p)
                    for act_p, act_task_id in active_paths:
                        if req_p == act_p:
                            blocked_reason = "path_overlap"
                            # get owner of act_task_id
                            with store.transaction() as conn:
                                cur = conn.execute("SELECT payload FROM tasks WHERE id = ?", (act_task_id,))
                                act_row = cur.fetchone()
                                if act_row:
                                    act_payload = json.loads(act_row[0])
                                    blocked_owner = act_payload.get("owner")
                            break
                    if blocked_reason:
                        break
                        
                if blocked_reason:
                    print(f"Task {task_id} blocked: {blocked_reason} by {blocked_owner}")
                    continue
                    
                # Try to dispatch
                print(f"Found queued task {task_id}, dispatching...")
                try:
                    do_run(store_path, task_id, args.cwd, args.tmpdir)
                    print(f"Task {task_id} dispatched.")
                    dispatched = True
                    break
                except Exception as e:
                    print(f"Failed to dispatch {task_id}: {e}")
                    with store.transaction() as conn:
                        conn.execute("UPDATE tasks SET state = 'stalled' WHERE id = ?", (task_id,))
                        
        if not dispatched:
            time.sleep(5)
