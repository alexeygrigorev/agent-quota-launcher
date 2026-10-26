import argparse
import sqlite3
import sys
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from launcher.store import Store, launch_lock, StateTransitionError
from launcher.admission import task_requirements_for
from launcher.launch import do_run, native_status
from launcher.tags import run_tag_for

REPORT = "report"  # exported so tests can build the projection


def config_dir_for(args):
    val = getattr(args, 'config_dir', None)
    if not val or not str(val).strip():
        val = os.environ.get('LAUNCHER_CONFIG_DIR')
    if not val or not str(val).strip():
        raise ValueError("Configuration error: --config-dir or LAUNCHER_CONFIG_DIR environment variable must be set. The silent default ~/.config/agent-quota-launcher fallback is removed.")
    return Path(val).resolve()


def get_store(args):
    return Store(str(config_dir_for(args) / 'state.db'))


def init(args):
    get_store(args)
    print("init complete")
    return 0


def submit(args):
    store = get_store(args)
    payload = json.loads(args.payload)
    paths_arg = args.paths or []
    normalized_paths = []
    for p_arg in paths_arg:
        for p in p_arg.split(','):
            stripped = p.strip()
            if stripped:
                normalized_paths.append(stripped)
    task_id = store.submit_task(args.id, args.key, payload, normalized_paths)
    if getattr(args, 'json', False):
        print(json.dumps({"task_id": task_id, "state": store.get_task(task_id)["state"]}))
    else:
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
        from launcher.admission import is_managed_profile_payload, extract_requested_model
        is_managed = is_managed_profile_payload(payload)
        account_label = payload.get("account_label") if is_managed else None
        managed_dir = payload.get("managed_dir") if is_managed else None
        task_reqs = task_requirements_for(payload)
        req_model = extract_requested_model(payload, provider=payload.get("provider") if is_managed else None)
        quse_data = fetch_quse(
            account_label=account_label,
            provider=payload.get("provider") if is_managed else None,
            requested_model=req_model,
            managed_dir=managed_dir,
        )
        config_dir = config_dir_for(args)
        valid_routes, rejections = validate_quse(
            quse_data, task_requirements=task_reqs,
            check_capacity=True, config_dir=config_dir)
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
    # Explicit operator/host argument crosses systemd-run without relying on
    # inherited client environment. Never read this hint from task payloads.
    raw = getattr(args, "trusted_choking_json", None)
    if raw is None:
        return _run_with_host_hint(args)
    from launcher.cpu_resources import validate_resource_choking
    hint = None
    if isinstance(raw, str) and len(raw.encode("utf-8")) <= 512:
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and set(value) == {"reason", "observed_at"}:
                hint = validate_resource_choking(value, cooldown_s=3600)
        except (ValueError, TypeError):
            pass
    if hint is None:
        from launcher.cpu_resources import trusted_choking_from_env
        inherited = trusted_choking_from_env(cooldown_s=3600)
        args.trusted_choking_json = json.dumps(inherited) if inherited is not None else None
        return _run_with_host_hint(args)
    sanitized = json.dumps(hint, separators=(",", ":"))
    previous = os.environ.get("LAUNCHER_TRUSTED_CHOKING_JSON")
    args.trusted_choking_json = sanitized
    os.environ["LAUNCHER_TRUSTED_CHOKING_JSON"] = sanitized
    try:
        return _run_with_host_hint(args)
    finally:
        if previous is None:
            os.environ.pop("LAUNCHER_TRUSTED_CHOKING_JSON", None)
        else:
            os.environ["LAUNCHER_TRUSTED_CHOKING_JSON"] = previous


def _run_with_host_hint(args):
    config_dir = config_dir_for(args)
    backend = getattr(args, "backend", "aplexer")
    if backend == "task-units":
        return run_task_units(args)
    if backend not in ("aplexer", "task-units"):
        print(json.dumps({"error": f"unknown backend {backend}"}))
        return 1
    lock_path = config_dir / 'launch.lock'
    rc = do_run(str(config_dir / 'state.db'), args.id, args.cwd, args.tmpdir,
                str(lock_path),
                resource_config_path=getattr(args, "resource_config", None))
    if rc != 0 or backend != "aplexer":
        return rc
    # Narrow native receipt for the maintained aplexer backend (Hub fleet
    # binding): on success (first-action validated, session running) emit a
    # JSON receipt with the genuine native UUID so run_json callers can bind
    # without faking. Files are the do_run-written evidence; missing/invalid
    # evidence prints nothing (caller treats empty stdout as uncertain, never
    # as a fake receipt). Exit code and launch-record semantics preserved.
    try:
        cwd_path = Path(args.cwd).resolve()
        start_doc = json.loads(
            (cwd_path / ".local" / f"launch-{args.id}-start.json").read_text(
                encoding="utf-8"))
        record_doc = json.loads(
            (cwd_path / ".local" / f"launch-{args.id}.json").read_text(
                encoding="utf-8"))
        session_id = start_doc.get("id") or record_doc.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return rc
        if record_doc.get("outcome") != "running":
            return rc
        if not record_doc.get("first_action_validated_at"):
            return rc
        receipt = {
            "session_id": session_id,
            "state": "running",
            "first_action_verified": True,
            "model": record_doc.get("observed_model"),
            "requested_model": record_doc.get("requested_model"),
            "model_verified": record_doc.get("model_verified") is True,
            "model_identity_source": "native-start-command",
            "tag": start_doc.get("tag"),
            "workspace": start_doc.get("workspace"),
            "engine": start_doc.get("engine"),
            "phase": start_doc.get("phase"),
        }
        if start_doc.get("profile"):
            receipt["profile"] = start_doc["profile"]
        if start_doc.get("account_label") or record_doc.get("account_label"):
            receipt["account_label"] = start_doc.get("account_label") or record_doc.get("account_label")
        print(json.dumps({"backend": "aplexer", "task_id": args.id,
                          "receipt": receipt}))
    except Exception:
        pass
    return rc


def controller_unit_name(task_id: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in task_id)
    return f"ql-ctl-{safe}.service"


def spawn_ql_controller(args, return_dict=False):
    """Place the proven blocking public CLI in a sibling app.slice unit.

    Inner process still uses execute_transient_task_unit(--wait) so ExecMainStatus
    is sampled before --collect. Head CLI returns after controller start.
    """
    launcher_root = str(Path(__file__).resolve().parent.parent)
    config_dir = config_dir_for(args)
    unit = controller_unit_name(args.id)
    effective_tmpdir = args.tmpdir or str(Path(args.cwd).resolve() / '.local' / 'tmp' / args.id)
    cmd = [
        "systemd-run", "--user", f"--unit={unit}", "--slice=app.slice", "--collect",
        "-p", "MemoryMax=256M", "-p", "TasksMax=100",
        "-p", f"WorkingDirectory={launcher_root}",
        "-E", f"PYTHONPATH={launcher_root}",
        # dynamically inject TMPDIR
        "-E", f"TMPDIR={effective_tmpdir}",
        "-E", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
        "-E", f"HOME={os.environ.get('HOME', '')}",
        "--",
        sys.executable, "-m", "launcher",
        "--config-dir", str(config_dir),
        "run", "--backend", "task-units", "--as-controller",
        "--id", args.id, "--cwd", args.cwd,
        "--tmpdir", effective_tmpdir,
    ]
    if getattr(args, "trusted_choking_json", None) is not None:
        cmd.extend(["--trusted-choking-json", args.trusted_choking_json])
    if getattr(args, "resource_config", None) is not None:
        cmd.extend(["--resource-config", args.resource_config])
    res = subprocess.run(cmd, cwd=launcher_root, capture_output=True, text=True)
    if res.returncode != 0:
        err_res = {
            "error": f"ql-ctl spawn failed rc={res.returncode}",
            "stderr": (res.stderr or "")[:400],
            "task_id": args.id,
        }
        if return_dict:
            return err_res
        print(json.dumps(err_res))
        return 1
    
    success_res = {
        "backend": "task-units",
        "detached_controller": True,
        "controller_unit": unit,
        "task_id": args.id,
        "state": "queued-or-starting",
    }
    if return_dict:
        return success_res
    print(json.dumps(success_res))
    return 0


def task_unit_resource_context(store, args, payload, memory_mb):
    """Called under launch.lock before taking the task-unit start lease."""
    from launcher.resource_policy import load_resource_policy, resolve_memory_estimate_mb
    from launcher.cpu_resources import resolve_expected_cpu, ramping_cpu_cores, collect_admission_context
    from launcher.resources import check_resources
    policy = load_resource_policy(payload, explicit_path=getattr(args, "resource_config", None))
    estimate = resolve_memory_estimate_mb(payload, store.memory_samples_p90(policy["estimate_percentile"]),
                                          policy, cap_mb=memory_mb)
    expected, source = resolve_expected_cpu(payload, policy=policy)
    frozen = store.freeze_cpu_start(args.id, expected, source)
    active_mem, active_disk = store.get_active_resources(exclude_task_id=args.id,
        ramp_reserve_mb=policy["ramp_reserve_default_mb"], estimate_percentile=policy["estimate_percentile"],
        ramp_seconds=policy["ramp_seconds"], estimate_multiplier=policy["estimate_multiplier"])
    entries = store.cpu_ramping_entries(exclude_task_id=args.id, ramp_seconds=policy["ramp_seconds"],
                                        default_expected=policy["cpu_default_cores"])
    usage, trusted = collect_admission_context(store, policy)
    context = dict(active_mem_mb=active_mem, active_disk_mb=active_disk,
        memory_floor_mb=policy["memory_floor_mb"], memory_estimate_mb=estimate,
        cpu_load_factor=policy["cpu_load_factor"], cpu_policy=policy, cpu_usage=usage,
        trusted_choking=trusted, expected_cpu_cores=frozen["expected_cpu_cores"],
        ramping_cpu_cores=ramping_cpu_cores(entries, ramp_seconds=policy["ramp_seconds"]))
    tmpdir = args.tmpdir or str(Path(args.cwd).resolve() / ".local" / "tmp" / args.id)
    from launcher.task_resource_limits import hub_headless_memory_mb, MAX_TASK_MEMORY_MB
    cap = {}
    if hub_headless_memory_mb(payload) is not None:
        context["memory_estimate_mb"] = max(estimate, memory_mb)
        cap["memory_ceiling_mb"] = MAX_TASK_MEMORY_MB
    check_resources(memory_mb, args.cwd, tmpdir, repo_root=args.cwd, **cap, **context)
    return context


def run_task_units(args):
    """Public CLI path: launch.lock + Store lease + sibling systemd unit."""
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

    from launcher.task_profiles import resolve_task_bounds
    bounds = resolve_task_bounds(payload)
    is_root = (
        bounds.get("profile") == "root-run"
        or payload.get("profile") == "root-run"
        or payload.get("root_run")
        or "root-run" in str(args.id)
    )
    if is_root:
        from launcher.root_profile import check_root_pre_action, RootProfileError
        lock_path = config_dir / "launch.lock"
        try:
            check_root_pre_action(
                payload,
                task_id=args.id,
                workspace=payload.get("cwd") or str(config_dir),
                allow_test_fixture=False,
            )
        except RootProfileError as e:
            with launch_lock(str(lock_path)):
                try:
                    store.transition_task(args.id, "blocked", ("queued",), reason=str(e))
                except Exception:
                    pass
            print(json.dumps({
                "error": str(e),
                "backend": "task-units",
                "task_id": args.id,
                "profile": "root-run",
                "operational_root_qualified": False,
            }))
            return 1

    if not getattr(args, "as_controller", False):
        return spawn_ql_controller(args)
    from launcher.admission import fetch_quse, validate_quse
    from launcher.launch import build_adapter_argv
    from launcher.task_units import execute_transient_task_unit

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
    from launcher.capacity import check_provider_capacity, provider_reservation
    from launcher.ranking import select_candidate

    requested_provider = payload.get("provider") or (
        payload.get("model_requirements", {}).get("provider")
        if isinstance(payload.get("model_requirements"), dict)
        else None
    )
    allow_fallback = payload.get("allow_fallback", True)

    tmpdir = args.tmpdir
    memory_mb = bounds["memory_mb"]
    timeout_sec = float(payload.get("timeout") or bounds["timeout"])
    protect_system = payload.get("protect_system")
    read_only_paths = payload.get("read_only_paths")
    read_write_paths = payload.get("read_write_paths")
    lock_path = config_dir / "launch.lock"

    # Concurrent sibling launches can collide on quse (observed rc=1). Retry
    # before leasing so a transient fetch failure leaves the task queued.
    quse = None
    last_quse_err = None
    for attempt in range(4):
        try:
            quse = fetch_quse()
            break
        except (ValueError, OSError) as e:
            last_quse_err = e
            time.sleep(0.5 * (attempt + 1))
    if quse is None:
        print(json.dumps({
            "error": f"quse fetch failed: {last_quse_err}",
            "backend": "task-units",
            "task_id": args.id,
        }))
        return 1

    chosen_provider = None
    chosen_model = None
    if quse == {"ok": True}:
        chosen_provider = requested_provider or "grok"
    else:
        # Capacity-aware route validation
        valid_routes, rejections = validate_quse(
            quse,
            task_requirements=task_requirements_for(payload),
            check_capacity=True,
            config_dir=config_dir,
        )

        if requested_provider and requested_provider != "auto":
            matching = [
                r for r in valid_routes
                if r.get("provider") == requested_provider or r.get("name") == requested_provider
            ]
            if matching:
                chosen_provider = requested_provider
                chosen_model = matching[0].get("model")
            elif allow_fallback:
                seed = int(time.time() * 1000)
                chosen, provenance = select_candidate(valid_routes, seed=seed)
                if chosen:
                    chosen_provider = chosen.get("provider")
                    chosen_model = chosen.get("model")
            if not chosen_provider:
                reason = rejections.get(requested_provider, "provider route rejected or at capacity")
                full_reason = f"admission: requested provider '{requested_provider}' rejected: {reason}"
                print(json.dumps({
                    "error": full_reason,
                    "rejections": rejections,
                    "backend": "task-units",
                    "task_id": args.id,
                }))
                store.record_reason(args.id, full_reason)
                try:
                    store.transition_task(args.id, "failed", ("queued", "starting"), reason=full_reason)
                except Exception:
                    pass
                return 1
        else:
            seed = int(time.time() * 1000)
            chosen, provenance = select_candidate(valid_routes, seed=seed)
            if not chosen:
                reason = f"admission: no eligible provider route available: {rejections}"
                print(json.dumps({
                    "error": "no eligible provider route available",
                    "rejections": rejections,
                    "backend": "task-units",
                    "task_id": args.id,
                }))
                store.record_reason(args.id, reason)
                try:
                    store.transition_task(args.id, "failed", ("queued", "starting"), reason=reason)
                except Exception:
                    pass
                return 1
            chosen_provider = chosen.get("provider")
            chosen_model = chosen.get("model")

    provider = chosen_provider
    model = chosen_model or payload.get("model")
    argv = build_adapter_argv(provider, goal, model=model)
    # C2456: hold launch.lock only for Store lease, not the model wait.
    with launch_lock(str(lock_path)):
        try:
            resource_context = task_unit_resource_context(store, args, payload, memory_mb)
        except ValueError as e:
            store.record_reason(args.id, str(e))
            print(json.dumps({"error": str(e), "backend": "task-units", "task_id": args.id}))
            return 1
        store.transition_task(args.id, "starting", ("queued",),
                              reason=f"task-units lease {provider}")
    try:
        with provider_reservation(provider, args.id, config_dir=config_dir):
            with launch_lock(str(lock_path)):
                store.transition_task(args.id, "running", ("starting",),
                                      reason=f"task-units unit launched under app.slice ({provider})")
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
                check_capacity=False,
                config_dir=config_dir,
                protect_system=protect_system,
                read_only_paths=read_only_paths,
                read_write_paths=read_write_paths,
                extra_env=payload.get("private_env") or payload.get("extra_env"),
                **resource_context,
            )
    except Exception as e:
        with launch_lock(str(lock_path)):
            store.transition_task(args.id, "failed", ("starting", "running"), reason=str(e)[:400])
        print(json.dumps({"error": str(e), "backend": "task-units", "task_id": args.id}))
        return 1
    completed_ok = False
    with launch_lock(str(lock_path)):
        if receipt.get("exit_code") == 0:
            store.transition_task(
                args.id, "completed-awaiting-review", ("starting", "running"),
                reason="task-units sibling unit exit 0",
            )
            print(json.dumps({"backend": "task-units", "task_id": args.id, "receipt": receipt}))
            completed_ok = True
        else:
            store.transition_task(
                args.id, "failed", ("starting", "running"),
                reason=f"task-units exit {receipt.get('exit_code')}",
            )
            print(json.dumps({"backend": "task-units", "task_id": args.id, "receipt": receipt}))

    if completed_ok:
        print(f"task {args.id} completed-awaiting-review")
        from types import SimpleNamespace
        from launcher.watch import watch_loop
        refill_args = SimpleNamespace(
            config_dir=config_dir,
            backend=getattr(args, "backend", "task-units"),
            once=True,
            wait_for_review="dependencies",
        )
        print(f"triggering automated disjoint horizontal refill after completion of {args.id}")
        try:
            watch_loop(refill_args, max_passes=1)
        except Exception as e:
            print(f"refill dispatch error: {e}")
        return 0
    return 1


def request(args):
    import uuid
    from launcher.task_profiles import resolve_task_bounds, validate_source_commit

    store = get_store(args)
    task_id = args.id or f"task-{uuid.uuid4().hex[:8]}"
    idem_key = getattr(args, 'key', None) or str(uuid.uuid4())
    cwd = args.cwd or os.getcwd()
    
    payload = {
        "goal": args.goal,
        "cwd": cwd,
        "owner": os.environ.get("USER", "alexey")
    }
    if args.profile:
        payload["profile"] = args.profile
    if args.memory_mb:
        payload["memory_mb"] = args.memory_mb
    if args.timeout:
        payload["timeout"] = args.timeout

    try:
        bounds = resolve_task_bounds(payload)
    except ValueError as e:
        print(json.dumps({"error": f"Profile resolution failed: {e}"}))
        return 1
    
    source_receipt = None
    if args.target_commit or args.target_worktree:
        try:
            source_receipt = validate_source_commit(
                repo_path=args.target_worktree or cwd,
                commit_ref=args.target_commit or "HEAD",
                require_clean=False
            )
            payload["source_receipt"] = source_receipt
        except Exception as e:
            print(json.dumps({"error": f"Source validation failed: {e}"}))
            return 1

    paths_arg = args.paths or []
    normalized_paths = []
    for p_arg in paths_arg:
        for p in p_arg.split(','):
            stripped = p.strip()
            if stripped:
                normalized_paths.append(stripped)

    is_root = (
        bounds.get("profile") == "root-run"
        or payload.get("profile") == "root-run"
        or payload.get("root_run")
        or "root-run" in str(task_id)
    )
    if is_root:
        from launcher.root_profile import check_root_pre_action, RootProfileError
        try:
            check_root_pre_action(
                payload,
                task_id=task_id,
                workspace=cwd,
                allow_test_fixture=False,
            )
        except RootProfileError as e:
            try:
                store.submit_task(task_id, idem_key, payload, normalized_paths)
                store.transition_task(task_id, "blocked", ("queued",), reason=str(e))
            except Exception:
                pass
            print(json.dumps({
                "error": str(e),
                "task_id": task_id,
                "profile": "root-run",
                "operational_root_qualified": False,
            }))
            return 1

    # Submit task
    try:
        store.submit_task(task_id, idem_key, payload, normalized_paths)
    except sqlite3.IntegrityError as e:
        print(json.dumps({"error": f"Task submission failed (duplicate id '{task_id}'): {e}"}))
        return 1
    except ValueError as e:
        print(json.dumps({"error": f"Task submission failed: {e}"}))
        return 1
    
    # Spawn controller
    class SpawnArgs:
        def __init__(self, t_id, t_cwd, t_config_dir):
            self.id = t_id
            self.cwd = t_cwd
            self.tmpdir = None
            self.config_dir = t_config_dir
    spawn_args = SpawnArgs(task_id, cwd, getattr(args, 'config_dir', None))
    
    ctrl_res = spawn_ql_controller(spawn_args, return_dict=True)
    
    if "error" in ctrl_res:
        print(json.dumps(ctrl_res))
        return 1

    out = {
        "task_id": task_id,
        "controller_unit": ctrl_res["controller_unit"],
        "status": ctrl_res["state"],
        "profile": bounds,
        "timeout": bounds["timeout"],
    }
    if source_receipt:
        out["source_receipt"] = source_receipt
        
    print(json.dumps(out, indent=2))
    return 0


def status(args):
    store = get_store(args)
    if getattr(args, 'id', None):
        task_id = store.resolve_task_id(args.id) or args.id
        task = store.get_task(task_id)
        if task:
            binding = store.get_native_binding(task_id)
            if binding:
                task["native_binding"] = binding
            print(json.dumps(task, indent=2))
        else:
            print(json.dumps({"error": f"unknown task {args.id}"}))
            return 1
    else:
        print(json.dumps(store.list_tasks(), indent=2))
    return 0


def complete(args):
    store = get_store(args)
    task_id = store.resolve_task_id(args.id) or args.id
    task = store.get_task(task_id)
    if not task:
        print(f"error: unknown task {args.id}")
        return 1
    if task["state"] != "running":
        print(f"error: task {task_id} is {task['state']}, complete requires running")
        return 1

    binding = store.get_native_binding(task_id)
    if not binding:
        binding = store.recover_binding_from_artifacts(task_id, task.get("payload"))

    if binding:
        from launcher.native_lifecycle import probe_native_lifecycle
        status, detail = probe_native_lifecycle(binding)
        is_dead = status in ("dead", "retired-finished")
    else:
        alive, detail = native_status(run_tag_for(task_id))
        is_dead = (alive == "dead")
        status = alive

    if not is_dead:
        print(f"error: native death not confirmed (status: {status}; {detail}). "
              f"An unknown status is not evidence; retry after `watch` reconciles, "
              f"or use `fail` only once death is confirmed.")
        return 1
    evidence = _result_evidence(store, task_id)
    if not evidence:
        print(f"error: no result evidence: no owned path holds a non-empty artifact. "
              f"Native death alone is not success; close with `fail --reason ...` "
              f"instead of completing.")
        return 1
    try:
        store.complete_task(task_id, args.reviewer,
                            reason=f"head marked complete; native death confirmed "
                                   f"({detail}); result evidence: {', '.join(evidence)}")
    except StateTransitionError as e:
        print(f"error: {e}")
        return 1
    print(f"completed-awaiting-review: {task_id} (reviewer {args.reviewer}; "
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
    task_id = store.resolve_task_id(args.id) or args.id
    task = store.get_task(task_id)
    if not task:
        print(f"error: unknown task {args.id}")
        return 1
    if task["state"] != "running":
        print(f"error: task {task_id} is {task['state']}, fail requires running")
        return 1

    binding = store.get_native_binding(task_id)
    if not binding:
        binding = store.recover_binding_from_artifacts(task_id, task.get("payload"))

    if binding:
        from launcher.native_lifecycle import probe_native_lifecycle
        status, detail = probe_native_lifecycle(binding)
        is_dead = status in ("dead", "retired-finished")
    else:
        alive, detail = native_status(run_tag_for(task_id))
        is_dead = (alive == "dead")
        status = alive

    if not is_dead:
        print(f"error: native death not confirmed (status: {status}; {detail}); "
              f"refusing to fail a possibly-live worker")
        return 1
    try:
        store.fail_task(task_id, args.reviewer,
                        reason=f"failed by {args.reviewer}: {args.reason}; "
                               f"native death confirmed ({detail})")
    except StateTransitionError as e:
        print(f"error: {e}")
        return 1
    print(f"failed: {task_id} (reviewer {args.reviewer}; native: {detail})")
    return 0


def model_support(args):
    """Pure adapter capability query; no quota, state or native effects."""
    from launcher.admission import ADAPTER_SUPPORTED_MODELS
    supported = args.model in ADAPTER_SUPPORTED_MODELS.get(args.provider, ())
    print(json.dumps({"provider": args.provider, "model": args.model,
                      "supported": supported,
                      "reason": None if supported else "model_unsupported"}))
    return 0


def resource_status(args):
    """Reconcile without dispatch or stopping sessions; dry-run is readonly.

    Dry-run never transitions tasks, never writes CPU samples, OOM baselines,
    or choking events. CPU admission here is choking-only (t1511u): busy and
    predicted load are advisory only.
    """
    from launcher.resources import (
        get_mem_available,
        DEFAULT_NATIVE_MEMORY_MB,
        CODEX_MANAGED_NATIVE_MEMORY_MB,
        DEFAULT_MEMORY_FLOOR_MB,
    )
    from launcher.resource_policy import (
        cpu_guard_status,
        load_resource_policy,
        resolve_memory_estimate_mb,
    )
    from launcher.cpu_resources import (
        check_cpu_choking,
        oom_delta,
        read_oom_counter,
        ramping_cpu_cores,
        resolve_expected_cpu,
        sample_cpu_usage,
        sanitize_cpu_usage,
        trusted_choking_from_env,
    )
    store = get_store(args)
    policy = load_resource_policy(
        None, explicit_path=getattr(args, "resource_config", None))
    measured_p90 = store.memory_samples_p90(policy["estimate_percentile"])
    estimate_new = resolve_memory_estimate_mb(
        None, measured_p90, policy, cap_mb=DEFAULT_NATIVE_MEMORY_MB)
    estimate_codex = resolve_memory_estimate_mb(
        None, measured_p90, policy, cap_mb=CODEX_MANAGED_NATIVE_MEMORY_MB)
    changes = []
    breakdown = store.resource_breakdown(
        dry_run=args.dry_run, reconciliation=changes,
        ramp_reserve_mb=policy["ramp_reserve_default_mb"],
        estimate_percentile=policy["estimate_percentile"],
        ramp_seconds=policy["ramp_seconds"],
        estimate_multiplier=policy["estimate_multiplier"])
    memory = sum(r["reserve_mb"] for r in breakdown)
    disk = sum(r["disk_mb"] for r in breakdown)
    ramp_total = sum(r["reserve_mb"] for r in breakdown
                     if r["basis"] in ("queued-estimate", "start-ramp"))
    available = get_mem_available() // (1024 * 1024)
    floor_mb = policy["memory_floor_mb"]
    from launcher.store import RESOURCE_HOLDING_STATES
    with store.get_conn() as conn:
        slots = ",".join("?" for _ in RESOURCE_HOLDING_STATES)
        count, reserved = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(r.memory_mb),0) FROM tasks t JOIN task_resources r ON r.task_id=t.id WHERE t.state IN ({slots})",
            RESOURCE_HOLDING_STATES).fetchone()
    if args.dry_run:
        count -= len(changes)
        reserved -= sum(r["memory_mb"] for r in changes)
    legacy_cpu = cpu_guard_status(factor=policy["cpu_load_factor"])
    from launcher.cpu_resources import collect_admission_context
    cpu_usage_live, trusted_hint = collect_admission_context(store, policy, dry_run=args.dry_run)
    try:
        default_expected, default_source = resolve_expected_cpu(None, policy=policy)
    except ValueError:
        default_expected, default_source = policy["cpu_default_cores"], "default"
    try:
        entries = store.cpu_ramping_entries(
            ramp_seconds=policy["ramp_seconds"],
            default_expected=policy["cpu_default_cores"])
        ramping_total = float(ramping_cpu_cores(entries, ramp_seconds=policy["ramp_seconds"]))
    except Exception:
        entries = []
        ramping_total = 0.0
    import time as _time
    _now = int(_time.time() * 1000)
    try:
        _cores = (cpu_usage_live or {}).get("cores")
        allowed, cpu_reason, admission = check_cpu_choking(
            cpu_usage=cpu_usage_live, policy=policy, cores=_cores,
            expected_cpu_cores=default_expected,
            ramping_cpu_cores=ramping_total,
            trusted_choking=trusted_hint, now_ms=_now)
    except Exception:
        allowed, cpu_reason, admission = True, None, None
    try:
        _san = sanitize_cpu_usage(cpu_usage_live) if cpu_usage_live else None
        _cores2 = (cpu_usage_live or {}).get("cores")
        _busy = _san.get("busy_cores") if _san else None
        _limit = _cores2 * policy["cpu_target_utilization"] \
            if isinstance(_cores2, int) else None
        predicted = (_busy + default_expected + ramping_total > _limit) \
            if (_busy is not None and _limit is not None) else None
    except Exception:
        predicted = None
    cpu = {"allowed": allowed, "reason": cpu_reason, "admission": admission,
           "expected_cpu_cores": default_expected,
           "expected_cpu_source": default_source,
           "ramping_cpu_cores": ramping_total,
           "predicted_over_target": predicted,
           "telemetry": cpu_usage_live,
           "trusted_choking": trusted_hint,
           "legacy_load_display": legacy_cpu}
    mem_ok = available - memory - estimate_new >= floor_mb
    default_allowed = mem_ok and allowed
    if not mem_ok:
        reason = "launcher_memory_reserve"
    elif not allowed:
        reason = cpu_reason
    else:
        reason = None
    # Managed Codex uses the same measured admission policy as native start.
    # Its 4096MiB containment cap bounds the estimate; it is not a RAM hold.
    codex_headroom = available - memory - estimate_codex >= floor_mb
    codex_allowed = codex_headroom and allowed
    print(json.dumps({"active_mem_mb": memory, "active_disk_mb": disk,
                      "active_tasks": count, "reservation_mem_mb": reserved,
                      "mem_available_mb": available,
                      "memory_floor_mb": floor_mb,
                      "ramp_reserve_mb": ramp_total,
                      "measured_p90_mb": measured_p90,
                      "estimate_new_mb": estimate_new,
                      "estimate_new_codex_managed_mb": estimate_codex,
                      "estimate_percentile": policy["estimate_percentile"],
                      "estimate_multiplier": policy["estimate_multiplier"],
                      "ramp_reserve_default_mb": policy["ramp_reserve_default_mb"],
                      "ramp_seconds": policy["ramp_seconds"],
                      "cpu_load_factor": policy["cpu_load_factor"],
                      "cpu_target_utilization": policy["cpu_target_utilization"],
                      "cpu_default_cores": policy["cpu_default_cores"],
                      "cpu_estimate_multiplier": policy["cpu_estimate_multiplier"],
                      "cpu_psi_hard_cap_pct": policy["cpu_psi_hard_cap_pct"],
                      "cpu_psi_full_hard_cap_pct": policy["cpu_psi_full_hard_cap_pct"],
                      "memory_psi_full_hard_cap_pct": policy["memory_psi_full_hard_cap_pct"],
                      "choking_cooldown_seconds": policy["choking_cooldown_seconds"],
                      "cpu": cpu,
                      "breakdown": breakdown,
                      "launch_allowed": default_allowed,
                      "reason": reason,
                      "launch_allowed_codex_managed": codex_allowed,
                      "reason_codex_managed": None if codex_allowed else (
                          "launcher_memory_reserve" if not codex_headroom else cpu_reason),
                      "default_memory_mb": DEFAULT_NATIVE_MEMORY_MB,
                      "codex_managed_memory_mb": CODEX_MANAGED_NATIVE_MEMORY_MB,
                      "dry_run": args.dry_run, "reconciled": changes}))
    return 0


def reconcile(args):
    """Targeted reconciliation of a specific task or session without dispatching queued work."""
    store = get_store(args)
    task_id = store.resolve_task_id(args.id) or args.id
    task = store.get_task(task_id)
    if not task:
        err = f"error: unknown task {args.id}"
        if getattr(args, "json", False):
            print(json.dumps({"error": err, "id": args.id}))
        else:
            print(err)
        return 1

    state = task["state"]
    payload = task.get("payload") or {}
    binding = store.get_native_binding(task_id)
    if not binding:
        binding = store.recover_binding_from_artifacts(task_id, payload)

    status, detail = "unknown", "no native binding"
    if binding:
        from launcher.native_lifecycle import probe_native_lifecycle
        status, detail = probe_native_lifecycle(binding)
    else:
        alive, act_detail = native_status(run_tag_for(task_id))
        if alive == "dead":
            status, detail = "dead", act_detail
        elif alive == "alive":
            status, detail = "alive", act_detail
        else:
            status, detail = "unknown", act_detail

    evidence = _result_evidence(store, task_id)
    reconciled_action = None

    if status in ("dead", "retired-finished"):
        if evidence:
            if state in ("running", "starting", "stalled", "launch-uncertain"):
                try:
                    store.transition_task(
                        task_id,
                        "completed-awaiting-review",
                        (state,),
                        reason=f"reconciled: native exit confirmed ({detail}); result evidence: {', '.join(evidence)}",
                        reviewer=getattr(args, "reviewer", "reconcile"),
                    )
                    reconciled_action = "completed-awaiting-review"
                    state = "completed-awaiting-review"
                except StateTransitionError:
                    pass
        elif state in ("running", "starting", "stalled", "launch-uncertain"):
            try:
                store.transition_task(
                    task_id,
                    "failed",
                    (state,),
                    reason=f"reconciled: native exit confirmed ({detail}); no result evidence",
                    reviewer=getattr(args, "reviewer", "reconcile"),
                )
                reconciled_action = "failed"
                state = "failed"
            except StateTransitionError:
                pass

    resources_held = (
        state in ("queued", "starting", "launch-uncertain", "stalled", "running")
        and status not in ("dead", "retired-finished")
    )

    res_doc = {
        "task_id": task_id,
        "input_id": args.id,
        "state": state,
        "native_status": status,
        "native_detail": detail,
        "reconciled_action": reconciled_action,
        "result_evidence_count": len(evidence),
        "resources_reserved": resources_held,
    }
    if binding:
        res_doc["native_id"] = binding.get("native_id")
        res_doc["workspace"] = binding.get("workspace")
        res_doc["tag"] = binding.get("tag")

    if getattr(args, "json", False):
        print(json.dumps(res_doc, indent=2))
    else:
        msg = f"reconciled: {task_id} (state: {state}, native: {status}, detail: {detail})"
        if reconciled_action:
            msg += f" -> transitioned to {reconciled_action}"
        print(msg)
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

    # Automated refill: only triggered AFTER distinct independent review acceptance
    if getattr(args, "refill", True):
        from types import SimpleNamespace
        from launcher.watch import watch_loop
        config_dir = config_dir_for(args)
        refill_args = SimpleNamespace(
            config_dir=config_dir,
            backend=getattr(args, "backend", "task-units"),
            once=True,
            wait_for_review="dependencies",
        )
        print(f"triggering automated review-gated refill after acceptance of {args.id}")
        try:
            watch_loop(refill_args, max_passes=1)
        except Exception as e:
            print(f"refill dispatch error: {e}")
    return 0


def reject(args):
    store = get_store(args)
    task = store.get_task(args.id)
    if not task:
        print(f"error: unknown task {args.id}")
        return 1
    try:
        store.reject_task(args.id, args.reviewer, args.reason)
    except StateTransitionError as e:
        print(f"error: {e}")
        return 1
    print(f"rejected: {args.id} (reviewer {args.reviewer}; reason: {args.reason})")
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


def load_receipt_arg(receipt_str: str) -> dict:
    if not receipt_str:
        raise ValueError("missing --receipt")
    trimmed = receipt_str.strip()
    if trimmed.startswith("{") or "\n" in trimmed:
        return json.loads(trimmed)
    try:
        p = Path(trimmed)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        pass
    return json.loads(trimmed)


def verify_review(args):
    """Validate a review receipt against anti-self-review, prompt independence,
    report hash, temporal consistency, valid verdict, and optional runtime witness."""
    from launcher.review_receipt import validate_review_receipt
    try:
        receipt = load_receipt_arg(args.receipt)
    except Exception as e:
        print(json.dumps({"error": f"failed to load receipt JSON: {e}"}))
        return 1

    verify_files = not getattr(args, "no_verify_files", False)
    verify_git = not getattr(args, "no_verify_git", False)
    require_witness = getattr(args, "require_witness", False)
    store = get_store(args) if getattr(args, "verify_task_in_store", False) else None

    is_accepted, status, details = validate_review_receipt(
        receipt,
        verify_files=verify_files,
        verify_git=verify_git,
        require_witness=require_witness,
        store=store,
    )
    result = {
        "accepted": is_accepted,
        "status": status,
        "details": details,
    }
    print(json.dumps(result, indent=2))
    return 0 if is_accepted else 2


def record_review(args):
    """Validate and record a review receipt into store preserving negative history."""
    from launcher.review_receipt import validate_review_receipt
    try:
        receipt = load_receipt_arg(args.receipt)
    except Exception as e:
        print(json.dumps({"error": f"failed to load receipt JSON: {e}"}))
        return 1

    verify_files = not getattr(args, "no_verify_files", False)
    verify_git = not getattr(args, "no_verify_git", False)
    require_witness = getattr(args, "require_witness", False)
    store = get_store(args)

    is_accepted, status, details = validate_review_receipt(
        receipt,
        verify_files=verify_files,
        verify_git=verify_git,
        require_witness=require_witness,
        store=store,
    )
    store.add_review_receipt(receipt, status, details)

    result = {
        "recorded": True,
        "accepted": is_accepted,
        "status": status,
        "details": details,
    }
    print(json.dumps(result, indent=2))
    return 0 if is_accepted else 2


def main():
    parser = argparse.ArgumentParser(prog="launcher", description="Agent Quota Launcher")
    parser.add_argument("--config-dir", help="Path to config directory")

    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_init = subparsers.add_parser("init", help="create config + state dir")
    parser_init.set_defaults(func=init)

    parser_submit = subparsers.add_parser("submit", help="idempotent task submit")
    parser_submit.add_argument("--id", required=True)
    parser_submit.add_argument("--key", required=True)
    parser_submit.add_argument("--payload", required=True)
    parser_submit.add_argument("--paths", action="append", default=[])
    parser_submit.add_argument("--json", action="store_true", help="Emit the machine-readable task receipt")
    parser_submit.set_defaults(func=submit)

    parser_plan = subparsers.add_parser("plan", help="dry-run admission/ranking, no launch")
    parser_plan.add_argument("--id", help="use stored task payload")
    parser_plan.add_argument("--payload", help="inline task payload JSON")
    parser_plan.set_defaults(func=plan)

    parser_run = subparsers.add_parser("run", help="admit, reserve, launch, record")
    parser_run.add_argument("--id", required=True)
    parser_run.add_argument("--cwd", required=True)
    parser_run.add_argument("--tmpdir", default=None, help="Optional. Defaults to cwd-local owned tmpdir")
    parser_run.add_argument("--trusted-choking-json", default=None,
                            help="Bounded trusted host runtime evidence, independent of task payload")
    parser_run.add_argument(
        "--backend",
        default="aplexer",
        choices=["aplexer", "task-units"],
        help="aplexer = nested native start (held); task-units = sibling systemd unit",
    )
    parser_run.add_argument(
        "--as-controller",
        action="store_true",
        help="inner ql-ctl process: keep proven --wait execute path",
    )
    parser_run.add_argument(
        "--resource-config",
        default=None,
        help="explicit path to config/resource-management.json (never auto-loaded)",
    )
    parser_run.set_defaults(func=run)

    parser_request = subparsers.add_parser("request", help="one-command admission, validation, and execution")
    parser_request.add_argument("--goal", required=True, help="Task goal/prompt")
    parser_request.add_argument("--cwd", help="Task working directory")
    parser_request.add_argument("--target-commit", help="Target commit SHA to validate")
    parser_request.add_argument("--target-worktree", help="Path to worktree to validate commit")
    parser_request.add_argument("--profile", help="Task profile")
    parser_request.add_argument("--memory-mb", type=int, help="Memory override")
    parser_request.add_argument("--timeout", type=float, help="Timeout override")
    parser_request.add_argument("--paths", action="append", default=[])
    parser_request.add_argument("--id", help="Explicit task ID")
    parser_request.add_argument("--key", help="Idempotency key; resubmitting with the same key and payload safely replays (Store.submit_task)")
    parser_request.set_defaults(func=request)

    parser_status = subparsers.add_parser("status", help="show task/lifecycle")
    parser_status.add_argument("--id", help="show structured status for specific task")
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
    parser_accept.add_argument("--no-refill", dest="refill", action="store_false", default=True,
                               help="do not trigger automated refill dispatch after acceptance")
    parser_accept.add_argument("--backend", default="task-units", choices=["aplexer", "task-units"])
    parser_accept.set_defaults(func=accept)

    parser_reject = subparsers.add_parser("reject", help="reject reviewed artifacts, releasing path lease")
    parser_reject.add_argument("--id", required=True)
    parser_reject.add_argument("--reviewer", required=True)
    parser_reject.add_argument("--reason", required=True)
    parser_reject.set_defaults(func=reject)

    parser_fail = subparsers.add_parser(
        "fail",
        help="close a running task as failed; requires confirmed native death and a reason")
    parser_fail.add_argument("--id", required=True)
    parser_fail.add_argument("--reviewer", required=True)
    parser_fail.add_argument("--reason", required=True)
    parser_fail.set_defaults(func=fail)

    parser_model = subparsers.add_parser("model-support", help="Check an exact provider/model adapter capability without effects")
    parser_model.add_argument("--provider", required=True)
    parser_model.add_argument("--model", required=True)
    parser_model.set_defaults(func=model_support)

    parser_resources = subparsers.add_parser("resource-status", help="Reconcile reservations without launching or stopping sessions")
    parser_resources.add_argument("--dry-run", action="store_true")
    parser_resources.add_argument(
        "--resource-config",
        default=None,
        help="explicit path to config/resource-management.json (never auto-loaded)",
    )
    parser_resources.set_defaults(func=resource_status)

    parser_reconcile = subparsers.add_parser(
        "reconcile",
        help="targeted reconciliation of task or session without dispatching queued work",
    )
    parser_reconcile.add_argument("--id", required=True, help="task id or native session UUID to reconcile")
    parser_reconcile.add_argument("--reviewer", default="reconcile", help="reviewer recording transition")
    parser_reconcile.add_argument("--json", action="store_true", help="output machine-readable JSON")
    parser_reconcile.set_defaults(func=reconcile)

    parser_report = subparsers.add_parser("report", help="bounded JSON/JSONL dashboard projection")
    parser_report.add_argument("--jsonl", action="store_true")
    parser_report.add_argument("--project-id", default=None,
                               help="project identity stamped on every emitted line "
                                    "(default: quota-launcher)")
    parser_report.set_defaults(func=report)

    parser_watch = subparsers.add_parser("watch",
                                         help="non-LLM loop: reconcile uncertain launches, dispatch next queued task")
    parser_watch.add_argument("--cwd", default=None, help="fallback cwd if not specified in task payload")
    parser_watch.add_argument("--tmpdir", default=None, help="legacy tmpdir override; ignored in favor of task-local contained tmpdir")
    parser_watch.add_argument("--backend", default="task-units", choices=["aplexer", "task-units"])
    parser_watch.add_argument("--no-wait-review", dest="wait_for_review", action="store_false", default=True,
                               help="do not wait for unreviewed tasks before dispatch")
    parser_watch.add_argument("--interval", type=float, default=10.0)
    parser_watch.add_argument("--once", action="store_true", help="single reconcile+dispatch pass")
    parser_watch.add_argument("--cleanup-timeout", type=float, default=None)
    parser_watch.add_argument("--cleanup-cooldown-sec", type=float, default=300.0)
    parser_watch.add_argument("--cleanup-owner", type=str, default=None)
    parser_watch.set_defaults(func=watch)

    parser_verify = subparsers.add_parser(
        "verify-review",
        help="validate review receipt against anti-self-review, prompt independence, report hash, and timing",
    )
    parser_verify.add_argument("--receipt", required=True, help="path to receipt JSON or inline JSON string")
    parser_verify.add_argument("--no-verify-files", action="store_true", help="skip on-disk report file hash check")
    parser_verify.add_argument("--no-verify-git", action="store_true", help="skip git rev-parse commit check")
    parser_verify.add_argument("--require-witness", action="store_true", help="require verified systemd or aplexer runtime witness")
    parser_verify.add_argument("--verify-task-in-store", action="store_true", help="verify task_id exists in store")
    parser_verify.set_defaults(func=verify_review)

    parser_record = subparsers.add_parser(
        "record-review",
        help="validate and record review receipt to store preserving negative history",
    )
    parser_record.add_argument("--receipt", required=True, help="path to receipt JSON or inline JSON string")
    parser_record.add_argument("--no-verify-files", action="store_true", help="skip on-disk report file hash check")
    parser_record.add_argument("--no-verify-git", action="store_true", help="skip git rev-parse commit check")
    parser_record.add_argument("--require-witness", action="store_true", help="require verified systemd or aplexer runtime witness")
    parser_record.set_defaults(func=record_review)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as e:
        if "Configuration error" in str(e):
            parser.error(str(e))
        else:
            raise


if __name__ == "__main__":
    sys.exit(main())
