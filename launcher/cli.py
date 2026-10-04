import argparse
import sys
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from launcher.store import Store, launch_lock, StateTransitionError
from launcher.launch import do_run, native_status

BERLIN = ZoneInfo("Europe/Berlin")
REPORT = "report"  # exported so tests can build the projection


def config_dir_for(args):
    return Path(getattr(args, 'config_dir', os.path.expanduser('~/.config/agent-quota-launcher')))


def get_store(args):
    return Store(str(config_dir_for(args) / 'state.db'))


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
    """Always dry-run: admission + ranking projection only. No state change,
    no reservation, no launch; never represented as live execution."""
    from launcher.admission import fetch_quse, validate_quse, PROMO_MULTIPLIER, PROMO_CUTOFF_UTC
    from launcher.ranking import select_candidate

    if args.id:
        store = get_store(args)
        task = store.get_task(args.id)
        if not task:
            print(json.dumps({"mode": "dry-run", "error": f"unknown task id {args.id}"}))
            return 1
        payload = task["payload"]
    elif args.payload:
        payload = json.loads(args.payload)
    else:
        print(json.dumps({"mode": "dry-run", "error": "need --id or --payload"}))
        return 1

    try:
        quse_data = fetch_quse()
        valid_routes, rejections = validate_quse(
            quse_data, task_requirements=payload.get("model_requirements"))
        seed = 0  # deterministic dry-run projection
        chosen, provenance = select_candidate(valid_routes, seed=seed)
    except Exception as e:
        print(json.dumps({"mode": "dry-run", "error": str(e)}))
        return 1

    print(json.dumps({
        "mode": "dry-run",
        "chosen": chosen.get("provider") if chosen else None,
        "eligible": [{k: r.get(k) for k in ("name", "provider", "model", "health",
                                            "remaining_fraction", "hours_to_reset",
                                            "task_fit", "promo_multiplier")}
                     for r in valid_routes],
        "ranking": provenance,
        "rejections": rejections,
        "promo_multiplier": PROMO_MULTIPLIER,
        "promo_cutoff_utc": PROMO_CUTOFF_UTC,
    }, indent=2))
    return 0


def run(args):
    config_dir = config_dir_for(args)
    lock_path = config_dir / 'launch.lock'
    return do_run(str(config_dir / 'state.db'), args.id, args.cwd, args.tmpdir,
                  str(lock_path))


def status(args):
    store = get_store(args)
    print(json.dumps(store.list_tasks(), indent=2))
    return 0


def complete(args):
    store = get_store(args)
    task = store.get_task(args.id)
    if not task:
        print(f"error: unknown task {args.id}")
        return 1
    if task["state"] != "running":
        print(f"error: task {args.id} is {task['state']}, complete requires running")
        return 1
    alive, detail = native_status(f"task-{args.id}")
    if alive == "alive":
        print(f"error: native process still alive ({detail}); RAM lease must not be "
              f"released before confirmed death")
        return 1
    try:
        store.complete_task(args.id, args.reviewer)
    except StateTransitionError as e:
        print(f"error: {e}")
        return 1
    print(f"completed-awaiting-review: {args.id} (reviewer {args.reviewer}; native: {detail})")
    return 0


def accept(args):
    store = get_store(args)
    task = store.get_task(args.id)
    if not task:
        print(f"error: unknown task {args.id}")
        return 1
    try:
        store.accept_task(args.id, args.reviewer)
    except StateTransitionError as e:
        print(f"error: {e}")
        return 1
    print(f"accepted: {args.id} (reviewer {args.reviewer})")
    return 0


def build_report(store, now=None):
    """Dashboard projection: hourly Berlin buckets, 24h coverage with explicit
    gaps. Token usage stays null unless natively proven; quota deltas are a
    separate object; unknown stays unknown (never zero-fabricated)."""
    now = now or datetime.now(timezone.utc)
    buckets = {}
    for task in store.list_tasks():
        try:
            dt = datetime.fromisoformat(task["created_at"]).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            dt = now
        label = dt.astimezone(BERLIN).strftime("%Y-%m-%dT%H:00:00%z")
        buckets.setdefault(label, []).append({
            "id": task["id"],
            "state": task["state"],
            "reviewer": task["reviewer"],
            "reason": task["reason"],
            "usage": {
                "input_tokens": None,
                "output_tokens": None,
                "cached_tokens": None,
                "cost": None,
                "source": "unproven",
            },
            "quota": {
                "percent_delta": None,
                "snapshots": None,
                "note": "account percent delta is not token use or cost",
            },
        })

    hour_start = now.astimezone(BERLIN).replace(minute=0, second=0, microsecond=0)
    gaps = []
    for i in range(24):
        label = (hour_start - timedelta(hours=23 - i)).strftime("%Y-%m-%dT%H:00:00%z")
        if label not in buckets:
            gaps.append(label)

    return {
        "timezone": "Europe/Berlin",
        "generated_at": now.isoformat(),
        "coverage": {
            "window_hours": 24,
            "first_bucket": min(buckets) if buckets else None,
            "last_bucket": max(buckets) if buckets else None,
            "gaps_within_last_24h": gaps,
            "note": "historical hours without tasks are gaps, not simulated activity",
        },
        "buckets": buckets,
    }


def report(args):
    store = get_store(args)
    projection = build_report(store)
    if getattr(args, 'jsonl', False):
        for bucket in sorted(projection["buckets"]):
            print(json.dumps({"bucket": bucket, "tasks": projection["buckets"][bucket]}))
        print(json.dumps({"coverage": projection["coverage"]}))
    else:
        print(json.dumps(projection, indent=2))
    return 0


def watch(args):
    from launcher.watch import watch_loop
    return watch_loop(args)


def main():
    parser = argparse.ArgumentParser(prog="launcher", description="Agent Quota Launcher")
    parser.add_argument("--config-dir", default=os.path.expanduser("~/.config/agent-quota-launcher"),
                        help="Path to config directory")

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
    parser_plan.add_argument("--id", help="use stored task payload")
    parser_plan.add_argument("--payload", help="inline task payload JSON")
    parser_plan.set_defaults(func=plan)

    parser_run = subparsers.add_parser("run", help="admit, reserve, launch, record")
    parser_run.add_argument("--id", required=True)
    parser_run.add_argument("--cwd", required=True)
    parser_run.add_argument("--tmpdir", required=True)
    parser_run.set_defaults(func=run)

    parser_status = subparsers.add_parser("status", help="show task/lifecycle")
    parser_status.set_defaults(func=status)

    parser_complete = subparsers.add_parser("complete",
                                            help="mark running task completed-awaiting-review (needs reviewer)")
    parser_complete.add_argument("--id", required=True)
    parser_complete.add_argument("--reviewer", required=True)
    parser_complete.set_defaults(func=complete)

    parser_accept = subparsers.add_parser("accept",
                                          help="accept reviewed artifacts; exit 0 != accepted (needs reviewer)")
    parser_accept.add_argument("--id", required=True)
    parser_accept.add_argument("--reviewer", required=True)
    parser_accept.set_defaults(func=accept)

    parser_report = subparsers.add_parser("report", help="bounded JSON/JSONL dashboard projection")
    parser_report.add_argument("--jsonl", action="store_true")
    parser_report.set_defaults(func=report)

    parser_watch = subparsers.add_parser("watch",
                                         help="non-LLM loop: reconcile uncertain launches, dispatch next queued task")
    parser_watch.add_argument("--cwd", required=True)
    parser_watch.add_argument("--tmpdir", required=True)
    parser_watch.add_argument("--interval", type=float, default=10.0)
    parser_watch.add_argument("--once", action="store_true", help="single reconcile+dispatch pass")
    parser_watch.set_defaults(func=watch)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
