"""Manager-spawned bounded sibling systemd TASK units (C2438 / C2441).

Spawns detached transient task service units outside the interactive head's
cgroup (aplexer-workload-6be4c247-4410-4bdb-968e-7fc2d5844941.scope).
Each task unit receives independent bounded MemoryMax and memory-derived TasksMax
under app.slice, preventing parent-bound 100-PID exhaustion and allowing
up to 50 concurrent headless workers across host memory.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from launcher.admission import validate_quse
from launcher.provider_admission import (
    QuotaAcquisition, identify_command,
    validate_command_environment, validate_provider_quota, require_quota_lifecycle,
)
from launcher.resources import (
    DEFAULT_MEMORY_FLOOR_MB,
    check_resources,
)
from launcher.resource_policy import RESOURCE_DEFAULTS
from launcher.task_profiles import (
    ENV_KEY_PATTERN,
    MAX_ENV_FILE_BYTES,
    ROOT_RUN_AUTH_ROLES,
    ROOT_RUN_ENV_ALLOWLIST,
    ROOT_RUN_IDENTITY_ENV_KEYS,
    ROOT_RUN_PROFILE_NAME,
    is_reserved_env_key,
    is_root_run_env_key_allowed,
    namespace_policy_for_profile,
    require_full_sha,
    require_full_sha256,
)

from launcher.task_resource_limits import MAX_TASK_MEMORY_MB, MIN_TASKS, tasks_max_for

MAX_MEMORY_MB = MAX_TASK_MEMORY_MB
TASKS_MAX = MIN_TASKS  # compatibility export: minimum, not the actual per-task limit
MAX_DISK_LOG_BYTES = 64 * 1024  # 64 KiB bounded disk log
CLEANUP_POLL_INTERVAL_SEC = 0.05
CLEANUP_TIMEOUT_SEC = 5.0

HEAD_SCOPE_FORBIDDEN_MARKERS = (
    "aplexer-workload-6be4c247-4410-4bdb-968e-7fc2d5844941",
    "aplexer-workload-",
)


class TaskUnitError(Exception):
    """Base exception for task unit operations."""
    pass


class TaskUnitAdmissionError(TaskUnitError):
    """Raised when task unit violates quota or resource floors."""
    pass


class TaskUnitExecutionError(TaskUnitError):
    """Raised when task unit execution fails or encounters cgroup errors."""
    pass


class TaskUnitCleanupError(TaskUnitError):
    """Raised when task unit fails to cleanly dissolve lingering PIDs."""
    pass


class TaskUnitTimeoutError(TaskUnitExecutionError):
    """Timeout without claiming foreign unit ownership."""

    def __init__(self, message: str, identity: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.identity = identity or {}


@dataclass(frozen=True)
class TrustedRootJobEvidence:
    """Immutable, typed trusted admission evidence for one Root-run job.

    Every field is mandatory and there is no default. This object is a plain
    Python dataclass: it CAN be constructed by any in-process code that fills
    the fields (including tests, fixtures, and self-minted callers). Type,
    fields, and hashes alone are NEVER authority.

    No in-process Python check can enforce an issuer boundary: a module-level
    dict or id() registry cannot distinguish a genuine out-of-process issuer
    from same-process injection. The trusted unmask branch is therefore
    REMOVED (default deny). Every Root job keeps full generic isolation until
    a genuine out-of-process verified issuer exists. No code in this repo mints
    production authority, and none must be added here.
    """

    job_id: str
    run_id: str
    lease_epoch: int
    host_id: str
    session_id: str
    script_path: str
    script_sha256: str
    source_commit: str
    auth_role: str
    expires_at: str

    def validate(self, now_utc: Optional[datetime.datetime] = None) -> Optional[str]:
        """Return the first denial reason, or None when the evidence holds."""
        for field_name in ("job_id", "run_id", "host_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                return f"root-trusted-evidence-missing-{field_name}"

        if isinstance(self.lease_epoch, bool) or not isinstance(self.lease_epoch, int):
            return "root-trusted-evidence-invalid-lease-epoch"
        if self.lease_epoch < 1:
            return "root-trusted-evidence-stale-lease-epoch"

        try:
            require_full_sha256(self.script_sha256, "script_sha256")
        except Exception:
            return "root-trusted-evidence-invalid-script-digest"
        try:
            require_full_sha(self.source_commit, "source_commit")
        except Exception:
            return "root-trusted-evidence-invalid-source-commit"

        if self.auth_role not in ROOT_RUN_AUTH_ROLES:
            return "root-trusted-evidence-unsupported-auth-role"

        script = Path(str(self.script_path))
        if not script.is_absolute():
            return "root-trusted-evidence-script-path-not-absolute"
        try:
            if script.is_symlink():
                return "root-trusted-evidence-script-symlink-denied"
            script_stat = script.stat()
        except OSError:
            return "root-trusted-evidence-script-unreadable"
        if not stat.S_ISREG(script_stat.st_mode):
            return "root-trusted-evidence-script-not-regular-file"
        if not os.access(str(script), os.X_OK):
            return "root-trusted-evidence-script-not-executable"
        try:
            actual_script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
        except OSError:
            return "root-trusted-evidence-script-unreadable"
        if actual_script_sha != str(self.script_sha256).strip().lower():
            return "root-trusted-evidence-script-sha-mismatch"

        if not isinstance(self.expires_at, str) or not self.expires_at.strip():
            return "root-trusted-evidence-missing-expiry"
        try:
            expiry = datetime.datetime.fromisoformat(self.expires_at.strip().replace("Z", "+00:00"))
        except ValueError:
            return "root-trusted-evidence-invalid-expiry"
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=datetime.timezone.utc)
        now = now_utc or datetime.datetime.now(datetime.timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        if expiry <= now:
            return "root-trusted-evidence-expired"
        try:
            ttl = (expiry - now).total_seconds()
        except Exception:
            return "root-trusted-evidence-invalid-expiry"
        if ttl > MAX_TRUSTED_EVIDENCE_TTL_SEC:
            return "root-trusted-evidence-ttl-exceeds-maximum"

        return None


@dataclass(frozen=True)
class NamespacePolicy:
    """Process/service namespace policy bound to one task unit."""

    policy: str
    profile: str
    isolated_pid: bool
    mask_native_services: bool
    reason: str


GENERIC_NAMESPACE_POLICY = NamespacePolicy(
    policy="generic-isolated",
    profile="generic",
    isolated_pid=True,
    mask_native_services=True,
    reason="generic-worker-isolation",
)


def _masked_namespace_policy(profile_name: Optional[str], reason: str) -> NamespacePolicy:
    """Root profile that did not earn the trusted namespace: full masking kept."""
    return NamespacePolicy(
        policy="root-trusted-masked",
        profile=str(profile_name or ""),
        isolated_pid=True,
        mask_native_services=True,
        reason=reason,
    )


ROOT_TRUSTED_NAMESPACE_POLICY = NamespacePolicy(
    policy="root-trusted-candidate",
    profile=ROOT_RUN_PROFILE_NAME,
    isolated_pid=False,
    mask_native_services=False,
    reason="root-trusted-admitted",
)


# F1/F2 trust-boundary hardening (source-only guard, runtime HELD).
# The Root unmask branch is REMOVED (default deny) until a genuine
# out-of-process verified issuer exists. ProductionRootReceiver (see
# launcher/root_profile.py) is currently unqualified
# (is_operationally_qualified() is False), so no positive issuer exists and
# every constructed TrustedRootJobEvidence stays fully masked.
# Type/fields/hash alone are NEVER authority. No in-process registry can
# enforce an issuer boundary, so none is kept here.
ROOT_TRUSTED_HELD_REASON = "root-trusted-held-no-qualified-receiver"

# Maximum trusted-evidence lifetime: an expiry more than this far in the
# future is rejected (F2: no 100-year self-minted leases).
MAX_TRUSTED_EVIDENCE_TTL_SEC = 3600

# HELD: the unmask branch was removed. resolve_namespace_policy() never
# returns the candidate policy; build_systemd_run_argv() refuses every
# trusted request. The constant below is retained only so existing imports
# keep resolving; it is NEVER returned by any live code path.


def resolve_namespace_policy(
    profile_name: Optional[str],
    trusted_root_evidence: Optional[TrustedRootJobEvidence] = None,
    now_utc: Optional[datetime.datetime] = None,
    require_synchronous_wait: bool = True,
) -> NamespacePolicy:
    """Decide the namespace policy for a task unit.

    Fail closed. A non-root profile, an absent evidence object, a wrong-typed
    object or an evidence object that fails any validation all yield MORE
    isolation, never less.

    HELD (R3): the trusted unmask branch is REMOVED. Even fully valid
    constructed evidence stays masked with ROOT_TRUSTED_HELD_REASON until a
    genuine out-of-process verified issuer exists. No in-process registry can
    authorize unmasking. The synchronous-wait gate is still enforced first so
    callers get the most specific denial reason.
    """
    declared = namespace_policy_for_profile(profile_name)
    if declared != "root-trusted-candidate":
        return GENERIC_NAMESPACE_POLICY

    # Exact type identity: a subclass or duck-typed impostor is rejected.
    if trusted_root_evidence is None:
        return _masked_namespace_policy(profile_name, "root-trusted-evidence-absent")
    if type(trusted_root_evidence) is not TrustedRootJobEvidence:
        return _masked_namespace_policy(profile_name, "root-trusted-evidence-invalid-type")

    denial = trusted_root_evidence.validate(now_utc)
    if denial:
        return _masked_namespace_policy(profile_name, denial)

    if not require_synchronous_wait:
        return _masked_namespace_policy(profile_name, "root-trusted-requires-synchronous-wait")

    # Default deny: no positive issuer exists. Never unmask.
    return _masked_namespace_policy(profile_name, ROOT_TRUSTED_HELD_REASON)


def invocation_ids_match(expected: Optional[str], live: Optional[str]) -> bool:
    """Kill/stop requires nonempty expected Invocation AND nonempty live match."""
    exp = (expected or "").strip()
    liv = (live or "").strip()
    return bool(exp) and bool(liv) and exp == liv


def show_unit_props(
    unit_name: str,
    properties: Tuple[str, ...] = (
        "InvocationID",
        "ControlGroup",
        "MainPID",
        "ExecMainPID",
        "LoadState",
        "ActiveState",
    ),
) -> Dict[str, str]:
    cmd = ["systemctl", "--user", "show", unit_name]
    for prop in properties:
        cmd.extend(["-p", prop])
    chk = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=False)
    props: Dict[str, str] = {}
    for line in (chk.stdout or "").splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            props[key] = val
    return props


def timeout_output_text(value) -> str:
    """Popen._check_timeout yields bytes stdout/stderr even when text=True (C2513)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_invocation_id(text: str) -> Optional[str]:
    match = re.search(r"invocation ID:\s*([a-zA-Z0-9_\-]+)", text or "", re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip() or None


def signal_owned_unit(unit_name: str, expected_inv: Optional[str], verb: str) -> bool:
    """Issue kill/stop only when nonempty expected Invocation matches nonempty live Invocation."""
    if verb not in ("kill", "stop"):
        raise ValueError("verb must be kill or stop")
    live = show_unit_props(unit_name)
    if not invocation_ids_match(expected_inv, live.get("InvocationID")):
        return False
    if verb == "kill":
        cmd = ["systemctl", "--user", "kill", "--signal=SIGKILL", unit_name]
    else:
        cmd = ["systemctl", "--user", "stop", unit_name]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return True


def sanitize_unit_name(task_id: str) -> str:
    """Derive a safe systemd unit name from a task identifier."""
    if not task_id or not isinstance(task_id, str):
        raise TaskUnitAdmissionError("task_id must be a non-empty string")
    clean = task_id.strip()
    if not clean:
        raise TaskUnitAdmissionError("task_id contains no valid unit characters")
    if "/" in clean or "\\" in clean or ".." in clean:
        raise TaskUnitAdmissionError(f"path traversal or directory separator forbidden in task_id: {task_id}")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", clean):
        raise TaskUnitAdmissionError(
            f"task_id contains invalid characters (only alphanumeric, _, - allowed): {task_id}"
        )
    return f"agent-task-{clean}.service"


def assert_cgroup_outside_head(cgroup_path: str) -> bool:
    """Verify that a cgroup path is not nested under any head scope."""
    if not cgroup_path or not isinstance(cgroup_path, str):
        return True
    for marker in HEAD_SCOPE_FORBIDDEN_MARKERS:
        if marker in cgroup_path:
            raise TaskUnitExecutionError(
                f"cgroup '{cgroup_path}' is nested under forbidden head scope marker '{marker}'"
            )
    return True


def find_enclosing_git_root(path: Union[str, Path]) -> Path:
    """Traverse up from path.resolve() to find enclosing git root.

    Traverses up from path.resolve() checking (current / ".git").exists().
    If found, returns current. If root / is reached without finding .git,
    returns Path(path).resolve().
    """
    current = Path(path).resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return Path(path).resolve()
        current = parent


def validate_task_memory(memory_mb, profile=None):
    ceiling = 1500 if profile == ROOT_RUN_PROFILE_NAME else MAX_MEMORY_MB
    if type(memory_mb) is not int or memory_mb <= 0:
        raise TaskUnitAdmissionError("worker memory must be a positive integer")
    if memory_mb > ceiling:
        raise TaskUnitAdmissionError(
            f"worker memory {memory_mb}M exceeds maximum allowed ceiling of {ceiling}M")


def admit_task_unit(
    task_id: str,
    memory_mb: int,
    workspace: str,
    tmpdir: Optional[str] = None,
    quse_json: Optional[Union[Dict[str, Any], QuotaAcquisition]] = None,
    provider: Optional[str] = None,
    active_mem_mb: int = 0,
    active_disk_mb: int = 0,
    check_capacity: bool = False,
    config_dir: Optional[Path] = None,
    command_argv: Optional[List[str]] = None,
    model: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    memory_floor_mb: Optional[int] = None,
    memory_estimate_mb: Optional[int] = None,
    cpu_load_factor: Optional[float] = None,
    cpu_usage: Optional[dict] = None,
    trusted_choking: Optional[dict] = None,
    expected_cpu_cores: Optional[float] = None,
    ramping_cpu_cores: float = 0.0,
    cpu_policy: Optional[dict] = None,
) -> bool:
    """Enforce floors and actual-command quota.

    No argv is a legacy quota/resource preflight only, never launch authority.
    Both execution entry points always supply argv; opaque commands are denied.
    Model commands are HELD until a qualified atomic process cohort backend
    exists, even with eligible bound quota. Raw dictionaries remain legacy
    non-model preflight evidence only; no model launch authority is granted.
    CPU admission is choking-only (t1511u): busy/predicted/unknown never
    refuse; only fresh PSI FULL CPU/memory, OOM, or trusted runtime hints.
    """
    validate_task_memory(memory_mb)

    # Fail closed on missing quota evidence (C2447)
    if quse_json is None:
        raise TaskUnitAdmissionError(
            "fresh quse_json evidence is required for task unit admission (fail closed)"
        )
    try:
        identity = identify_command(command_argv, provider, model) if command_argv is not None else None
        if identity is not None and identity.provider is not None:
            validate_command_environment(identity, extra_env)
            validate_provider_quota(
                quse_json, identity, check_capacity=check_capacity, config_dir=config_dir,
                command_argv=command_argv, extra_env=extra_env,
            )
        else:
            # Legacy preflight and explicit harmless non-model utilities retain
            # the old adapter quota gate. No unknown model command gets here.
            valid_routes, rejections = validate_quse(
                quse_json, check_capacity=check_capacity, config_dir=config_dir
            )
            if not valid_routes:
                raise TaskUnitAdmissionError(f"no valid quota route available: {rejections}")
            if provider:
                matching = [
                    r for r in valid_routes
                    if r.get("provider") == provider or r.get("name") == provider
                ]
                if not matching:
                    reason = rejections.get(provider, "provider route not accepted or exhausted")
                    raise TaskUnitAdmissionError(
                        f"requested provider '{provider}' quota rejected: {reason}"
                    )
    except TaskUnitAdmissionError:
        raise
    except Exception as e:
        raise TaskUnitAdmissionError(f"quota admission validation failed: {e}") from e

    # Enforce tmpdir strictly under repo .local/tmp
    repo_path = Path(workspace).resolve()
    if tmpdir is None:
        tmpdir = str(repo_path / ".local" / "tmp" / task_id)
    tmp_path = Path(tmpdir).resolve()
    allowed_tmp_root = repo_path / ".local" / "tmp"
    if not tmp_path.is_relative_to(allowed_tmp_root):
        raise TaskUnitAdmissionError(
            f"TMPDIR '{tmp_path}' must be located under '{allowed_tmp_root}'"
        )

    # Check live host floors (MemAvailable ramp+estimate >= floor, CPU
    # choking-only gate, disk floor, deny /data). The estimate defaults to
    # the full worker cap when the caller has no measured value (fail closed).
    # Runtime defaults track the shared Hub contract (floor 2048).
    try:
        check_resources(
            int(memory_mb),
            workspace,
            tmpdir,
            active_mem_mb=active_mem_mb,
            active_disk_mb=active_disk_mb,
            repo_root=workspace,
            memory_floor_mb=memory_floor_mb if memory_floor_mb is not None else DEFAULT_MEMORY_FLOOR_MB,
            memory_estimate_mb=memory_estimate_mb,
            memory_ceiling_mb=MAX_MEMORY_MB,
            cpu_load_factor=cpu_load_factor if cpu_load_factor is not None else RESOURCE_DEFAULTS["cpu_load_factor"],
            cpu_usage=cpu_usage,
            trusted_choking=trusted_choking,
            expected_cpu_cores=expected_cpu_cores,
            ramping_cpu_cores=ramping_cpu_cores,
            cpu_policy=cpu_policy,
        )
    except Exception as e:
        raise TaskUnitAdmissionError(f"resource floor check failed: {e}") from e

    # Deny /data destination explicitly (18.9 GiB free, below 50 GiB floor)
    if str(tmp_path).startswith("/data") or str(repo_path).startswith("/data"):
        raise TaskUnitAdmissionError("destination under /data is denied due to floor deficit")

    if identity is not None and identity.provider is not None:
        try:
            require_quota_lifecycle()
        except ValueError as exc:
            raise TaskUnitAdmissionError(str(exc)) from exc

    return True


def _validated_env_lines(
    extra_env: Dict[str, Any],
    namespace_policy: NamespacePolicy,
) -> List[str]:
    """Validate and render private per-job environment lines.

    Applies to both namespace policies (tightening, never loosening the base):
    key charset, reserved-name rejection, NUL and CR/LF rejection in both key
    and value, a hard 64 KiB size ceiling, and the root-trusted allowlist with
    mandatory identity keys that must equal the trusted evidence identity.
    """
    lines: List[str] = []
    trusted = namespace_policy.policy == "root-trusted-candidate"
    for key, value in sorted(extra_env.items(), key=lambda kv: str(kv[0])):
        if key is None or value is None:
            continue
        k = str(key)
        # F5: strict fullmatch plus explicit NUL/CRLF rejection in keys.
        # re.match with '$' would accept a trailing newline; fullmatch closes it.
        if not re.fullmatch(ENV_KEY_PATTERN, k):
            raise TaskUnitAdmissionError(
                f"Invalid private environment key name '<redacted>': must match {ENV_KEY_PATTERN}"
            )
        if "\x00" in k or "\n" in k or "\r" in k:
            raise TaskUnitAdmissionError(
                "Invalid private environment key name '<redacted>': NUL/CR/LF forbidden"
            )
        if is_reserved_env_key(k):
            raise TaskUnitAdmissionError(
                f"Reserved private environment key '{k}' is forbidden (loader/interpreter injection)"
            )
        v = str(value)
        if "\x00" in k or "\x00" in v:
            raise TaskUnitAdmissionError(
                f"NUL byte in private environment entry '{k}': <redacted>"
            )
        if "\n" in v or "\r" in v:
            raise TaskUnitAdmissionError(
                f"Newline in private environment value for '{k}': <redacted> (no silent escaping)"
            )
        # F5: a trailing backslash merges the next line via systemd line
        # continuation, so refuse it instead of silently widening the value.
        if v.endswith("\\"):
            raise TaskUnitAdmissionError(
                f"Invalid private environment value for '{k}': <redacted> (trailing backslash forbidden)"
            )
        # R4: systemd EnvironmentFile has no quoting/escaping facility here
        # (values are written raw as KEY=VALUE). A quote character could
        # merge or split entries under a systemd parser, so refuse both
        # quote types instead of silently passing them through.
        if '"' in v or "'" in v:
            raise TaskUnitAdmissionError(
                f"Invalid private environment value for '{k}': <redacted> (quote characters forbidden)"
            )
        if trusted:
            if not is_root_run_env_key_allowed(k):
                raise TaskUnitAdmissionError(
                    f"Environment key '{k}' is not on the root-run allowlist"
                )
            lines.append(f"{k}={v}\n")
            continue
        lines.append(f"{k}={v}\n")

    if trusted:
        # Identity binding: no borrowed identity, no missing lease/session.
        for identity_key in ROOT_RUN_IDENTITY_ENV_KEYS:
            if identity_key not in extra_env:
                raise TaskUnitAdmissionError(
                    f"root-run trusted admission requires identity key {identity_key}"
                )

    rendered = "".join(lines)
    if len(rendered.encode("utf-8")) > MAX_ENV_FILE_BYTES:
        raise TaskUnitAdmissionError(
            f"private environment file exceeds the {MAX_ENV_FILE_BYTES} byte ceiling"
        )
    return lines


def _assert_private_env_dir(tmp_path: Path, strict_private: bool = False) -> None:
    """The EnvironmentFile parent must be a real, non-symlinked directory.

    Generic units keep the shared worktree tmpdir and rely on O_NOFOLLOW plus
    the 0600 file mode. A trusted Root job also transports a job token, so it
    additionally requires a directory that is not readable or writable by group
    or other (fail closed on a shared/world-writable tmpdir).
    """
    if tmp_path.is_symlink():
        raise TaskUnitAdmissionError(
            f"EnvironmentFile directory must not be a symlink: {tmp_path}"
        )
    try:
        mode = tmp_path.stat().st_mode
    except OSError as e:
        raise TaskUnitAdmissionError(f"EnvironmentFile directory is unusable: {e}") from e
    if not stat.S_ISDIR(mode):
        raise TaskUnitAdmissionError(f"EnvironmentFile parent is not a directory: {tmp_path}")
    if strict_private and mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TaskUnitAdmissionError(
            f"EnvironmentFile directory {tmp_path} is not private (trusted job env requires 0700)"
        )


def build_systemd_run_argv(
    unit_name: str,
    command_argv: List[str],
    memory_mb: int,
    tmpdir: str,
    workspace: Optional[str] = None,
    stdout_path: Optional[str] = None,
    stderr_path: Optional[str] = None,
    wait: bool = False,
    slice_name: str = "app.slice",
    extra_env: Optional[Dict[str, str]] = None,
    protect_system: Optional[str] = None,
    read_only_paths: Optional[List[str]] = None,
    read_write_paths: Optional[List[str]] = None,
    trusted_root_evidence: Optional[TrustedRootJobEvidence] = None,
    profile: Optional[str] = None,
    quota_acquisition: Optional[QuotaAcquisition] = None,
    **_rejected_kwargs: Any,
) -> List[str]:
    """Construct systemd-run invocation for a detached transient service unit.

    Notice: Does NOT pass `--scope`. Runs as a standalone transient `.service`
    unit directly under the user manager, ensuring it does not inherit the
    head's 100-task or 1500M cgroup ceiling.

    Namespace policy is fail closed and strictly derived inside this function
    (F1: no public namespace_policy override exists; any legacy
    ``namespace_policy=`` kwarg raises). The trusted host-PID form is REMOVED
    (R3 default deny): every trusted request is refused loudly and the argv is
    always the generic isolated form. No code in this repo mints production
    authority.
    """
    validate_task_memory(memory_mb, profile)
    if _rejected_kwargs:
        # F1: the public override was removed. Fail loudly so a caller that
        # still passes namespace_policy=/ROOT_TRUSTED_*/duck-type cannot unmask.
        bad = sorted(_rejected_kwargs.keys())
        raise TaskUnitAdmissionError(
            f"namespace_policy override was removed (F1 trust boundary); got unexpected kwarg(s): {bad}"
        )
    if not unit_name.endswith(".service"):
        raise TaskUnitAdmissionError(f"transient unit must end with .service, got: {unit_name}")

    # Fail-closed namespace resolution before any file or process effect.
    # Strictly derived: caller booleans/duck-types cannot inject a policy.
    policy = resolve_namespace_policy(
        profile,
        trusted_root_evidence=trusted_root_evidence,
        require_synchronous_wait=wait,
    )
    if policy.policy not in ("generic-isolated", "root-trusted-masked"):
        raise TaskUnitAdmissionError(
            f"unrecognized namespace policy '{policy.policy}' (reason: {policy.reason})"
        )
    if trusted_root_evidence is not None:
        # R3 HELD/REMOVED: the trusted unmask branch was deleted. Any trusted
        # request is refused loudly instead of silently degrading a Root job
        # to an isolated unit that cannot reach its own systemd/Bus.
        # Known incompatibilities held with the removal (no positive issuer):
        # (a) a symlinked token resolving to the script would exec a
        # retargetable path; (b) validate->hash->exec-by-path is a TOCTOU
        # window without fd/private-copy exec; (c) execute/spawn always wrap
        # as [python, prelude, *command] so an exact single-token
        # [evidence-script] binding can never be satisfied on the real call
        # path. Default deny until a genuine out-of-process verified issuer
        # exists.
        raise TaskUnitAdmissionError(
            f"trusted root admission refused: {policy.reason}"
        )

    # This builder alone grants no execution authority for opaque/legacy argv.
    # Refuse identifiable model commands even if the caller omits the receipt.
    try:
        build_identity = identify_command(command_argv)
    except ValueError:
        build_identity = None
    if quota_acquisition is not None or (build_identity is not None and build_identity.provider is not None):
        try:
            require_quota_lifecycle()
        except ValueError as exc:
            raise TaskUnitAdmissionError(str(exc)) from exc

    argv = [
        "systemd-run",
        "--user",
        f"--unit={unit_name}",
        f"--slice={slice_name}",
        "--expand-environment=no",
        "--collect",
    ]
    if wait:
        argv.append("--wait")
    argv.extend([
        "-p", f"MemoryMax={int(memory_mb)}M",
        "-p", f"TasksMax={tasks_max_for(memory_mb)}",
        "-E", f"TMPDIR={tmpdir}",
        "-E", f"TEMP={tmpdir}",
        "-E", f"TMP={tmpdir}",
    ])

    # Model-unit construction is held above. Legacy generic build-only paths
    # preserve their existing environment transport and are not launch authority.
    account_env = None

    # Propagate strict whitelisted execution PATH, HOME, USER to transient unit
    path_entries = [
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".nvm" / "versions" / "node" / "v24.13.1" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    valid_paths = [p for p in path_entries if os.path.isdir(p)]
    unit_path = account_env["PATH"] if account_env else ":".join(valid_paths)

    argv.extend([
        "-E", f"PATH={unit_path}",
        "-E", f"HOME={account_env['HOME'] if account_env else os.environ.get('HOME', str(Path.home()))}",
        "-E", f"USER={account_env['USER'] if account_env else os.environ.get('USER', 'alexey')}",
    ])

    # Propagate ZCode runtime environment if present
    zcode_cjs = os.environ.get("ZCODE_CJS", "/opt/ZCode/resources/glm/zcode.cjs")
    if os.path.exists(zcode_cjs):
        argv.extend(["-E", f"ZCODE_CJS={zcode_cjs}"])

    # Detect credential flags in command_argv regardless of extra_env presence
    for arg in command_argv:
        lower = str(arg).lower()
        if any(pattern in lower for pattern in ("--password=", "--token=", "--api-key=", "--secret=", "bearer ")):
            raise TaskUnitAdmissionError(
                "Credential argument detected in command argv: <redacted>. Use EnvironmentFile transport instead."
            )
        if lower in ("--password", "--token", "--api-key", "--secret", "-token"):
            raise TaskUnitAdmissionError(
                "Credential argument detected in command argv: <redacted>. Use EnvironmentFile transport instead."
            )
        if str(arg).startswith("hubt_") or str(arg).startswith("hubr_"):
            raise TaskUnitAdmissionError(
                "Credential argument detected in command argv: <redacted>. Use EnvironmentFile transport instead."
            )

    # F4: validate the workspace BEFORE any file effect, so a bad workspace
    # can never leave a token-bearing EnvironmentFile behind.
    ws_path = None
    if workspace:
        ws_path = Path(workspace).resolve()
        if not ws_path.is_dir():
            raise TaskUnitAdmissionError(f"workspace path does not exist or is not a directory: {workspace}")

    # F4: every exception during argv build (including after the env file is
    # written) and every execute/spawn start failure must remove the private
    # env file. Track it here and unlink on any failure below.
    env_file_path: Optional[Path] = None
    try:
        if extra_env:
            # Prevent secret leakage in command argv: ensure no private env value appears in command_argv
            joined_cmd = " ".join(str(c) for c in command_argv)
            for k, v in extra_env.items():
                if v and len(str(v)) >= 4 and str(v) in joined_cmd:
                    raise TaskUnitAdmissionError(
                        f"Secret value for '{k}' detected in command argv: private credentials must be passed via EnvironmentFile only"
                    )

            if policy.policy == "root-trusted-candidate":
                # Identity binding: the HR_* values must be this evidence's own
                # identity, never a borrowed one from another job/lease/run.
                expected_identity = {
                    "HR_RUN_ID": str(trusted_root_evidence.run_id),
                    "HR_HUB_JOB_ID": str(trusted_root_evidence.job_id),
                    "HR_HUB_LEASE_EPOCH": str(trusted_root_evidence.lease_epoch),
                }
                for identity_key, expected_value in expected_identity.items():
                    actual = extra_env.get(identity_key)
                    if actual is None:
                        raise TaskUnitAdmissionError(
                            f"root-run trusted admission requires identity key {identity_key}"
                        )
                    if str(actual).strip() != expected_value:
                        raise TaskUnitAdmissionError(
                            f"borrowed identity refused: {identity_key} does not match the trusted evidence"
                        )

            # Validate before touching the filesystem, then write the private
            # EnvironmentFile 0600 (O_NOFOLLOW denies a symlinked target).
            lines = _validated_env_lines(extra_env, policy)

            tmp_p = Path(tmpdir).resolve()
            tmp_p.mkdir(parents=True, exist_ok=True, mode=0o700)
            _assert_private_env_dir(tmp_p, strict_private=policy.policy == "root-trusted-candidate")
            env_file_path = tmp_p / f"{unit_name}.env"
            if env_file_path.is_symlink():
                raise TaskUnitAdmissionError("EnvironmentFile path must not be a symlink")
            try:
                env_file_path.parent.resolve().relative_to(tmp_p)
            except ValueError as e:
                raise TaskUnitAdmissionError("EnvironmentFile must live directly under tmpdir") from e
            fd = os.open(
                str(env_file_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fchmod(fd, 0o600)
                with open(fd, "w", encoding="utf-8", closefd=False) as f:
                    f.write("".join(lines))
            finally:
                os.close(fd)
            written_mode = stat.S_IMODE(os.stat(str(env_file_path)).st_mode)
            if written_mode != 0o600:
                raise TaskUnitAdmissionError(
                    f"EnvironmentFile mode {oct(written_mode)} is not the required 0600"
                )
            argv.extend(["-p", f"EnvironmentFile={env_file_path}"])
        if ws_path is not None:
            argv.extend(["-p", f"WorkingDirectory={ws_path}"])
        if stdout_path:
            argv.extend(["-p", f"StandardOutput=file:{stdout_path}"])
        if stderr_path:
            argv.extend(["-p", f"StandardError=file:{stderr_path}"])
        if not stdout_path and not stderr_path and not wait:
            argv.append("--pipe")

        if protect_system:
            argv.extend(["-p", f"ProtectSystem={protect_system}"])
        if read_only_paths:
            for p in read_only_paths:
                argv.extend(["-p", f"ReadOnlyPaths={p}"])
        if read_write_paths:
            for p in read_write_paths:
                argv.extend(["-p", f"ReadWritePaths={p}"])

        # Namespace application (R3: unmask removed, default deny). Every unit
        # stays in a private PID namespace with aplexer/systemd/Bus masked.
        from launcher.pid_isolation import manager_properties
        argv.extend(manager_properties())

        argv.append("--")
        from launcher.pid_isolation import wrap_command
        argv.extend(wrap_command(command_argv, sys.executable))
        return argv
    except BaseException:
        # R2/F4: remove the token-bearing file on any BaseException raised
        # during argv construction (negatives: KI/SystemExit and
        # wrap_command/manager_properties/fchmod failures leave no env
        # file). Re-raise original.
        if env_file_path is not None:
            try:
                if env_file_path.exists() and not env_file_path.is_symlink():
                    env_file_path.unlink()
            except BaseException:
                pass
        raise


def generate_prelude_code(expected_unit_name: str, expected_workspace: Optional[str] = None) -> str:
    """Generate inline Python prelude verifying cgroup isolation and working directory before execution."""
    ws_check_code = ""
    if expected_workspace:
        ws_resolved = str(Path(expected_workspace).resolve())
        ws_check_code = f"""
# 4. Verify working directory matches expected workspace (fail-closed per C2465/C2468)
expected_ws = {repr(ws_resolved)}
current_cwd = os.path.realpath(os.getcwd())
if current_cwd != os.path.realpath(expected_ws):
    sys.stderr.write(f"FATAL: Service cwd '{{current_cwd}}' does not match expected workspace '{{expected_ws}}'\\n")
    sys.exit(99)
"""

    return f"""# Auto-generated task unit prelude
import os, sys

# 1. Inspect own cgroup
try:
    with open('/proc/self/cgroup', 'r') as f:
        cg_content = f.read().strip()
except Exception as e:
    sys.stderr.write(f"Prelude error reading /proc/self/cgroup: {{e}}\\n")
    sys.exit(95)

# 2. Verify NOT in head scope
forbidden = {repr(HEAD_SCOPE_FORBIDDEN_MARKERS)}
for mark in forbidden:
    if mark in cg_content:
        sys.stderr.write(f"FATAL: Process running inside forbidden head scope {{mark}}\\n")
        sys.exit(96)

# 3. Verify in expected transient unit and app.slice (fail-closed per C2447)
unit_base = {repr(expected_unit_name.replace('.service', ''))}
if unit_base not in cg_content:
    sys.stderr.write(f"FATAL: cgroup '{{cg_content}}' does not match expected unit '{{unit_base}}'\\n")
    sys.exit(97)
if 'app.slice' not in cg_content:
    sys.stderr.write(f"FATAL: cgroup '{{cg_content}}' is not placed under app.slice\\n")
    sys.exit(98)
{ws_check_code}
# Proceed to execute wrapped command
if len(sys.argv) > 1:
    os.execvp(sys.argv[1], sys.argv[1:])
"""


def _is_cgroup_dissolved_or_empty(cg_rel_path: str) -> bool:
    """Check cgroup.events for populated 0 and verify all descendant cgroup.procs empty."""
    cgroup_root = Path("/sys/fs/cgroup")
    clean_rel = cg_rel_path.strip("/")
    target = cgroup_root / clean_rel

    if not target.exists():
        return True

    events_file = target / "cgroup.events"
    if events_file.exists():
        try:
            content = events_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("populated"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] != "0":
                        return False
        except Exception:
            pass

    for procs_file in target.glob("**/cgroup.procs"):
        try:
            pids = procs_file.read_text(encoding="utf-8").strip()
            if pids:
                return False
        except Exception:
            pass

    return True


def verify_task_unit_cleanup(
    unit_name: str,
    timeout_sec: float = CLEANUP_TIMEOUT_SEC,
    systemctl_cmd: Optional[List[str]] = None,
    expected_invocation_id: Optional[str] = None,
    expected_cgroup: Optional[str] = None,
    expected_pid: Optional[int] = None,
) -> Tuple[bool, Dict[str, str]]:
    """Verify that a transient unit has stopped and its cgroup is fully dissolved.

    Handles systemd --collect metadata erasure cleanly: when a unit completes and
    systemd garbage-collects its properties (ActiveState=inactive, SubState=dead,
    ControlGroup="", InvocationID=""), verifies that the known prior cgroup
    and PID are fully dead. Never touches foreign or reused active units.
    """
    cmd_base = systemctl_cmd or ["systemctl", "--user"]
    deadline = time.time() + timeout_sec
    props: Dict[str, str] = {}

    while time.time() < deadline:
        try:
            res = subprocess.run(
                cmd_base + [
                    "show",
                    unit_name,
                    "-p", "ActiveState",
                    "-p", "SubState",
                    "-p", "ControlGroup",
                    "-p", "InvocationID",
                    "-p", "ExecMainPID",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            props = {}
            for line in res.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v

            active_state = props.get("ActiveState", "unknown")
            sub_state = props.get("SubState", "unknown")
            cg = props.get("ControlGroup", "")
            inv_id = props.get("InvocationID", "")
            pid_str = props.get("ExecMainPID", "0")
            current_pid = int(pid_str) if pid_str.isdigit() else 0

            # If expected invocation was given and a different or unverified active invocation is present, do not touch it
            if expected_invocation_id:
                if inv_id and inv_id != expected_invocation_id:
                    return False, props
                if active_state not in ("inactive", "failed") and not inv_id:
                    return False, props

            # If inactive or dead, verify cgroup dissolution and PID termination
            if active_state in ("inactive", "failed") and sub_state in ("dead", "failed", ""):
                # 1. Verify tracked PID is dead
                target_pid = expected_pid or (current_pid if current_pid > 0 else None)
                if target_pid and os.path.exists(f"/proc/{target_pid}"):
                    return False, props

                # 2. Verify cgroup dissolution (either current cg, expected cg, or slice path)
                target_cg = cg or expected_cgroup or f"app.slice/{unit_name}"
                if _is_cgroup_dissolved_or_empty(target_cg):
                    return True, props

        except Exception:
            pass
        time.sleep(CLEANUP_POLL_INTERVAL_SEC)

    # Final attempt to stop unit if still running and verified owned
    try:
        inv_id = props.get("InvocationID", "")
        active_state = props.get("ActiveState", "unknown")
        # Only stop when nonempty expected Invocation matches nonempty live Invocation.
        if not invocation_ids_match(expected_invocation_id, inv_id):
            return False, props

        subprocess.run(
            cmd_base + ["stop", unit_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass

    return False, props


def _bounded_pipe_pump(pipe, target_path: Path, max_bytes: int):
    """Pump subprocess stream to disk with strict byte bounding."""
    bytes_written = 0
    try:
        with open(target_path, "wb") as f:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                if bytes_written < max_bytes:
                    allowed = min(len(chunk), max_bytes - bytes_written)
                    f.write(chunk[:allowed])
                    bytes_written += allowed
                    f.flush()
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def execute_transient_task_unit(
    task_id: str,
    command_argv: List[str],
    memory_mb: int,
    workspace: str,
    tmpdir: Optional[str] = None,
    timeout_sec: float = 1200.0,
    quse_json: Optional[Union[Dict[str, Any], QuotaAcquisition]] = None,
    provider: Optional[str] = None,
    log_dir: Optional[str] = None,
    check_capacity: bool = False,
    config_dir: Optional[Path] = None,
    protect_system: Optional[str] = None,
    read_only_paths: Optional[List[str]] = None,
    read_write_paths: Optional[List[str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    profile: Optional[str] = None,
    trusted_root_evidence: Optional[TrustedRootJobEvidence] = None,
    model: Optional[str] = None,
    memory_floor_mb: Optional[int] = None,
    memory_estimate_mb: Optional[int] = None,
    cpu_load_factor: Optional[float] = None,
    cpu_usage: Optional[dict] = None,
    trusted_choking: Optional[dict] = None,
    expected_cpu_cores: Optional[float] = None,
    ramping_cpu_cores: float = 0.0,
    cpu_policy: Optional[dict] = None,
    active_mem_mb: int = 0,
    active_disk_mb: int = 0,
) -> Dict[str, Any]:
    """Execute a task in a manager-spawned sibling systemd unit.

    Returns an execution receipt detailing unit, cgroup, timing, and exit code.
    Fails closed if admission fails, unit fails to start, or lingering PIDs remain.

    ``profile``/``trusted_root_evidence`` only select the namespace policy and
    can never relax admission: actual-command quota admission runs before launch.
    """
    # 0. Calculate loaded module digest BEFORE execution per C2469
    command_argv = list(command_argv)
    try:
        module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except Exception:
        module_sha256 = "unknown"

    # 1. Admission check
    validate_task_memory(memory_mb, profile)
    admit_task_unit(
        task_id,
        memory_mb,
        workspace,
        tmpdir,
        quse_json=quse_json,
        provider=provider,
        check_capacity=check_capacity,
        config_dir=config_dir,
        command_argv=command_argv,
        model=model,
        extra_env=extra_env,
        memory_floor_mb=memory_floor_mb,
        memory_estimate_mb=memory_estimate_mb,
        cpu_load_factor=cpu_load_factor,
        cpu_usage=cpu_usage,
        trusted_choking=trusted_choking,
        expected_cpu_cores=expected_cpu_cores,
        ramping_cpu_cores=ramping_cpu_cores,
        cpu_policy=cpu_policy,
        active_mem_mb=active_mem_mb,
        active_disk_mb=active_disk_mb,
    )
    command_identity = identify_command(command_argv, provider, model)
    provider = command_identity.provider
    model = command_identity.model
    unit_name = sanitize_unit_name(task_id)

    repo_dir = Path(workspace).resolve()
    if not repo_dir.is_dir():
        raise TaskUnitAdmissionError(f"workspace directory does not exist: {workspace}")
    if tmpdir is None:
        tmpdir = str(repo_dir / ".local" / "tmp" / task_id)
    tmpdir_path = Path(tmpdir).resolve()

    # Mandatory policy binding & negative escaping path validation
    if protect_system is None:
        protect_system = "strict"
    if read_only_paths is None:
        repo_root = find_enclosing_git_root(repo_dir)
        read_only_paths = [str(repo_root)]
    if read_write_paths is None:
        read_write_paths = [str(repo_dir), str(tmpdir_path)]
    else:
        for p in read_write_paths:
            p_res = Path(p).resolve()
            if not (p_res.is_relative_to(repo_dir) or p_res.is_relative_to(tmpdir_path)):
                raise TaskUnitAdmissionError(
                    f"read_write_paths contains unauthorized escaping path outside workspace: '{p}'"
                )

    tmpdir_path.mkdir(mode=0o700, parents=True, exist_ok=True)

    out_dir = Path(log_dir).resolve() if log_dir else repo_dir / ".local"
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = out_dir / f"{task_id}-stdout.log"
    stderr_log = out_dir / f"{task_id}-stderr.log"
    stdout_log.touch(mode=0o600, exist_ok=True)
    stderr_log.touch(mode=0o600, exist_ok=True)

    # 2. Write prelude with verified expected workspace (fail-closed cwd check)
    prelude_script = tmpdir_path / f"prelude_{task_id}.py"
    prelude_code = generate_prelude_code(unit_name, expected_workspace=str(repo_dir))
    prelude_script.write_text(prelude_code, encoding="utf-8")
    prelude_script.chmod(0o700)

    # 3. Construct systemd-run invocation with file redirection (0 pump threads)
    # and explicit WorkingDirectory. R2/F4: build_systemd_run_argv removes
    # the EnvironmentFile on BaseException during argv construction, and
    # validates the workspace before any file effect. R2/R5: this outer
    # wrapper also unlinks the token env AND the non-secret prelude if the
    # build raises (including KeyboardInterrupt/SystemExit, which never
    # reach the Popen try below). S2: started_at/clean_env are computed
    # inside this same try so a BaseException between a successful build
    # and Popen startup still removes both files.
    wrapped_command = [sys.executable, str(prelude_script)] + list(command_argv)
    try:
        systemd_cmd = build_systemd_run_argv(
            unit_name=unit_name,
            command_argv=wrapped_command,
            memory_mb=memory_mb,
            tmpdir=str(tmpdir_path),
            workspace=str(repo_dir),
            stdout_path=str(stdout_log),
            stderr_path=str(stderr_log),
            wait=True,
            protect_system=protect_system,
            read_only_paths=read_only_paths,
            read_write_paths=read_write_paths,
            extra_env=extra_env,
            profile=profile,
            trusted_root_evidence=trusted_root_evidence,
            quota_acquisition=quse_json if isinstance(quse_json, QuotaAcquisition) else None,
        )
        # S2: inside the build-failure wrapper (see above).
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        clean_env = dict(os.environ)
        clean_env["TMPDIR"] = str(tmpdir_path)
        clean_env["TEMP"] = str(tmpdir_path)
        clean_env["TMP"] = str(tmpdir_path)
    except BaseException:
        try:
            if prelude_script.exists():
                prelude_script.unlink()
        except BaseException:
            pass
        try:
            leaked = tmpdir_path / f"{unit_name}.env"
            if leaked.exists() and not leaked.is_symlink():
                leaked.unlink()
        except BaseException:
            pass
        raise

    assigned_inv_id = None
    proc = None

    try:
        proc = subprocess.Popen(
            systemd_cmd,
            cwd=str(repo_dir),
            env=clean_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        # Expected identity is the systemd-run launch witness, not an immediate
        # systemctl show (same-name unit can still be live from a prior run).

        captured_cg = ""
        captured_mem_peak = None
        captured_cpu_usage = None
        
        def _poll_telemetry():
            nonlocal captured_cg, captured_mem_peak, captured_cpu_usage
            while proc.poll() is None:
                try:
                    props = show_unit_props(unit_name, properties=("ControlGroup", "MemoryPeak", "CPUUsageNSec"))
                    cg_val = props.get("ControlGroup")
                    if cg_val and cg_val.strip() and cg_val.strip() != "[not set]":
                        captured_cg = cg_val.strip()
                    
                    mp_val = props.get("MemoryPeak")
                    if mp_val and mp_val.isdigit():
                        captured_mem_peak = int(mp_val)
                        
                    cpu_val = props.get("CPUUsageNSec")
                    if cpu_val and cpu_val.isdigit():
                        captured_cpu_usage = int(cpu_val)
                except Exception:
                    pass
                time.sleep(CLEANUP_POLL_INTERVAL_SEC)

        t = threading.Thread(target=_poll_telemetry, daemon=True)
        t.start()

        try:
            out, err = proc.communicate(timeout=timeout_sec)
            combined_run_output = timeout_output_text(out) + "\n" + timeout_output_text(err)
            assigned_inv_id = parse_invocation_id(combined_run_output)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as timed_out:
            partial = (
                timeout_output_text(getattr(timed_out, "stdout", None))
                + "\n"
                + timeout_output_text(getattr(timed_out, "stderr", None))
            )
            assigned_inv_id = parse_invocation_id(partial)
            live = show_unit_props(unit_name)
            killed = signal_owned_unit(unit_name, assigned_inv_id, "kill")
            try:
                proc.kill()
            except Exception:
                pass
            identity = {
                "expected_invocation_id": assigned_inv_id or "",
                "live_invocation_id": (live.get("InvocationID") or "").strip(),
                "live_cgroup": (live.get("ControlGroup") or "").strip(),
                "live_pid": (live.get("MainPID") or live.get("ExecMainPID") or "").strip(),
                "load_state": (live.get("LoadState") or "").strip(),
                "killed": killed,
                "witness_source": "systemd-run-timeout-output",
            }
            raise TaskUnitTimeoutError(
                f"task unit {unit_name} exceeded timeout of {timeout_sec}s identity={identity}",
                identity=identity,
            )

    except TaskUnitTimeoutError:
        raise
    except BaseException as e:
        # R2/S1: after startup begins, stop the owned unit, then re-raise
        # the original. Only plain Exceptions are wrapped as
        # TaskUnitExecutionError; BaseExceptions (KeyboardInterrupt/
        # SystemExit) propagate unwrapped after cleanup.
        try:
            signal_owned_unit(unit_name, assigned_inv_id, "stop")
            verify_task_unit_cleanup(
                unit_name,
                timeout_sec=2.0,
                expected_invocation_id=assigned_inv_id,
            )
        except BaseException:
            pass
        if isinstance(e, Exception):
            raise TaskUnitExecutionError(f"execution failed: {e}") from e
        raise

    finally:
        # Remove prelude script and environment file (R5: prelude too).
        try:
            if prelude_script.exists():
                prelude_script.unlink()
        except BaseException:
            pass
        try:
            env_file = tmpdir_path / f"{unit_name}.env"
            if env_file.exists():
                env_file.unlink()
        except BaseException:
            pass

    # 4. Verify post-stop cleanup & cgroup dissolution
    cleaned_up, props = verify_task_unit_cleanup(
        unit_name,
        timeout_sec=CLEANUP_TIMEOUT_SEC,
        expected_invocation_id=assigned_inv_id,
    )
    if not cleaned_up:
        raise TaskUnitCleanupError(
            f"unit {unit_name} did not cleanly dissolve lingering PIDs or cgroup: {props}"
        )

    # 5. Verify cgroup was outside head scope
    cg = captured_cg or props.get("ControlGroup", "")
    if cg:
        assert_cgroup_outside_head(cg)

    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 6. Extract genuine structured telemetry events (no synthesized events)
    events = extract_telemetry_events(stdout_log)
    tool_calls, raw_tool_events_count = parse_tool_events(events)
    first_tool = tool_calls[0] if tool_calls else None

    result_info = None
    for ev in events:
        if ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            result_info = ev["result"]
            break

    # Preserve genuine structured events alongside final artifact in workspace
    events_artifact = repo_dir / f"{task_id}-telemetry.jsonl"
    log_events_path = out_dir / f"{task_id}-telemetry.jsonl"
    if events:
        try:
            with open(events_artifact, "w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")
        except Exception:
            pass
        try:
            with open(log_events_path, "w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")
        except Exception:
            pass

    return {
        "task_id": task_id,
        "unit_name": unit_name,
        "invocation_id": assigned_inv_id,
        "exit_code": exit_code,
        "cgroup": cg,
        "memory_peak_bytes": captured_mem_peak,
        "cpu_usage_nsec": captured_cpu_usage,
        "workspace": str(repo_dir),
        "working_directory": str(repo_dir),
        "protect_system": protect_system,
        "read_only_paths": read_only_paths,
        "read_write_paths": read_write_paths,
        "module_sha256": module_sha256,
        "provider": provider,
        "model": model,
        "model_identity_source": "command-argv",
        "timeout_sec": timeout_sec,
        "started_at": started_at,
        "finished_at": finished_at,
        "cleanup_verified": True,
        "memory_max_mb": memory_mb,
        "tasks_max": tasks_max_for(memory_mb),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "telemetry_events_path": str(events_artifact) if events else None,
        "log_telemetry_path": str(log_events_path) if events else None,
        "tool_calls_count": len(tool_calls),
        "tool_events_count": raw_tool_events_count,
        "first_tool": first_tool,
        "model_status": result_info.get("status") if result_info else None,
        "usage": result_info.get("usage") if result_info else None,
    }


_raw_execute_transient_task_unit = execute_transient_task_unit


def extract_telemetry_events(stdout_path: Path) -> List[Dict[str, Any]]:
    """Extract genuine structured invocation/tool events from stdout log.
    Never synthesizes events: returns only lines that are valid JSON objects
    representing genuine structured events from adapters."""
    events = []
    if not stdout_path.is_file():
        return events
    try:
        with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not (line.startswith("{") and line.endswith("}")):
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        # Matches agy stream-json, grok streaming-json, or zcodex json
                        if (
                            "event" in obj
                            or "type" in obj
                            or "tool_name" in obj
                            or "step_update" in obj
                            or "result" in obj
                        ):
                            events.append(obj)
                except Exception:
                    continue
    except Exception:
        pass
    return events


def parse_tool_events(events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Extract distinct tool-call events and the total count of raw tool events from stream.

    Dedup assumptions (C3045/C3048):
    - Indexed entries are deduplicated by ``step_index`` alone, which assumes the
      adapter emits globally unique indices across the whole run; an adapter that
      resets indices per turn would collapse distinct calls. ``tool_events_count``
      (the raw count) is retained in receipts as the audit fallback whenever
      ``tool_calls_count`` looks suspiciously low.
    - Index-less ACTIVE events are paired with a later index-less DONE of the same
      tool_name across the entire stream, so cross-turn false pairing is possible.
      Telemetry only: no control-flow decision consumes these counts.
    """
    raw_tools = []
    for ev in events:
        step = ev.get("step_update") if isinstance(ev.get("step_update"), dict) else ev
        if (
            step.get("step_type") == "tool"
            or "tool_name" in step
            or ("tool_info" in step and isinstance(step.get("tool_info"), dict))
        ):
            tool_name = step.get("tool_name") or (step.get("tool_info") or {}).get("name")
            tool_entry = {
                "tool_name": tool_name,
                "state": step.get("state"),
                "step_index": step.get("step_index"),
                "duration_seconds": step.get("duration_seconds"),
                "tool_info": step.get("tool_info"),
            }
            raw_tools.append(tool_entry)
        elif ev.get("type") in ("tool_use", "tool_call"):
            tool_entry = {
                "tool_name": ev.get("name") or ev.get("tool_name"),
                "state": ev.get("state", "DONE"),
                "parameters": ev.get("parameters") or ev.get("input"),
            }
            raw_tools.append(tool_entry)

    distinct = []
    seen_indices = set()
    indexed = {}
    for tc in raw_tools:
        idx = tc.get("step_index")
        if idx is not None:
            if idx not in indexed:
                indexed[idx] = []
            indexed[idx].append(tc)

    i = 0
    while i < len(raw_tools):
        tc = raw_tools[i]
        idx = tc.get("step_index")
        if idx is not None:
            if idx not in seen_indices:
                seen_indices.add(idx)
                best = tc
                for c in indexed[idx]:
                    if c.get("state") == "DONE":
                        best = c
                        break
                distinct.append(best)
            i += 1
        else:
            state = tc.get("state")
            if state == "ACTIVE":
                found_done = False
                for j in range(i + 1, len(raw_tools)):
                    future = raw_tools[j]
                    if future.get("step_index") is None and future.get("tool_name") == tc.get("tool_name") and future.get("state") == "DONE":
                        found_done = True
                        break
                if not found_done:
                    distinct.append(tc)
            else:
                distinct.append(tc)
            i += 1

    return distinct, len(raw_tools)


def spawn_transient_task_unit(
    task_id: str,
    command_argv: List[str],
    memory_mb: int,
    workspace: str,
    tmpdir: Optional[str] = None,
    quse_json: Optional[Union[Dict[str, Any], QuotaAcquisition]] = None,
    provider: Optional[str] = None,
    log_dir: Optional[str] = None,
    check_capacity: bool = False,
    config_dir: Optional[Path] = None,
    protect_system: Optional[str] = None,
    read_only_paths: Optional[List[str]] = None,
    read_write_paths: Optional[List[str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    profile: Optional[str] = None,
    trusted_root_evidence: Optional[TrustedRootJobEvidence] = None,
    model: Optional[str] = None,
    memory_floor_mb: Optional[int] = None,
    memory_estimate_mb: Optional[int] = None,
    cpu_load_factor: Optional[float] = None,
    cpu_usage: Optional[dict] = None,
    trusted_choking: Optional[dict] = None,
    expected_cpu_cores: Optional[float] = None,
    ramping_cpu_cores: float = 0.0,
    cpu_policy: Optional[dict] = None,
    active_mem_mb: int = 0,
    active_disk_mb: int = 0,
) -> Dict[str, Any]:
    """Asynchronously spawn a manager-spawned sibling systemd unit.

    Returns immediately with unit details and invocation ID.
    0 PIDs and 0 threads remain in the calling controller process during execution.

    The trusted Root namespace is unavailable here: this path has wait=False and
    therefore no owner that removes the private EnvironmentFile afterwards.
    """
    command_argv = list(command_argv)
    try:
        module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except Exception:
        module_sha256 = "unknown"

    validate_task_memory(memory_mb, profile)
    admit_task_unit(
        task_id,
        memory_mb,
        workspace,
        tmpdir,
        quse_json=quse_json,
        provider=provider,
        check_capacity=check_capacity,
        config_dir=config_dir,
        command_argv=command_argv,
        model=model,
        extra_env=extra_env,
        memory_floor_mb=memory_floor_mb,
        memory_estimate_mb=memory_estimate_mb,
        cpu_load_factor=cpu_load_factor,
        cpu_usage=cpu_usage,
        trusted_choking=trusted_choking,
        expected_cpu_cores=expected_cpu_cores,
        ramping_cpu_cores=ramping_cpu_cores,
        cpu_policy=cpu_policy,
        active_mem_mb=active_mem_mb,
        active_disk_mb=active_disk_mb,
    )
    command_identity = identify_command(command_argv, provider, model)
    provider = command_identity.provider
    model = command_identity.model
    unit_name = sanitize_unit_name(task_id)

    repo_dir = Path(workspace).resolve()
    if not repo_dir.is_dir():
        raise TaskUnitAdmissionError(f"workspace directory does not exist: {workspace}")
    if tmpdir is None:
        tmpdir = str(repo_dir / ".local" / "tmp" / task_id)
    tmpdir_path = Path(tmpdir).resolve()

    # Mandatory policy binding & negative escaping path validation
    if protect_system is None:
        protect_system = "strict"
    if read_only_paths is None:
        repo_root = find_enclosing_git_root(repo_dir)
        read_only_paths = [str(repo_root)]
    if read_write_paths is None:
        read_write_paths = [str(repo_dir), str(tmpdir_path)]
    else:
        for p in read_write_paths:
            p_res = Path(p).resolve()
            if not (p_res.is_relative_to(repo_dir) or p_res.is_relative_to(tmpdir_path)):
                raise TaskUnitAdmissionError(
                    f"read_write_paths contains unauthorized escaping path outside workspace: '{p}'"
                )

    tmpdir_path.mkdir(mode=0o700, parents=True, exist_ok=True)

    out_dir = Path(log_dir).resolve() if log_dir else repo_dir / ".local"
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = out_dir / f"{task_id}-stdout.log"
    stderr_log = out_dir / f"{task_id}-stderr.log"
    stdout_log.touch(mode=0o600, exist_ok=True)
    stderr_log.touch(mode=0o600, exist_ok=True)

    prelude_script = tmpdir_path / f"prelude_{task_id}.py"
    prelude_code = generate_prelude_code(unit_name, expected_workspace=str(repo_dir))
    prelude_script.write_text(prelude_code, encoding="utf-8")
    prelude_script.chmod(0o700)

    try:
        wrapped_command = [sys.executable, str(prelude_script)] + list(command_argv)
        systemd_cmd = build_systemd_run_argv(
            unit_name=unit_name,
            command_argv=wrapped_command,
            memory_mb=memory_mb,
            tmpdir=str(tmpdir_path),
            workspace=str(repo_dir),
            stdout_path=str(stdout_log),
            stderr_path=str(stderr_log),
            wait=False,
            protect_system=protect_system,
            read_only_paths=read_only_paths,
            read_write_paths=read_write_paths,
            extra_env=extra_env,
            profile=profile,
            trusted_root_evidence=trusted_root_evidence,
            quota_acquisition=quse_json if isinstance(quse_json, QuotaAcquisition) else None,
        )
        # S2: inside the build-failure wrapper so a BaseException between
        # a successful build and subprocess.run startup still removes the
        # token env and the prelude (see except below).
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        clean_env = dict(os.environ)
        clean_env["TMPDIR"] = str(tmpdir_path)
        clean_env["TEMP"] = str(tmpdir_path)
        clean_env["TMP"] = str(tmpdir_path)
    except BaseException:
        # R2/F4+R5: build failure must not leave the token-bearing env file
        # nor the non-secret prelude behind. Re-raise original (including KI).
        try:
            if prelude_script.exists():
                prelude_script.unlink()
        except BaseException:
            pass
        try:
            leaked = tmpdir_path / f"{unit_name}.env"
            if leaked.exists() and not leaked.is_symlink():
                leaked.unlink()
        except BaseException:
            pass
        raise

    try:
        res = subprocess.run(
            systemd_cmd,
            cwd=str(repo_dir),
            env=clean_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except BaseException:
        # R2/F4: spawn-start failure must not leak the private env file.
        try:
            leaked = tmpdir_path / f"{unit_name}.env"
            if leaked.exists() and not leaked.is_symlink():
                leaked.unlink()
        except BaseException:
            pass
        raise
    if res.returncode != 0:
        try:
            leaked = tmpdir_path / f"{unit_name}.env"
            if leaked.exists() and not leaked.is_symlink():
                leaked.unlink()
        except BaseException:
            pass
        raise TaskUnitExecutionError(
            f"failed to spawn unit {unit_name}: rc={res.returncode}, stderr={res.stderr}"
        )

    combined = (res.stdout or "") + "\n" + (res.stderr or "")
    m = re.search(r"invocation ID:\s*([a-zA-Z0-9_\-]+)", combined, re.IGNORECASE)
    inv_id = m.group(1) if m else None

    return {
        "task_id": task_id,
        "unit_name": unit_name,
        "invocation_id": inv_id,
        "workspace": str(repo_dir),
        "working_directory": str(repo_dir),
        "protect_system": protect_system,
        "read_only_paths": read_only_paths,
        "read_write_paths": read_write_paths,
        "module_sha256": module_sha256,
        "provider": provider,
        "model": model,
        "model_identity_source": "command-argv",
        "started_at": started_at,
        "memory_max_mb": memory_mb,
        "tasks_max": tasks_max_for(memory_mb),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
