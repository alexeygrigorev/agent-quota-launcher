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

            # With wait_for_review="global", t-2 should NOT be dispatchable
            tid, note = _next_dispatchable(store, wait_for_review="global")
            self.assertIsNone(tid)
            self.assertIn("automatic refill waiting for distinct independent review acceptance", note)
            self.assertIn("t-1", note)

            # With wait_for_review=False, t-2 is dispatchable
            tid, note = _next_dispatchable(store, wait_for_review=False)
            self.assertEqual(tid, "t-2")

            # Accept t-1, now t-2 should become dispatchable even with wait_for_review="global"
            store.accept_task("t-1", reviewer="test-reviewer")
            tid, note = _next_dispatchable(store, wait_for_review="global")
            self.assertEqual(tid, "t-2")

    def test_independent_tasks_dispatch_when_another_awaiting_review(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            # Submit task-1 and move to completed-awaiting-review
            store.submit_task("t-1", "k-1", {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "g1"}, [str(cfg / "p1")])
            store.transition_task("t-1", "starting", ("queued",), reason="lease")
            store.transition_task("t-1", "completed-awaiting-review", ("starting",), reason="exit 0")

            # Submit task-dep in queued state, depending on t-1
            store.submit_task(
                "t-dep", "k-dep",
                {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "g-dep", "depends_on": ["t-1"]},
                [str(cfg / "p-dep")],
            )

            # Submit task-indep in queued state, without dependencies
            store.submit_task(
                "t-indep", "k-indep",
                {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "g-indep"},
                [str(cfg / "p-indep")],
            )

            # With default wait_for_review="dependencies", t-dep is blocked on unaccepted t-1,
            # but independent task t-indep dispatches immediately!
            tid, note = _next_dispatchable(store, wait_for_review="dependencies")
            self.assertEqual(tid, "t-indep")

            # Same with wait_for_review=True (evaluates per-task dependencies)
            tid, note = _next_dispatchable(store, wait_for_review=True)
            self.assertEqual(tid, "t-indep")

            # Mark t-indep starting so only t-dep remains queued
            store.transition_task("t-indep", "starting", ("queued",), reason="lease")

            # t-dep is still blocked because t-1 is completed-awaiting-review, not accepted
            tid, note = _next_dispatchable(store, wait_for_review="dependencies")
            self.assertIsNone(tid)
            self.assertIn("1 queued, all blocked", note)

            # Accept t-1
            store.accept_task("t-1", reviewer="test-reviewer")

            # Now t-dep is dispatchable!
            tid, note = _next_dispatchable(store, wait_for_review="dependencies")
            self.assertEqual(tid, "t-dep")

            # Also verify "dependencies" key synonym
            store.submit_task(
                "t-dep2", "k-dep2",
                {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "g-dep2", "dependencies": ["t-dep"]},
                [str(cfg / "p-dep2")],
            )
            store.transition_task("t-dep", "starting", ("queued",), reason="lease")
            tid, note = _next_dispatchable(store, wait_for_review="dependencies")
            self.assertIsNone(tid)
            store.transition_task("t-dep", "completed-awaiting-review", ("starting",), reason="exit 0")
            store.accept_task("t-dep", reviewer="test-reviewer")
            tid, note = _next_dispatchable(store, wait_for_review="dependencies")
            self.assertEqual(tid, "t-dep2")

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
            self.assertEqual(called_refill_args.wait_for_review, "dependencies")

    def test_bare_proposal_without_substantive_prompt_blocked(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))

            # Bare proposal 1: empty goal
            store.submit_task("t-empty", "k-empty", {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": ""}, [str(cfg / "p-empty")])
            # Bare proposal 2: whitespace goal
            store.submit_task("t-ws", "k-ws", {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "   "}, [str(cfg / "p-ws")])
            # Bare proposal 3: goal == task_id
            store.submit_task("t-same", "k-same", {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "t-same"}, [str(cfg / "p-same")])

            # With only bare proposals, none should be dispatchable
            tid, note = _next_dispatchable(store)
            self.assertIsNone(tid)
            self.assertIn("3 queued, all blocked", note)

            with store.get_conn() as conn:
                r_empty = conn.execute("SELECT reason FROM tasks WHERE id = 't-empty'").fetchone()[0]
                r_same = conn.execute("SELECT reason FROM tasks WHERE id = 't-same'").fetchone()[0]
            self.assertEqual(r_empty, "watch: blocked: bare proposal without substantive prompt")
            self.assertEqual(r_same, "watch: blocked: bare proposal without substantive prompt")

            # Submit task with genuine substantive goal
            store.submit_task("t-good", "k-good", {"owner": "ql", "cwd": tmp_dir, "timeout": 60, "goal": "genuine prompt to implement feature"}, [str(cfg / "p-good")])
            tid, note = _next_dispatchable(store)
            self.assertEqual(tid, "t-good")
            self.assertIsNone(note)

    def test_disk_pressure_29_gib_eligible_continues_and_cleanup_enqueued(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            task_cwd = os.path.join(tmp_dir, "my-work")
            os.makedirs(task_cwd, exist_ok=True)
            store.submit_task(
                "t-work", "k-work",
                {"owner": "ql", "cwd": task_cwd, "timeout": 60, "goal": "implement valuable feature"},
                [str(cfg / "pwork")],
            )

            args = argparse.Namespace(
                config_dir=str(cfg),
                cwd=task_cwd,
                tmpdir=os.path.join(task_cwd, ".local", "tmp"),
                backend="task-units",
                once=True,
            )

            captured = {}
            def fake_spawn(dispatch_args):
                captured["id"] = dispatch_args.id
                return 0

            GiB = 1024 * 1024 * 1024

            with patch("shutil.disk_usage", return_value=type("DiskUsage", (), {"free": 29 * GiB})()), \
                 patch("launcher.cli.spawn_ql_controller", side_effect=fake_spawn):
                rc = watch_loop(args, max_passes=1)

            self.assertEqual(rc, 0)
            self.assertEqual(captured.get("id"), "t-work")

            cleanup_task = store.get_task("disk-pressure-cleanup-1")
            self.assertIsNotNone(cleanup_task)
            payload = cleanup_task["payload"]
            self.assertEqual(
                payload["goal"],
                "Prune expired scratch/temporary files in .local/tmp/. "
                "Exclude active leases, dirty/unmerged worktrees, histories/auth/recovery paths. "
                "Unknown ownership: no deletion.",
            )
            self.assertEqual(payload["cwd"], "/home/alexey/git/cloudflare-agent-git")
            self.assertEqual(payload["tmpdir"], "/home/alexey/git/cloudflare-agent-git/.local/tmp/cleanup")
            self.assertEqual(payload["owner"], "ant-head-continuation-resume-20261005")
            self.assertEqual(payload["timeout"], 600)
            self.assertEqual(payload["model_requirements"], {"provider": "antigravity"})
            self.assertEqual(cleanup_task["state"], "queued")

    def test_disk_pressure_deduplication_multiple_passes_at_29_gib(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            task_cwd = os.path.join(tmp_dir, "my-work")
            os.makedirs(task_cwd, exist_ok=True)

            args = argparse.Namespace(
                config_dir=str(cfg),
                cwd=task_cwd,
                tmpdir=os.path.join(task_cwd, ".local", "tmp"),
                backend="task-units",
                interval=0.01,
            )

            GiB = 1024 * 1024 * 1024

            def fake_spawn(dispatch_args):
                store.transition_task(dispatch_args.id, "running", ("queued",), reason="lease")
                return 0

            with patch("shutil.disk_usage", return_value=type("DiskUsage", (), {"free": 29 * GiB})()), \
                 patch("launcher.cli.spawn_ql_controller", side_effect=fake_spawn):
                rc = watch_loop(args, max_passes=3)

            self.assertEqual(rc, 0)
            with store.get_conn() as conn:
                count = conn.execute(
                    "SELECT count(*) FROM tasks WHERE idempotency_key LIKE 'disk-pressure-cleanup-%'"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_disk_pressure_rearm_at_30_gib_and_subsequent_drop(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            task_cwd = os.path.join(tmp_dir, "my-work")
            os.makedirs(task_cwd, exist_ok=True)

            episode_state = {"in_episode": False, "episode_id": 0}
            args = argparse.Namespace(
                config_dir=str(cfg),
                cwd=task_cwd,
                tmpdir=os.path.join(task_cwd, ".local", "tmp"),
                backend="task-units",
                once=True,
                episode_state=episode_state,
            )

            GiB = 1024 * 1024 * 1024

            def fake_spawn(dispatch_args):
                return 0

            # Pass 1: at 29 GiB -> enqueues disk-pressure-cleanup-1
            with patch("shutil.disk_usage", return_value=type("DiskUsage", (), {"free": 29 * GiB})()), \
                 patch("launcher.cli.spawn_ql_controller", side_effect=fake_spawn):
                watch_loop(args, max_passes=1)

            self.assertIsNotNone(store.get_task("disk-pressure-cleanup-1"))
            self.assertTrue(episode_state["in_episode"])
            self.assertEqual(episode_state["episode_id"], 1)

            # Pass 2: recovers to 30 GiB -> pressure cleared, rearm enabled
            with patch("shutil.disk_usage", return_value=type("DiskUsage", (), {"free": 30 * GiB})()), \
                 patch("launcher.cli.spawn_ql_controller", side_effect=fake_spawn):
                watch_loop(args, max_passes=1)

            self.assertFalse(episode_state["in_episode"])

            # Cleanup task 1 completes and is accepted by reviewer, resolving active state
            store.transition_task("disk-pressure-cleanup-1", "starting", ("queued",), reason="lease")
            store.transition_task("disk-pressure-cleanup-1", "completed-awaiting-review", ("starting",), reason="pruned")
            store.accept_task("disk-pressure-cleanup-1", reviewer="test-reviewer")

            # Pass 3: drops back to 28 GiB -> new episode, enqueues disk-pressure-cleanup-2
            with patch("shutil.disk_usage", return_value=type("DiskUsage", (), {"free": 28 * GiB})()), \
                 patch("launcher.cli.spawn_ql_controller", side_effect=fake_spawn):
                watch_loop(args, max_passes=1)

            self.assertTrue(episode_state["in_episode"])
            self.assertEqual(episode_state["episode_id"], 2)
            self.assertIsNotNone(store.get_task("disk-pressure-cleanup-2"))

    def test_disk_pressure_hard_floor_rejection_below_20_gib(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = Path(tmp_dir)
            store = Store(str(cfg / "state.db"))
            task_cwd = os.path.join(tmp_dir, "my-work")
            os.makedirs(task_cwd, exist_ok=True)
            store.submit_task(
                "t-floor", "k-floor",
                {"owner": "ql", "cwd": task_cwd, "timeout": 60, "goal": "must not dispatch under 20GiB"},
                [str(cfg / "pfloor")],
            )

            args = argparse.Namespace(
                config_dir=str(cfg),
                cwd=task_cwd,
                tmpdir=os.path.join(task_cwd, ".local", "tmp"),
                backend="task-units",
                once=True,
            )

            spawned = []
            def fake_spawn(dispatch_args):
                spawned.append(dispatch_args.id)
                return 0

            GiB = 1024 * 1024 * 1024

            # 19 GiB (< 20 GiB hard floor)
            with patch("shutil.disk_usage", return_value=type("DiskUsage", (), {"free": 19 * GiB})()), \
                 patch("launcher.cli.spawn_ql_controller", side_effect=fake_spawn):
                rc = watch_loop(args, max_passes=1)

            self.assertEqual(rc, 0)
            self.assertEqual(spawned, [])
            self.assertEqual(store.get_task("t-floor")["state"], "queued")
            with store.get_conn() as conn:
                count = conn.execute(
                    "SELECT count(*) FROM tasks WHERE idempotency_key LIKE 'disk-pressure-cleanup-%'"
                ).fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()

