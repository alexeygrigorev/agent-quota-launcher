import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher.launch import (validate_first_action, build_adapter_argv,
                             ADAPTERS, _kill_process_group, _bounded_timeout)
from launcher.store import Store

PAYLOAD = {"goal": "write report", "owner": "head-x", "cwd": ".",
           "timeout": 600,
           "model_requirements": {"providers": ["zai"], "models": ["glm-5.3-flash"]}}

class TestFirstActionValidator(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fa = Path(self.dir) / "first-action-t1.json"
        self.min_mtime = time.time() - 60
        # The wrapper start record captured at launch (aplexer start --json):
        # rich, with engine/wrapper keys. Identity fields pin the session.
        self.start = {"schema_version": 1, "id": "sess-1", "tag": "task-t1",
                      "workspace": "/repo", "parent_session": "parent-1",
                      "engine": "zcodex", "phase": "starting",
                      "command": ["zcodex", "exec"],
                      "created_at_ms": 1791116912591}
        self.now_ms = self.start["created_at_ms"] + 60_000

    def write(self, obj):
        self.fa.write_text(json.dumps(obj))

    def check(self):
        return validate_first_action(str(self.fa), self.start, self.min_mtime,
                                     now_ms=self.now_ms)

    def rich_whoami(self):
        # What a native `aplexer whoami --json` actually emits: identity plus
        # command/phase/parent_session/schema_version etc.
        return dict(self.start, phase="running",
                    created_at_ms=self.start["created_at_ms"] + 5000,
                    updated_at_ms=self.start["created_at_ms"] + 9000,
                    worker_pid=1234,
                    boot_id="edbec548-453f-4111-b38e-e7c16d12aa93",
                    agent="grok",
                    state="running",
                    pids_current=12,
                    memory_current=198983680,
                    systemd_unit="aplexer-workload-sess-1.scope")

    def test_rich_native_whoami_accepted(self):
        # command/phase/parent_session/schema_version keys are expected on a
        # genuine whoami and must NOT be blacklisted (Principal C1450).
        self.write(self.rich_whoami())
        self.assertTrue(self.check())

    def test_minimal_identity_plus_timestamp_rejected(self):
        # 4-key identity card lacks execution state (command, phase, schema_version, worker_pid)
        self.write({"id": "sess-1", "tag": "task-t1", "workspace": "/repo",
                    "timestamp": "2026-10-04T12:29:00Z"})
        self.assertFalse(self.check())

    def test_start_record_phase_running_with_worker_pid_and_dummy_rejected(self):
        # Start record with phase flipped to running, worker_pid, and dummy=123 must FAIL
        self.write(dict(self.start, phase="running", worker_pid=1234, dummy=123,
                        timestamp="2026-10-04T12:29:00Z"))
        self.assertFalse(self.check())

    def test_start_record_phase_running_with_worker_pid_no_live_time_rejected(self):
        # Start record with phase flipped to running and worker_pid, but no genuine live time progression must FAIL
        self.write(dict(self.start, phase="running", worker_pid=1234,
                        timestamp="2026-10-04T12:29:00Z"))
        self.assertFalse(self.check())

    def test_start_record_copy_rejected(self):
        # Byte-identical wrapper start JSON is not a first action...
        self.fa.write_text(json.dumps(self.start))
        self.assertFalse(self.check())

    def test_reformatted_start_record_copy_rejected(self):
        # ...and neither is the same record re-serialized with other whitespace.
        self.fa.write_text(json.dumps(self.start, indent=2, sort_keys=True))
        self.assertFalse(self.check())

    def test_start_record_plus_timestamp_rejected(self):
        # Laundering: the wrapper start JSON with nothing but an added
        # timestamp is still the wrapper's own record, not an agent action
        # (head C1538: identity match is not tool provenance).
        self.fa.write_text(json.dumps(dict(self.start, timestamp="2026-10-04T12:29:00Z")))
        self.assertFalse(self.check())

    def test_start_record_plus_ms_fields_rejected(self):
        # Same laundering with epoch-ms time fields instead of an ISO stamp.
        self.fa.write_text(json.dumps(dict(
            self.start, created_at_ms=self.start["created_at_ms"] + 5000)))
        self.assertFalse(self.check())

    def test_start_record_plus_dummy_rejected(self):
        # A start-record superset with arbitrary extra keys must fail.
        self.write(dict(self.start, dummy=123))
        self.assertFalse(self.check())

    def test_start_record_plus_dummy_and_timestamp_rejected(self):
        # Start record + dummy key + valid timestamp must still fail.
        self.write(dict(self.start, dummy=123, timestamp="2026-10-04T12:29:00Z"))
        self.assertFalse(self.check())

    def test_start_record_plus_invented_live_fields_with_phase_starting_rejected(self):
        # Invented worker_pid/last_activity with all original start keys unchanged
        # including phase=starting is a start-record clone and must FAIL.
        self.write(dict(self.start, timestamp="2026-10-04T12:29:00Z",
                        last_activity_ms=self.start["created_at_ms"] + 8000,
                        worker_pid=4242))
        self.assertFalse(self.check())

    def test_positive_pin_real_whoami_fixture_accepted(self):
        # Full rich native whoami fixture (modeled on live aplexer whoami --json):
        # phase is running, updated_at_ms > created_at_ms, worker_pid and
        # session containment keys present.
        fixture = {
            "schema_version": 1,
            "id": self.start["id"],
            "workspace": self.start["workspace"],
            "tag": self.start["tag"],
            "engine": self.start["engine"],
            "command": list(self.start["command"]),
            "parent_session": self.start["parent_session"],
            "cwd": self.start["workspace"],
            "env": {},
            "limits": {"memory_bytes": 1572864000, "pids": 100},
            "history_bytes": 4194304,
            "created_at_ms": self.start["created_at_ms"],
            "updated_at_ms": self.start["created_at_ms"] + 5000,
            "last_activity_ms": self.start["created_at_ms"] + 4500,
            "reported_state": "working",
            "phase": "running",
            "worker_pid": 560806,
            "workload_pid": 560857,
            "containment_empty": False,
            "socket_path": f"/run/user/1000/aplexer/sessions/{self.start['id']}/control.sock",
            "history_path": f"/home/alexey/.local/state/aplexer/sessions/{self.start['id']}/history.bin",
        }
        self.write(fixture)
        self.assertTrue(self.check())

    def test_naive_iso_timestamp_rejected(self):
        # No timezone: no provable instant (in-window naive timestamp).
        self.write(dict(self.rich_whoami(), timestamp="2026-10-04T12:29:00"))
        self.assertFalse(self.check())

    def test_timestamp_stale_vs_launch_rejected(self):
        # Before the launched session's own creation: impossible for a
        # genuine whoami taken after start.
        self.write(dict(self.rich_whoami(),
                        timestamp="2026-10-04T11:00:00Z"))  # 1h+ before start ms
        self.assertFalse(self.check())

    def test_timestamp_far_future_rejected(self):
        self.write(dict(self.rich_whoami(), timestamp="2026-10-04T13:30:00Z"))
        # now_ms = start + 60s; 13:30Z is far ahead -> fabricated
        self.assertFalse(self.check())

    def test_wrong_id_rejected(self):
        self.write(dict(self.rich_whoami(), id="sess-2"))
        self.assertFalse(self.check())

    def test_wrong_tag_rejected(self):
        self.write(dict(self.rich_whoami(), tag="task-other"))
        self.assertFalse(self.check())

    def test_wrong_workspace_rejected(self):
        self.write(dict(self.rich_whoami(), workspace="/other"))
        self.assertFalse(self.check())

    def test_parent_session_mismatch_rejected(self):
        self.write(dict(self.rich_whoami(), parent_session="forged-parent"))
        self.assertFalse(self.check())

    def test_parent_session_absent_still_accepted(self):
        data = self.rich_whoami()
        del data["parent_session"]
        self.write(data)
        self.assertTrue(self.check())

    def test_no_time_field_rejected(self):
        data = self.rich_whoami()
        for field in ("timestamp", "created_at_ms", "updated_at_ms"):
            data.pop(field, None)
        self.write(data)
        self.assertFalse(self.check())

    def test_unparsable_timestamp_rejected(self):
        self.write(dict(self.rich_whoami(), timestamp="not-a-time"))
        self.assertFalse(self.check())

    def test_created_before_session_rejected(self):
        self.write(dict(self.rich_whoami(),
                        created_at_ms=self.start["created_at_ms"] - 60_000))
        self.assertFalse(self.check())

    def test_far_future_created_rejected(self):
        self.write(dict(self.rich_whoami(),
                        created_at_ms=self.now_ms + 3_600_000))
        self.assertFalse(self.check())

    def test_missing_file_rejected(self):
        self.assertFalse(self.check())

    def test_stale_file_rejected(self):
        # A pre-existing artifact from before the launch must not satisfy it.
        self.write(self.rich_whoami())
        old = time.time() - 3600
        os.utime(self.fa, (old, old))
        self.assertFalse(validate_first_action(str(self.fa), self.start,
                                               time.time() - 60, now_ms=self.now_ms))

class TestAdapters(unittest.TestCase):
    def test_grok_argv_exact(self):
        argv = build_adapter_argv("grok", "goal text")
        self.assertEqual(argv, ["/home/alexey/.local/bin/grok", "--model",
                                "grok-4.6", "--effort", "high",
                                "--permission-mode", "auto", "-p", "goal text"])

    def test_antigravity_strips_api_keys_and_sets_print_timeout(self):
        argv = build_adapter_argv("antigravity", "goal")
        self.assertEqual(argv[0], "/usr/bin/env")
        self.assertIn("-u", argv)
        self.assertEqual(argv[argv.index("-u") + 1], "GEMINI_API_KEY")
        self.assertIn("GOOGLE_API_KEY", argv)
        self.assertIn("/home/alexey/.local/bin/agy", argv)
        self.assertNotIn("agy", argv)
        self.assertLess(argv.index("GEMINI_API_KEY"), argv.index("/home/alexey/.local/bin/agy"))
        self.assertLess(argv.index("GOOGLE_API_KEY"), argv.index("/home/alexey/.local/bin/agy"))
        self.assertIn("--print-timeout", argv)
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "0")
        self.assertIn("-p", argv)
        self.assertLess(argv.index("--print-timeout"), argv.index("-p"))
        self.assertEqual(argv[argv.index("-p") + 1], "goal")
        self.assertNotIn("sh", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.1-pro-high")

    def test_antigravity_supports_model_override(self):
        argv = build_adapter_argv("antigravity", "goal", model="gemini-3.8-flash-high")
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")

    def test_zai_uses_exact_zcodex_invocation(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertEqual(argv[0], "/home/alexey/.local/bin/zcodex")
        self.assertEqual(argv[1:4], ["exec", "--model", "glm-5.3-flash"])
        self.assertIn("-s", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--json", argv)
        self.assertEqual(ADAPTERS["zai"]["env"].get("ZCODE_CJS"),
                         "/opt/ZCode/resources/glm/zcode.cjs")
        self.assertEqual(argv[-1], "goal")

    def test_zai_sandbox_workspace_write_enforced(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertIn("-s", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_zai_no_ask_for_approval_in_exec(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertNotIn("-a", argv)
        self.assertNotIn("--ask-for-approval", argv)

    def test_codex_never_appears_as_adapter(self):
        self.assertNotIn("codex", ADAPTERS)
        with self.assertRaisesRegex(ValueError, "Unsupported provider"):
            build_adapter_argv("codex", "goal")

class TestUnsubmittedId(unittest.TestCase):
    def test_unsubmitted_id_fails_before_any_dispatch(self):
        fd, db = tempfile.mkstemp()
        os.close(fd)
        try:
            with patch("launcher.launch.fetch_quse") as fq:
                fq.side_effect = AssertionError("quota fetched for unsubmitted id")
                from launcher.launch import do_run
                with self.assertRaisesRegex(ValueError, "unsubmitted id"):
                    do_run(db, "missing-task", "/repo", "/repo/.local/tmp",
                           "/repo/.local/launch.lock")
                fq.assert_not_called()
        finally:
            os.remove(db)

class TestTimeoutEnforcement(unittest.TestCase):
    def test_process_group_killed_on_deadline(self):
        # A real process group: shell + child sleep; both must die.
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        time.sleep(0.2)
        _kill_process_group(proc, grace=1.0)
        self.assertLess(proc.returncode, 0)
        with self.assertRaises(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), 0)

    def test_bounded_timeout_validation(self):
        self.assertEqual(_bounded_timeout({"timeout": 600}), 600.0)
        with self.assertRaisesRegex(ValueError, "outside bounded range"):
            _bounded_timeout({"timeout": 10})
        with self.assertRaisesRegex(ValueError, "must be a number"):
            _bounded_timeout({"timeout": None})

class TestDoRunWaitLoop(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        (Path(self.dir) / ".local").mkdir()
        self.db_path = Path(self.dir) / "store.db"
        self.store = Store(str(self.db_path))
        self.task_id = "task1"
        self.store.submit_task(self.task_id, "ikey", {"owner": "me", "cwd": self.dir, "timeout": 60}, [self.dir])
        self.lock_path = str(Path(self.dir) / "launch.lock")

    @patch("launcher.launch.fetch_quse")
    @patch("launcher.launch.validate_quse")
    @patch("launcher.launch.select_candidate")
    @patch("launcher.launch.check_resources")
    @patch("launcher.launch.subprocess.Popen")
    @patch("launcher.launch.native_status")
    @patch("launcher.launch.validate_first_action")
    @patch("launcher.launch.time.sleep")
    def test_early_termination_breaks_out_without_sleeping(self, mock_sleep, mock_vfa, mock_ns, mock_popen, mock_cr, mock_sc, mock_vq, mock_fq):
        mock_vq.return_value = (["grok"], [])
        mock_sc.return_value = ({"provider": "grok"}, "test")
        
        proc_mock = mock_popen.return_value
        proc_mock.returncode = 0
        proc_mock.communicate.return_value = ('{"id": "s1", "tag": "t1", "workspace": "/w"}', "")
        
        mock_vfa.return_value = False
        mock_ns.return_value = ("dead", "process exited")
        
        from launcher.launch import do_run
        
        res = do_run(str(self.db_path), self.task_id, self.dir, None, self.lock_path)
        
        self.assertEqual(res, 1)
        mock_sleep.assert_not_called()
        self.assertEqual(mock_ns.call_count, 1)
        
        task = self.store.get_task(self.task_id)
        self.assertEqual(task["state"], "failed")

    @patch("launcher.launch.fetch_quse")
    @patch("launcher.launch.validate_quse")
    @patch("launcher.launch.select_candidate")
    @patch("launcher.launch.check_resources")
    @patch("launcher.launch.subprocess.Popen")
    @patch("launcher.launch.native_status")
    @patch("launcher.launch.validate_first_action")
    @patch("launcher.launch.time.sleep")
    def test_state_transition_collision_handled(self, mock_sleep, mock_vfa, mock_ns, mock_popen, mock_cr, mock_sc, mock_vq, mock_fq):
        mock_vq.return_value = (["grok"], [])
        mock_sc.return_value = ({"provider": "grok"}, "test")
        
        proc_mock = mock_popen.return_value
        proc_mock.returncode = 0
        proc_mock.communicate.return_value = ('{"id": "s1", "tag": "t1", "workspace": "/w"}', "")
        
        mock_vfa.return_value = False
        
        def side_effect_ns(*args, **kwargs):
            self.store.transition_task(self.task_id, "failed", ("starting",), reason="watcher")
            return ("dead", "process exited")
            
        mock_ns.side_effect = side_effect_ns
        
        from launcher.launch import do_run
        
        res = do_run(str(self.db_path), self.task_id, self.dir, None, self.lock_path)
        self.assertEqual(res, 1)
        task = self.store.get_task(self.task_id)
        self.assertEqual(task["state"], "failed")

if __name__ == '__main__':
    unittest.main()
