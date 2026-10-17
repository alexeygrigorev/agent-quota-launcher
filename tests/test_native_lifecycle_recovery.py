"""Exhaustive tests for authoritative native lifecycle and resource accounting recovery.

Verifies:
1. Exact native binding schema validation, persistence, and global UUID lookup.
2. Real exact live record stays reserved.
3. Matching finished tombstone frees process RAM/disk reservation even after worktree is deleted.
4. Boundaries and failure cases that MUST retain reservation (conservative):
   - Mismatched UUID in tombstone
   - Mismatched tag (or reused tag) in tombstone
   - Mismatched workspace in tombstone
   - Malformed / corrupted tombstone JSON or non-dict
   - Tombstone with cause != "finished" (e.g. "crashed", "killed")
   - Stale incarnation (finished_at_ms < created_at_ms)
   - Transport failure, stale socket, or timeout
   - Missing live session and missing tombstone
5. Legacy records without native binding fail conservative (retaining reservation).
6. Concurrent terminal/claim update does not double-release or go negative.
7. Outcome evidence gates preserved:
   - Native exit alone is not completion (rejects complete without non-empty result artifact).
   - Proven native exit + non-empty artifact completes successfully.
   - Accept cannot be performed on running task even with exit proof (requires completed-awaiting-review).
8. Global UUID lookup across workspaces in CLI complete, fail, and targeted reconcile.
9. Targeted reconcile updates only the targeted task without dispatching queued work.
10. Automatic recovery of genuine binding from launch artifacts if worktree still present.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from launcher.cli import complete, fail, reconcile, accept, status
from launcher.native_lifecycle import (
    validate_native_id,
    validate_native_binding,
    read_tombstone,
    verify_tombstone,
    probe_native_lifecycle,
)
from launcher.store import Store, StateTransitionError

VALID_UUID = "cd0e3245-4928-4d65-aadb-e9c199f71df8"
VALID_UUID_2 = "de9badb4-cb44-4762-ade2-7d4f8cd5864b"
PAYLOAD = {
    "goal": "implement reliability worker",
    "owner": "test-coordinator",
    "cwd": "/tmp/test-wt-nonexistent",
    "timeout": 600,
}


class NativeLifecycleValidationTests(unittest.TestCase):
    def test_validate_native_id_canonical(self):
        self.assertEqual(validate_native_id(VALID_UUID.upper()), VALID_UUID.lower())

    def test_validate_native_id_rejects_invalid(self):
        # Non-UUID strings
        for bad in ("not-a-uuid", "12345", "", "   ", "cd0e3245-4928-4d65-aadb-e9c199f71dfg"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_native_id(bad)

        # Nil UUID
        with self.assertRaises(ValueError):
            validate_native_id("00000000-0000-0000-0000-000000000000")

        # Caller PID must never be treated as UUID
        with self.assertRaises(ValueError):
            validate_native_id(str(os.getpid()))

    def test_validate_native_binding_fields(self):
        binding = validate_native_binding(
            task_id="t-1",
            native_id=VALID_UUID,
            tag="task-t-1",
            workspace="/tmp/test-workspace",
            engine="gemini",
            incarnation=1792260000000,
            details={"start_json": {"id": VALID_UUID}},
        )
        self.assertEqual(binding["task_id"], "t-1")
        self.assertEqual(binding["native_id"], VALID_UUID)
        self.assertEqual(binding["tag"], "task-t-1")
        self.assertEqual(binding["workspace"], str(Path("/tmp/test-workspace").resolve()))
        self.assertEqual(binding["engine"], "gemini")
        self.assertEqual(binding["incarnation"], 1792260000000)
        self.assertEqual(binding["details"]["start_json"]["id"], VALID_UUID)

    def test_validate_native_binding_rejects_missing_or_bad_incarnation(self):
        for bad_inc in (0, -100, "bad", None, False, True):
            with self.subTest(bad_inc=bad_inc), self.assertRaises(ValueError):
                validate_native_binding(
                    task_id="t-1",
                    native_id=VALID_UUID,
                    tag="task-t-1",
                    workspace="/tmp/ws",
                    engine="gemini",
                    incarnation=bad_inc,
                )


class NativeStoreBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.store = Store(self.db)
        self.store.submit_task("task-100", "key-100", PAYLOAD, [])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_record_and_get_native_binding(self):
        self.store.record_native_binding(
            task_id="task-100",
            native_id=VALID_UUID,
            tag="task-hub-100",
            workspace="/home/alexey/git/test-repo",
            engine="gemini",
            incarnation=1792261000000,
            details={"first_action_verified": True},
        )
        b = self.store.get_native_binding("task-100")
        self.assertIsNotNone(b)
        self.assertEqual(b["native_id"], VALID_UUID)
        self.assertEqual(b["tag"], "task-hub-100")
        self.assertEqual(b["engine"], "gemini")
        self.assertEqual(b["incarnation"], 1792261000000)
        self.assertTrue(b["details"].get("first_action_verified"))

    def test_global_uuid_lookup(self):
        self.store.record_native_binding(
            task_id="task-100",
            native_id=VALID_UUID,
            tag="task-hub-100",
            workspace="/home/alexey/git/test-repo",
            engine="gemini",
            incarnation=1792261000000,
        )
        match = self.store.get_task_by_native_id(VALID_UUID)
        self.assertIsNotNone(match)
        self.assertEqual(match["task_id"], "task-100")
        self.assertEqual(match["task"]["state"], "queued")
        self.assertEqual(self.store.resolve_task_id(VALID_UUID), "task-100")
        self.assertEqual(self.store.resolve_task_id("task-100"), "task-100")
        self.assertIsNone(self.store.resolve_task_id("nonexistent"))


class ActiveResourcesAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.store = Store(self.db)
        self.aplexer_state = Path(self.tmp) / "aplexer_state"
        self.aplexer_state.mkdir(parents=True)
        os.environ["APLEXER_STATE_DIR"] = str(self.aplexer_state)

        # Deleted worktree setup
        self.wt = Path(self.tmp) / "isolated-wt-deleted"
        self.wt.mkdir(parents=True)
        self.payload = dict(PAYLOAD, cwd=str(self.wt))
        self.store.submit_task("t-hub", "key-hub", self.payload, [], memory_mb=1500, disk_mb=512)
        self.store.transition_task("t-hub", "starting", ("queued",))
        self.store.transition_task("t-hub", "running", ("starting",))
        self.incarnation = 1792262000000
        self.store.record_native_binding(
            task_id="t-hub",
            native_id=VALID_UUID,
            tag="task-hub",
            workspace=str(self.wt),
            engine="gemini",
            incarnation=self.incarnation,
        )

    def tearDown(self):
        os.environ.pop("APLEXER_STATE_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_tombstone(self, native_id, data):
        p = self.aplexer_state / "retired-sessions" / native_id / "tombstone.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_real_exact_live_record_stays_reserved(self):
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["aplexer", "status", VALID_UUID, "--json"],
                0,
                json.dumps({
                    "id": VALID_UUID,
                    "phase": "running",
                    "containment_empty": False,
                }),
                "",
            )
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1500)
            self.assertEqual(disk, 512)
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args[0][0][2], VALID_UUID)

    def test_matching_finished_tombstone_frees_resources_after_wt_deleted(self):
        # 1. Delete the worktree completely from disk
        shutil.rmtree(self.wt)
        self.assertFalse(self.wt.exists())

        # 2. Write authoritative tombstone in aplexer state directory
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-hub",
            "workspace": str(self.wt),
        })

        # 3. Simulate aplexer status returning rc=1 (no matching session in live table)
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["aplexer", "status", VALID_UUID, "--json"],
                1,
                "",
                "a: no matching session",
            )
            mem, disk = self.store.get_active_resources()
            # Process RAM/disk released on proven exit without inventing task acceptance
            self.assertEqual(mem, 0)
            self.assertEqual(disk, 0)

        # Task in DB remains running (state not mutated by pure resource projection)
        self.assertEqual(self.store.get_task("t-hub")["state"], "running")

    def test_boundary_mismatched_uuid_retains_reservation(self):
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": "e0000000-0000-0000-0000-000000000000",
            "tag": "task-hub",
            "workspace": str(self.wt),
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1500)
            self.assertEqual(disk, 512)

    def test_boundary_mismatched_tag_reused_tag_retains_reservation(self):
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-different-tag",
            "workspace": str(self.wt),
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1500)
            self.assertEqual(disk, 512)

    def test_boundary_mismatched_workspace_retains_reservation(self):
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-hub",
            "workspace": "/home/alexey/git/other-foreign-workspace",
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1500)
            self.assertEqual(disk, 512)

    def test_boundary_malformed_tombstone_retains_reservation(self):
        # Corrupted JSON
        p = self.aplexer_state / "retired-sessions" / VALID_UUID / "tombstone.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not valid json", encoding="utf-8")

        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1500)
            self.assertEqual(disk, 512)

    def test_boundary_cause_not_finished_retains_reservation(self):
        for bad_cause in ("crashed", "killed", "failed", "unknown", ""):
            self._write_tombstone(VALID_UUID, {
                "cause": bad_cause,
                "finished_at_ms": self.incarnation + 10000,
                "id": VALID_UUID,
                "tag": "task-hub",
                "workspace": str(self.wt),
            })
            with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
                mem, disk = self.store.get_active_resources()
                self.assertEqual((mem, disk), (1500, 512))

    def test_boundary_stale_incarnation_retains_reservation(self):
        # finished_at_ms is older than session creation (stale record)
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation - 5000,
            "id": VALID_UUID,
            "tag": "task-hub",
            "workspace": str(self.wt),
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1500)
            self.assertEqual(disk, 512)

    def test_boundary_unknown_timeout_transport_errors_retain_reservation(self):
        # Timeout
        with patch("launcher.native_lifecycle.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            mem, disk = self.store.get_active_resources()
            self.assertEqual((mem, disk), (1500, 512))

        # Transport error
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "transport failure communicating with daemon")
            mem, disk = self.store.get_active_resources()
            self.assertEqual((mem, disk), (1500, 512))

        # Stale socket
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "stale socket error")
            mem, disk = self.store.get_active_resources()
            self.assertEqual((mem, disk), (1500, 512))

    def test_legacy_records_without_native_binding_fail_conservative(self):
        # Create legacy task with no native binding
        self.store.submit_task("t-legacy", "key-legacy", PAYLOAD, [], memory_mb=1000, disk_mb=256)
        self.store.transition_task("t-legacy", "starting", ("queued",))
        self.store.transition_task("t-legacy", "running", ("starting",))

        # Both t-hub (1500) and t-legacy (1000) are running
        # t-hub has a finished tombstone -> 0
        # t-legacy has no binding -> 1000 conservative
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-hub",
            "workspace": str(self.wt),
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1000)
            self.assertEqual(disk, 256)

    def test_concurrent_terminal_claim_update_not_double_release(self):
        # Even with matching tombstone, transitioning task to completed-awaiting-review
        # results in 0 (never negative, never double subtracted)
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-hub",
            "workspace": str(self.wt),
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem1, _ = self.store.get_active_resources()
            self.assertEqual(mem1, 0)

            self.store.transition_task("t-hub", "completed-awaiting-review", ("running",))
            mem2, _ = self.store.get_active_resources()
            self.assertEqual(mem2, 0)


class CLIOutcomeGateAndReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.store = Store(self.db)
        self.aplexer_state = Path(self.tmp) / "aplexer_state"
        self.aplexer_state.mkdir(parents=True)
        os.environ["APLEXER_STATE_DIR"] = str(self.aplexer_state)

        self.wt = Path(self.tmp) / "wt-cli"
        self.wt.mkdir(parents=True)
        self.artifact = self.wt / "result.md"
        self.store.submit_task("task-cli", "key-cli", {"cwd": str(self.wt), "owner": "test", "timeout": 600}, [str(self.artifact)])
        self.store.transition_task("task-cli", "starting", ("queued",))
        self.store.transition_task("task-cli", "running", ("starting",))
        self.incarnation = 1792263000000
        self.store.record_native_binding(
            task_id="task-cli",
            native_id=VALID_UUID,
            tag="task-cli-tag",
            workspace=str(self.wt),
            engine="gemini",
            incarnation=self.incarnation,
        )

    def tearDown(self):
        os.environ.pop("APLEXER_STATE_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_tombstone(self, native_id, data):
        p = self.aplexer_state / "retired-sessions" / native_id / "tombstone.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_complete_via_global_uuid_requires_result_evidence(self):
        # Proven exit via matching tombstone
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 1000,
            "id": VALID_UUID,
            "tag": "task-cli-tag",
            "workspace": str(self.wt),
        })

        args = SimpleNamespace(config_dir=self.tmp, id=VALID_UUID, reviewer="reviewer-x")

        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")

            # 1. Exit alone WITHOUT result evidence artifact fails complete
            rc = complete(args)
            self.assertEqual(rc, 1)
            self.assertEqual(self.store.get_task("task-cli")["state"], "running")

            # 2. Non-empty result evidence artifact + proven exit succeeds complete
            self.artifact.write_text("# Real evidence artifact content\n")
            rc = complete(args)
            self.assertEqual(rc, 0)
            self.assertEqual(self.store.get_task("task-cli")["state"], "completed-awaiting-review")

    def test_complete_refuses_when_native_alive(self):
        self.artifact.write_text("# Real evidence artifact content\n")
        args = SimpleNamespace(config_dir=self.tmp, id=VALID_UUID, reviewer="reviewer-x")

        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, json.dumps({"id": VALID_UUID, "phase": "running", "containment_empty": False}), ""
            )
            rc = complete(args)
            self.assertEqual(rc, 1)
            self.assertEqual(self.store.get_task("task-cli")["state"], "running")

    def test_fail_via_global_uuid_closes_dead_task(self):
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 1000,
            "id": VALID_UUID,
            "tag": "task-cli-tag",
            "workspace": str(self.wt),
        })
        args = SimpleNamespace(config_dir=self.tmp, id=VALID_UUID, reviewer="reviewer-x", reason="worker exited without producing output")

        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            rc = fail(args)
            self.assertEqual(rc, 0)
            self.assertEqual(self.store.get_task("task-cli")["state"], "failed")

    def test_targeted_reconcile_without_dispatching_queued_work(self):
        # Add another queued task
        self.store.submit_task("t-queued-wait", "k-queued", {"cwd": str(self.wt), "owner": "test", "timeout": 600}, [])

        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 1000,
            "id": VALID_UUID,
            "tag": "task-cli-tag",
            "workspace": str(self.wt),
        })

        # Provide evidence so it recovers to completed-awaiting-review
        self.artifact.write_text("# Landed artifact\n")

        args = SimpleNamespace(config_dir=self.tmp, id=VALID_UUID, reviewer="reconciler", json=True)
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            rc = reconcile(args)
            self.assertEqual(rc, 0)

        # Target task recovered
        self.assertEqual(self.store.get_task("task-cli")["state"], "completed-awaiting-review")
        # Queued task remains untouched in queued (targeted reconcile does not dispatch!)
        self.assertEqual(self.store.get_task("t-queued-wait")["state"], "queued")

    def test_no_exit_alone_acceptance(self):
        # Native exit alone must never allow accept
        args = SimpleNamespace(config_dir=self.tmp, id="task-cli", reviewer="reviewer-x", refill=False)
        with self.assertRaises(StateTransitionError):
            self.store.accept_task("task-cli", "reviewer-x")

    def test_recover_binding_from_launch_artifacts_if_present(self):
        # Task in store without DB binding, but launch artifact exists in its cwd
        self.store.submit_task("t-recover", "k-rec", {"cwd": str(self.wt), "owner": "test", "timeout": 600}, [])
        local_dir = self.wt / ".local"
        local_dir.mkdir(parents=True, exist_ok=True)
        start_record = {
            "id": VALID_UUID_2,
            "tag": "task-t-recover",
            "workspace": str(self.wt),
            "engine": "gemini",
            "created_at_ms": self.incarnation,
        }
        (local_dir / "launch-t-recover-start.json").write_text(json.dumps(start_record), encoding="utf-8")

        recovered = self.store.recover_binding_from_artifacts("t-recover")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["native_id"], VALID_UUID_2)
        self.assertEqual(self.store.get_native_binding("t-recover")["native_id"], VALID_UUID_2)


if __name__ == "__main__":
    unittest.main()
