import argparse
import sys
import json
import os
from pathlib import Path
from datetime import datetime, timezone

from launcher.store import Store, launch_lock
from launcher.launch import do_run

def get_store(args):
    config_dir = Path(getattr(args, 'config_dir', os.path.expanduser('~/.config/agent-quota-launcher')))
    store_path = config_dir / 'state.db'
    return Store(str(store_path))

def init(args):
    get_store(args)
    print("init complete")
    return 0

def submit(args):
    store = get_store(args)
    payload = json.loads(args.payload)
    task_id = store.submit_task(args.id, args.key, payload, args.paths)
    print(f"submitted: {task_id}")
    return 0

def plan(args):
    print("plan")
    return 0

def run(args):
    config_dir = Path(getattr(args, 'config_dir', os.path.expanduser('~/.config/agent-quota-launcher')))
    lock_path = config_dir / 'launch.lock'
    with launch_lock(str(lock_path)):
        return do_run(str(config_dir / 'state.db'), args.id, args.cwd, args.tmpdir)

def status(args):
    store = get_store(args)
    with store.get_conn() as conn:
        cursor = conn.execute("SELECT id, state FROM tasks")
        for row in cursor.fetchall():
            print(f"{row[0]}: {row[1]}")
    return 0

def complete(args):
    store = get_store(args)
    # transition state to completed-awaiting-review
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET state = 'completed-awaiting-review' WHERE id = ?", (args.id,))
    print(f"completed: {args.id}")
    return 0

def accept(args):
    store = get_store(args)
    # transition state to accepted
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET state = 'accepted' WHERE id = ?", (args.id,))
    print(f"accepted: {args.id}")
    return 0

def report(args):
    store = get_store(args)
    buckets = {}
    with store.get_conn() as conn:
        cursor = conn.execute("SELECT id, state, created_at FROM tasks")
        for row in cursor.fetchall():
            task_id, state, created_at = row
            try:
                # sqlite CURRENT_TIMESTAMP is UTC
                dt = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
            except ValueError:
                dt = datetime.now(timezone.utc)
                
            # hourly bucket
            bucket = dt.strftime("%Y-%m-%dT%H:00:00Z")
            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append({
                "id": task_id,
                "state": state,
                "usage_metadata": None,
                "quota_percent_delta": None
            })
            
    if getattr(args, 'jsonl', False):
        for b, tasks in sorted(buckets.items()):
            print(json.dumps({"bucket": b, "tasks": tasks}))
    else:
        print(json.dumps({"buckets": buckets}, indent=2))
    return 0

def main():
    parser = argparse.ArgumentParser(prog="launcher", description="Agent Quota Launcher")
    parser.add_argument("--config-dir", default=os.path.expanduser("~/.config/agent-quota-launcher"), help="Path to config directory")
    
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_init = subparsers.add_parser("init", help="create config + state dir")
    parser_init.set_defaults(func=init)

    parser_submit = subparsers.add_parser("submit", help="idempotent task submit")
    parser_submit.add_argument("--id", required=True)
    parser_submit.add_argument("--key", required=True)
    parser_submit.add_argument("--payload", required=True)
    parser_submit.add_argument("--paths", action="append", default=[])
    parser_submit.set_defaults(func=submit)

    parser_plan = subparsers.add_parser("plan", help="dry-run admission/ranking, no launch")
    parser_plan.set_defaults(func=plan)

    parser_run = subparsers.add_parser("run", help="admit, reserve, launch, record")
    parser_run.add_argument("--id", required=True)
    parser_run.add_argument("--cwd", required=True)
    parser_run.add_argument("--tmpdir", required=True)
    parser_run.set_defaults(func=run)

    parser_status = subparsers.add_parser("status", help="show task/lifecycle")
    parser_status.set_defaults(func=status)

    parser_complete = subparsers.add_parser("complete", help="head review (alias)")
    parser_complete.add_argument("--id", required=True)
    parser_complete.set_defaults(func=complete)

    parser_accept = subparsers.add_parser("accept", help="head review; exit 0 != accepted")
    parser_accept.add_argument("--id", required=True)
    parser_accept.set_defaults(func=accept)

    parser_report = subparsers.add_parser("report", help="bounded JSON/JSONL export")
    parser_report.add_argument("--jsonl", action="store_true")
    parser_report.set_defaults(func=report)

    args = parser.parse_args()
    return args.func(args)

if __name__ == "__main__":
    sys.exit(main())

    watch_parser = subparsers.add_parser('watch', help='Poll and drain task queue')
    watch_parser.add_argument('--cwd', required=True, help='Current working directory')
    watch_parser.add_argument('--tmpdir', required=True, help='Temp directory for aplexer')
    watch_parser.set_defaults(func=lambda args: __import__('launcher.watch').watch.watch(args))
