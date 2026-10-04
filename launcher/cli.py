import argparse
import sys
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from launcher.store import Store, launch_lock, StateTransitionError
from launcher.launch import do_run, native_status
from launcher.tags import run_tag_for

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
    alive, detail = native_status(run_tag_for(args.id))
    if alive != "dead":
        print(f"error: native death not confirmed (status: {alive}; {detail}). "
              f"An unknown status is not evidence; retry after `watch` reconciles, "
              f"or use `fail` only once death is confirmed.")
        return 1
    evidence = _result_evidence(store, args.id)
    if not evidence:
        print(f"error: no result evidence: no owned path holds a non-empty artifact. "
              f"Native death alone is not success; close with `fail --reason ...` "
              f"instead of completing.")
        return 1
    try:
        store.complete_task(args.id, args.reviewer,
                            reason=f"head marked complete; native death confirmed "
                                   f"({detail}); result evidence: {', '.join(evidence)}")
    except StateTransitionError as e:
        print(f"error: {e}")
        return 1
    print(f"completed-awaiting-review: {args.id} (reviewer {args.reviewer}; "
          f"native: {detail}; evidence: {len(evidence)} artifact(s))")
    return 0


def _result_evidence(store, task_id):
    """Owned paths holding a non-empty file count as result evidence."""
    found = []
    for p in store.get_task_paths(task_id):
        try:
            pp = Path(p)
            if pp.is_file() and pp.stat().st_size > 0:
                found.append(str(pp))
        except OSError:
            continue
    return found


def fail(args):
    store = get_store(args)
    task = store.get_task(args.id)
    if not task:
        print(f"error: unknown task {args.id}")
        return 1
    if task["state"] != "running":
        print(f"error: task {args.id} is {task['state']}, fail requires running")
        return 1
    alive, detail = native_status(run_tag_for(args.id))
    if alive != "dead":
        print(f"error: native death not confirmed (status: {alive}; {detail}); "
              f"refusing to fail a possibly-live worker")
        return 1
    try:
        store.fail_task(args.id, args.reviewer,
                        reason=f"failed by {args.reviewer}: {args.reason}; "
                               f"native death confirmed ({detail})")
    except StateTransitionError as e:
        print(f"error: {e}")
        return 1
    print(f"failed: {args.id} (reviewer {args.reviewer}; native: {detail})")
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


def build_report(store, now=None, project_id=None):
    """Dashboard projection: hourly UTC buckets over the half-open window
    [as_of-24h, as_of) with explicit gap labels and a project_id on every
    artifact. Token usage stays null unless natively proven; quota deltas are
    a separate object; unknown stays unknown (never zero-fabricated)."""
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=24)

    def z(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def hour_label(dt):
        return dt.strftime("%Y-%m-%dT%H:00:00Z")

    window_labels = []
    h = window_start.replace(minute=0, second=0, microsecond=0)
    if h < window_start:
        h += timedelta(hours=1)
    while h < now:
        window_labels.append(hour_label(h))
        h += timedelta(hours=1)

    buckets = {}
    outside_window = 0
    for task in store.list_tasks():
        try:
            dt = datetime.fromisoformat(task["created_at"]).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            dt = now  # unattributable tasks land in the generation hour
        if not (window_start <= dt < now):
            outside_window += 1
            continue
        label = hour_label(dt)
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

    gaps = [label for label in window_labels if label not in buckets]

    return {
        "project_id": project_id,
        "timezone": "UTC",
        "generated_at": z(now),
        "coverage": {
            "window_hours": 24,
            "window_start": z(window_start),
            "window_end": z(now),
            "window_half_open": "[window_start, window_end)",
            "first_bucket": min(buckets) if buckets else None,
            "last_bucket": max(buckets) if buckets else None,
            "gaps_within_window": gaps,
            "tasks_outside_window": outside_window,
            "note": "historical hours without tasks are gaps, not simulated "
                    "activity; tasks outside the window are counted, not emitted",
        },
        "buckets": buckets,
    }


def report(args):
    store = get_store(args)
    project_id = getattr(args, 'project_id', None) or Path.cwd().resolve().name
    projection = build_report(store, project_id=project_id)
    if getattr(args, 'jsonl', False):
        for bucket in sorted(projection["buckets"]):
            print(json.dumps({"project_id": project_id, "bucket": bucket,
                              "tasks": projection["buckets"][bucket]}))
        print(json.dumps({"project_id": project_id, "coverage": projection["coverage"]}))
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

    parser_fail = subparsers.add_parser(
        "fail",
        help="close a running task as failed; requires confirmed native death and a reason")
    parser_fail.add_argument("--id", required=True)
    parser_fail.add_argument("--reviewer", required=True)
    parser_fail.add_argument("--reason", required=True)
    parser_fail.set_defaults(func=fail)

    parser_report = subparsers.add_parser("report", help="bounded JSON/JSONL dashboard projection")
    parser_report.add_argument("--jsonl", action="store_true")
    parser_report.add_argument("--project-id", default=None,
                               help="project identity stamped on every emitted line "
                                    "(default: current directory name)")
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
