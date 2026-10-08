import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from launcher.review_receipt import (
    ReviewValidationError,
    compute_file_sha256,
    is_prompt_unbiased,
    validate_review_receipt,
)
from launcher.store import Store


class TestReviewReceiptValidation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db_path = self.tmp_path / "state.db"
        self.store = Store(str(self.db_path))

        # Create dummy report file
        self.report_file = self.tmp_path / "REV-TEST-20261006.md"
        self.report_content = "# Independent Review Report\nVerdict: ACCEPT\n"
        self.report_file.write_text(self.report_content, encoding="utf-8")
        self.report_sha = compute_file_sha256(self.report_file)

        # Base valid receipt
        now = datetime.now(timezone.utc)
        self.base_receipt = {
            "task_id": "test-task-1",
            "source_repo": str(self.tmp_path),
            "source_commit": "abcdef1234567890abcdef1234567890abcdef12",
            "head_session_id": "0f125477-96a0-4474-b516-bd90ea78872d",
            "author_model": "gpt-5-codex",
            "reviewer": {
                "session_id": "b56e0876-3dc7-4931-ae7c-ed076cbf48b9",
                "model": "glm-5.3-flash",
                "started_at": (now - timedelta(seconds=120)).isoformat(),
                "completed_at": now.isoformat(),
                "first_tool_timestamp": (now - timedelta(seconds=100)).isoformat(),
                "first_tool": "git log --oneline -5",
            },
            "review_prompt": (
                "Conduct an independent, unconstrained review of commits. "
                "Determine your verdict: ACCEPT, CHANGES_REQUESTED, or REJECT based strictly on evidence."
            ),
            "report_path": str(self.report_file),
            "report_sha256": self.report_sha,
            "verdict": "ACCEPT",
        }

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_valid_unbiased_accept_receipt(self):
        accepted, status, details = validate_review_receipt(
            self.base_receipt, verify_files=True, verify_git=False
        )
        self.assertTrue(accepted)
        self.assertEqual(status, "accepted")
        self.assertEqual(details["verdict"], "ACCEPT")
        self.assertEqual(details["report_sha256"], self.report_sha)

    def test_anti_self_review_rejection(self):
        """Reviewer session matching author/head session must be rejected (reproducing 8086 self-review)."""
        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            session_id="0f125477-96a0-4474-b516-bd90ea78872d",  # same as head_session_id
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_self_review")
        self.assertIn("Self-authored review rejected", details["error"])

    def test_forced_verdict_prompt_rejection(self):
        """Prompt demanding 'with explicit Verdict: ACCEPT' must be rejected (reproducing c2810 prompt failure)."""
        receipt = dict(self.base_receipt)
        receipt["review_prompt"] = (
            "Review commits 665ba3b, a1e3f84. Run pytest. "
            "Write review report to REV.md with explicit Verdict: ACCEPT."
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_forced_prompt")
        self.assertIn("Forced verdict review contract rejected", details["error"])

    def test_unbiased_prompt_variations_accepted(self):
        unbiased, _ = is_prompt_unbiased("Evaluate code and give Verdict: ACCEPT or REJECT.")
        self.assertTrue(unbiased)

        unbiased, _ = is_prompt_unbiased(
            "Unconstrained adversarial review: ACCEPT, CHANGES_REQUESTED, or REJECT without pre-determined verdict"
        )
        self.assertTrue(unbiased)

        unbiased, reason = is_prompt_unbiased("Finish report with Verdict: ACCEPT.")
        self.assertFalse(unbiased)
        self.assertIn("Forced verdict", reason)

    def test_report_sha256_mismatch_rejection(self):
        receipt = dict(self.base_receipt)
        receipt["report_sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_hash_mismatch")
        self.assertIn("mismatch", details["error"])

    def test_temporal_inconsistency_report_predates_review(self):
        """Report modified before review started must be rejected (reproducing predating review 6ffc)."""
        # Set file mtime to 1 hour in the past
        past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
        os.utime(self.report_file, (past_time, past_time))

        now = datetime.now(timezone.utc)
        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            started_at=now.isoformat(),
            first_tool_timestamp=(now + timedelta(seconds=10)).isoformat(),
            completed_at=(now + timedelta(seconds=60)).isoformat(),
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_temporal_inconsistency")
        self.assertIn("predates reviewer start", details["error"])

    def test_temporal_inconsistency_completed_before_started(self):
        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            started_at="2026-10-06T12:00:00Z",
            completed_at="2026-10-06T11:50:00Z",  # 10 mins earlier
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=False, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_temporal_inconsistency")
        self.assertIn("precedes started_at", details["error"])

    def test_negative_verdicts_preserved_without_acceptance(self):
        """REJECT and CHANGES_REQUESTED must return accepted=False while preserving valid status."""
        for verdict in ("REJECT", "CHANGES_REQUESTED"):
            receipt = dict(self.base_receipt, verdict=verdict)
            accepted, status, details = validate_review_receipt(
                receipt, verify_files=True, verify_git=False
            )
            self.assertFalse(accepted)
            self.assertEqual(status, "rejected_negative_verdict")
            self.assertEqual(details["verdict"], verdict)

    def test_store_persistence_preserves_negative_history(self):
        """Store must preserve all review attempts (negative and positive) without silent erasure."""
        payload = {"goal": "test", "owner": "test-owner", "cwd": ".", "timeout": 600}
        self.store.submit_task("task-audit-1", "key-audit-1", payload, [])

        # 1. Record self-review failure
        r1 = dict(self.base_receipt, task_id="task-audit-1")
        r1["reviewer"] = dict(r1["reviewer"], session_id=r1["head_session_id"])
        _, status1, d1 = validate_review_receipt(r1, verify_files=False, verify_git=False)
        self.store.add_review_receipt(r1, status1, d1)

        # 2. Record forced prompt failure
        r2 = dict(self.base_receipt, task_id="task-audit-1")
        r2["review_prompt"] = "Finish with explicit Verdict: ACCEPT"
        _, status2, d2 = validate_review_receipt(r2, verify_files=False, verify_git=False)
        self.store.add_review_receipt(r2, status2, d2)

        # 3. Record valid acceptance
        r3 = dict(self.base_receipt, task_id="task-audit-1")
        _, status3, d3 = validate_review_receipt(r3, verify_files=True, verify_git=False)
        self.store.add_review_receipt(r3, status3, d3)

        # Verify all 3 are stored in history
        history = self.store.list_review_receipts("task-audit-1")
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["status"], "rejected_self_review")
        self.assertEqual(history[1]["status"], "rejected_forced_prompt")
        self.assertEqual(history[2]["status"], "accepted")

    def test_temporal_inconsistency_report_postdates_reviewer_completion(self):
        """Report modified after review completed must be rejected (detecting post-completion tampering)."""
        now = datetime.now(timezone.utc)
        future_time = (now + timedelta(seconds=60)).timestamp()
        os.utime(self.report_file, (future_time, future_time))

        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            started_at=(now - timedelta(seconds=120)).isoformat(),
            completed_at=(now - timedelta(seconds=10)).isoformat(),
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_temporal_inconsistency")
        self.assertIn("postdates reviewer completion", details["error"])

    def test_unknown_task_in_store_rejection(self):
        """When store is provided, unknown task_id must be rejected."""
        receipt = dict(self.base_receipt, task_id="nonexistent-task-uuid")
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, store=self.store
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_unknown_task")
        self.assertIn("not found in store", details["error"])

    def test_runtime_witness_verification(self):
        """Runtime witness requirement must verify systemd or aplexer traces."""
        # Nonexistent unit and session must fail when require_witness=True
        fake_receipt = dict(self.base_receipt)
        fake_receipt["reviewer"] = dict(
            fake_receipt["reviewer"],
            unit_name=f"agent-task-{fake_receipt['task_id']}.service",
            session_id="00000000-0000-0000-0000-000000000000",
        )
        accepted, status, details = validate_review_receipt(
            fake_receipt, verify_files=True, verify_git=False, require_witness=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_runtime_witness")

    def test_reject_systemd_argument_injection(self):
        """Reject argument injection like '--help'."""
        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            unit_name="--help",
            session_id="00000000-0000-0000-0000-000000000000",
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, require_witness=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_runtime_witness")
        self.assertIn("does not match allowed pattern", details["witness_details"]["error"])

    def test_reject_foreign_service_unit(self):
        """Reject foreign service units like 'dbus.service'."""
        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            unit_name="dbus.service",
            session_id="00000000-0000-0000-0000-000000000000",
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, require_witness=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_runtime_witness")
        self.assertIn("does not match allowed pattern", details["witness_details"]["error"])

    def test_reject_mismatched_task_unit_name(self):
        """Reject unit name that does not match task_id."""
        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            unit_name="agent-task-different-task.service",
            session_id="00000000-0000-0000-0000-000000000000",
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, require_witness=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_runtime_witness")
        self.assertIn("does not match expected", details["witness_details"]["error"])

    def test_reject_mismatched_invocation_id(self):
        """Reject if InvocationID does not match expected."""
        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            unit_name=f"agent-task-{receipt['task_id']}.service",
            session_id="00000000-0000-0000-0000-000000000000",
            invocation_id="fake-invocation-id",
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, require_witness=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_runtime_witness")

    def test_cli_load_receipt_arg_long_inline_json(self):
        """load_receipt_arg must safely parse inline JSON strings > 255 chars without OSError."""
        from launcher.cli import load_receipt_arg
        long_receipt = dict(self.base_receipt)
        long_receipt["review_prompt"] = "A" * 500  # length > 500 chars
        raw_json = json.dumps(long_receipt)
        self.assertGreater(len(raw_json), 255)
        parsed = load_receipt_arg(raw_json)
        self.assertEqual(parsed["task_id"], "test-task-1")
        self.assertEqual(len(parsed["review_prompt"]), 500)

    def test_same_model_review_rejection(self):
        """Receipt with matching author_model and reviewer.model must be rejected."""
        receipt = dict(self.base_receipt)
        receipt["author_model"] = "gemini-3.8-flash-high"
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            model="gemini-3.8-flash-high",
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_same_model_review")
        self.assertEqual(
            details["error"],
            "Same-model review rejected: reviewer model matches author model",
        )

        # Case-insensitive and whitespace-stripped match
        receipt_case = dict(self.base_receipt)
        receipt_case["author_model"] = "  Gemini-3.8-Flash-High  "
        receipt_case["reviewer"] = dict(
            receipt_case["reviewer"],
            model="gemini-3.8-flash-high",
        )
        accepted, status, details = validate_review_receipt(
            receipt_case, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_same_model_review")

    def test_different_model_review_accepted(self):
        """Receipt with different models (e.g. gemini-3.8-flash-high vs gemini-3.1-pro-high) is accepted."""
        receipt = dict(self.base_receipt)
        receipt["author_model"] = "gemini-3.8-flash-high"
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            model="gemini-3.1-pro-high",
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False
        )
        self.assertTrue(accepted)
        self.assertEqual(status, "accepted")
        self.assertEqual(details["verdict"], "ACCEPT")

    def test_terminal_task_replay_rejection(self):
        """Submitting a review receipt for a task already in 'accepted' or 'failed' state in store is rejected."""
        payload = {"goal": "test", "owner": "test-owner", "cwd": ".", "timeout": 600}

        # 1. Terminal state 'accepted'
        task_id_accepted = "task-terminal-accepted"
        self.store.submit_task(task_id_accepted, "key-term-accepted", payload, [])
        self.store.transition_task(task_id_accepted, "completed-awaiting-review", ("queued",))
        self.store.accept_task(task_id_accepted, reviewer="rev-accept-1")

        receipt_acc = dict(self.base_receipt, task_id=task_id_accepted)
        accepted, status, details = validate_review_receipt(
            receipt_acc, verify_files=True, verify_git=False, store=self.store
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_terminal_replay")
        self.assertEqual(
            details["error"],
            f"Task {task_id_accepted} is already in terminal state 'accepted'; review replay prohibited",
        )

        # 2. Terminal state 'failed'
        task_id_failed = "task-terminal-failed"
        self.store.submit_task(task_id_failed, "key-term-failed", payload, [])
        self.store.transition_task(task_id_failed, "failed", ("queued",), reason="native death")

        receipt_fail = dict(self.base_receipt, task_id=task_id_failed)
        accepted, status, details = validate_review_receipt(
            receipt_fail, verify_files=True, verify_git=False, store=self.store
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_terminal_replay")
        self.assertEqual(
            details["error"],
            f"Task {task_id_failed} is already in terminal state 'failed'; review replay prohibited",
        )

        # 3. Terminal state 'rejected'
        task_id_rejected = "task-terminal-rejected"
        self.store.submit_task(task_id_rejected, "key-term-rejected", payload, [])
        self.store.transition_task(task_id_rejected, "completed-awaiting-review", ("queued",))
        self.store.reject_task(task_id_rejected, reviewer="rev-reject-1", reason="verdict reject")

        receipt_rej = dict(self.base_receipt, task_id=task_id_rejected)
        accepted, status, details = validate_review_receipt(
            receipt_rej, verify_files=True, verify_git=False, store=self.store
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_terminal_replay")
        self.assertEqual(
            details["error"],
            f"Task {task_id_rejected} is already in terminal state 'rejected'; review replay prohibited",
        )

    def test_missing_author_model_rejection(self):
        """Receipt without author_model or with empty string must be rejected when require_model_provenance=True."""
        # 1. Missing author_model key
        receipt_missing = dict(self.base_receipt)
        receipt_missing.pop("author_model", None)
        accepted, status, details = validate_review_receipt(
            receipt_missing, verify_files=True, verify_git=False, require_model_provenance=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_model_provenance")
        self.assertEqual(
            details["error"],
            "Author model provenance required: author_model must be a non-empty string",
        )

        # 2. None author_model
        receipt_none = dict(self.base_receipt, author_model=None)
        accepted, status, details = validate_review_receipt(
            receipt_none, verify_files=True, verify_git=False, require_model_provenance=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_model_provenance")
        self.assertEqual(
            details["error"],
            "Author model provenance required: author_model must be a non-empty string",
        )

        # 3. Empty string author_model
        receipt_empty = dict(self.base_receipt, author_model="")
        accepted, status, details = validate_review_receipt(
            receipt_empty, verify_files=True, verify_git=False, require_model_provenance=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_model_provenance")
        self.assertEqual(
            details["error"],
            "Author model provenance required: author_model must be a non-empty string",
        )

        # 4. Whitespace-only author_model
        receipt_ws = dict(self.base_receipt, author_model="   ")
        accepted, status, details = validate_review_receipt(
            receipt_ws, verify_files=True, verify_git=False, require_model_provenance=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_model_provenance")
        self.assertEqual(
            details["error"],
            "Author model provenance required: author_model must be a non-empty string",
        )

    def test_missing_reviewer_model_rejection(self):
        """Receipt without reviewer.model must be rejected with rejected_missing_model_provenance."""
        # 1. Missing model key in reviewer
        receipt_missing = dict(self.base_receipt)
        receipt_missing["reviewer"] = dict(receipt_missing["reviewer"])
        receipt_missing["reviewer"].pop("model", None)
        accepted, status, details = validate_review_receipt(
            receipt_missing, verify_files=True, verify_git=False, require_model_provenance=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_model_provenance")
        self.assertEqual(
            details["error"],
            "Reviewer model provenance required: reviewer.model must be a non-empty string",
        )

        # 2. None model in reviewer
        receipt_none = dict(self.base_receipt)
        receipt_none["reviewer"] = dict(receipt_none["reviewer"], model=None)
        accepted, status, details = validate_review_receipt(
            receipt_none, verify_files=True, verify_git=False, require_model_provenance=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_model_provenance")
        self.assertEqual(
            details["error"],
            "Reviewer model provenance required: reviewer.model must be a non-empty string",
        )

        # 3. Empty string model in reviewer
        receipt_empty = dict(self.base_receipt)
        receipt_empty["reviewer"] = dict(receipt_empty["reviewer"], model="")
        accepted, status, details = validate_review_receipt(
            receipt_empty, verify_files=True, verify_git=False, require_model_provenance=True
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_model_provenance")
        self.assertEqual(
            details["error"],
            "Reviewer model provenance required: reviewer.model must be a non-empty string",
        )

    def test_missing_first_tool_timestamp_rejection(self):
        """Receipt without first_tool_timestamp is rejected with status missing_first_tool_timestamp."""
        # 1. Missing first_tool_timestamp key
        receipt_missing = dict(self.base_receipt)
        receipt_missing["reviewer"] = dict(receipt_missing["reviewer"])
        receipt_missing["reviewer"].pop("first_tool_timestamp", None)
        accepted, status, details = validate_review_receipt(
            receipt_missing, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "missing_first_tool_timestamp")
        self.assertEqual(
            details["error"],
            "reviewer first_tool_timestamp is required for temporal qualification",
        )

        # 2. None first_tool_timestamp
        receipt_none = dict(self.base_receipt)
        receipt_none["reviewer"] = dict(receipt_none["reviewer"], first_tool_timestamp=None)
        accepted, status, details = validate_review_receipt(
            receipt_none, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "missing_first_tool_timestamp")
        self.assertEqual(
            details["error"],
            "reviewer first_tool_timestamp is required for temporal qualification",
        )

        # 3. Empty string first_tool_timestamp
        receipt_empty = dict(self.base_receipt)
        receipt_empty["reviewer"] = dict(receipt_empty["reviewer"], first_tool_timestamp="")
        accepted, status, details = validate_review_receipt(
            receipt_empty, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "missing_first_tool_timestamp")
        self.assertEqual(
            details["error"],
            "reviewer first_tool_timestamp is required for temporal qualification",
        )

        # 4. Invalid ISO timestamp format
        receipt_invalid = dict(self.base_receipt)
        receipt_invalid["reviewer"] = dict(receipt_invalid["reviewer"], first_tool_timestamp="not-a-valid-timestamp")
        accepted, status, details = validate_review_receipt(
            receipt_invalid, verify_files=True, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "invalid_first_tool_timestamp")
        self.assertIn("is not a valid ISO timestamp", details["error"])

    def test_author_model_unknown_rejected_when_not_allowed(self):
        """Receipt with author_model='unknown' must be rejected when allow_unknown_author=False."""
        receipt = dict(self.base_receipt, author_model="unknown")
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, require_model_provenance=True, allow_unknown_author=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_missing_model_provenance")
        self.assertEqual(
            details["error"],
            "Author model provenance required: 'unknown' author prohibited unless allow_unknown_author=True",
        )

        # Also test default allow_unknown_author=False
        accepted_default, status_default, details_default = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, require_model_provenance=True
        )
        self.assertFalse(accepted_default)
        self.assertEqual(status_default, "rejected_missing_model_provenance")
        self.assertEqual(
            details_default["error"],
            "Author model provenance required: 'unknown' author prohibited unless allow_unknown_author=True",
        )

    def test_archival_receipt_with_unknown_author_allowed(self):
        """Archival receipt with author_model='unknown' is allowed when allow_unknown_author=True or require_model_provenance=False."""
        receipt = dict(self.base_receipt, author_model="unknown")
        # Allowed when require_model_provenance=False
        accepted_legacy, status_legacy, details_legacy = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, require_model_provenance=False
        )
        self.assertTrue(accepted_legacy)
        self.assertEqual(status_legacy, "accepted")
        self.assertEqual(details_legacy["verdict"], "ACCEPT")

        # Also accepted when allow_unknown_author=True (with require_model_provenance=True)
        accepted_archival, status_archival, details_archival = validate_review_receipt(
            receipt, verify_files=True, verify_git=False, require_model_provenance=True, allow_unknown_author=True
        )
        self.assertTrue(accepted_archival)
        self.assertEqual(status_archival, "accepted")
        self.assertEqual(details_archival["verdict"], "ACCEPT")

    def test_temporal_inconsistency_first_tool_postdates_completed(self):
        """Receipt where first_tool_timestamp postdates completed_at must be rejected."""
        now = datetime.now(timezone.utc)
        receipt = dict(self.base_receipt)
        receipt["reviewer"] = dict(
            receipt["reviewer"],
            started_at=(now - timedelta(seconds=120)).isoformat(),
            completed_at=(now - timedelta(seconds=60)).isoformat(),
            first_tool_timestamp=now.isoformat(),
        )
        accepted, status, details = validate_review_receipt(
            receipt, verify_files=False, verify_git=False
        )
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected_temporal_inconsistency")
        self.assertIn("postdates completed_at", details["error"])

    def test_report_path_collision_rejected_across_rounds(self):
        """Submit round 1 receipt to store, then validate round 2 receipt with same report_path but different report_sha256. Verify validate_review_receipt returns status 'rejected_report_path_collision'."""
        payload = {"goal": "test", "owner": "test-owner", "cwd": ".", "timeout": 600}
        self.store.submit_task("test-task-1", "key-task-col-round1", payload, [])

        # Submit round 1 receipt
        r1 = dict(self.base_receipt)
        r1["verdict"] = "CHANGES_REQUESTED"
        accepted1, status1, details1 = validate_review_receipt(r1, verify_files=True, verify_git=False)
        self.assertFalse(accepted1)
        self.store.add_review_receipt(r1, status1, details1)

        # Validate round 2 receipt with same report_path but different report_sha256
        r2 = dict(self.base_receipt)
        r2["report_sha256"] = "1111111111111111111111111111111111111111111111111111111111111111"
        accepted2, status2, details2 = validate_review_receipt(
            r2, verify_files=False, verify_git=False, store=self.store
        )
        self.assertFalse(accepted2)
        self.assertEqual(status2, "rejected_report_path_collision")
        self.assertIn("already recorded with different hash", details2["error"])
        self.assertEqual(details2["report_path"], str(self.report_file))

        # Also verify with file modified on disk (verify_files=True)
        self.report_file.write_text("# Round 2 modified\nVerdict: ACCEPT\n", encoding="utf-8")
        new_sha = compute_file_sha256(self.report_file)
        r2["report_sha256"] = new_sha
        accepted2_vf, status2_vf, details2_vf = validate_review_receipt(
            r2, verify_files=True, verify_git=False, store=self.store
        )
        self.assertFalse(accepted2_vf)
        self.assertEqual(status2_vf, "rejected_report_path_collision")

    def test_unique_report_path_accepted_across_rounds(self):
        """Submit round 1 receipt, then validate round 2 receipt with distinct unique report_path (e.g. REV-TEST-ROUND02.md). Verify it is accepted."""
        payload = {"goal": "test", "owner": "test-owner", "cwd": ".", "timeout": 600}
        self.store.submit_task("test-task-1", "key-task-unique-round2", payload, [])

        # Submit round 1 receipt
        r1 = dict(self.base_receipt)
        r1["verdict"] = "CHANGES_REQUESTED"
        accepted1, status1, details1 = validate_review_receipt(r1, verify_files=True, verify_git=False)
        self.assertFalse(accepted1)
        self.store.add_review_receipt(r1, status1, details1)

        # Round 2 receipt with distinct unique report_path
        round2_file = self.tmp_path / "REV-TEST-ROUND02.md"
        round2_content = "# Independent Review Report - Round 2\nVerdict: ACCEPT\n"
        round2_file.write_text(round2_content, encoding="utf-8")
        round2_sha = compute_file_sha256(round2_file)

        now = datetime.now(timezone.utc)
        r2 = dict(self.base_receipt)
        r2["report_path"] = str(round2_file)
        r2["report_sha256"] = round2_sha
        r2["verdict"] = "ACCEPT"
        r2["reviewer"] = dict(
            self.base_receipt["reviewer"],
            session_id="c67f0987-4ed8-5042-bf8d-fe187dcf59ca",
            started_at=(now - timedelta(seconds=120)).isoformat(),
            completed_at=now.isoformat(),
            first_tool_timestamp=(now - timedelta(seconds=100)).isoformat(),
        )

        accepted2, status2, details2 = validate_review_receipt(
            r2, verify_files=True, verify_git=False, store=self.store
        )
        self.assertTrue(accepted2)
        self.assertEqual(status2, "accepted")
        self.assertEqual(details2["verdict"], "ACCEPT")
        self.assertEqual(details2["report_sha256"], round2_sha)

    def test_add_review_receipt_raises_on_path_collision(self):
        """Verify store.add_review_receipt raises ReviewValidationError when inserting a conflicting hash for an existing report_path."""
        payload = {"goal": "test", "owner": "test-owner", "cwd": ".", "timeout": 600}
        self.store.submit_task("test-task-1", "key-task-path-col", payload, [])

        r1 = dict(self.base_receipt)
        self.store.add_review_receipt(r1, "accepted", {"verdict": "ACCEPT"})

        # Attempt to insert receipt with same report_path but conflicting hash
        r2 = dict(self.base_receipt)
        r2["report_sha256"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        with self.assertRaises(ReviewValidationError) as ctx:
            self.store.add_review_receipt(r2, "accepted", {"verdict": "ACCEPT"})

        self.assertEqual(ctx.exception.status, "rejected_report_path_collision")
        self.assertIn("Report path collision", str(ctx.exception))
        self.assertIn(str(self.report_file), str(ctx.exception))

    def test_relocate_historical_report_pointer(self):
        """Verify store.relocate_historical_report_pointer safely updates report_path when SHA matches, and raises on SHA mismatch or missing file."""
        payload = {"goal": "test", "owner": "test-owner", "cwd": ".", "timeout": 600}
        self.store.submit_task("test-task-1", "key-task-relocate", payload, [])

        r1 = dict(self.base_receipt)
        self.store.add_review_receipt(r1, "accepted", {"verdict": "ACCEPT"})
        receipts = self.store.list_review_receipts("test-task-1")
        self.assertEqual(len(receipts), 1)
        receipt_id = receipts[0]["id"]

        # 1. Target file does not exist
        non_existent = str(self.tmp_path / "non_existent.md")
        with self.assertRaises(ValueError) as ctx:
            self.store.relocate_historical_report_pointer(receipt_id, non_existent, self.report_sha)
        self.assertIn("does not exist or is a symlink", str(ctx.exception))

        # 2. Target file is a symlink
        symlink_path = self.tmp_path / "symlink_report.md"
        symlink_path.symlink_to(self.report_file)
        with self.assertRaises(ValueError) as ctx:
            self.store.relocate_historical_report_pointer(receipt_id, str(symlink_path), self.report_sha)
        self.assertIn("does not exist or is a symlink", str(ctx.exception))

        # 3. SHA mismatch between file and expected_sha
        mismatched_file = self.tmp_path / "mismatched.md"
        mismatched_file.write_text("different content", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            self.store.relocate_historical_report_pointer(receipt_id, str(mismatched_file), self.report_sha)
        self.assertIn("SHA mismatch for relocated report", str(ctx.exception))

        # 4. SHA mismatch between expected_sha and recorded SHA in receipt
        wrong_expected_sha = compute_file_sha256(mismatched_file)
        with self.assertRaises(ValueError) as ctx:
            self.store.relocate_historical_report_pointer(receipt_id, str(mismatched_file), wrong_expected_sha)
        self.assertIn("does not match expected", str(ctx.exception))

        # 5. Non-existent receipt_id
        target_file = self.tmp_path / "frozen_report.md"
        target_file.write_text(self.report_content, encoding="utf-8")  # same content as report_file
        with self.assertRaises(ValueError) as ctx:
            self.store.relocate_historical_report_pointer(999999, str(target_file), self.report_sha)
        self.assertIn("Review receipt #999999 not found", str(ctx.exception))

        # 6. Successful relocation
        res = self.store.relocate_historical_report_pointer(receipt_id, str(target_file), self.report_sha)
        self.assertTrue(res)

        # Verify update in DB
        updated_receipts = self.store.list_review_receipts("test-task-1")
        self.assertEqual(len(updated_receipts), 1)
        self.assertEqual(updated_receipts[0]["report_path"], str(target_file))

    def test_symlink_report_file_rejected(self):
        """Verify validate_review_receipt rejects report file if it is a symlink when verify_files is True."""
        symlink_report = self.tmp_path / "symlink_report_test.md"
        symlink_report.symlink_to(self.report_file)

        receipt = dict(self.base_receipt, report_path=str(symlink_report))
        accepted, status, details = validate_review_receipt(receipt, verify_files=True, verify_git=False)
        self.assertFalse(accepted)
        self.assertEqual(status, "missing_report_file")
        self.assertIn("is a symlink", details["error"])

    def test_list_review_receipts_handles_non_json_plain_text_details(self):
        """Verify list_review_receipts safely handles legacy/corrupted non-JSON plain text details."""
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_receipts (
                    task_id, source_commit, reviewer_session, reviewer_model,
                    head_session, verdict, status, details, report_path, report_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-task-plain-text",
                    "c0ffee1234567890abcdef1234567890abcdef12",
                    "legacy-session-1",
                    "legacy-model-1",
                    "head-session-1",
                    "ACCEPT",
                    "accepted",
                    "Historical audit note: manually verified report without JSON format",
                    str(self.report_file),
                    self.report_sha,
                ),
            )

        # By task_id
        receipts = self.store.list_review_receipts("test-task-plain-text")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["task_id"], "test-task-plain-text")
        self.assertEqual(
            receipts[0]["details"],
            {"raw": "Historical audit note: manually verified report without JSON format"},
        )

        # All receipts
        all_receipts = self.store.list_review_receipts()
        matched = [r for r in all_receipts if r["task_id"] == "test-task-plain-text"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(
            matched[0]["details"],
            {"raw": "Historical audit note: manually verified report without JSON format"},
        )


if __name__ == "__main__":
    unittest.main()

