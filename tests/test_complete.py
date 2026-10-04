import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from launcher.cli import complete, fail
from launcher.store import Store

PAYLOAD = {"goal": "test", "owner": "head-x", "cwd": ".", "timeout": 600}


class CompleteEvidenceGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.store = Store(self.db)
        self.artifact = os.path.join(self.tmp, "result.md")
        self.store.submit_task("t1", "key1", PAYLOAD, [self.artifact])
        self.store.transition_task("t1", "starting", ("queued",))
        self.store.transition_task("t1", "running", ("starting",))
        self.args = SimpleNamespace(config_dir=self.tmp, id="t1", reviewer="head-x")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def set_native(self, status, detail="phase=exited"):
        return patch("launcher.cli.native_status", return_value=(status, detail))

    def row(self, task_id):
        return next(t for t in self.store.list_tasks() if t["id"] == task_id)

    def test_unknown_native_status_refused(self):
        # An unknown aplexer status is not evidence of death.
        with self.set_native("unknown"):
            self.assertEqual(complete(self.args), 1)
        self.assertEqual(self.store.get_task("t1")["state"], "running")

    def test_alive_refused(self):
        with self.set_native("alive"):
            self.assertEqual(complete(self.args), 1)
        self.assertEqual(self.store.get_task("t1")["state"], "running")

    def test_dead_without_result_evidence_refused(self):
        # Native death alone is not success (Principal C1450).
        with self.set_native("dead"):
            self.assertEqual(complete(self.args), 1)
        self.assertEqual(self.store.get_task("t1")["state"], "running")

    def test_dead_with_empty_artifact_refused(self):
        open(self.artifact, "w").close()  # exists but empty
        with self.set_native("dead"):
            self.assertEqual(complete(self.args), 1)
        self.assertEqual(self.store.get_task("t1")["state"], "running")

    def test_dead_with_result_evidence_completes(self):
        with open(self.artifact, "w") as f:
            f.write("# result\nincremental artifact content\n")
        with self.set_native("dead") as m:
            self.assertEqual(complete(self.args), 0)
        task = self.row("t1")
        self.assertEqual(task["state"], "completed-awaiting-review")
        self.assertEqual(task["reviewer"], "head-x")
        self.assertIn(self.artifact, task["reason"])
        m.assert_called_once_with("task-t1")  # run_tag_for prefixes bare ids


class FailSubcommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.store = Store(self.db)
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        self.store.transition_task("t1", "starting", ("queued",))
        self.store.transition_task("t1", "running", ("starting",))
        self.args = SimpleNamespace(config_dir=self.tmp, id="t1", reviewer="head-x",
                                    reason="no result artifacts produced")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def row(self, task_id):
        return next(t for t in self.store.list_tasks() if t["id"] == task_id)

    def test_fail_requires_confirmed_death(self):
        with patch("launcher.cli.native_status", return_value=("unknown", "x")):
            self.assertEqual(fail(self.args), 1)
        self.assertEqual(self.store.get_task("t1")["state"], "running")

    def test_fail_closes_dead_running_task(self):
        with patch("launcher.cli.native_status", return_value=("dead", "phase=exited")):
            self.assertEqual(fail(self.args), 0)
        task = self.row("t1")
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["reviewer"], "head-x")
        self.assertIn("no result artifacts produced", task["reason"])

if __name__ == '__main__':
    unittest.main()
