import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from launcher.review_receipt import (
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


if __name__ == "__main__":
    unittest.main()
