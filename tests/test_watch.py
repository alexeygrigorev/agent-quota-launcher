import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from launcher.cli import accept, run_task_units
from launcher.store import Store
from launcher.watch import (
    _next_dispatchable,
    _unreviewed_task_ids,
    derive_task_tmpdir,
    watch_loop,
)


class WatchRefillTests(unittest.TestCase):
    def test_derive_task_tmpdir_contained(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_cwd = os.path.join(tmp_dir, "task-20")
            os.makedirs(task_cwd, exist_ok=True)

            # Case 1: no tmpdir in payload -> defaults to task_cwd/.local/tmp
            res1 = derive_task_tmpdir(task_cwd, {})
            expected = os.path.join(task_cwd, ".local", "tmp")
            self.assertEqual(res1, expected)
            self.assertTrue(os.path.isdir(res1))

            # Case 2: foreign tmpdir (e.g. from task-17) -> rejected and defaults to task_cwd/.local/tmp
            foreign = os.path.join(tmp_dir, "task-17", ".local", "tmp")
            res2 = derive_task_tmpdir(task_cwd, {"tmpdir": foreign})
            self.assertEqual(res2, expected)

            # Case 3: valid subfolder under task_cwd/.local/tmp -> accepted
            valid_sub = os.path.join(task_cwd, ".local", "tmp", "sub")
            res3 = derive_task_tmpdir(task_cwd, {"tmpdir": valid_sub})
            self.assertEqual(res3, valid_sub)
            self.assertTrue(os.path.isdir(valid_sub))

    def test_watch_loop_waits_for_unreviewed_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            # Submit task-1 and move to completed-awaiting-review
            store.submit_task("t-1", "k-1", {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "g1"}, [str(cfg / "p1")])
            store.transition_task("t-1", "starting", ("queued",), reason="lease")
            store.transition_task("t-1", "completed-awaiting-review", ("starting",), reason="exit 0")

            # Submit task-2 in queued state
            store.submit_task("t-2", "k-2", {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "g2"}, [str(cfg / "p2")])

            # With wait_for_review=True, t-2 should NOT be dispatchable
            tid, note = _next_dispatchable(store, wait_for_review=True)
            self.assertIsNone(tid)
            self.assertIn("automatic refill waiting for distinct independent review acceptance", note)
            self.assertIn("t-1", note)

            # With wait_for_review=False, t-2 is dispatchable
            tid, note = _next_dispatchable(store, wait_for_review=False)
            self.assertEqual(tid, "t-2")

            # Accept t-1, now t-2 should become dispatchable even with wait_for_review=True
            store.accept_task("t-1", reviewer="test-reviewer")
            tid, note = _next_dispatchable(store, wait_for_review=True)
            self.assertEqual(tid, "t-2")

    def test_watch_loop_dispatches_with_task_local_tmpdir_not_args_tmpdir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            task_cwd = os.path.join(tmp_dir, "my-task")
            os.makedirs(task_cwd, exist_ok=True)
            store.submit_task(
                "t-local", "k-local",
                {"owner": "ql", "cwd": task_cwd, "timeout": 60, "goal": "run local"},
                [str(cfg / "plocal")],
            )

            # args passes a foreign tmpdir (e.g. from an earlier task)
            foreign_tmp = os.path.join(tmp_dir, "foreign", ".local", "tmp")
            args = argparse.Namespace(
                config_dir=str(cfg),
                cwd=tmp_dir,
                tmpdir=foreign_tmp,
                backend="task-units",
                once=True,
            )

            captured = {}
            def fake_spawn(dispatch_args):
                captured["id"] = dispatch_args.id
                captured["cwd"] = dispatch_args.cwd
                captured["tmpdir"] = dispatch_args.tmpdir
                return 0

            with patch("launcher.cli.spawn_ql_controller", side_effect=fake_spawn):
                rc = watch_loop(args, max_passes=1)

            self.assertEqual(rc, 0)
            self.assertEqual(captured["id"], "t-local")
            self.assertEqual(captured["cwd"], str(Path(task_cwd).resolve()))
            # TMPDIR must be strictly under task_cwd, NOT foreign_tmp!
            expected_tmp = str(Path(task_cwd).resolve() / ".local" / "tmp")
            self.assertEqual(captured["tmpdir"], expected_tmp)
            self.assertNotEqual(captured["tmpdir"], foreign_tmp)

    def test_run_task_units_does_not_trigger_refill_on_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            task_cwd = os.path.join(tmp_dir, "task-run")
            task_tmp = os.path.join(task_cwd, ".local", "tmp")
            os.makedirs(task_tmp, exist_ok=True)

            store.submit_task(
                "t-run", "k-run",
                {"owner": "ql", "cwd": task_cwd, "timeout": 60, "goal": "test goal", "provider": "grok"},
                [str(cfg / "p-run")],
            )
            args = argparse.Namespace(
                id="t-run",
                cwd=task_cwd,
                tmpdir=task_tmp,
                backend="task-units",
                config_dir=str(cfg),
                as_controller=True,
            )

            with patch("launcher.admission.fetch_quse", return_value={"ok": True}), \
                 patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
                 patch("launcher.task_units.execute_transient_task_unit",
                       return_value={"exit_code": 0, "unit": "u", "invocation_id": "inv"}), \
                 patch("launcher.watch.watch_loop") as mock_watch:
                rc = run_task_units(args)

            self.assertEqual(rc, 0)
            self.assertEqual(store.get_task("t-run")["state"], "completed-awaiting-review")
            # Verify watch_loop was NOT called (refill is held waiting for review acceptance)
            mock_watch.assert_not_called()

    def test_accept_triggers_refill_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            store.submit_task(
                "t-acc", "k-acc",
                {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "acc goal"},
                [str(cfg / "p-acc")],
            )
            store.transition_task("t-acc", "starting", ("queued",), reason="lease")
            store.transition_task("t-acc", "completed-awaiting-review", ("starting",), reason="exit 0")

            args = argparse.Namespace(
                id="t-acc",
                reviewer="reviewer-alice",
                config_dir=str(cfg),
                backend="task-units",
                refill=True,
            )

            with patch("launcher.watch.watch_loop") as mock_watch:
                rc = accept(args)

            self.assertEqual(rc, 0)
            self.assertEqual(store.get_task("t-acc")["state"], "accepted")
            # Verify watch_loop was called after acceptance
            mock_watch.assert_called_once()
            called_refill_args = mock_watch.call_args[0][0]
            self.assertEqual(called_refill_args.backend, "task-units")
            self.assertTrue(called_refill_args.once)


if __name__ == "__main__":
    unittest.main()
