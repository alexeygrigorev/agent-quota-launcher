import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from launcher.consumer_fencing import (
    Fenced,
    HAVE_COORD_FENCING,
    LauncherConsumerFencing,
    reject_head_cred_inheritance,
)
from launcher.store import Store

if HAVE_COORD_FENCING:
    from coordination.role_failover import RoleAuthority
else:
    RoleAuthority = None


@unittest.skipUnless(HAVE_COORD_FENCING, "agent-coordination not available for consumer fencing tests")
class ConsumerFencingTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.now = [1000.0]
        self.auth_db = str(self.tmp_path / "roles.db")
        self.authority = RoleAuthority(self.auth_db, lambda: self.now[0])
        self.fencing = LauncherConsumerFencing(self.authority)
        self.store = Store(str(self.tmp_path / "state.db"))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _setup_role(self, project="proj-1", role="lead", incumbent="agent-a", successor="agent-b"):
        self.authority.configure(project, role, [incumbent, successor])
        self.authority.observe(
            incumbent, "hetzner", "g1", ready=True, draft=False, quota_ok=True, priority=0
        )
        self.authority.observe(
            successor, "hetzner", "g2", ready=True, draft=False, quota_ok=True, priority=1
        )
        elect = self.authority.tick(project, role)
        self.assertEqual(elect["state"], "elected")
        self.assertEqual(elect["holder"], incumbent)
        self.assertEqual(elect["epoch"], 1)

        # Activate incumbent
        self.authority.activation(
            project, role, incumbent, "g1", 1,
            role_ack="ack-incumbent", first_action="tool-init"
        )

    def test_valid_admit_and_submit_to_store(self):
        """Verifies successful guarded admission and durable Store submission under valid epoch."""
        self._setup_role()
        task_cwd = str(self.tmp_path / "work")
        os.makedirs(task_cwd, exist_ok=True)

        payload = {
            "owner": "test-owner",
            "cwd": task_cwd,
            "timeout": 120,
            "goal": "execute guarded task",
        }

        outcome = self.fencing.admit_and_submit_task(
            store=self.store,
            project="proj-1",
            role="lead",
            actor="agent-a",
            generation="g1",
            epoch=1,
            task_id="t-fenced-1",
            payload=payload,
            paths=[str(self.tmp_path / "p1")],
        )

        self.assertEqual(outcome.get("state"), "queued")
        self.assertEqual(outcome.get("task_id"), "t-fenced-1")
        self.assertEqual(outcome.get("key"), "ql-task:proj-1:lead:1:t-fenced-1")

        # Verify task is durably in Store
        task = self.store.get_task("t-fenced-1")
        self.assertIsNotNone(task)
        self.assertEqual(task["state"], "queued")
        stored_payload = task["payload"]
        self.assertIn("coordination_fencing", stored_payload)
        fencing_meta = stored_payload["coordination_fencing"]
        self.assertEqual(fencing_meta["epoch"], 1)
        self.assertEqual(fencing_meta["actor"], "agent-a")
        self.assertEqual(fencing_meta["idempotency_key"], "ql-task:proj-1:lead:1:t-fenced-1")

    def test_stale_epoch_rejected_with_fenced(self):
        """Verifies that once an incumbent is superseded, admission with stale epoch fails closed."""
        self._setup_role()
        # Advance time beyond lease to trigger takeover by successor
        self.now[0] += 181.0
        self.authority.observe("agent-b", "hetzner", "g2", ready=True, draft=False, quota_ok=True, priority=1)
        diag = self.authority.tick("proj-1", "lead")
        self.assertEqual(diag["state"], "diagnosing")

        # Past diagnosis grace period, promote successor
        self.now[0] += 121.0
        self.authority.observe("agent-b", "hetzner", "g2", ready=True, draft=False, quota_ok=True, priority=1)
        elect2 = self.authority.tick("proj-1", "lead")
        self.assertEqual(elect2["state"], "elected")
        self.assertEqual(elect2["holder"], "agent-b")
        self.assertEqual(elect2["epoch"], 2)

        payload = {
            "owner": "test-owner",
            "cwd": str(self.tmp_path),
            "timeout": 120,
            "goal": "stale attempt",
        }

        # Stale incumbent attempts submission at epoch 1 -> must raise Fenced!
        with self.assertRaises(Fenced):
            self.fencing.admit_and_submit_task(
                store=self.store,
                project="proj-1",
                role="lead",
                actor="agent-a",
                generation="g1",
                epoch=1,
                task_id="t-stale-attempt",
                payload=payload,
            )

        # Verify task was NOT created in Store
        self.assertIsNone(self.store.get_task("t-stale-attempt"))

    def test_head_cred_inheritance_rejected_at_admission(self):
        """Verifies that any payload carrying head credential material is rejected."""
        self._setup_role()
        forbidden_payloads = [
            {"goal": "run", "head.cred": "secret-token", "timeout": 60},
            {"goal": "run", "head_token": "secret-token", "timeout": 60},
            {"goal": "run", "head": {"cred": "secret-token"}, "timeout": 60},
        ]

        for p in forbidden_payloads:
            with self.subTest(payload=p):
                with self.assertRaises(ValueError):
                    self.fencing.admit_and_submit_task(
                        store=self.store,
                        project="proj-1",
                        role="lead",
                        actor="agent-a",
                        generation="g1",
                        epoch=1,
                        task_id="t-forbidden-cred",
                        payload=p,
                    )
                self.assertIsNone(self.store.get_task("t-forbidden-cred"))

    def test_duplicate_submission_deduped(self):
        """Verifies that submitting the same task twice under the same epoch is deduplicated."""
        self._setup_role()
        payload = {"owner": "test-owner", "cwd": str(self.tmp_path), "timeout": 60, "goal": "dedup test"}

        res1 = self.fencing.admit_and_submit_task(
            store=self.store,
            project="proj-1",
            role="lead",
            actor="agent-a",
            generation="g1",
            epoch=1,
            task_id="t-dedup-1",
            payload=payload,
        )
        self.assertEqual(res1.get("state"), "queued")

        # Second submission with same key
        res2 = self.fencing.admit_and_submit_task(
            store=self.store,
            project="proj-1",
            role="lead",
            actor="agent-a",
            generation="g1",
            epoch=1,
            task_id="t-dedup-1",
            payload=payload,
        )
        self.assertIn(res2.get("state"), ("already_enqueued", "queued"))

        # Store should have exactly one task
        task = self.store.get_task("t-dedup-1")
        self.assertIsNotNone(task)

    def test_runtime_epoch_validation_current_vs_stale(self):
        """Verifies validate_runtime_epoch returns True for current holder and False for stale."""
        self._setup_role()
        # Current holder/epoch
        valid, reason = self.fencing.validate_runtime_epoch(
            project="proj-1", role="lead", actor="agent-a", generation="g1", expected_epoch=1
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "valid_epoch")

        # Stale expected epoch
        valid, reason = self.fencing.validate_runtime_epoch(
            project="proj-1", role="lead", actor="agent-a", generation="g1", expected_epoch=0
        )
        self.assertFalse(valid)
        self.assertIn("fenced", reason)

        # Expire lease and elect successor
        self.now[0] += 181.0
        self.authority.observe("agent-b", "hetzner", "g2", ready=True, draft=False, quota_ok=True, priority=1)
        self.authority.tick("proj-1", "lead")
        self.now[0] += 121.0
        self.authority.observe("agent-b", "hetzner", "g2", ready=True, draft=False, quota_ok=True, priority=1)
        self.authority.tick("proj-1", "lead")
        # Old actor at epoch 1 is now stale
        valid, reason = self.fencing.validate_runtime_epoch(
            project="proj-1", role="lead", actor="agent-a", generation="g1", expected_epoch=1
        )
        self.assertFalse(valid)
        self.assertIn("fenced", reason)


if __name__ == "__main__":
    unittest.main()
