import unittest
import tempfile
import os
from pathlib import Path
from launcher.store import Store, StateTransitionError

PAYLOAD = {"goal": "test", "owner": "head-x", "cwd": ".", "timeout": 600}

class TestStore(unittest.TestCase):
    def setUp(self):
        self.fd, self.db_path = tempfile.mkstemp()
        os.close(self.fd)
        self.store = Store(self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    def test_submit_idempotent(self):
        res1 = self.store.submit_task("t1", "key1", PAYLOAD, [])
        self.assertEqual(res1, "t1")
        res2 = self.store.submit_task("t2", "key1", PAYLOAD, [])
        self.assertEqual(res2, "t1")
        payload_diff = dict(PAYLOAD, goal="different")
        with self.assertRaises(ValueError):
            self.store.submit_task("t3", "key1", payload_diff, [])

    def test_path_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            dir_path = Path(d)
            p1 = dir_path / "a" / "b"
            p1.mkdir(parents=True)
            p2 = dir_path / "a"
            self.store.submit_task("t1", "key1", PAYLOAD, [str(p1)])
            with self.assertRaises(ValueError) as ctx:
                self.store.submit_task("t2", "key2", PAYLOAD, [str(p2)])
            self.assertIn("Path overlap", str(ctx.exception))
            with self.assertRaises(ValueError):
                self.store.submit_task("t3", "key3", PAYLOAD, [str(p1)])
            p3 = dir_path / "a" / "c"
            p3.mkdir()
            self.store.submit_task("t4", "key4", PAYLOAD, [str(p3)])

    def test_accept_on_queued_rejected(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        with self.assertRaises(StateTransitionError):
            self.store.accept_task("t1", "head-x")

    def test_complete_on_queued_rejected(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        with self.assertRaises(StateTransitionError):
            self.store.complete_task("t1", "head-x")

    def test_accept_requires_completed_awaiting_review(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        self.store.transition_task("t1", "running", ("queued",))
        with self.assertRaises(StateTransitionError):
            self.store.accept_task("t1", "head-x")  # running cannot be accepted
        self.store.transition_task("t1", "completed-awaiting-review", ("running",))
        self.store.accept_task("t1", "head-x")
        self.assertEqual(self.store.get_task("t1")["state"], "accepted")

    def test_launch_uncertain_cannot_be_completed_or_accepted(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        self.store.transition_task("t1", "starting", ("queued",))
        self.store.transition_task("t1", "launch-uncertain", ("starting",))
        with self.assertRaises(StateTransitionError):
            self.store.complete_task("t1", "head-x")
        with self.assertRaises(StateTransitionError):
            self.store.accept_task("t1", "head-x")

    def test_reviewer_recorded(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        self.store.transition_task("t1", "running", ("queued",))
        self.store.complete_task("t1", "reviewer-a")
        self.store.accept_task("t1", "reviewer-b")
        with self.store.get_conn() as conn:
            row = conn.execute("SELECT reviewer FROM tasks WHERE id='t1'").fetchone()
        self.assertEqual(row[0], "reviewer-b")

    def test_path_lease_holds_through_completed_awaiting_review(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "owned"
            p.mkdir()
            self.store.submit_task("t1", "key1", PAYLOAD, [str(p)])
            self.store.transition_task("t1", "running", ("queued",))
            self.store.transition_task("t1", "completed-awaiting-review", ("running",))
            with self.store.get_conn() as conn:
                active = self.store.get_active_paths(conn)
            self.assertEqual(len(active), 1)  # lease still held
            with self.assertRaises(ValueError):
                self.store.submit_task("t2", "key2", PAYLOAD, [str(p)])
            self.store.accept_task("t1", "head-x")
            with self.store.get_conn() as conn:
                active = self.store.get_active_paths(conn)
            self.assertEqual(active, [])

    def test_active_resources_exclude_task(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        mem, disk = self.store.get_active_resources(exclude_task_id="t1")
        self.assertEqual((mem, disk), (0, 0))
        # t1 is queued -> holds 1500/512 for others (subprocess call to aplexer
        # fails in test env and queued state still holds).
        mem2, disk2 = self.store.get_active_resources()
        self.assertEqual((mem2, disk2), (1500, 512))

    def test_completed_awaiting_review_releases_ram(self):
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        self.store.transition_task("t1", "running", ("queued",))
        mem, _ = self.store.get_active_resources()
        self.assertEqual(mem, 1500)
        self.store.transition_task("t1", "completed-awaiting-review", ("running",))
        mem2, _ = self.store.get_active_resources()
        self.assertEqual(mem2, 0)  # RAM/disk released after confirmed completion

if __name__ == '__main__':
    unittest.main()
