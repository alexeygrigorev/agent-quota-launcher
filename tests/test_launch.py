import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from launcher.launch import (validate_first_action, build_adapter_argv,
                             ADAPTERS, _kill_process_group, _bounded_timeout,
                             ZCODE_NODE, ZCODE_CJS, ZCODEX_BIN,
                             validate_zai_runtime, ZaiRuntimeError,
                             ZaiRuntimeMissingError, ZaiRuntimeUnexecutableError)
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
        self.assertEqual(argv[0], "/usr/bin/env")
        self.assertIn(f"ZCODE_NODE={ZCODE_NODE}", argv)
        self.assertIn(f"ZCODE_CJS={ZCODE_CJS}", argv)
        z_idx = argv.index(ZCODEX_BIN)
        self.assertEqual(argv[z_idx + 1:z_idx + 4], ["exec", "--model", "glm-5.3-flash"])
        self.assertIn("-s", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--json", argv)
        self.assertEqual(ADAPTERS["zai"]["env"].get("ZCODE_CJS"),
                         ZCODE_CJS)
        self.assertEqual(ADAPTERS["zai"]["env"].get("ZCODE_NODE"),
                         ZCODE_NODE)
        self.assertEqual(argv[-1], "goal")

    def test_zai_adapter_argv_exact_propagation(self):
        argv = build_adapter_argv("zai", "task payload")
        expected = [
            "/usr/bin/env",
            f"ZCODE_NODE={ZCODE_NODE}",
            f"ZCODE_CJS={ZCODE_CJS}",
            ZCODEX_BIN,
            "exec",
            "--model",
            "glm-5.3-flash",
            "-s",
            "workspace-write",
            "-c",
            "check_for_update_on_startup=false",
            "--json",
            "task payload",
        ]
        self.assertEqual(argv, expected)

    def test_zai_supports_model_override(self):
        argv = build_adapter_argv("zai", "goal", model="custom-zai-model")
        self.assertEqual(argv[argv.index("--model") + 1], "custom-zai-model")

    def test_zai_sandbox_workspace_write_enforced(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertIn("-s", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_zai_no_ask_for_approval_in_exec(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertNotIn("-a", argv)
        self.assertNotIn("--ask-for-approval", argv)

    def test_zai_runtime_missing_node_fails_closed(self):
        with patch("launcher.launch.ZCODE_NODE", "/tmp/nonexistent-node-binary"):
            with self.assertRaises(ZaiRuntimeError) as ctx:
                build_adapter_argv("zai", "goal")
            self.assertIn("node runtime missing", str(ctx.exception).lower())
            self.assertTrue(isinstance(ctx.exception, RuntimeError))
            self.assertTrue(isinstance(ctx.exception, FileNotFoundError))

    def test_zai_runtime_unexecutable_node_fails_closed(self):
        with tempfile.NamedTemporaryFile(mode="w") as f:
            f.write("#!/bin/sh\n")
            f.flush()
            os.chmod(f.name, 0o644)  # non-executable
            with patch("launcher.launch.ZCODE_NODE", f.name):
                with self.assertRaises(ZaiRuntimeError) as ctx:
                    build_adapter_argv("zai", "goal")
                self.assertIn("not executable", str(ctx.exception).lower())
                self.assertTrue(isinstance(ctx.exception, RuntimeError))
                self.assertTrue(isinstance(ctx.exception, PermissionError))

    def test_zai_runtime_missing_cjs_fails_closed(self):
        with patch("launcher.launch.ZCODE_CJS", "/tmp/nonexistent-zcode.cjs"):
            with self.assertRaises(ZaiRuntimeError) as ctx:
                build_adapter_argv("zai", "goal")
            self.assertIn("cjs runtime missing", str(ctx.exception).lower())
            self.assertTrue(isinstance(ctx.exception, RuntimeError))
            self.assertTrue(isinstance(ctx.exception, FileNotFoundError))

    def test_zai_runtime_unreadable_cjs_fails_closed(self):
        real_access = os.access

        def mock_access(path, mode, *args, **kwargs):
            if path == "/tmp/test-unreadable.cjs" and mode & os.R_OK:
                return False
            return real_access(path, mode, *args, **kwargs)

        with patch("os.path.isfile", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch("os.access", side_effect=mock_access), \
             patch("launcher.launch.ZCODE_CJS", "/tmp/test-unreadable.cjs"):
            with self.assertRaises(ZaiRuntimeError) as ctx:
                build_adapter_argv("zai", "goal")
            self.assertIn("not readable", str(ctx.exception).lower())
            self.assertTrue(isinstance(ctx.exception, RuntimeError))

    def test_zai_runtime_missing_zcodex_bin_fails_closed(self):
        with patch("launcher.launch.ZCODEX_BIN", "/tmp/nonexistent-zcodex"):
            with self.assertRaises(ZaiRuntimeError) as ctx:
                build_adapter_argv("zai", "goal")
            self.assertIn("zcodex binary missing", str(ctx.exception).lower())
            self.assertTrue(isinstance(ctx.exception, RuntimeError))
            self.assertTrue(isinstance(ctx.exception, FileNotFoundError))

    def test_zai_runtime_unexecutable_zcodex_bin_fails_closed(self):
        with tempfile.NamedTemporaryFile(mode="w") as f:
            f.write("#!/bin/sh\n")
            f.flush()
            os.chmod(f.name, 0o644)  # non-executable
            with patch("launcher.launch.ZCODEX_BIN", f.name):
                with self.assertRaises(ZaiRuntimeError) as ctx:
                    build_adapter_argv("zai", "goal")
                self.assertIn("not executable", str(ctx.exception).lower())
                self.assertTrue(isinstance(ctx.exception, RuntimeError))
                self.assertTrue(isinstance(ctx.exception, PermissionError))

    def test_validate_zai_runtime_direct(self):
        node, cjs, zbin = validate_zai_runtime()
        self.assertEqual(node, ZCODE_NODE)
        self.assertEqual(cjs, ZCODE_CJS)
        self.assertEqual(zbin, ZCODEX_BIN)
        self.assertTrue(os.path.isfile(node) and os.access(node, os.X_OK))
        self.assertTrue(os.path.isfile(cjs) and os.access(cjs, os.R_OK))
        self.assertTrue(os.path.isfile(zbin) and os.access(zbin, os.X_OK))

    def test_other_providers_unaffected_by_zai_runtime_state(self):
        # When ZAI runtime is broken/missing, other providers still build normally
        with patch("launcher.launch.ZCODE_NODE", "/tmp/nonexistent-node-binary"), \
             patch("launcher.launch.ZCODE_CJS", "/tmp/nonexistent-cjs"), \
             patch("launcher.launch.ZCODEX_BIN", "/tmp/nonexistent-zcodex"):
            grok_argv = build_adapter_argv("grok", "grok goal")
            self.assertIn("/home/alexey/.local/bin/grok", grok_argv)
            agy_argv = build_adapter_argv("antigravity", "agy goal")
            self.assertIn("/home/alexey/.local/bin/agy", agy_argv)

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

    @patch("launcher.launch.fetch_quse")
    @patch("launcher.launch.validate_quse")
    @patch("launcher.launch.select_candidate")
    @patch("launcher.launch.check_resources")
    @patch("launcher.launch.subprocess.Popen")
    @patch("launcher.launch.validate_first_action", return_value=True)
    def test_antigravity_native_identity_and_selected_model(self, fa, popen, resources, select, validate, fetch):
        validate.return_value = (["antigravity"], [])
        select.return_value = ({"provider": "antigravity", "model": "gemini-3.8-flash-high"}, {})
        proc = popen.return_value
        proc.returncode = 0
        proc.communicate.return_value = (json.dumps({"id": "real-native", "tag": "task-t1", "workspace": self.dir}), "")
        from launcher.launch import do_run
        self.assertEqual(do_run(str(self.db_path), self.task_id, self.dir, None, self.lock_path), 0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--engine") + 1], "antigravity")
        adapter = argv[argv.index("--") + 1:]
        self.assertEqual(adapter[adapter.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertIn("first-action-" + self.task_id + ".json", adapter[-1])
        self.assertIn("aplexer whoami --json", adapter[-1])
        self.assertTrue(fa.called)

    @patch("launcher.launch.fetch_quse")
    @patch("launcher.launch.validate_quse")
    @patch("launcher.launch.select_candidate")
    @patch("launcher.launch.check_resources")
    @patch("launcher.launch.subprocess.Popen")
    @patch("launcher.launch.validate_first_action", return_value=True)
    @patch("launcher.launch.validate_zai_runtime", return_value=("/usr/bin/node", "/opt/zcode.cjs", "/usr/bin/zcodex"))
    def test_zai_native_identity_and_genuine_first_action(self, runtime, fa, popen, resources, select, validate, fetch):
        validate.return_value = (["zai"], [])
        select.return_value = ({"provider": "zai", "model": "glm-5.3-flash"}, {})
        proc = popen.return_value
        proc.returncode = 0
        proc.communicate.return_value = (json.dumps({"id": "real-native", "tag": "task-t1", "workspace": self.dir}), "")
        from launcher.launch import do_run
        self.assertEqual(do_run(str(self.db_path), self.task_id, self.dir, None, self.lock_path), 0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--engine") + 1], "zcodex")
        adapter = argv[argv.index("--") + 1:]
        self.assertEqual(adapter[adapter.index("--model") + 1], "glm-5.3-flash")
        self.assertIn("exec", adapter)
        self.assertIn("first-action-" + self.task_id + ".json", adapter[-1])
        self.assertIn("aplexer whoami --json", adapter[-1])
        self.assertTrue(fa.called)
        self.assertTrue(resources.called)
        self.assertTrue(validate.called)

    def test_codex_adapter_unsupported_rejection(self):
        with self.assertRaises(ValueError) as cm:
            build_adapter_argv("codex", "write code")
        self.assertIn("Unsupported provider: codex", str(cm.exception))
        self.assertIn("genuine protected wrapper", str(cm.exception))

    def test_do_run_root_run_pre_action_denied_before_claim(self):
        from launcher.root_profile import RootExecutionDeniedError
        from launcher.launch import do_run

        root_task_id = "t-root-launch-denied"
        root_payload = {
            "id": root_task_id,
            "goal": "system admin task",
            "owner": "head-root",
            "cwd": self.dir,
            "timeout": 1200,
            "profile": "root-run",
            "host": "test-host",
            "session_id": "sess-root-1",
            "run_id": "run-root-1",
            "lease_epoch": 1,
        }
        self.store.submit_task(root_task_id, "idem-root-1", root_payload, [])

        with self.assertRaises(RootExecutionDeniedError) as cm:
            do_run(str(self.db_path), root_task_id, self.dir, None, self.lock_path)

        self.assertIn("Root execution denied", str(cm.exception))
        task = self.store.get_task(root_task_id)
        self.assertEqual(task["state"], "blocked")

        # Verify launch record
        record_path = Path(self.dir) / ".local" / f"launch-{root_task_id}.json"
        self.assertTrue(record_path.exists())
        record = json.loads(record_path.read_text())
        self.assertEqual(record["outcome"], "root-execution-denied")
        self.assertFalse(record["operational_root_qualified"])
        self.assertTrue(record["dependency_proof"]["preclaim_hold"])

    def test_f3_do_run_detects_implicit_root_run_and_denies_before_claim(self):
        from launcher.root_profile import RootExecutionDeniedError
        from launcher.launch import do_run

        implicit_task_id = "t-root-implicit-task"
        payload = {
            "id": implicit_task_id,
            "goal": "You are root: review principal health and recover if needed",
            "owner": "head-root",
            "cwd": self.dir,
            "timeout": 1200,
            "root_run": True,
            "host": "test-host",
            "session_id": "sess-root-2",
            "run_id": "run-root-2",
            "lease_epoch": 1,
        }
        self.store.submit_task(implicit_task_id, "idem-root-imp", payload, [])

        with self.assertRaises(RootExecutionDeniedError):
            do_run(str(self.db_path), implicit_task_id, self.dir, None, self.lock_path)

        task = self.store.get_task(implicit_task_id)
        self.assertEqual(task["state"], "blocked")
        record_path = Path(self.dir) / ".local" / f"launch-{implicit_task_id}.json"
        self.assertTrue(record_path.exists())

    def test_f3_do_run_exception_handler_transitions_blocked_and_writes_record_on_invalid_payload(self):
        from launcher.root_profile import RootAdmissionError
        from launcher.launch import do_run

        invalid_task_id = "t-root-missing-sess"
        payload = {
            "profile": "root-run",
            "goal": "root check",
            "owner": "head-root",
            "cwd": self.dir,
            "timeout": 600,
        }
        self.store.submit_task(invalid_task_id, "idem-root-inv", payload, [])

        with self.assertRaises(RootAdmissionError):
            do_run(str(self.db_path), invalid_task_id, self.dir, None, self.lock_path)

        task = self.store.get_task(invalid_task_id)
        self.assertEqual(task["state"], "blocked")
        record_path = Path(self.dir) / ".local" / f"launch-{invalid_task_id}.json"
        self.assertTrue(record_path.exists())


    def test_build_managed_profile_argv(self):
        from launcher.launch import build_managed_profile_argv
        argv_zai = build_managed_profile_argv("zai", "do work", "acc-zai", "glm-5.3-flash")
        self.assertIn("agent_hub_host.provisioning", argv_zai[2])
        self.assertIn("execute", argv_zai[2])
        self.assertEqual(argv_zai[3], "acc-zai")
        self.assertIn("exec", argv_zai)
        self.assertIn("glm-5.3-flash", argv_zai)

        with patch("launcher.launch.get_codex_wrapper", return_value="/bin/launch-codex.sh"):
            argv_codex = build_managed_profile_argv("codex", "do work", "acc-codex", "gpt-6.1-sol")
            self.assertIn("agent_hub_host.provisioning", argv_codex[2])
            self.assertEqual(argv_codex[3], "acc-codex")
            self.assertIn("exec", argv_codex)
            self.assertIn("gpt-6.1-sol", argv_codex)

        argv_opencode = build_managed_profile_argv("opencode", "do work", "acc-go", "opencode-go/muse-spark-1.3-contributor")
        self.assertIn("agent_hub_host.provisioning", argv_opencode[2])
        self.assertEqual(argv_opencode[3], "acc-go")
        self.assertIn("run", argv_opencode)

        with self.assertRaises(ValueError):
            build_managed_profile_argv("claude", "do work", "acc-claude")

    def test_managed_execution_receives_explicit_catalog(self):
        from launcher.launch import build_managed_profile_argv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "agent_hub_host"
            module.mkdir()
            (module / "__init__.py").write_text("")
            (module / "provisioning.py").write_text(
                "import os,json,sys\n"
                "def execute():\n"
                " print(json.dumps({'catalog':os.environ['AGENT_HUB_MANAGED_DIR'],'label':sys.argv[1]}))\n")
            # Quotes, spaces and shell metacharacters remain path data.
            selected = root / "catalog ' second;account"
            with patch("launcher.launch.resolve_hub_path", return_value=str(root)):
                command = build_managed_profile_argv("zai", "task", "second-zai", managed_dir=selected)
            env = dict(os.environ, AGENT_HUB_MANAGED_DIR=str(root / "wrong-default"))
            result = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), {"catalog": str(selected), "label": "second-zai"})

    def test_fetch_quse_with_managed_account_probes_isolated_env(self):
        from launcher.admission import fetch_quse
        managed_dir = Path(self.dir) / "managed-engines"
        dest = managed_dir / "acc1-rev1"
        dest.mkdir(parents=True)
        (dest / "home").mkdir()
        bundle = {
            "label": "acc1",
            "provider": "zai",
            "version": "0.155.1",
            "revision": "rev1",
            "files": {},
            "env": {"ZAI_API_KEY": "tok-acc1"},
        }
        (dest / "bundle.json").write_text(json.dumps(bundle))
        (managed_dir / "state.json").write_text(json.dumps([{"label": "acc1", "revision": "rev1", "provider": "zai"}]))

        captured_env = {}
        fake_quse_out = json.dumps({"zai": {"status": "ok", "windows": {"weekly": {"percent_remaining": 80.0, "reset_at": "2026-11-01T00:00:00+00:00"}}}})

        def fake_subprocess_run(cmd, env=None, **kwargs):
            captured_env.update(env or {})
            class FakeRes:
                returncode = 0
                stdout = fake_quse_out
            return FakeRes()

        with patch.dict(os.environ, {"AGENT_HUB_CONFIG_DIR": self.dir}), \
             patch("launcher.admission.subprocess.run", side_effect=fake_subprocess_run):
            data = fetch_quse(account_label="acc1", provider="zai")

        self.assertIn("zai", data)
        self.assertEqual(captured_env.get("ZAI_API_KEY"), "tok-acc1")
        self.assertEqual(captured_env.get("HOME"), str(dest / "home"))
        self.assertEqual(captured_env.get("ZCODE_HOME"), str(dest / "home" / ".zcode"))

    def test_do_run_passes_managed_profile_to_aplexer(self):
        from launcher.launch import do_run
        task_id = "t-managed-profile"
        payload = {
            "id": task_id,
            "goal": "test managed goal",
            "owner": "hub-fleet",
            "cwd": self.dir,
            "timeout": 60,
            "account_label": "hubacc",
            "account_profile": "hub-hubacc",
            "model_requirements": {"providers": ["opencode"], "models": ["opencode-go/muse-spark-1.3-contributor"]},
        }
        self.store.submit_task(task_id, "idem-managed", payload, [])
        fake_quse = {
            "go": {
                "status": "ok",
                "windows": {
                    "monthly": {"percent_remaining": 80.0, "reset_at": "2026-11-01T00:00:00+00:00"}
                }
            }
        }
        captured_cmd = []
        test_dir = str(Path(self.dir).resolve())
        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                return None
            def communicate(self, timeout=None):
                start_json = {
                    "schema_version": 1,
                    "id": "11111111-1111-4111-8111-111111111111",
                    "tag": f"task-{task_id}",
                    "workspace": test_dir,
                    "engine": "opencode",
                    "command": "python3",
                    "phase": "running",
                    "worker_pid": 12345,
                    "created_at_ms": 1000000,
                    "updated_at_ms": 1000500,
                    "profile": "hub-hubacc",
                }
                return json.dumps(start_json), ""

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return FakeProc()

        fa_path = Path(self.dir) / ".local" / f"first-action-{task_id}.json"
        fa_path.parent.mkdir(parents=True, exist_ok=True)
        fa_data = {
            "schema_version": 1,
            "id": "11111111-1111-4111-8111-111111111111",
            "tag": f"task-{task_id}",
            "workspace": str(Path(self.dir).resolve()),
            "engine": "opencode",
            "command": "python3",
            "phase": "running",
            "worker_pid": 12345,
            "created_at_ms": 1000000,
            "updated_at_ms": 1001000,
            "profile": "hub-hubacc",
            "timestamp": "2026-10-20T14:00:00+00:00",
        }
        fa_path.write_text(json.dumps(fa_data))

        with patch("launcher.launch.fetch_quse", return_value=fake_quse), \
             patch("launcher.launch.subprocess.Popen", side_effect=fake_popen), \
             patch("launcher.launch.validate_first_action", return_value=True), \
             patch("launcher.launch.validate_opencode_runtime", return_value="/bin/opencode"):
            rc = do_run(str(self.db_path), task_id, self.dir, None, self.lock_path)

        self.assertEqual(rc, 0)
        self.assertIn("--profile", captured_cmd)
        prof_idx = captured_cmd.index("--profile")
        self.assertEqual(captured_cmd[prof_idx + 1], "hub-hubacc")
        dash_idx = captured_cmd.index("--")
        post_dash = captured_cmd[dash_idx + 1:]
        self.assertIn("agent_hub_host.provisioning", " ".join(post_dash))
        self.assertIn("hubacc", post_dash)

        # Check recorded start record
        rec_path = Path(self.dir) / ".local" / f"launch-{task_id}.json"
        self.assertTrue(rec_path.exists())
        rec = json.loads(rec_path.read_text())
        self.assertEqual(rec.get("profile"), "hub-hubacc")
        self.assertEqual(rec.get("account_label"), "hubacc")

    def test_plan_and_run_use_exact_managed_quota_context(self):
        from launcher.cli import plan
        from launcher.launch import do_run

        managed_task_id = "t-managed-quota"
        managed_payload = {
            "id": managed_task_id,
            "owner": "test-owner",
            "goal": "run under named profile",
            "cwd": self.dir,
            "timeout": 1200,
            "account_label": "namedB",
            "account_profile": "hub-namedB",
            "provider_profile": "hub-namedB",
            "profile": "model-task",
            "model_requirements": {"providers": ["opencode"], "models": ["opencode-go/muse-spark-1.3-contributor"]},
        }

        legacy_task_id = "t-legacy-quota"
        legacy_payload = {
            "id": legacy_task_id,
            "owner": "test-owner",
            "goal": "run under legacy account",
            "cwd": self.dir,
            "timeout": 1200,
            "account_label": "shared-gemini",
            "profile": "model-task",
            "model_requirements": {"providers": ["opencode"], "models": ["opencode-go/muse-spark-1.3-contributor"]},
        }

        state_store = Store(str(Path(self.dir) / "state.db"))
        state_store.submit_task(managed_task_id, "idem-m-1", managed_payload, [])
        state_store.submit_task(legacy_task_id, "idem-l-1", legacy_payload, [])
        self.store.submit_task(managed_task_id, "idem-m-1", managed_payload, [])
        self.store.submit_task(legacy_task_id, "idem-l-1", legacy_payload, [])

        calls_plan = []
        def fake_fetch_quse_plan(account_label=None, provider=None, config_dir=None, managed_dir=None):
            calls_plan.append(account_label)
            # 5% for ambient default (rejected), 80% for namedB (admitted)
            perc = 5.0 if account_label is None else 80.0
            return {
                "status": "ok",
                "go": {
                    "status": "ok",
                    "windows": {
                        "monthly": {"percent_remaining": perc, "reset_at": "2026-11-01T00:00:00+00:00"}
                    }
                }
            }

        class FakeArgs:
            def __init__(self, tid, cdir):
                self.id = tid
                self.payload = None
                self.config_dir = cdir

        with patch("launcher.admission.fetch_quse", side_effect=fake_fetch_quse_plan):
            plan(FakeArgs(managed_task_id, self.dir))
            self.assertEqual(calls_plan, ["namedB"])
            calls_plan.clear()

            plan(FakeArgs(legacy_task_id, self.dir))
            self.assertEqual(calls_plan, [None])

        # Test do_run dispatch behavior with exact managed quota context
        calls_run = []
        def fake_fetch_quse_run(account_label=None, provider=None, config_dir=None, managed_dir=None):
            calls_run.append(account_label)
            perc = 0.0 if account_label is None else 80.0
            return {
                "status": "ok",
                "go": {
                    "status": "ok",
                    "windows": {
                        "monthly": {"percent_remaining": perc, "reset_at": "2026-11-01T00:00:00+00:00"}
                    }
                }
            }

        with patch("launcher.launch.fetch_quse", side_effect=fake_fetch_quse_run), \
             patch("launcher.launch.check_resources", return_value=True), \
             patch("launcher.launch.validate_opencode_runtime", return_value="/bin/opencode"), \
             patch("launcher.launch.validate_first_action", return_value=True), \
             patch("launcher.launch.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (json.dumps({"id": "sess-1", "tag": f"task-{managed_task_id}", "workspace": self.dir}), "")
            mock_popen.return_value = mock_proc

            fa_path = Path(self.dir) / ".local" / f"first-action-{managed_task_id}.json"
            fa_path.parent.mkdir(parents=True, exist_ok=True)
            fa_path.write_text(json.dumps({
                "schema_version": 1,
                "id": "11111111-1111-4111-8111-111111111111",
                "tag": f"task-{managed_task_id}",
                "workspace": str(Path(self.dir).resolve()),
                "engine": "opencode",
                "command": "python3",
                "phase": "running",
                "worker_pid": 12345,
                "created_at_ms": 1000000,
                "updated_at_ms": 1001000,
                "profile": "hub-namedB",
                "timestamp": "2026-10-20T14:00:00+00:00",
            }))

            rc = do_run(str(self.db_path), managed_task_id, self.dir, None, self.lock_path)
            self.assertEqual(rc, 0)
            self.assertEqual(calls_run, ["namedB"])
            calls_run.clear()

            # Now run legacy task - with 5% remaining, quota gate should refuse it
            with self.assertRaises(ValueError):
                do_run(str(self.db_path), legacy_task_id, self.dir, None, self.lock_path)
            self.assertEqual(calls_run, [None])


if __name__ == '__main__':
    unittest.main()
