import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher.cli import run, run_task_units
from launcher.store import Store


class TaskUnitsCliTests(unittest.TestCase):
    def test_unsubmitted_id_fails_closed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        Store(str(cfg / "state.db"))
        args = argparse.Namespace(
            id="never-submitted",
            cwd=tmp.name,
            tmpdir=str(cfg / "tmp"),
            backend="task-units",
            config_dir=str(cfg),
            as_controller=True,
        )
        rc = run_task_units(args)
        self.assertEqual(rc, 1)

    def test_distinct_task_overlap_leases_without_holding_lock_during_wait(self):
        import threading
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        store = Store(str(cfg / "state.db"))
        for tid, key in (("t-a", "k-a"), ("t-b", "k-b")):
            store.submit_task(
                tid, key,
                {"owner": "ql", "cwd": tmp.name, "timeout": 60, "goal": tid, "provider": "grok"},
                [str(cfg / tid)],
            )
        overlapping = []
        barrier = threading.Barrier(2)

        def fake_execute(*args, **kwargs):
            overlapping.append(kwargs.get("task_id") or (args[0] if args else None))
            barrier.wait(timeout=5)
            return {"exit_code": 0, "unit": "x", "invocation_id": "i"}

        def run_one(tid):
            args = argparse.Namespace(
                id=tid, cwd=tmp.name, tmpdir=str(cfg / "tmp"),
                backend="task-units", config_dir=str(cfg), as_controller=True,
            )
            with patch("launcher.admission.fetch_quse", return_value={"ok": True}), \
                 patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
                 patch("launcher.task_units.execute_transient_task_unit", side_effect=fake_execute):
                return run_task_units(args)

        t1 = threading.Thread(target=run_one, args=("t-a",))
        t2 = threading.Thread(target=run_one, args=("t-b",))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)
        self.assertEqual(sorted(overlapping), ["t-a", "t-b"])
        self.assertEqual(store.get_task("t-a")["state"], "completed-awaiting-review")
        self.assertEqual(store.get_task("t-b")["state"], "completed-awaiting-review")

    def test_duplicate_lease_fails_closed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        store = Store(str(cfg / "state.db"))
        store.submit_task(
            "t-cli-dup", "k-cli-dup",
            {"owner": "ql", "cwd": tmp.name, "timeout": 60, "goal": "x", "provider": "grok"},
            [str(cfg / "p-dup")],
        )
        store.transition_task("t-cli-dup", "starting", ("queued",), reason="held")
        args = argparse.Namespace(
            id="t-cli-dup",
            cwd=tmp.name,
            tmpdir=str(cfg / "tmp"),
            backend="task-units",
            config_dir=str(cfg),
            as_controller=True,
        )
        rc = run_task_units(args)
        self.assertEqual(rc, 1)

    def test_missing_goal_fails_closed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        store = Store(str(cfg / "state.db"))
        store.submit_task(
            "t-cli-1", "k-cli-1",
            {"owner": "ql", "cwd": tmp.name, "timeout": 60, "provider": "grok"},
            [str(cfg / "p")],
        )
        args = argparse.Namespace(
            id="t-cli-1",
            cwd=tmp.name,
            tmpdir=str(cfg / "tmp"),
            backend="task-units",
            config_dir=str(cfg),
            as_controller=True,
        )
        rc = run_task_units(args)
        self.assertEqual(rc, 1)

    def test_quse_fetch_retry_leaves_task_queued(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        store = Store(str(cfg / "state.db"))
        store.submit_task(
            "t-cli-quse", "k-cli-quse",
            {"owner": "ql", "cwd": tmp.name, "timeout": 60, "goal": "x", "provider": "grok"},
            [str(cfg / "p-quse")],
        )
        args = argparse.Namespace(
            id="t-cli-quse", cwd=tmp.name, tmpdir=str(cfg / "tmp"),
            backend="task-units", config_dir=str(cfg), as_controller=True,
        )
        calls = {"n": 0}

        def flaky_quse():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("Failed to fetch quse (rc=1)")
            return {"ok": True}

        with patch("launcher.admission.fetch_quse", side_effect=flaky_quse), \
             patch("launcher.cli.time.sleep", return_value=None), \
             patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
             patch("launcher.task_units.execute_transient_task_unit",
                   return_value={"exit_code": 0, "unit": "x", "invocation_id": "i"}):
            rc = run_task_units(args)
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(store.get_task("t-cli-quse")["state"], "completed-awaiting-review")

    def test_quse_fetch_exhausted_keeps_queued(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        store = Store(str(cfg / "state.db"))
        store.submit_task(
            "t-cli-quse-fail", "k-cli-quse-fail",
            {"owner": "ql", "cwd": tmp.name, "timeout": 60, "goal": "x", "provider": "grok"},
            [str(cfg / "p-quse-fail")],
        )
        args = argparse.Namespace(
            id="t-cli-quse-fail", cwd=tmp.name, tmpdir=str(cfg / "tmp"),
            backend="task-units", config_dir=str(cfg), as_controller=True,
        )
        with patch("launcher.admission.fetch_quse",
                   side_effect=ValueError("Failed to fetch quse (rc=1)")), \
             patch("launcher.cli.time.sleep", return_value=None):
            rc = run_task_units(args)
        self.assertEqual(rc, 1)
        self.assertEqual(store.get_task("t-cli-quse-fail")["state"], "queued")

    def test_quse_oserror_retry_then_lease(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        store = Store(str(cfg / "state.db"))
        store.submit_task(
            "t-cli-quse-eagain", "k-cli-quse-eagain",
            {"owner": "ql", "cwd": tmp.name, "timeout": 60, "goal": "x", "provider": "grok"},
            [str(cfg / "p-quse-eagain")],
        )
        args = argparse.Namespace(
            id="t-cli-quse-eagain", cwd=tmp.name, tmpdir=str(cfg / "tmp"),
            backend="task-units", config_dir=str(cfg), as_controller=True,
        )
        calls = {"n": 0}

        def flaky_quse():
            calls["n"] += 1
            if calls["n"] < 2:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return {"ok": True}

        with patch("launcher.admission.fetch_quse", side_effect=flaky_quse), \
             patch("launcher.cli.time.sleep", return_value=None), \
             patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
             patch("launcher.task_units.execute_transient_task_unit",
                   return_value={"exit_code": 0, "unit": "x", "invocation_id": "i"}):
            rc = run_task_units(args)
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(store.get_task("t-cli-quse-eagain")["state"], "completed-awaiting-review")

    def test_head_cred_in_goal_fails_closed_queued(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        store = Store(str(cfg / "state.db"))
        store.submit_task(
            "t-cli-headcred", "k-cli-headcred",
            {
                "owner": "ql",
                "cwd": tmp.name,
                "timeout": 60,
                "provider": "grok",
                "goal": (
                    "inbox --cred /home/alexey/git/agent-quota-launcher/"
                    ".local/filebus/head.cred"
                ),
            },
            [str(cfg / "p-headcred")],
        )
        args = argparse.Namespace(
            id="t-cli-headcred", cwd=tmp.name, tmpdir=str(cfg / "tmp"),
            backend="task-units", config_dir=str(cfg), as_controller=True,
        )
        rc = run_task_units(args)
        self.assertEqual(rc, 1)
        self.assertEqual(store.get_task("t-cli-headcred")["state"], "queued")

    def test_outer_run_spawns_ql_ctl_without_as_controller(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name)
        Store(str(cfg / "state.db")).submit_task(
            "t-ctl", "k-ctl",
            {"owner": "ql", "cwd": tmp.name, "timeout": 60, "goal": "x", "provider": "grok"},
            [str(cfg / "p")],
        )
        args = argparse.Namespace(
            id="t-ctl", cwd=tmp.name, tmpdir=str(cfg / "tmp"),
            backend="task-units", config_dir=str(cfg), as_controller=False,
        )
        captured = {}
        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return argparse.Namespace(returncode=0, stdout="", stderr="")
        with patch("launcher.cli.subprocess.run", side_effect=fake_run):
            rc = run_task_units(args)
        self.assertEqual(rc, 0)
        self.assertIn("--as-controller", captured["cmd"])
        self.assertTrue(any(str(x).startswith("--unit=ql-ctl-") for x in captured["cmd"]))
        self.assertNotIn("--wait", captured["cmd"])


if __name__ == "__main__":
    unittest.main()
