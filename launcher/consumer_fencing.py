"""Consumer fencing integration for agent-quota-launcher (C2905, C2911, C2918).

Provides guarded task admission, epoch verification, and credential protection
between agent-coordination RoleAuthority and agent-quota-launcher Store.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

# Ensure agent-coordination is importable if on host
COORD_REPO = Path("/home/alexey/git/agent-coordination")
if COORD_REPO.is_dir() and str(COORD_REPO) not in sys.path:
    sys.path.insert(0, str(COORD_REPO))

try:
    from coordination.ql_consumer_fencing import (
        QLConsumerFencing,
        Fenced,
        reject_head_cred_inheritance,
    )
    from coordination.role_failover import RoleAuthority
    HAVE_COORD_FENCING = True
except ImportError:
    HAVE_COORD_FENCING = False
    QLConsumerFencing = None
    RoleAuthority = None
    Fenced = type("Fenced", (Exception,), {})  # type: ignore

    def reject_head_cred_inheritance(payload: dict, *, reject: bool = True) -> dict:  # type: ignore
        return payload


class LauncherConsumerFencing:
    """Bridge between agent-coordination QLConsumerFencing and launcher Store."""

    def __init__(self, authority: Optional[Any] = None) -> None:
        self.authority = authority
        self._fencing = (
            QLConsumerFencing(authority)
            if (HAVE_COORD_FENCING and authority is not None and QLConsumerFencing is not None)
            else None
        )

    @property
    def is_available(self) -> bool:
        return self._fencing is not None

    def admit_and_submit_task(
        self,
        store: Any,
        project: str,
        role: str,
        actor: str,
        generation: str,
        epoch: int,
        task_id: str,
        payload: Dict[str, Any],
        paths: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Admit task under verified role epoch and submit to launcher Store.

        Enforces:
        1. Role epoch authorization against RoleAuthority (raises Fenced if stale or unauthorized).
        2. Head credential material rejection/stripping.
        3. Control-plane idempotent submission with key f'ql-task:{project}:{role}:{epoch}:{task_id}'.
        4. Durable submission into launcher Store tasks and task_paths tables.
        """
        paths = paths or []
        if not self._fencing:
            raise RuntimeError("Coordination fencing authority not available")

        def _store_submit(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
            p_copy = dict(p)
            p_copy["coordination_fencing"] = {
                "project": project,
                "role": role,
                "actor": actor,
                "generation": generation,
                "epoch": epoch,
                "idempotency_key": key,
            }
            try:
                store.submit_task(
                    task_id=task_id,
                    idempotency_key=key,
                    payload=p_copy,
                    paths=paths,
                )
                return {
                    "status": "submitted",
                    "task_id": task_id,
                    "key": key,
                    "state": "queued",
                }
            except Exception as e:
                if "UNIQUE constraint failed" in str(e):
                    return {
                        "status": "already_submitted",
                        "task_id": task_id,
                        "key": key,
                    }
                raise

        return self._fencing.admit_and_enqueue_task(
            project=project,
            role=role,
            actor=actor,
            generation=generation,
            epoch=epoch,
            task_id=task_id,
            payload=payload,
            launcher_submit_fn=_store_submit,
        )

    def validate_runtime_epoch(
        self,
        project: str,
        role: str,
        actor: str,
        generation: str,
        expected_epoch: int,
    ) -> Tuple[bool, str]:
        """Verify active runtime epoch before dispatch or execution."""
        if not self._fencing:
            return False, "fencing authority not configured"
        is_valid = self._fencing.validate_consumer_epoch(
            project=project,
            role=role,
            actor=actor,
            generation=generation,
            expected_epoch=expected_epoch,
        )
        if not is_valid:
            return False, f"fenced: current holder or epoch does not match expected ({expected_epoch})"
        return True, "valid_epoch"
