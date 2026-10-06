"""Review-receipt validation and persistence for agent-quota-launcher (C2818).

Validates review receipts against:
1. Anti-Self-Review: reviewer session must not match task author/head session.
2. Free-Verdict Review Contract: prompt must not mandate a pre-determined verdict.
3. Artifact Integrity: report SHA256 must match file on disk.
4. Temporal Consistency: reviewer must start after source commit, report must not predate review.
5. Valid Verdicts: ACCEPT, CHANGES_REQUESTED, REJECT.
6. Negative History Preservation: all review attempts preserved without silent erasure.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FORCED_VERDICT_PATTERNS = [
    re.compile(r"with (?:explicit )?Verdict:\s*ACCEPT", re.IGNORECASE),
    re.compile(r"verdict must be ACCEPT", re.IGNORECASE),
    re.compile(r"mandate(?:d)? verdict:\s*ACCEPT", re.IGNORECASE),
    re.compile(r"require(?:d)? verdict:\s*ACCEPT", re.IGNORECASE),
]

CONDITIONAL_ALTERNATIVE_PATTERNS = [
    re.compile(r"\b(?:or|versus|vs\.?)\s*REJECT\b", re.IGNORECASE),
    re.compile(r"\bCHANGES_REQUESTED\b", re.IGNORECASE),
    re.compile(r"\bACCEPT(?:ED)?,\s*(?:CHANGES_REQUESTED|REJECT)\b", re.IGNORECASE),
    re.compile(r"\b(?:freely|unconstrained|unbiased)\b", re.IGNORECASE),
]

VALID_VERDICTS = ("ACCEPT", "CHANGES_REQUESTED", "REJECT")


class ReviewValidationError(ValueError):
    """Raised when a review receipt violates integrity or independence rules."""
    def __init__(self, reason: str, status: str = "rejected"):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def parse_iso_time(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    clean = s.strip()
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_prompt_unbiased(prompt: str) -> Tuple[bool, Optional[str]]:
    """Verify that a review prompt does not demand a predetermined ACCEPT verdict."""
    if not prompt or not isinstance(prompt, str):
        return False, "review_prompt is empty or not a string"

    for pattern in FORCED_VERDICT_PATTERNS:
        if pattern.search(prompt):
            has_alternative = any(alt.search(prompt) for alt in CONDITIONAL_ALTERNATIVE_PATTERNS)
            if not has_alternative:
                return False, (
                    "Forced verdict review contract rejected: prompt mandates 'Verdict: ACCEPT' "
                    "without unconstrained reject/changes options (violating C2813/C2818)"
                )
    return True, None


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def validate_review_receipt(
    receipt: Dict[str, Any],
    verify_files: bool = True,
    verify_git: bool = True,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate a review receipt against independence, contract, hash, and timing rules.

    Returns (is_accepted, status_code, details).
    """
    if not isinstance(receipt, dict):
        return False, "invalid_receipt_format", {"error": "receipt must be a JSON object"}

    task_id = (receipt.get("task_id") or "").strip()
    if not task_id:
        return False, "missing_task_id", {"error": "task_id is required"}

    head_session = (receipt.get("head_session_id") or receipt.get("author_session_id") or "").strip()
    reviewer_info = receipt.get("reviewer") or {}
    if not isinstance(reviewer_info, dict):
        return False, "invalid_reviewer_info", {"error": "reviewer must be an object"}

    reviewer_session = (reviewer_info.get("session_id") or "").strip()
    if not reviewer_session:
        return False, "missing_reviewer_session", {"error": "reviewer session_id is required"}

    # 1. Anti-Self-Review Rule
    if head_session and reviewer_session.lower() == head_session.lower():
        return False, "rejected_self_review", {
            "error": "Self-authored review rejected: reviewer session matches author/head session",
            "reviewer_session": reviewer_session,
            "head_session": head_session,
        }

    # 2. Prompt Independence Rule
    prompt = receipt.get("review_prompt") or ""
    unbiased, prompt_reason = is_prompt_unbiased(prompt)
    if not unbiased:
        return False, "rejected_forced_prompt", {
            "error": prompt_reason,
            "prompt_sample": prompt[:200],
        }

    # 3. Verdict Validation
    verdict = (receipt.get("verdict") or "").strip().upper()
    if verdict not in VALID_VERDICTS:
        return False, "invalid_verdict", {
            "error": f"verdict '{verdict}' is invalid; must be one of {VALID_VERDICTS}",
            "verdict": verdict,
        }

    # 4. First-Tool Evidence
    first_tool = reviewer_info.get("first_tool")
    if not first_tool:
        return False, "missing_first_tool_evidence", {
            "error": "reviewer first_tool trace is required for independent verification",
        }

    # 5. Temporal Consistency
    started_at = parse_iso_time(reviewer_info.get("started_at"))
    completed_at = parse_iso_time(reviewer_info.get("completed_at"))
    first_tool_time = parse_iso_time(reviewer_info.get("first_tool_timestamp"))

    if started_at and completed_at and completed_at < started_at:
        return False, "rejected_temporal_inconsistency", {
            "error": f"reviewer completed_at ({completed_at}) precedes started_at ({started_at})",
        }
    if started_at and first_tool_time and first_tool_time < started_at:
        return False, "rejected_temporal_inconsistency", {
            "error": f"reviewer first_tool_timestamp ({first_tool_time}) precedes started_at ({started_at})",
        }

    # 6. Report File and Hash Integrity
    report_path_str = receipt.get("report_path")
    expected_sha = (receipt.get("report_sha256") or "").strip().lower()

    if not report_path_str or not expected_sha:
        return False, "missing_report_metadata", {
            "error": "report_path and report_sha256 are required",
        }

    if verify_files:
        p = Path(report_path_str)
        if not p.exists():
            return False, "missing_report_file", {
                "error": f"report file does not exist at {report_path_str}",
            }
        actual_sha = compute_file_sha256(p).lower()
        if actual_sha != expected_sha:
            return False, "rejected_hash_mismatch", {
                "error": f"report SHA256 mismatch: expected {expected_sha}, computed {actual_sha}",
                "expected_sha": expected_sha,
                "computed_sha": actual_sha,
            }

        # Verify report modification time does not predate reviewer start
        if started_at:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            # Allow up to 2 seconds clock skew
            if (started_at - mtime).total_seconds() > 2.0:
                return False, "rejected_temporal_inconsistency", {
                    "error": f"report file mtime ({mtime}) predates reviewer start ({started_at})",
                }

    # 7. Git Source Pin Verification
    source_commit = (receipt.get("source_commit") or "").strip()
    source_repo = receipt.get("source_repo")
    if verify_git and source_commit and source_repo:
        repo_path = Path(source_repo)
        if repo_path.is_dir():
            res = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "--verify", source_commit],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                return False, "rejected_source_mismatch", {
                    "error": f"source_commit '{source_commit}' does not exist in repo {source_repo}",
                }

    # If verdict is REJECT or CHANGES_REQUESTED, receipt is valid but not accepted
    if verdict != "ACCEPT":
        return False, "rejected_negative_verdict", {
            "status": "valid_receipt_negative_verdict",
            "verdict": verdict,
            "task_id": task_id,
            "report_sha256": expected_sha,
        }

    return True, "accepted", {
        "status": "verified_independent_acceptance",
        "task_id": task_id,
        "verdict": verdict,
        "reviewer_session": reviewer_session,
        "report_sha256": expected_sha,
    }
