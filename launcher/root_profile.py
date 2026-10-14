"""Root admission profile and pre-action receiver guard (bounded useful launcher v0.1).

Defines:
- Explicit 'root-run' admission profile bounded to 1500s timeout / 1500MiB memory.
- RootJobSpec: typed admission validation for immutable job/host/session/runID/epoch
  and private per-job environment transport via 0600 EnvironmentFile.
- Secret sanitization: prevents secret leakage in argv, commands, or logs.
- RootReceiver interface and ProductionRootReceiver: fail-closed pre-action guard
  enforcing that actual Root/native effects remain DENIED before claim/start/effect
  absent an independently qualified trusted receiver.
- IsolatedTestReceiver: isolated offline test-only positive fixture that explicitly
  cannot enable operational root execution.
- Dependency proof generation for preclaim hold.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

ROOT_PROFILE_NAME = "root-run"
ROOT_MAX_TIMEOUT_SEC: float = 1500.0
ROOT_MIN_TIMEOUT_SEC: float = 60.0
ROOT_MAX_MEMORY_MB: int = 1500
ROOT_MIN_MEMORY_MB: int = 128

UNSUPPORTED_RECEIVER_REASON = (
    "No independently qualified trusted receiver authenticating current "
    "epoch/session/runID and fencing before real native action is installed"
)


class RootProfileError(Exception):
    """Base error for root profile operations."""
    pass


class RootAdmissionError(RootProfileError, ValueError):
    """Raised when root-run admission specification fails validation."""
    pass


class RootExecutionDeniedError(RootProfileError, PermissionError, ValueError):
    """Raised when root execution is denied at the pre-action boundary."""
    pass


class RootSecretLeakError(RootAdmissionError):
    """Raised when secrets or private credentials appear in argv or command lines."""
    pass


@dataclass(frozen=True)
class RootJobSpec:
    """Typed, immutable specification for root-run task admission."""
    job_id: str
    host: str
    session_id: str
    run_id: str
    lease_epoch: Union[int, str]
    workspace: str
    timeout_sec: float = ROOT_MAX_TIMEOUT_SEC
    memory_mb: int = ROOT_MAX_MEMORY_MB
    private_env: Dict[str, str] = field(default_factory=dict)
    command_argv: Tuple[str, ...] = field(default_factory=tuple)
    provider: Optional[str] = None
    model: Optional[str] = None
    profile: str = ROOT_PROFILE_NAME
    expires_at: Optional[str] = None
    expired: bool = False
    deadline_at: Optional[str] = None
    revoked: bool = False
    revoked_at: Optional[str] = None
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate immutability, safety ceilings, and lack of secrets in argv."""
        if not self.job_id or not str(self.job_id).strip():
            raise RootAdmissionError("job_id must be a non-empty string")
        if not self.host or not str(self.host).strip():
            raise RootAdmissionError("host must be a non-empty string")
        if not self.session_id or not str(self.session_id).strip():
            raise RootAdmissionError("session_id must be a non-empty string")
        if not self.run_id or not str(self.run_id).strip():
            raise RootAdmissionError("run_id must be a non-empty string")
        if self.lease_epoch is None or isinstance(self.lease_epoch, bool):
            raise RootAdmissionError("lease_epoch must be provided as a positive integer")
        try:
            if isinstance(self.lease_epoch, int):
                int_epoch = self.lease_epoch
            elif isinstance(self.lease_epoch, str) and self.lease_epoch.isdigit():
                int_epoch = int(self.lease_epoch)
            else:
                raise ValueError
            if int_epoch < 1:
                raise ValueError
        except (ValueError, TypeError):
            raise RootAdmissionError(f"lease_epoch must be a positive integer >= 1, got {self.lease_epoch!r}")

        ws = Path(self.workspace).resolve()
        if not ws.is_dir():
            raise RootAdmissionError(f"workspace path does not exist or is not a directory: {self.workspace}")

        if not (ROOT_MIN_TIMEOUT_SEC <= self.timeout_sec <= ROOT_MAX_TIMEOUT_SEC):
            raise RootAdmissionError(
                f"Root-run timeout {self.timeout_sec}s outside bounded range "
                f"[{ROOT_MIN_TIMEOUT_SEC}, {ROOT_MAX_TIMEOUT_SEC}]"
            )
        if not (ROOT_MIN_MEMORY_MB <= self.memory_mb <= ROOT_MAX_MEMORY_MB):
            raise RootAdmissionError(
                f"Root-run memory {self.memory_mb}MiB outside bounded range "
                f"[{ROOT_MIN_MEMORY_MB}, {ROOT_MAX_MEMORY_MB}]"
            )

        if self.provider is not None:
            p_lower = str(self.provider).lower().strip()
            if p_lower in ("codex", "openai") or p_lower not in ("grok", "antigravity", "zai"):
                raise RootAdmissionError(f"Provider '{self.provider}' is unsupported for root-run profile")

        # Enforce that no private_env values appear in command_argv
        if self.command_argv and self.private_env:
            joined_argv = " ".join(self.command_argv)
            for k, v in self.private_env.items():
                if v and len(str(v)) >= 4 and str(v) in joined_argv:
                    raise RootSecretLeakError(
                        f"Secret value for '{k}' detected in command argv. "
                        "Private per-job credentials must be passed via EnvironmentFile only."
                    )

        # Detect generic secret patterns in argv (both '=' and space-separated, and hubt_/hubr_ tokens)
        for arg in self.command_argv:
            lower = str(arg).lower()
            if any(pattern in lower for pattern in (
                "--password=", "--token=", "--api-key=", "--secret=", "bearer "
            )):
                raise RootSecretLeakError(
                    "Credential argument detected in command argv: <redacted>. "
                    "Use private_env EnvironmentFile transport instead."
                )
            if lower in ("--password", "--token", "--api-key", "--secret", "-token"):
                raise RootSecretLeakError(
                    "Credential argument detected in command argv: <redacted>. "
                    "Use private_env EnvironmentFile transport instead."
                )
            if str(arg).startswith("hubt_") or str(arg).startswith("hubr_"):
                raise RootSecretLeakError(
                    "Credential argument detected in command argv: <redacted>. "
                    "Use private_env EnvironmentFile transport instead."
                )

    @classmethod
    def from_payload(
        cls,
        payload: Dict[str, Any],
        task_id: Optional[str] = None,
        workspace: Optional[str] = None,
    ) -> RootJobSpec:
        """Extract and validate RootJobSpec from a raw task payload."""
        jid = str(task_id or payload.get("id") or payload.get("task_id") or "").strip()
        if not jid:
            raise RootAdmissionError("job_id must be provided and non-empty")

        if "host" not in payload or not str(payload.get("host") or "").strip():
            raise RootAdmissionError("host must be provided and non-empty")
        host = str(payload.get("host")).strip()

        sess = str(payload.get("session_id") or payload.get("parent_session") or payload.get("session") or "").strip()
        if not sess:
            raise RootAdmissionError("session_id must be provided and non-empty")

        rid = str(payload.get("run_id") or payload.get("run_tag") or payload.get("tag") or "").strip()
        if not rid:
            raise RootAdmissionError("run_id must be provided and non-empty")

        if "lease_epoch" in payload:
            epoch = payload["lease_epoch"]
        elif "epoch" in payload:
            epoch = payload["epoch"]
        else:
            raise RootAdmissionError("lease_epoch must be provided")

        ws = str(workspace or payload.get("workspace") or payload.get("cwd") or os.getcwd()).strip()

        # Parse timeout and memory (no silent clamping)
        t_raw = payload.get("timeout")
        try:
            t = float(t_raw) if t_raw is not None else ROOT_MAX_TIMEOUT_SEC
        except (ValueError, TypeError):
            raise RootAdmissionError(f"Invalid timeout in payload: {t_raw}")

        m_raw = payload.get("memory_mb")
        try:
            m = int(m_raw) if m_raw is not None else ROOT_MAX_MEMORY_MB
        except (ValueError, TypeError):
            raise RootAdmissionError(f"Invalid memory_mb in payload: {m_raw}")

        # Command argv
        cmd = payload.get("command") or payload.get("command_argv") or []
        if isinstance(cmd, str):
            cmd_tuple = tuple(cmd.split())
        elif isinstance(cmd, (list, tuple)):
            cmd_tuple = tuple(str(c) for c in cmd)
        else:
            cmd_tuple = ()

        # Private env
        penv = payload.get("private_env") or payload.get("extra_env") or {}
        if not isinstance(penv, dict):
            raise RootAdmissionError("private_env must be a key-value dictionary")
        clean_penv = {str(k): str(v) for k, v in penv.items() if k is not None and v is not None}

        # Check goal and prompt for secrets or credential patterns
        goal_text = str(payload.get("goal") or "")
        prompt_text = str(payload.get("prompt") or "")
        combined_text = f"{goal_text} {prompt_text}"

        for k, v in clean_penv.items():
            if v and len(str(v)) >= 4 and str(v) in combined_text:
                raise RootSecretLeakError(
                    f"Secret value for '{k}' detected in task goal/prompt. "
                    "Private credentials must be passed via EnvironmentFile only."
                )

        provider = payload.get("provider")
        model = payload.get("model")

        expires_at = payload.get("expires_at")
        expired = bool(payload.get("expired", False))
        deadline_at = payload.get("deadline_at")
        revoked = bool(payload.get("revoked", False))
        revoked_at = payload.get("revoked_at")

        spec = cls(
            job_id=jid,
            host=host,
            session_id=sess,
            run_id=rid,
            lease_epoch=epoch,
            workspace=ws,
            timeout_sec=t,
            memory_mb=m,
            private_env=clean_penv,
            command_argv=cmd_tuple,
            provider=provider,
            model=model,
            profile=ROOT_PROFILE_NAME,
            expires_at=str(expires_at) if expires_at is not None else None,
            expired=expired,
            deadline_at=str(deadline_at) if deadline_at is not None else None,
            revoked=revoked,
            revoked_at=str(revoked_at) if revoked_at is not None else None,
            extra_metadata={k: v for k, v in payload.items() if k not in (
                "id", "task_id", "host", "session_id", "parent_session", "session",
                "run_id", "run_tag", "tag", "lease_epoch", "epoch", "workspace",
                "cwd", "timeout", "memory_mb", "command", "command_argv",
                "private_env", "extra_env", "provider", "model", "profile",
                "expires_at", "expired", "deadline_at", "revoked", "revoked_at",
            )},
        )
        spec.validate()
        return spec

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "job_id": self.job_id,
            "host": self.host,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lease_epoch": self.lease_epoch,
            "workspace": self.workspace,
            "timeout_sec": self.timeout_sec,
            "memory_mb": self.memory_mb,
            "private_env_keys": sorted(list(self.private_env.keys())),
            "command_argv": list(self.command_argv),
            "provider": self.provider,
            "model": self.model,
            "profile": self.profile,
        }
        if self.expires_at:
            d["expires_at"] = self.expires_at
        if self.expired:
            d["expired"] = self.expired
        if self.deadline_at:
            d["deadline_at"] = self.deadline_at
        if self.revoked:
            d["revoked"] = self.revoked
        if self.revoked_at:
            d["revoked_at"] = self.revoked_at
        return d


class RootReceiver:
    """Abstract interface for pre-action root receiver."""
    is_test_fixture: bool = False

    def validate_pre_action(self, spec: RootJobSpec) -> Tuple[bool, str, Dict[str, Any]]:
        raise NotImplementedError

    def is_operationally_qualified(self) -> bool:
        raise NotImplementedError

    def unsupported_reason(self) -> str:
        return UNSUPPORTED_RECEIVER_REASON


class ProductionRootReceiver(RootReceiver):
    """Production receiver: currently unqualified (fail closed).
    
    No installed receiver is currently qualified to validate pre-action
    boundary tokens or grant native Root execution authority.
    """
    is_test_fixture: bool = False

    def validate_pre_action(self, spec: RootJobSpec) -> Tuple[bool, str, Dict[str, Any]]:
        return False, self.unsupported_reason(), {
            "qualified": False,
            "installed": False,
            "job_id": spec.job_id,
        }

    def is_operationally_qualified(self) -> bool:
        return False

    def unsupported_reason(self) -> str:
        return UNSUPPORTED_RECEIVER_REASON


class IsolatedTestReceiver(RootReceiver):
    """Explicit offline test-only receiver fixture.
    
    CRITICAL: Cannot enable operational support (is_operationally_qualified is permanently False).
    Used solely for isolated positive/negative contract testing in offline suites.
    """
    is_test_fixture: bool = True

    def __init__(
        self,
        expected_job_id: Optional[str] = None,
        expected_host: Optional[str] = None,
        expected_session_id: Optional[str] = None,
        expected_run_id: Optional[str] = None,
        expected_epoch: Optional[Union[int, str]] = None,
    ):
        if not expected_job_id or not expected_host or not expected_session_id or not expected_run_id:
            raise ValueError("IsolatedTestReceiver requires all 5 expected tokens non-empty")
        if expected_epoch is None or isinstance(expected_epoch, bool):
            raise ValueError("IsolatedTestReceiver requires expected_epoch >= 1")
        try:
            ep = int(expected_epoch)
            if ep < 1:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("IsolatedTestReceiver requires positive integer expected_epoch >= 1")

        self.expected_job_id = str(expected_job_id)
        self.expected_host = str(expected_host)
        self.expected_session_id = str(expected_session_id)
        self.expected_run_id = str(expected_run_id)
        self.expected_epoch = ep
        self._consumed_tokens: set[tuple[str, str, str, int]] = set()

    def is_operationally_qualified(self) -> bool:
        return False

    def unsupported_reason(self) -> str:
        return "Isolated test receiver fixture cannot enable operational root execution"

    def validate_pre_action(self, spec: RootJobSpec) -> Tuple[bool, str, Dict[str, Any]]:
        # Check expired or revoked
        if spec.expired or spec.revoked or spec.revoked_at:
            return False, "Token expired or revoked", {
                "test_fixture": True,
                "valid": False,
                "expired_or_revoked": True,
            }

        now_utc = datetime.now(timezone.utc)
        for dt_field in (spec.expires_at, spec.deadline_at):
            if dt_field:
                try:
                    dt = datetime.fromisoformat(str(dt_field).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < now_utc:
                        return False, f"Token expired at {dt_field}", {
                            "test_fixture": True,
                            "valid": False,
                            "expired": True,
                        }
                except Exception:
                    return False, f"Invalid expiry timestamp {dt_field}", {
                        "test_fixture": True,
                        "valid": False,
                        "invalid_expiry": True,
                    }

        # Check stale lease epoch
        try:
            spec_ep = int(spec.lease_epoch)
        except (ValueError, TypeError):
            return False, "Invalid lease_epoch", {"test_fixture": True, "valid": False}

        if spec_ep < self.expected_epoch:
            return False, f"Stale lease epoch {spec_ep} < expected {self.expected_epoch}", {
                "test_fixture": True,
                "valid": False,
                "stale_epoch": True,
            }

        # Check replay
        token_key = (spec.job_id, spec.run_id, spec.session_id, spec_ep)
        if token_key in self._consumed_tokens:
            return False, "Token replay detected: token tuple already consumed", {
                "test_fixture": True,
                "valid": False,
                "replay": True,
            }

        mismatches = []
        if spec.job_id != self.expected_job_id:
            mismatches.append(f"job_id: expected {self.expected_job_id}, got {spec.job_id}")
        if spec.host != self.expected_host:
            mismatches.append(f"host: expected {self.expected_host}, got {spec.host}")
        if spec.session_id != self.expected_session_id:
            mismatches.append(f"session_id: expected {self.expected_session_id}, got {spec.session_id}")
        if spec.run_id != self.expected_run_id:
            mismatches.append(f"run_id: expected {self.expected_run_id}, got {spec.run_id}")
        if spec_ep != self.expected_epoch:
            mismatches.append(f"lease_epoch: expected {self.expected_epoch}, got {spec.lease_epoch}")

        if mismatches:
            return False, f"Token mismatch: {'; '.join(mismatches)}", {
                "test_fixture": True,
                "valid": False,
                "mismatches": mismatches,
            }

        self._consumed_tokens.add(token_key)
        return True, "Pre-action tokens validated by offline test receiver fixture", {
            "test_fixture": True,
            "valid": True,
            "operational_qualified": False,
        }


_ACTIVE_RECEIVER: Optional[RootReceiver] = None


def get_active_receiver() -> RootReceiver:
    global _ACTIVE_RECEIVER
    if _ACTIVE_RECEIVER is not None:
        return _ACTIVE_RECEIVER
    return ProductionRootReceiver()


def set_active_receiver(receiver: Optional[RootReceiver]) -> None:
    global _ACTIVE_RECEIVER
    _ACTIVE_RECEIVER = receiver


def check_root_pre_action(
    spec_or_payload: Union[RootJobSpec, Dict[str, Any]],
    receiver: Optional[RootReceiver] = None,
    allow_test_fixture: bool = False,
    task_id: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Pre-action boundary enforcement.
    
    Actual Root/native effects remain DENIED before claim/start/effect absent
    real trusted receiver validating current runID/session/host/lease epoch at
    pre-action boundary.
    """
    if isinstance(spec_or_payload, RootJobSpec):
        spec = spec_or_payload
    else:
        spec = RootJobSpec.from_payload(spec_or_payload, task_id=task_id, workspace=workspace)

    # NO genuine installed trusted pre-action receiver exists.
    # Operational root qualification MUST remain false/deny.
    if not allow_test_fixture:
        reason = UNSUPPORTED_RECEIVER_REASON
        if receiver is not None and getattr(receiver, "is_test_fixture", False):
            reason = f"{UNSUPPORTED_RECEIVER_REASON}: isolated test receiver fixture cannot enable operational root execution"
        raise RootExecutionDeniedError(
            f"Root execution denied: {reason}"
        )

    # When allow_test_fixture=True (explicit offline test harness only):
    rcv = receiver or get_active_receiver()
    if type(rcv) is not IsolatedTestReceiver:
        raise RootExecutionDeniedError(
            f"Root execution denied: {UNSUPPORTED_RECEIVER_REASON} (non-isolated receiver rejected)"
        )

    valid, msg, details = rcv.validate_pre_action(spec)
    if not valid:
        raise RootExecutionDeniedError(
            f"Root pre-action validation failed under test fixture: {msg}"
        )
    return {
        "denied": False,
        "operational_root_qualified": False,
        "test_fixture": True,
        "reason": msg,
        "details": details,
        "spec": spec.to_dict(),
    }


def generate_root_dependency_proof(
    spec_or_payload: Union[RootJobSpec, Dict[str, Any]],
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce structured dependency proof when native pre-action hook is unavailable.
    
    Preserves explicit unsupported RootJob/preclaim hold.
    """
    spec = None
    if isinstance(spec_or_payload, RootJobSpec):
        spec = spec_or_payload
    else:
        try:
            spec = RootJobSpec.from_payload(spec_or_payload)
        except Exception:
            spec = None

    if spec is not None:
        p_str = str(spec.provider).lower().strip() if spec.provider else ""
        provider_supported = (p_str not in ("codex", "openai")) if p_str else True
        admitted = provider_supported
        timeout_sec = spec.timeout_sec
        memory_mb = spec.memory_mb
        job_id = spec.job_id
        host = spec.host
        session_id = spec.session_id
        run_id = spec.run_id
        lease_epoch = spec.lease_epoch
        private_keys = sorted(list(spec.private_env.keys()))
        prov = spec.provider
    else:
        payload = spec_or_payload if isinstance(spec_or_payload, dict) else {}
        prov = payload.get("provider")
        p_str = str(prov).lower().strip() if prov else ""
        provider_supported = (p_str not in ("codex", "openai")) if p_str else True
        admitted = False
        timeout_sec = payload.get("timeout")
        memory_mb = payload.get("memory_mb")
        job_id = payload.get("id") or payload.get("task_id")
        host = payload.get("host")
        session_id = payload.get("session_id")
        run_id = payload.get("run_id")
        lease_epoch = payload.get("lease_epoch")
        penv = payload.get("private_env") or payload.get("extra_env") or {}
        private_keys = sorted(list(penv.keys())) if isinstance(penv, dict) else []

    proof_reason = reason or UNSUPPORTED_RECEIVER_REASON
    if p_str in ("codex", "openai"):
        proof_reason = f"Provider '{prov}' unsupported for root-run profile"

    return {
        "profile": ROOT_PROFILE_NAME,
        "admitted": admitted,
        "preclaim_hold": True,
        "operational_root_qualified": False,
        "unsupported_reason": proof_reason,
        "validated_bounds": {
            "timeout_sec": timeout_sec,
            "memory_mb": memory_mb,
        },
        "immutable_tokens": {
            "job_id": job_id,
            "host": host,
            "session_id": session_id,
            "run_id": run_id,
            "lease_epoch": lease_epoch,
        },
        "transport": {
            "type": "EnvironmentFile",
            "mode": "0600",
            "private_env_keys": private_keys,
            "argv_secrets_allowed": False,
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def prepare_root_environment_file(
    spec: RootJobSpec,
    tmpdir: Union[str, Path],
    unit_name: str,
) -> Path:
    """Write private per-job environment variables into an atomic 0600 EnvironmentFile."""
    p_tmp = Path(tmpdir).resolve()
    p_tmp.mkdir(parents=True, exist_ok=True)
    env_file = p_tmp / f"{unit_name}.env"

    lines = []
    for k, v in sorted(spec.private_env.items()):
        if k and v is not None:
            if "\n" in str(k) or "\r" in str(k):
                raise RootSecretLeakError("Invalid newline in environment variable key: <redacted>")
            safe_v = str(v).replace("\r", "").replace("\n", "\\n")
            lines.append(f"{k}={safe_v}\n")

    fd = os.open(str(env_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with open(fd, "w", encoding="utf-8", closefd=False) as f:
            f.write("".join(lines))
    finally:
        os.close(fd)
    return env_file
