"""Exhaustive tests for authoritative native lifecycle and resource accounting recovery.

Verifies:
1. Exact native binding schema validation, persistence, and global UUID lookup.
2. Real exact live record stays reserved; exact identity matching (UUID, tag, workspace, engine, incarnation).
3. Active/starting phases always remain reserved even if containment_empty is true; truthy string cannot release.
4. Terminal phases require full identity and explicit boolean containment_empty is True; otherwise unknown.
5. Matching finished tombstone frees process RAM/disk reservation even after worktree is deleted.
6. Bounded regular nofollow file reading rejects symlinks, oversize files, and malformed JSON.
7. Workspace validation: missing workspace never resolves to cwd; must be absolute.
8. Immutability of native bindings: one UUID per task, no rebinding to different UUID or incarnation.
9. Submitted task matching: tag must match run_tag_for(task), workspace must match cwd, engine must match expected.
10. Compact details: raw env, argv, prompt, start_json are stripped.
11. Launch removes invented metadata: missing required observed fields leaves binding unknown conservatively.
12. Artifact recovery requires full observed start identity without fallbacks to provider, cwd, or timestamps.
13. End-to-end get_active_resources recovery without SQLite deadlock.
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
    read_bounded_json_file,
    read_tombstone,
    verify_tombstone,
    probe_native_lifecycle,
)
from launcher.store import Store, StateTransitionError
from launcher.tags import run_tag_for

VALID_UUID = "cd0e3245-4928-4d65-aadb-e9c199f71df8"
VALID_UUID_2 = "de9badb4-cb44-4762-ade2-7d4f8cd5864b"
VALID_UUID_3 = "8c6b7582-8419-482a-9e3f-67f082e66699"


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
            details={"start_json": {"phase": "running", "worker_pid": 4321, "env": {"SECRET": "raw"}}},
        )
        self.assertEqual(binding["task_id"], "t-1")
        self.assertEqual(binding["native_id"], VALID_UUID)
        self.assertEqual(binding["tag"], "task-t-1")
        self.assertEqual(binding["workspace"], str(Path("/tmp/test-workspace").resolve()))
        self.assertEqual(binding["engine"], "gemini")
        self.assertEqual(binding["incarnation"], 1792260000000)
        # Compact details: start_json extracted phase and worker_pid, env stripped!
        self.assertEqual(binding["details"]["start_phase"], "running")
        self.assertEqual(binding["details"]["worker_pid"], 4321)
        self.assertNotIn("env", binding["details"])

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

    def test_validate_native_binding_rejects_non_absolute_workspace(self):
        for bad_ws in ("relative/path", "", "   ", "wt-dir"):
            with self.subTest(bad_ws=bad_ws), self.assertRaises(ValueError):
                validate_native_binding(
                    task_id="t-1",
                    native_id=VALID_UUID,
                    tag="task-t-1",
                    workspace=bad_ws,
                    engine="gemini",
                    incarnation=1792260000000,
                )


class NativeStoreBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.store = Store(self.db)
        self.payload = {
            "goal": "implement reliability worker",
            "owner": "test-coordinator",
            "cwd": str(Path(self.tmp).resolve()),
            "timeout": 600,
        }
        self.store.submit_task("task-100", "key-100", self.payload, [])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_record_and_get_native_binding(self):
        self.store.record_native_binding(
            task_id="task-100",
            native_id=VALID_UUID,
            tag="task-100",
            workspace=str(Path(self.tmp).resolve()),
            engine="gemini",
            incarnation=1792261000000,
            details={"first_action_verified": True},
        )
        b = self.store.get_native_binding("task-100")
        self.assertIsNotNone(b)
        self.assertEqual(b["native_id"], VALID_UUID)
        self.assertEqual(b["tag"], "task-100")
        self.assertEqual(b["engine"], "gemini")
        self.assertEqual(b["incarnation"], 1792261000000)
        self.assertTrue(b["details"].get("first_action_verified"))

    def test_global_uuid_lookup(self):
        self.store.record_native_binding(
            task_id="task-100",
            native_id=VALID_UUID,
            tag="task-100",
            workspace=str(Path(self.tmp).resolve()),
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

    def test_record_native_binding_rejects_tag_mismatch(self):
        with self.assertRaises(ValueError) as ctx:
            self.store.record_native_binding(
                task_id="task-100",
                native_id=VALID_UUID,
                tag="mismatched-tag",
                workspace=str(Path(self.tmp).resolve()),
                engine="gemini",
                incarnation=1792261000000,
            )
        self.assertIn("does not match expected task tag", str(ctx.exception))

    def test_record_native_binding_rejects_workspace_mismatch(self):
        with self.assertRaises(ValueError) as ctx:
            self.store.record_native_binding(
                task_id="task-100",
                native_id=VALID_UUID,
                tag="task-100",
                workspace="/tmp/different-workspace",
                engine="gemini",
                incarnation=1792261000000,
            )
        self.assertIn("does not match task cwd", str(ctx.exception))

    def test_record_native_binding_rejects_engine_mismatch_when_known(self):
        self.store.submit_task(
            "task-engine-check", "key-eng",
            {"owner": "me", "cwd": str(Path(self.tmp).resolve()), "timeout": 60, "engine": "gemini"},
            []
        )
        with self.assertRaises(ValueError) as ctx:
            self.store.record_native_binding(
                task_id="task-engine-check",
                native_id=VALID_UUID_2,
                tag="task-engine-check",
                workspace=str(Path(self.tmp).resolve()),
                engine="opencode",
                incarnation=1792261000000,
            )
        self.assertIn("does not match expected engine", str(ctx.exception))

    def test_record_native_binding_one_uuid_per_task(self):
        self.store.record_native_binding(
            task_id="task-100",
            native_id=VALID_UUID,
            tag="task-100",
            workspace=str(Path(self.tmp).resolve()),
            engine="gemini",
            incarnation=1792261000000,
        )
        self.store.submit_task(
            "task-200", "key-200",
            {"owner": "me", "cwd": str(Path(self.tmp).resolve()), "timeout": 60},
            []
        )
        with self.assertRaises(ValueError) as ctx:
            self.store.record_native_binding(
                task_id="task-200",
                native_id=VALID_UUID,
                tag="task-200",
                workspace=str(Path(self.tmp).resolve()),
                engine="gemini",
                incarnation=1792261000000,
            )
        self.assertIn("already bound to task task-100", str(ctx.exception))

    def test_record_native_binding_immutable_identity(self):
        self.store.record_native_binding(
            task_id="task-100",
            native_id=VALID_UUID,
            tag="task-100",
            workspace=str(Path(self.tmp).resolve()),
            engine="gemini",
            incarnation=1792261000000,
        )
        # Attempting to rebind task-100 to a different UUID fails
        with self.assertRaises(ValueError) as ctx:
            self.store.record_native_binding(
                task_id="task-100",
                native_id=VALID_UUID_2,
                tag="task-100",
                workspace=str(Path(self.tmp).resolve()),
                engine="gemini",
                incarnation=1792261000000,
            )
        self.assertIn("cannot rebind", str(ctx.exception))

        # Attempting to rebind task-100 to a different incarnation fails
        with self.assertRaises(ValueError) as ctx:
            self.store.record_native_binding(
                task_id="task-100",
                native_id=VALID_UUID,
                tag="task-100",
                workspace=str(Path(self.tmp).resolve()),
                engine="gemini",
                incarnation=1792269999999,
            )
        self.assertIn("cannot rebind", str(ctx.exception))

        # Re-recording same identity updates/merges details cleanly
        self.store.record_native_binding(
            task_id="task-100",
            native_id=VALID_UUID,
            tag="task-100",
            workspace=str(Path(self.tmp).resolve()),
            engine="gemini",
            incarnation=1792261000000,
            details={"step": 2},
        )
        b = self.store.get_native_binding("task-100")
        self.assertEqual(b["details"].get("step"), 2)


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
        self.payload = {
            "goal": "implement reliability worker",
            "owner": "test-coordinator",
            "cwd": str(self.wt),
            "timeout": 600,
        }
        self.store.submit_task("t-hub", "key-hub", self.payload, [], memory_mb=1500, disk_mb=512)
        self.store.transition_task("t-hub", "starting", ("queued",))
        self.store.transition_task("t-hub", "running", ("starting",))
        self.incarnation = 1792262000000
        self.store.record_native_binding(
            task_id="t-hub",
            native_id=VALID_UUID,
            tag="task-t-hub",
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
                    "tag": "task-t-hub",
                    "workspace": str(self.wt),
                    "engine": "gemini",
                    "created_at_ms": self.incarnation,
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

    def test_live_oracle_active_starting_always_reserved_even_if_containment_empty_true(self):
        for phase in ("running", "starting", "working", "launching"):
            with self.subTest(phase=phase), patch("launcher.native_lifecycle.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    ["aplexer", "status", VALID_UUID, "--json"],
                    0,
                    json.dumps({
                        "id": VALID_UUID,
                        "tag": "task-t-hub",
                        "workspace": str(self.wt),
                        "engine": "gemini",
                        "created_at_ms": self.incarnation,
                        "phase": phase,
                        "containment_empty": True,
                    }),
                    "",
                )
                mem, disk = self.store.get_active_resources()
                self.assertEqual(mem, 1500)
                self.assertEqual(disk, 512)

    def test_live_oracle_terminal_requires_explicit_containment_empty_is_true(self):
        # Case 1: containment_empty is explicitly boolean True -> frees resources!
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["aplexer", "status", VALID_UUID, "--json"],
                0,
                json.dumps({
                    "id": VALID_UUID,
                    "tag": "task-t-hub",
                    "workspace": str(self.wt),
                    "engine": "gemini",
                    "created_at_ms": self.incarnation,
                    "phase": "exited",
                    "containment_empty": True,
                }),
                "",
            )
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 0)
            self.assertEqual(disk, 0)

        # Case 2: containment_empty is False -> remains reserved (conservative unknown)
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["aplexer", "status", VALID_UUID, "--json"],
                0,
                json.dumps({
                    "id": VALID_UUID,
                    "tag": "task-t-hub",
                    "workspace": str(self.wt),
                    "engine": "gemini",
                    "created_at_ms": self.incarnation,
                    "phase": "exited",
                    "containment_empty": False,
                }),
                "",
            )
            mem, disk = self.store.get_active_resources()
            self.assertEqual((mem, disk), (1500, 512))

        # Case 3: containment_empty is string "false" or "true" -> cannot release!
        for bad_val in ("false", "true", None, 0, 1):
            with self.subTest(bad_val=bad_val), patch("launcher.native_lifecycle.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    ["aplexer", "status", VALID_UUID, "--json"],
                    0,
                    json.dumps({
                        "id": VALID_UUID,
                        "tag": "task-t-hub",
                        "workspace": str(self.wt),
                        "engine": "gemini",
                        "created_at_ms": self.incarnation,
                        "phase": "exited",
                        "containment_empty": bad_val,
                    }),
                    "",
                )
                mem, disk = self.store.get_active_resources()
                self.assertEqual((mem, disk), (1500, 512))

    def test_live_oracle_mismatches_retain_reservation(self):
        valid_status = {
            "id": VALID_UUID,
            "tag": "task-t-hub",
            "workspace": str(self.wt),
            "engine": "gemini",
            "created_at_ms": self.incarnation,
            "phase": "exited",
            "containment_empty": True,
        }

        # Subtest each identity mismatch
        mismatches = [
            ("id", VALID_UUID_2),
            ("tag", "task-wrong-tag"),
            ("workspace", "/tmp/wrong-ws"),
            ("workspace", "relative/ws"),
            ("engine", "opencode"),
            ("created_at_ms", self.incarnation + 1000),
            ("created_at_ms", "bad-inc"),
        ]

        for field, bad_val in mismatches:
            status_copy = dict(valid_status)
            status_copy[field] = bad_val
            with self.subTest(field=field, bad_val=bad_val), patch("launcher.native_lifecycle.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    ["aplexer", "status", VALID_UUID, "--json"],
                    0,
                    json.dumps(status_copy),
                    "",
                )
                mem, disk = self.store.get_active_resources()
                self.assertEqual((mem, disk), (1500, 512))

    def test_matching_finished_tombstone_frees_resources_after_wt_deleted(self):
        # 1. Delete the worktree completely from disk
        shutil.rmtree(self.wt)
        self.assertFalse(self.wt.exists())

        # 2. Write authoritative tombstone in aplexer state directory
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-t-hub",
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
            self.assertEqual(mem, 0)
            self.assertEqual(disk, 0)

        # Task in DB remains running (state not mutated by pure resource projection)
        self.assertEqual(self.store.get_task("t-hub")["state"], "running")

    def test_boundary_mismatched_uuid_retains_reservation(self):
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": "e0000000-0000-0000-0000-000000000000",
            "tag": "task-t-hub",
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
            "tag": "task-t-hub",
            "workspace": "/home/alexey/git/other-foreign-workspace",
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1500)
            self.assertEqual(disk, 512)

    def test_boundary_tombstone_missing_workspace_never_resolves_cwd(self):
        # Empty/missing workspace in tombstone must NOT resolve to cwd
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-t-hub",
            "workspace": "",
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual((mem, disk), (1500, 512))

    def test_boundary_tombstone_symlink_rejected(self):
        # Symlink tombstone must be rejected by bounded nofollow read
        tombstone_dir = self.aplexer_state / "retired-sessions" / VALID_UUID
        tombstone_dir.mkdir(parents=True, exist_ok=True)
        real_target = self.aplexer_state / "target_tombstone.json"
        real_target.write_text(json.dumps({
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-t-hub",
            "workspace": str(self.wt),
        }))
        symlink_path = tombstone_dir / "tombstone.json"
        symlink_path.symlink_to(real_target)

        # read_tombstone rejects symlinks
        self.assertIsNone(read_tombstone(VALID_UUID, state_dir=self.aplexer_state))

        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual((mem, disk), (1500, 512))

    def test_boundary_tombstone_oversize_rejected(self):
        p = self.aplexer_state / "retired-sessions" / VALID_UUID / "tombstone.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write file larger than 64KB
        big_data = {"cause": "finished", "finished_at_ms": self.incarnation + 10000, "id": VALID_UUID, "tag": "task-t-hub", "workspace": str(self.wt), "padding": "x" * 70000}
        p.write_text(json.dumps(big_data), encoding="utf-8")

        self.assertIsNone(read_tombstone(VALID_UUID, state_dir=self.aplexer_state))

    def test_boundary_malformed_tombstone_retains_reservation(self):
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
                "tag": "task-t-hub",
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
            "tag": "task-t-hub",
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
        self.store.submit_task("t-legacy", "key-legacy", self.payload, [], memory_mb=1000, disk_mb=256)
        self.store.transition_task("t-legacy", "starting", ("queued",))
        self.store.transition_task("t-legacy", "running", ("starting",))

        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-t-hub",
            "workspace": str(self.wt),
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem, disk = self.store.get_active_resources()
            self.assertEqual(mem, 1000)
            self.assertEqual(disk, 256)

    def test_concurrent_terminal_claim_update_not_double_release(self):
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 10000,
            "id": VALID_UUID,
            "tag": "task-t-hub",
            "workspace": str(self.wt),
        })
        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            mem1, _ = self.store.get_active_resources()
            self.assertEqual(mem1, 0)

            self.store.transition_task("t-hub", "completed-awaiting-review", ("running",))
            mem2, _ = self.store.get_active_resources()
            self.assertEqual(mem2, 0)

    def test_get_active_resources_e2e_recovery_no_deadlock(self):
        # End-to-end test: get_active_resources triggers recover_binding_from_artifacts
        # without nested read cursor / BEGIN EXCLUSIVE deadlock!
        e2e_wt = Path(self.tmp) / "e2e-wt"
        e2e_wt.mkdir(parents=True)
        self.store.submit_task("t-e2e", "key-e2e", {"owner": "me", "cwd": str(e2e_wt), "timeout": 60}, [], memory_mb=1200, disk_mb=300)
        self.store.transition_task("t-e2e", "starting", ("queued",))
        self.store.transition_task("t-e2e", "running", ("starting",))

        # Write launch start artifact in e2e_wt/.local
        local_dir = e2e_wt / ".local"
        local_dir.mkdir(parents=True)
        start_doc = {
            "id": VALID_UUID_3,
            "tag": "task-t-e2e",
            "workspace": str(e2e_wt),
            "engine": "gemini",
            "created_at_ms": self.incarnation,
        }
        (local_dir / "launch-t-e2e-start.json").write_text(json.dumps(start_doc))

        # Write finished tombstone for VALID_UUID_3
        self._write_tombstone(VALID_UUID_3, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 5000,
            "id": VALID_UUID_3,
            "tag": "task-t-e2e",
            "workspace": str(e2e_wt),
        })

        # Tombstone for t-hub
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 5000,
            "id": VALID_UUID,
            "tag": "task-t-hub",
            "workspace": str(self.wt),
        })

        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            # Must run without sqlite deadlock and reconcile resources to 0
            mem, disk = self.store.get_active_resources()
            self.assertEqual((mem, disk), (0, 0))

        # Binding for t-e2e should now be durably persisted!
        binding = self.store.get_native_binding("t-e2e")
        self.assertIsNotNone(binding)
        self.assertEqual(binding["native_id"], VALID_UUID_3)


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
            tag="task-cli",
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
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 1000,
            "id": VALID_UUID,
            "tag": "task-cli",
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
                [], 0, json.dumps({
                    "id": VALID_UUID,
                    "tag": "task-cli",
                    "workspace": str(self.wt),
                    "engine": "gemini",
                    "created_at_ms": self.incarnation,
                    "phase": "running",
                    "containment_empty": False,
                }), ""
            )
            rc = complete(args)
            self.assertEqual(rc, 1)
            self.assertEqual(self.store.get_task("task-cli")["state"], "running")

    def test_fail_via_global_uuid_closes_dead_task(self):
        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 1000,
            "id": VALID_UUID,
            "tag": "task-cli",
            "workspace": str(self.wt),
        })
        args = SimpleNamespace(config_dir=self.tmp, id=VALID_UUID, reviewer="reviewer-x", reason="worker exited without producing output")

        with patch("launcher.native_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "no matching session")
            rc = fail(args)
            self.assertEqual(rc, 0)
            self.assertEqual(self.store.get_task("task-cli")["state"], "failed")

    def test_targeted_reconcile_without_dispatching_queued_work(self):
        self.store.submit_task("t-queued-wait", "k-queued", {"cwd": str(self.wt), "owner": "test", "timeout": 600}, [])

        self._write_tombstone(VALID_UUID, {
            "cause": "finished",
            "finished_at_ms": self.incarnation + 1000,
            "id": VALID_UUID,
            "tag": "task-cli",
            "workspace": str(self.wt),
        })

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
        args = SimpleNamespace(config_dir=self.tmp, id="task-cli", reviewer="reviewer-x", refill=False)
        with self.assertRaises(StateTransitionError):
            self.store.accept_task("task-cli", "reviewer-x")

    def test_recover_binding_from_launch_artifacts_if_present(self):
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

    def test_recover_binding_refuses_fallbacks_when_fields_missing(self):
        self.store.submit_task("t-fallback-refuse", "k-fb", {"cwd": str(self.wt), "owner": "test", "timeout": 60}, [])
        local_dir = self.wt / ".local"
        local_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = local_dir / "launch-t-fallback-refuse-start.json"

        # Missing engine (must NOT fallback to gemini or provider)
        artifact_path.write_text(json.dumps({
            "id": VALID_UUID_2,
            "tag": "task-t-fallback-refuse",
            "workspace": str(self.wt),
            "created_at_ms": self.incarnation,
        }))
        self.assertIsNone(self.store.recover_binding_from_artifacts("t-fallback-refuse"))

        # Missing workspace (must NOT fallback to cwd)
        artifact_path.write_text(json.dumps({
            "id": VALID_UUID_2,
            "tag": "task-t-fallback-refuse",
            "engine": "gemini",
            "created_at_ms": self.incarnation,
        }))
        self.assertIsNone(self.store.recover_binding_from_artifacts("t-fallback-refuse"))

        # Missing created_at_ms (must NOT fallback to started_at_ts)
        artifact_path.write_text(json.dumps({
            "id": VALID_UUID_2,
            "tag": "task-t-fallback-refuse",
            "workspace": str(self.wt),
            "engine": "gemini",
        }))
        self.assertIsNone(self.store.recover_binding_from_artifacts("t-fallback-refuse"))

        # Symlink artifact rejected
        artifact_path.unlink()
        target = self.wt / "target.json"
        target.write_text(json.dumps({
            "id": VALID_UUID_2,
            "tag": "task-t-fallback-refuse",
            "workspace": str(self.wt),
            "engine": "gemini",
            "created_at_ms": self.incarnation,
        }))
        artifact_path.symlink_to(target)
        self.assertIsNone(self.store.recover_binding_from_artifacts("t-fallback-refuse"))


if __name__ == "__main__":
    unittest.main()
