"""Launch adapters (strict allowlist, no shell parsing) and the bounded
launch lifecycle. Provider argv sets are exactly:

- grok:        /home/alexey/.local/bin/grok --model grok-4.6 --effort high
               --permission-mode auto -p <PROMPT>
               (installed grok  -p/--single requires the prompt as its value)
- antigravity: env -u GEMINI_API_KEY -u GOOGLE_API_KEY agy --model gemini-3.1-pro-high
               --effort high --dangerously-skip-permissions -p --print-timeout 0
               --output-format text <goal>   (OAuth route; ambient API keys stripped)
- zai:         /usr/bin/env ZCODE_NODE=/home/alexey/.nvm/versions/node/v24.13.1/bin/node
               ZCODE_CJS=/opt/ZCode/resources/glm/zcode.cjs
               /home/alexey/.local/bin/zcodex exec --model glm-5.3-flash
               -s workspace-write -c
               check_for_update_on_startup=false --json <goal>
               with ZCODE_NODE and ZCODE_CJS runtime pins propagated via argv and env
- opencode:    /home/alexey/.nvm/versions/node/v24.13.1/bin/opencode run
               --standalone --auto --model opencode-go/muse-spark-1.3-contributor
               <goal>  (installed 2.0.32; quse key go maps here; prompt is a
               separate argv element, never shell-interpolated; engine opencode)

Codex is unsupported in v0.1 (protected wrapper scripts/launch-codex.sh not
configured in this repo); `codex-cli -p` and `zai -p` never appear here.
GLM models run ONLY via zai (zcodex); build_adapter_argv refuses them elsewhere.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from launcher.admission import (
    fetch_quse,
    validate_quse,
    is_managed_profile_payload,
    task_requirements_for,
    extract_requested_model,
    PROMO_MULTIPLIER,
    PROMO_CUTOFF_UTC,
)
from launcher.ranking import select_candidate
from launcher.resources import (
    check_resources,
    native_memory_request_for,
    is_codex_managed_native,
)
from launcher.store import Store, launch_lock, StateTransitionError
from launcher.tags import run_tag_for

ZCODE_NODE = "/home/alexey/.nvm/versions/node/v24.13.1/bin/node"
ZCODE_CJS = "/opt/ZCode/resources/glm/zcode.cjs"
ZCODEX_BIN = "/home/alexey/.local/bin/zcodex"
OPENCODE_BIN = "/home/alexey/.nvm/versions/node/v24.13.1/bin/opencode"
OPENCODE_MODEL = "opencode-go/muse-spark-1.3-contributor"


class ZaiRuntimeError(RuntimeError, ValueError, OSError):
    """Raised when ZAI runtime requirements are missing or unexecutable."""
    pass


class ZaiRuntimeMissingError(ZaiRuntimeError, FileNotFoundError):
    """Raised when ZAI runtime dependencies (node, cjs, zcodex) are missing."""
    pass


class ZaiRuntimeUnexecutableError(ZaiRuntimeError, PermissionError):
    """Raised when ZAI runtime dependencies lack execution/read permissions."""
    pass


def validate_zai_runtime(node=None, cjs=None, zbin=None):
    """Validate that the pinned Node runtime, ZCode CJS bundle, and zcodex binary exist and are executable."""
    node_path = node if node is not None else ZCODE_NODE
    cjs_path = cjs if cjs is not None else ZCODE_CJS
    zbin_path = zbin if zbin is not None else ZCODEX_BIN

    if not os.path.exists(node_path) or not os.path.isfile(node_path):
        raise ZaiRuntimeMissingError(f"ZAI node runtime missing: {node_path}")
    if not os.access(node_path, os.X_OK):
        raise ZaiRuntimeUnexecutableError(f"ZAI node runtime not executable: {node_path}")

    if not os.path.exists(cjs_path) or not os.path.isfile(cjs_path):
        raise ZaiRuntimeMissingError(f"ZAI CJS runtime missing: {cjs_path}")
    if not os.access(cjs_path, os.R_OK):
        raise ZaiRuntimeUnexecutableError(f"ZAI CJS runtime not readable: {cjs_path}")

    if not os.path.exists(zbin_path) or not os.path.isfile(zbin_path):
        raise ZaiRuntimeMissingError(f"ZAI zcodex binary missing: {zbin_path}")
    if not os.access(zbin_path, os.X_OK):
        raise ZaiRuntimeUnexecutableError(f"ZAI zcodex binary not executable: {zbin_path}")

    return node_path, cjs_path, zbin_path


class OpencodeRuntimeError(RuntimeError, ValueError, OSError):
    """Raised when OpenCode runtime requirements are missing or unexecutable."""
    pass


class OpencodeRuntimeMissingError(OpencodeRuntimeError, FileNotFoundError):
    """Raised when the pinned OpenCode binary is missing."""
    pass


class OpencodeRuntimeUnexecutableError(OpencodeRuntimeError, PermissionError):
    """Raised when the pinned OpenCode binary lacks execution permission."""
    pass


def validate_opencode_runtime(opencode_bin=None):
    """Validate the pinned OpenCode binary exists and is executable.

    Model subscription itself is proven by `opencode models` containing
    OPENCODE_MODEL plus fresh quse go windows at admission; this check only
    refuses a missing/unexecutable binary (fail closed, no fallback model).
    """
    bin_path = opencode_bin if opencode_bin is not None else OPENCODE_BIN
    if not os.path.exists(bin_path) or not os.path.isfile(bin_path):
        raise OpencodeRuntimeMissingError(f"OpenCode binary missing: {bin_path}")
    if not os.access(bin_path, os.X_OK):
        raise OpencodeRuntimeUnexecutableError(f"OpenCode binary not executable: {bin_path}")
    return bin_path


ADAPTERS = {
    "grok": {
        "argv": ["/home/alexey/.local/bin/grok", "--model", "grok-4.6",
                 "--effort", "high", "--permission-mode", "auto", "-p"],
        "env": {},
    },
    "antigravity": {
        "argv": ["/usr/bin/env", "-u", "GEMINI_API_KEY", "-u", "GOOGLE_API_KEY",
                 "/home/alexey/.local/bin/agy", "--model", "gemini-3.1-pro-high", "--effort", "high",
                 "--dangerously-skip-permissions",
                 "--print-timeout", "0", "--output-format", "stream-json", "-p"],
        "env": {},
    },
    "zai": {
        "argv": ["/usr/bin/env", f"ZCODE_NODE={ZCODE_NODE}", f"ZCODE_CJS={ZCODE_CJS}",
                 ZCODEX_BIN, "exec", "--model", "glm-5.3-flash",
                 "-s", "workspace-write",
                 "-c", "check_for_update_on_startup=false", "--json"],
        "env": {"ZCODE_CJS": ZCODE_CJS, "ZCODE_NODE": ZCODE_NODE},
    },
    "opencode": {
        "argv": [OPENCODE_BIN, "run", "--standalone", "--auto", "--model",
                 OPENCODE_MODEL],
        "env": {},
    },
    "gemini": {
        "argv": ["/home/alexey/.local/bin/gemini", "-p"],
        "env": {},
    },
    "claude": {
        "argv": ["claude", "--dangerously-skip-permissions", "-p"],
        "env": {},
    },
}

# The native whoami is RICH (id, tag, workspace, engine, command, phase,
# parent_session, schema_version, created_at_ms, ...). A first action is
# validated by identity match against the launch start record, never by a
# key blacklist: genuine whoami output carries command/phase/... keys.
FIRST_ACTION_TIME_FIELDS = ("timestamp", "created_at_ms", "updated_at_ms")

MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 7200

# Appended to native OpenCode and Antigravity worker briefs.
# The first-action wait below can only succeed when the native worker knows
# the exact artifact path; without this instruction the child never learns
# it and every run stalls. The brief stays verbatim at the head (separate
# argv element downstream, never shell-interpolated); other providers keep
# existing semantics.
FIRST_ACTION_INSTRUCTION = (
    "\n\nFirst action (required before any other work): prove this worker "
    "is live by running `aplexer whoami --json` and writing its complete "
    "stdout to {path} (overwrite that file, nothing else). "
    "Then continue the brief."
)


# Routing rule (Alexey, 2026-10-14): GLM / Z.ai Coding Plan models run ONLY
# through zcodex (the `zai` route). OpenCode never runs GLM; it is pinned to
# Muse Spark 1.3 (OPENCODE_MODEL). Any GLM model on another route is refused.


def _is_glm_model(model):
    m = str(model or "").lower()
    return "glm" in m or m.startswith("zai-coding-plan/") or m.startswith("zai/")


def get_codex_wrapper():
    env_wrapper = os.environ.get("CODEX_LAUNCH_WRAPPER")
    if env_wrapper and os.path.isfile(env_wrapper) and os.access(env_wrapper, os.X_OK):
        return str(Path(env_wrapper).resolve())
    cand = Path("scripts/launch-codex.sh")
    if cand.is_file() and os.access(cand, os.X_OK):
        return str(cand.resolve())
    return None


def resolve_hub_path():
    env_path = os.environ.get("AGENT_HUB_HOST_PATH")
    if env_path and os.path.exists(env_path):
        return str(Path(env_path).resolve())
    hub_bin = shutil.which("hub") or str(Path.home() / ".local" / "bin" / "hub")
    if os.path.isfile(hub_bin):
        return str(Path(hub_bin).resolve())
    for cand in (
        Path.home() / "git" / "agent-hub" / "host",
        Path("/home/alexey/git/agent-hub/host"),
    ):
        if (cand / "agent_hub_host").is_dir():
            return str(cand.resolve())
    return None


def build_managed_profile_argv(provider, goal, label, model=None, managed_dir=None):
    hub_path = resolve_hub_path()
    if not hub_path:
        raise ValueError("Cannot resolve trusted Agent Hub host path for managed profile execution")
    context = ""
    if managed_dir is not None:
        catalog = str(Path(managed_dir).resolve())
        context = f"import os; os.environ['AGENT_HUB_MANAGED_DIR'] = {catalog!r}; "
    script = context + f"import sys; sys.path.insert(0, {hub_path!r}); from agent_hub_host.provisioning import execute; execute()"
    cmd = [sys.executable, "-c", script, label]
    if provider in ("codex", "openai"):
        args = ["exec", "--dangerously-bypass-approvals-and-sandbox", "--json"]
        if model:
            args += ["--model", str(model)]
        args.append(str(goal))
        return cmd + args
    elif provider in ("zai", "zcodex"):
        m = model or "glm-5.3-flash"
        return cmd + ["exec", "--model", str(m), "-s", "workspace-write", "-c", "check_for_update_on_startup=false", "--json", str(goal)]
    elif provider in ("go", "opencode"):
        m = model or "opencode-go/muse-spark-1.3-contributor"
        return cmd + ["run", "--standalone", "--auto", "--model", str(m), str(goal)]
    elif provider in ("gemini", "google"):
        m = model or "gemini-2.5-pro"
        return cmd + ["-m", str(m), "--skip-trust", "--approval-mode", "yolo", "-p", str(goal)]
    elif provider == "claude":
        m = model or "sonnet"
        return cmd + ["--dangerously-skip-permissions", "--model", str(m), "-p", str(goal)]
    else:
        raise ValueError(f"Provider {provider!r} is not supported for managed headless execution")


def native_command_model(command):
    """Only an explicit, unambiguous native command model is evidence."""
    if not isinstance(command, list) or any(not isinstance(x, str) for x in command):
        return None
    models = [command[i + 1] for i, flag in enumerate(command[:-1]) if flag in ("--model", "-m")]
    models += [x.split("=", 1)[1] for x in command if x.startswith("--model=")]
    return models[0] if models and len(set(models)) == 1 else None


def build_adapter_argv(provider, goal, model=None):
    if provider == "claude":
        raise ValueError("Claude provider requires managed profile")
    if model and _is_glm_model(model) and provider != "zai":
        raise ValueError(
            f"GLM model {model!r} is only allowed on the zai (zcodex) route, not {provider!r}"
        )
    if provider == "opencode":
        # Exact installed subscription model only; no fallback to paid/free
        # alternatives and no GLM via OpenCode (refused above). None means the
        # pinned default; any other value is refused (fail closed).
        if model is not None and str(model) != OPENCODE_MODEL:
            raise ValueError(
                f"Unsupported OpenCode model {model!r}; only {OPENCODE_MODEL!r} is supported"
            )
        validate_opencode_runtime()
    if provider == "codex":
        wrapper = get_codex_wrapper()
        if not wrapper:
            raise ValueError(
                "Unsupported provider: codex (genuine protected wrapper scripts/launch-codex.sh not configured; raw routing denied)"
            )
        chosen_model = model or "gpt-6.1-sol"
        return [wrapper, "exec", "--model", str(chosen_model),
                "--dangerously-bypass-approvals-and-sandbox", "--json", str(goal)]
    adapter = ADAPTERS.get(provider)
    if not adapter:
        raise ValueError(f"Unsupported provider: {provider}")
    if provider == "gemini":
        gemini_bin = os.environ.get("GEMINI_BIN", "/home/alexey/.local/bin/gemini")
        m = model or "gemini-2.5-pro"
        return [gemini_bin, "-m", str(m), "-p", str(goal)]
    if provider == "zai":
        node, cjs, zbin = validate_zai_runtime()
        argv = [
            "/usr/bin/env",
            f"ZCODE_NODE={node}",
            f"ZCODE_CJS={cjs}",
            zbin,
        ]
        base_argv = list(adapter["argv"])
        if zbin in base_argv:
            idx = base_argv.index(zbin) + 1
            argv.extend(base_argv[idx:])
        elif ZCODEX_BIN in base_argv:
            idx = base_argv.index(ZCODEX_BIN) + 1
            argv.extend(base_argv[idx:])
        else:
            argv.extend(["exec", "--model", "glm-5.3-flash",
                         "-s", "workspace-write",
                         "-c", "check_for_update_on_startup=false", "--json"])
    else:
        argv = list(adapter["argv"])
    if model:
        if "--model" in argv:
            idx = argv.index("--model")
            argv[idx + 1] = str(model)
    return argv + [str(goal)]


def _has_time_evidence(data, start_json, now_ms):
    """At least one time field, sane against the launch window: not before
    the launched session's creation, not in the fabricated future. ISO
    timestamps must be timezone-aware (naive fails) and are bounded like the
    epoch-ms fields."""
    ts = data.get("timestamp")
    if isinstance(ts, str):
        text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        parsed = datetime.fromisoformat(text)  # raises if unparsable
        if parsed.tzinfo is None:
            return False  # naive ISO timestamp: no provable instant
        if start_json.get("created_at_ms") and \
                parsed.timestamp() * 1000 < start_json["created_at_ms"] - 5000:
            return False  # stale: older than the launched session itself
        if now_ms is not None and parsed.timestamp() * 1000 > now_ms + 300_000:
            return False  # future: fabricated urgency
        return True
    for field in ("created_at_ms", "updated_at_ms"):
        ms = data.get(field)
        if isinstance(ms, (int, float)) and not isinstance(ms, bool):
            if start_json.get("created_at_ms") and ms < start_json["created_at_ms"] - 5000:
                return False  # older than the launched session itself
            if now_ms is not None and ms > now_ms + 300_000:
                return False  # fabricating the future
            return True
    return False


REQUIRED_WHOAMI_FIELDS = {
    "schema_version",
    "id",
    "tag",
    "workspace",
    "engine",
    "command",
    "phase",
    "worker_pid",
}

def validate_first_action(fa_path, start_json, min_mtime, now_ms=None):
    """Content check of the child's first-action artifact against the launch
    start record. Identity must match the launched session: id, tag and
    workspace equal the start record's, and parent_session must match when
    the artifact carries it. Identity match is not tool provenance: any
    record that is the wrapper start JSON itself — byte-identical,
    reformatted, or augmented with timestamp fields or arbitrary extra keys
    (such as dummy=123) while keeping start keys intact — is rejected as a
    laundered clone, not an agent action. A genuine rich native whoami taken
    after start must show live whoami mutation: live fields must differ from
    the start record (phase running/working vs starting, updated_at_ms /
    last_activity_ms after created_at_ms) and extra native keys are preserved."""
    try:
        if not os.path.exists(fa_path):
            return False
        if os.path.getmtime(fa_path) < min_mtime - 5:
            return False
        with open(fa_path, 'rb') as f:
            raw = f.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return False

        # 1. Structural authenticity: require all essential whoami fields.
        # Minimal 4-key cards (id, tag, workspace, timestamp) without execution
        # state (command, phase, schema_version, worker_pid) are rejected.
        if not REQUIRED_WHOAMI_FIELDS.issubset(data):
            return False
        if not isinstance(data.get("worker_pid"), int) or isinstance(data.get("worker_pid"), bool) or data["worker_pid"] <= 0:
            return False

        # Extra native whoami keys (boot_id, agent, state, pids_*, memory_*,
        # systemd_unit, ...) are preserved (C1450). Dummy keys on an unmodified
        # start clone still fail via the clone/superset checks below.

        # 3. Workload phase: a running workload whoami must be in an active phase.
        if data.get("phase") not in ("running", "working", "launching"):
            return False

        # 4. Identity match: id, tag, workspace must match the launched session.
        if data.get("id") != start_json.get("id"):
            return False
        if data.get("tag") != start_json.get("tag"):
            return False
        if data.get("workspace") != start_json.get("workspace"):
            return False
        if "parent_session" in data and \
                data.get("parent_session") != start_json.get("parent_session"):
            return False
        if "engine" in data and "engine" in start_json and data.get("engine") != start_json.get("engine"):
            return False
        if "command" in data and "command" in start_json and data.get("command") != start_json.get("command"):
            return False
        if "schema_version" in data and "schema_version" in start_json and data.get("schema_version") != start_json.get("schema_version"):
            return False

        # 5. Start-record superset / clone check:
        # If all keys match identically -> fail (start-record clone).
        if all(data.get(k) == v for k, v in start_json.items()):
            return False

        # If all non-phase keys match identically: flipping phase to running/working
        # and adding worker_pid is still a start-record clone unless authentic
        # time progression (updated_at_ms or last_activity_ms > created_at_ms) is present.
        non_phase_start = {k for k in start_json if k != "phase"}
        if all(data.get(k) == start_json[k] for k in non_phase_start):
            has_live_time = False
            for tf in ("updated_at_ms", "last_activity_ms"):
                val = data.get(tf)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    if start_json.get("created_at_ms") and val > start_json["created_at_ms"]:
                        has_live_time = True
            if not has_live_time:
                return False

        # 6. Altered start record check:
        differs = {k for k in start_json if data.get(k) != start_json[k]}
        added = set(data) - set(start_json)
        if added <= {"timestamp"} and \
                differs <= {"created_at_ms", "updated_at_ms", "phase"}:
            return False

        return _has_time_evidence(data, start_json, now_ms)
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

    out_err = (res.stderr or "") + " " + (res.stdout or "")
    out_err_lower = out_err.lower()
    
    if "stale" in out_err_lower and "socket" in out_err_lower:
        return "unknown", "aplexer status stale socket; reconcile native identity"
    if "transport" in out_err_lower:
        return "unknown", "aplexer status transport failure; reconcile native identity"

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        err_lower = err.lower()
        if "no session tagged" in err_lower or "has ever existed" in err_lower or "no matching session" in err_lower:
            from launcher.native_lifecycle import read_tombstone, UUID_RE
            if isinstance(tag, str) and UUID_RE.match(tag.strip()):
                tombstone = read_tombstone(tag.strip())
                if tombstone and tombstone.get("cause") == "finished":
                    return "dead", f"phase=finished (retired tombstone cause={tombstone.get('cause')})"
            return "unknown", f"aplexer status identity unavailable rc={res.returncode}; no exit proof"
        return "unknown", f"aplexer status error rc={res.returncode}: {err[:200]}"
        
    try:
        info = _extract_json(res.stdout)
    except Exception as e:
        return "unknown", f"unparsable status JSON: {e}"
        
    phase = info.get("phase")
    c_empty = info.get("containment_empty")
    if phase in ("running", "starting", "working", "launching"):
        return "alive", f"phase={phase} containment_empty={c_empty}"
    if phase in ("exited", "dead", "failed", "completed", "finished"):
        if c_empty is True:
            return "dead", f"phase={phase} containment_empty=True"
        return "unknown", f"terminal phase={phase} but containment_empty is not explicitly True (got {c_empty!r})"
    return "unknown", f"phase={phase!r}"


def _kill_process_group(proc, grace=10.0):
    """Terminate then kill the child's whole process group."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
            proc.wait(timeout=grace)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return

    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, AttributeError):
        try:
            proc.terminate()
            proc.wait(timeout=grace)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
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
    if tmpdir is None:
        tmpdir = str(Path(cwd).resolve() / ".local" / "tmp" / task_id)
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

    # Pre-action receiver guard for root-run profile
    from launcher.task_profiles import resolve_task_bounds
    bounds = resolve_task_bounds(payload)
    is_root = (
        bounds.get("profile") == "root-run"
        or payload.get("profile") == "root-run"
        or payload.get("root_run")
        or "root-run" in str(task_id)
    )
    if is_root:
        from launcher.root_profile import (
            check_root_pre_action,
            RootProfileError,
            generate_root_dependency_proof,
        )
        try:
            check_root_pre_action(
                payload,
                task_id=task_id,
                workspace=cwd,
                allow_test_fixture=False,
            )
        except RootProfileError as e:
            try:
                proof = generate_root_dependency_proof(payload, reason=str(e))
            except Exception:
                proof = {
                    "profile": "root-run",
                    "admitted": False,
                    "preclaim_hold": True,
                    "operational_root_qualified": False,
                    "unsupported_reason": str(e),
                }
            denied_record = {
                "task_id": task_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "outcome": "root-execution-denied",
                "reason": str(e),
                "operational_root_qualified": False,
                "dependency_proof": proof,
            }
            _write_launch_record(cwd, denied_record)
            try:
                store.transition_task(task_id, "blocked", ("queued",), reason=str(e))
            except Exception:
                pass
            raise

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
    config_dir = str(Path(lock_path).parent) if lock_path else None
    is_managed = is_managed_profile_payload(payload)
    account_label = payload.get("account_label") if is_managed else None
    managed_dir = payload.get("managed_dir") if is_managed else None
    task_reqs = task_requirements_for(payload)
    req_model = extract_requested_model(payload, provider=payload.get("provider") if is_managed else None)
    quse_data = fetch_quse(
        account_label=account_label,
        provider=payload.get("provider") if is_managed else None,
        requested_model=req_model,
        config_dir=config_dir,
        managed_dir=managed_dir,
    )
    record["quse_fetched_at"] = datetime.now(timezone.utc).isoformat()
    valid_routes, rejections = validate_quse(
        quse_data, task_requirements=task_reqs, config_dir=config_dir)
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

    # Single policy: managed native Codex defaults to 4096MiB (midrun OOM
    # at 1500MiB); every other provider stays 1500MiB. Host 10GiB, disk,
    # and quota gates are unchanged; the larger request simply reserves
    # more MemAvailable headroom via check_resources.
    requested_mem_mb = native_memory_request_for(
        provider=provider,
        is_managed=is_managed,
        explicit_mb=payload.get("memory_mb"),
    )
    record["requested_memory_mb"] = requested_mem_mb
    record["native_memory_managed_codex"] = bool(is_codex_managed_native(provider, is_managed))

    run_tag = run_tag_for(task_id)
    record["tag"] = run_tag

    # First-action artifact path (also recorded below): the child
    # needs it inside its brief (see FIRST_ACTION_INSTRUCTION).
    fa_path = Path(cwd) / ".local" / f"first-action-{task_id}.json"
    goal = payload.get("goal", "")
    if provider in {"opencode", "antigravity", "zai", "gemini", "claude"} or (provider == "codex" and is_managed):
        goal = str(goal) + FIRST_ACTION_INSTRUCTION.format(path=fa_path)

    cmd = [
        "aplexer", "start",
        "--json",
        "--memory", f"{requested_mem_mb}M",
        "--pids", "100",
        # Startup handshake only (30s). This is NOT a runtime cap: the
        # payload timeout bounds Popen.communicate (aplexer start) plus
        # the first-action wait below, never the native worker lifetime.
        "--startup-timeout-ms", "30000",
        "--workspace", workspace,
        "--cwd", str(Path(payload["cwd"]).resolve()),
        "--tag", run_tag,
    ]
    account_profile = payload.get("account_profile") or payload.get("provider_profile")
    if not account_profile and isinstance(payload.get("profile"), str) and payload["profile"].startswith("hub-"):
        account_profile = payload["profile"]

    if account_profile:
        cmd.extend(["--profile", account_profile])
        record["profile"] = account_profile
    account_label = payload.get("account_label")
    if account_label:
        record["account_label"] = account_label

    # Record the real model engine, rather than the generic shell default.
    # Antigravity has its own native lifecycle policy; Gemini is a distinct
    # engine even when the model name begins with gemini. Existing bindings
    # retain the engine recorded at their original birth.
    native_engine = {"opencode": "opencode", "antigravity": "antigravity", "zai": "zcodex", "codex": "codex", "gemini": "gemini", "claude": "claude"}.get(provider)
    if native_engine and not account_profile:
        cmd.extend(["--engine", native_engine])
    if provider == "opencode":
        validate_opencode_runtime()
    child_env = dict(ADAPTERS.get(provider, {}).get("env", {}))
    if provider == "zai":
        node, cjs, _ = validate_zai_runtime()
        child_env["ZCODE_NODE"] = node
        child_env["ZCODE_CJS"] = cjs
    child_env["TMPDIR"] = tmpdir_resolved
    for key, value in child_env.items():
        cmd.extend(["--env", f"{key}={value}"])
    cmd.extend(["--"])
    if account_profile:
        label = account_label or (account_profile[4:] if account_profile.startswith("hub-") else account_profile)
        cmd.extend(build_managed_profile_argv(provider, goal, label, model=chosen.get("model"), managed_dir=managed_dir))
    else:
        cmd.extend(build_adapter_argv(provider, goal, model=chosen.get("model")))
    record["adapter_argv"] = cmd[cmd.index("--") + 1:]

    deadline = time.monotonic() + timeout_seconds

    # --- critical section: reserve + spawn under the exclusive launch lock ---
    with launch_lock(lock_path):
        store.reserve_native_memory(task_id, requested_mem_mb)
        active_mem, active_disk = store.get_active_resources(exclude_task_id=task_id)
        record['active_mem_mb'], record['active_disk_mb'] = active_mem, active_disk
        check_resources(requested_mem_mb, workspace, tmpdir_resolved,
                        active_mem, active_disk, repo_root=workspace,
                        provider=provider, is_managed=is_managed)
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

    launch_output_path.parent.mkdir(parents=True, exist_ok=True)
    launch_output_path.write_text(json.dumps(start_json, indent=2, sort_keys=True))
    record["wrapper_start_json"] = start_json

    sess_id = start_json.get("id")
    sess_tag = start_json.get("tag")
    sess_ws = start_json.get("workspace")
    sess_engine = start_json.get("engine")
    sess_created_at_ms = start_json.get("created_at_ms")

    record["session_id"] = sess_id
    record["observed_tag"] = sess_tag

    # Durably persist compact native binding in store at real successful native start
    # Do NOT invent created_at_ms from started_at_ts or tag/ws/engine from request fallback.
    from launcher.native_lifecycle import UUID_RE
    has_full_observed_identity = (
        isinstance(sess_id, str) and bool(UUID_RE.match(sess_id.strip()))
        and isinstance(sess_tag, str) and bool(sess_tag.strip())
        and isinstance(sess_ws, str) and bool(sess_ws.strip()) and os.path.isabs(sess_ws.strip())
        and isinstance(sess_engine, str) and bool(sess_engine.strip())
        and type(sess_created_at_ms) is int and sess_created_at_ms > 0
    )

    if has_full_observed_identity:
        try:
            store.record_native_binding(
                task_id=task_id,
                native_id=sess_id,
                tag=sess_tag,
                workspace=sess_ws,
                engine=sess_engine,
                incarnation=int(sess_created_at_ms),
                details={
                    "start_phase": start_json.get("phase"),
                    "worker_pid": start_json.get("worker_pid"),
                    "profile": start_json.get("profile"),
                    "account_label": account_label,
                },
            )
        except Exception as e:
            record["native_binding_error"] = str(e)
    else:
        record["native_binding_unbound_reason"] = "missing or invalid required observed start fields"

    record["observed_model"] = native_command_model(start_json.get("command"))
    record["model_verified"] = record["observed_model"] == record["requested_model"]
    if not record["model_verified"]:
        record["outcome"] = "launch-uncertain"
        record["reason"] = "model_mismatch: native command does not prove requested model"
        _write_launch_record(cwd, record)
        store.transition_task(task_id, "launch-uncertain", ("starting",), reason=record["reason"])
        return 1

    # --- first-action wait happens OUTSIDE the launch lock ---
    record["first_action_path"] = str(fa_path)
    
    alive, detail = "unknown", "wait started"
    while time.monotonic() < deadline:
        if validate_first_action(str(fa_path), start_json,
                                 record["started_at_ts"], now_ms=time.time() * 1000):
            try:
                store.transition_task(task_id, "running", ("starting",),
                                      reason="genuine first action validated")
            except StateTransitionError:
                pass
            record["outcome"] = "running"
            record["first_action_validated_at"] = datetime.now(timezone.utc).isoformat()
            if has_full_observed_identity:
                try:
                    store.record_native_binding(
                        task_id=task_id,
                        native_id=sess_id,
                        tag=sess_tag,
                        workspace=sess_ws,
                        engine=sess_engine,
                        incarnation=int(sess_created_at_ms),
                        details={
                            "first_action_validated_at": record["first_action_validated_at"],
                            "start_phase": start_json.get("phase"),
                            "worker_pid": start_json.get("worker_pid"),
                            "profile": start_json.get("profile"),
                            "account_label": account_label,
                        },
                    )
                except Exception:
                    pass
            _write_launch_record(cwd, record)
            return 0
            
        # The CLI runs in the launcher checkout, while the child belongs to
        # payload.cwd. A bare tag would query the wrong workspace.
        alive, detail = native_status(sess_id or f"{sess_ws}:{sess_tag or run_tag}")
        if alive == "dead":
            break
            
        time.sleep(2)

    # Deadline reached or early native death without first action.
    if alive != "dead":
        alive, detail = native_status(sess_id or f"{sess_ws}:{sess_tag or run_tag}")
        
    record["native_status_at_deadline"] = detail
    if alive == "alive":
        try:
            store.transition_task(task_id, "stalled", ("starting",),
                                  reason=f"first action missing by deadline; native alive: {detail}")
        except StateTransitionError:
            pass
        record["outcome"] = "stalled"
    else:
        try:
            store.transition_task(task_id, "failed", ("starting",),
                                  reason=f"first action missing; native death confirmed: {detail}")
        except StateTransitionError:
            pass
        record["outcome"] = "failed"
    _write_launch_record(cwd, record)
    return 1
