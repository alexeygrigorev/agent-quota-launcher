import argparse
import sys
import json
import os
import time
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
    backend = getattr(args, "backend", "aplexer")
    if backend == "task-units":
        return run_task_units(args)
    if backend not in ("aplexer", "task-units"):
        print(json.dumps({"error": f"unknown backend {backend}"}))
        return 1
    lock_path = config_dir / 'launch.lock'
    return do_run(str(config_dir / 'state.db'), args.id, args.cwd, args.tmpdir,
                  str(lock_path))


def run_task_units(args):
    """Public CLI path: launch.lock + Store lease + sibling systemd unit."""
    from launcher.admission import fetch_quse
    from launcher.launch import build_adapter_argv
    from launcher.task_units import execute_transient_task_unit

    config_dir = config_dir_for(args)
    store = get_store(args)
    task = store.get_task(args.id)
    if not task:
        print(json.dumps({"error": f"unsubmitted id {args.id}"}))
        return 1
    if task["state"] != "queued":
        print(json.dumps({"error": f"duplicate lease: task {args.id} state is {task['state']}"}))
        return 1
    payload = task["payload"]
    goal = payload.get("goal") or payload.get("prompt") or ""
    if not goal:
        print(json.dumps({"error": "payload.goal required for task-units backend"}))
        return 1
    from launcher.filebus_backend import FileBusBackendError, reject_head_cred_inheritance
    try:
        reject_head_cred_inheritance(payload)
    except FileBusBackendError as e:
        print(json.dumps({
            "error": str(e),
            "backend": "task-units",
            "task_id": args.id,
        }))
        return 1
    provider = payload.get("provider") or "grok"
    tmpdir = args.tmpdir
    memory_mb = int(payload.get("memory_mb") or 768)
    timeout_sec = float(payload.get("timeout") or 300)
    lock_path = config_dir / "launch.lock"
    # Concurrent sibling launches can collide on quse (observed rc=1). Retry
    # before leasing so a transient fetch failure leaves the task queued.
    quse = None
    last_quse_err = None
    for attempt in range(4):
        try:
            quse = fetch_quse()
            break
        except ValueError as e:
            last_quse_err = e
            time.sleep(0.5 * (attempt + 1))
    if quse is None:
        print(json.dumps({
            "error": f"quse fetch failed: {last_quse_err}",
            "backend": "task-units",
            "task_id": args.id,
        }))
        return 1
    argv = build_adapter_argv(provider, goal)
    # C2456: hold launch.lock only for Store lease, not the model wait.
    with launch_lock(str(lock_path)):
        store.transition_task(args.id, "starting", ("queued",),
                              reason=f"task-units lease {provider}")
    try:
        receipt = execute_transient_task_unit(
            task_id=args.id,
            command_argv=argv,
            memory_mb=memory_mb,
            workspace=args.cwd,
            tmpdir=tmpdir,
            timeout_sec=timeout_sec,
            quse_json=quse,
            provider=provider,
            log_dir=str(config_dir),
        )
    except Exception as e:
        with launch_lock(str(lock_path)):
            store.transition_task(args.id, "failed", ("starting",), reason=str(e)[:400])
        print(json.dumps({"error": str(e), "backend": "task-units", "task_id": args.id}))
        return 1
    with launch_lock(str(lock_path)):
        if receipt.get("exit_code") == 0:
            store.transition_task(
                args.id, "completed-awaiting-review", ("starting",),
                reason="task-units sibling unit exit 0",
            )
            print(json.dumps({"backend": "task-units", "task_id": args.id, "receipt": receipt}))
            return 0
        store.transition_task(args.id, "failed", ("starting",),
                              reason=f"task-units exit {receipt.get('exit_code')}")
    print(json.dumps({"backend": "task-units", "task_id": args.id, "receipt": receipt}))
    return 1


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
    """Dashboard projection: hourly UTC buckets tiling the half-open window
    [as_of-24h, as_of) with explicit gap labels and a project_id on every
    artifact. Offset-aware created_at values are converted to UTC; malformed
    ones count as invalid and are never attributed. Token usage stays null
    unless natively proven; quota deltas are a separate object; unknown stays
    unknown (never zero-fabricated)."""
    now = now or datetime.now(timezone.utc)
    project_id = project_id or "quota-launcher"
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
    label_set = set(window_labels)

    buckets = {}
    outside_window = 0
    created_at_invalid = 0
    for task in store.list_tasks():
        try:
            parsed = datetime.fromisoformat(task["created_at"])
        except (ValueError, TypeError):
            parsed = None
        if parsed is None:
            # Malformed timestamps are unknown/invalid, never attributed to a
            # fabricated hour and never counted as outside-window.
            created_at_invalid += 1
            continue
        if parsed.tzinfo is None:
            dt = parsed.replace(tzinfo=timezone.utc)  # store writes UTC-naive
        else:
            dt = parsed.astimezone(timezone.utc)  # convert real offsets
        if not (window_start <= dt < now):
            outside_window += 1
            continue
        bucket_key = hour_label(dt)
        if bucket_key not in label_set:
            bucket_key = window_labels[0]
        buckets.setdefault(bucket_key, []).append({
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
            "created_at_invalid": created_at_invalid,
            "note": "historical hours without tasks are gaps, not simulated "
                    "activity; tasks outside the window and tasks with "
                    "malformed timestamps are counted, not emitted or "
                    "attributed",
        },
        "buckets": buckets,
    }


def report(args):
    store = get_store(args)
    project_id = getattr(args, 'project_id', None) or "quota-launcher"
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
    parser_run.add_argument(
        "--backend",
        default="aplexer",
        choices=["aplexer", "task-units"],
        help="aplexer = nested native start (held); task-units = sibling systemd unit",
    )
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
                                    "(default: quota-launcher)")
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
