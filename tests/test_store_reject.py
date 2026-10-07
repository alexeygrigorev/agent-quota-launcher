import unittest
import tempfile
import os
from pathlib import Path

from launcher.store import Store, StateTransitionError

PAYLOAD = {"goal": "test reject", "owner": "tester", "cwd": ".", "timeout": 600}

class TestStoreReject(unittest.TestCase):
    def setUp(self):
        self.fd, self.db_path = tempfile.mkstemp()
        os.close(self.fd)
        self.store = Store(self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    def test_reject_task_success(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        self.store.transition_task("t1", "running", ("queued",))
        self.store.transition_task("t1", "completed-awaiting-review", ("running",))
        
        self.store.reject_task("t1", "reviewer-1", "bad code")
        task = self.store.get_task("t1")
        self.assertEqual(task["state"], "rejected")

    def test_reject_task_invalid_states(self):
        # Queued
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        with self.assertRaises(StateTransitionError):
            self.store.reject_task("t1", "reviewer-1", "bad code")
        
        # Running
        self.store.transition_task("t1", "running", ("queued",))
        with self.assertRaises(StateTransitionError):
            self.store.reject_task("t1", "reviewer-1", "bad code")
        
        # Accepted
        self.store.transition_task("t1", "completed-awaiting-review", ("running",))
        self.store.accept_task("t1", "reviewer-1")
        with self.assertRaises(StateTransitionError):
            self.store.reject_task("t1", "reviewer-2", "changed mind")

    def test_reject_task_releases_path_lease(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "workspace"
            p.mkdir()
            self.store.submit_task("t1", "key1", PAYLOAD, [str(p)])
            self.store.transition_task("t1", "running", ("queued",))
            self.store.transition_task("t1", "completed-awaiting-review", ("running",))
            
            # Lease is held
            with self.assertRaises(ValueError):
                self.store.submit_task("t2", "key2", PAYLOAD, [str(p)])
                
            # Reject task
            self.store.reject_task("t1", "reviewer-1", "rejected")
            
            # Lease should be released, submit should succeed
            res = self.store.submit_task("t3", "key3", PAYLOAD, [str(p)])
            self.assertEqual(res, "t3")

    def test_reject_task_preserves_review_receipts(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        self.store.transition_task("t1", "running", ("queued",))
        self.store.transition_task("t1", "completed-awaiting-review", ("running",))
        
        # Add a review receipt
        receipt = {
            "task_id": "t1",
            "source_commit": "abcdef123",
            "source_repo": "/repo",
            "reviewer": {"session_id": "sess-1", "model": "gemini"},
            "head_session_id": "head-1",
            "review_prompt": "review this",
            "report_path": "/report.txt",
            "report_sha256": "sha-hash",
            "verdict": "REJECT",
        }
        self.store.add_review_receipt(receipt, status="RECORDED", details={"issue": "bugs"})
        
        # Reject task
        self.store.reject_task("t1", "reviewer-1", "rejected")
        
        # Verify receipt is preserved
        receipts = self.store.list_review_receipts("t1")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["verdict"], "REJECT")
        self.assertEqual(receipts[0]["status"], "RECORDED")

if __name__ == '__main__':
    unittest.main()
