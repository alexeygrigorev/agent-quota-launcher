import json
import math
import os
import stat
import tempfile
import time
import unittest
import uuid
import subprocess
from unittest.mock import patch, MagicMock

import launcher.liveness as liveness

class TestLivenessObserver(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = self.temp_dir.name
        self.session_uuid = "12345678-1234-5678-1234-567812345678"
        
    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_status_valid(self):
        stdout = json.dumps({"id": self.session_uuid, "phase": "running", "last_activity_ms": 1000})
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertTrue(res["available"])
        self.assertFalse(res["dead"])
        self.assertEqual(res["last_activity_ms"], 1000)
        self.assertEqual(res["phase"], "running")

    def test_parse_status_with_warnings(self):
        data = {
            "id": self.session_uuid,
            "phase": "running",
            "warnings": {"some": "warning"},
            "worker_placement": {"placement": "login_session"},
            "limits": {"pids": 50}
        }
        stdout = json.dumps(data); data["warning"] = "SIGHUP task-limit"; stdout = json.dumps(data)
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertTrue(res["available"])
        self.assertFalse(res["dead"])
        self.assertTrue(res["login_session_risk"])
        self.assertTrue(res["pids_limit_risk"])
        self.assertTrue(res["warning_task_limit"])
        self.assertTrue(res["warning_sighup"])
        
    def test_parse_status_identity_mismatch(self):
        stdout = json.dumps({"id": "87654321-4321-8765-4321-876543210987", "phase": "running"})
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertFalse(res["available"])
        self.assertEqual(res["error"], "identity mismatch")

    def test_parse_status_malformed(self):
        stdout = 'SIGHUP ignored\njust some text'
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertFalse(res["available"])
        self.assertEqual(res["error"], "malformed status")
        
        # Test malformed list for last_activity_ms
        stdout = json.dumps({"id": self.session_uuid, "last_activity_ms": [1000]})
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertFalse(res["available"])
        self.assertIn("malformed last_activity_ms", res["error"])

        # Test boolean for last_activity_ms
        stdout = json.dumps({"id": self.session_uuid, "last_activity_ms": True})
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertFalse(res["available"])
        self.assertIn("malformed last_activity_ms", res["error"])
        
        # Test nonfinite for last_activity_ms
        stdout = json.dumps({"id": self.session_uuid, "last_activity_ms": math.inf})
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertFalse(res["available"])
        self.assertIn("malformed last_activity_ms", res["error"])

    def test_parse_status_empty_telemetry(self):
        res = liveness.parse_status(self.session_uuid, "", "", 0, None, 2000)
        self.assertFalse(res["available"])
        self.assertEqual(res["error"], "empty output")

    def test_parse_status_dead(self):
        stdout = json.dumps({"id": self.session_uuid, "phase": "exited"})
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertTrue(res["available"])
        self.assertTrue(res["dead"])

    def test_parse_status_future_activity(self):
        stdout = json.dumps({"id": self.session_uuid, "phase": "running", "last_activity_ms": 3000})
        res = liveness.parse_status(self.session_uuid, stdout, "", 0, None, 2000)
        self.assertFalse(res["available"])
        self.assertEqual(res["error"], "future activity")

    def test_parse_status_nonzero_code(self):
        stdout = json.dumps({"id": self.session_uuid, "phase": "running"})
        res = liveness.parse_status(self.session_uuid, stdout, "", 1, None, 2000)
        self.assertFalse(res["available"])
        self.assertIn("nonzero status code", res["error"])

    def test_classify_state_protected_states(self):
        status_info = {"available": True, "last_activity_ms": 1000}
        self.assertEqual(liveness.classify_state("==== content filter ====", status_info, 60000, 2000, 0), "content-filter")
        self.assertEqual(liveness.classify_state("[quota exhausted]", status_info, 60000, 2000, 0), "quota-held")
        self.assertEqual(liveness.classify_state("==== menu ====", status_info, 60000, 2000, 0), "menu")

    def test_classify_state_explicit_working_remains_working(self):
        # explicit explicit working
        status_info = {"available": True, "reported_state": "working", "last_activity_ms": 1000}
        self.assertEqual(liveness.classify_state("", status_info, 60000, 100000, 0), "working")

    def test_silence_is_not_inferred_idle(self):
        status_info = {"available": True, "last_activity_ms": 1000}
        self.assertEqual(liveness.classify_state("some normal text", status_info, 60000, 62000, 0), "unknown")

    def test_classify_state_unknown_and_capture_failure(self):
        status_info = {"available": True, "last_activity_ms": 1000}
        # Unknown otherwise
        self.assertEqual(liveness.classify_state("some normal text", status_info, 60000, 2000, 0), "unknown")
        # Capture failure code -> unknown
        self.assertEqual(liveness.classify_state("==== menu ====", status_info, 60000, 2000, 1), "unknown")

    def test_atomic_write_and_rotation(self):
        status_file = os.path.join(self.output_dir, "status.json")
        events_file = os.path.join(self.output_dir, "events.jsonl")
        
        data = {"test": 1}
        liveness.write_atomic_json(status_file, data)
        self.assertTrue(os.path.exists(status_file))
        self.assertEqual(stat.S_IMODE(os.stat(status_file).st_mode) & 0o777, 0o600)
        
        liveness.append_events_jsonl(events_file, data)
        self.assertTrue(os.path.exists(events_file))
        self.assertEqual(stat.S_IMODE(os.stat(events_file).st_mode) & 0o777, 0o600)
        
        large_data = {"test": "a" * (liveness.MAX_EVENTS_FILE_SIZE + 10)}
        with self.assertRaises(ValueError):
            liveness.append_events_jsonl(events_file, large_data)
        liveness.append_events_jsonl(events_file, {"test": "a" * (liveness.MAX_EVENTS_FILE_SIZE - 30)})
        liveness.append_events_jsonl(events_file, {"test": 2})
        
        self.assertTrue(os.path.exists(events_file + ".1"))

    @patch('subprocess.run')
    def test_cli_execution(self, mock_run):
        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["aplexer", "status"]:
                return MagicMock(stdout=json.dumps({"id": self.session_uuid, "phase": "running"}), stderr="", returncode=0)
            elif cmd[:2] == ["aplexer", "capture"]:
                return MagicMock(stdout='some screen', stderr="", returncode=0)
            return MagicMock(returncode=1)
            
        mock_run.side_effect = side_effect
        
        args = ["--session", self.session_uuid, "--output-dir", self.output_dir, "--once"]
        liveness.run_observer(args)
        
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "status.json")))
        
        calls = mock_run.call_args_list
        for call in calls:
            cmd = call[0][0]
            self.assertEqual(cmd[0], "aplexer")
            self.assertIn(cmd[1], ["status", "capture"])
            
    def test_duplicate_writer(self):
        fd1, lock_file = liveness.acquire_lock(self.output_dir)
        with self.assertRaises(SystemExit):
            liveness.acquire_lock(self.output_dir)
        os.close(fd1)

    def test_lock_inode_stable_after_release(self):
        fd1, lock_file = liveness.acquire_lock(self.output_dir)
        inode1 = os.stat(lock_file).st_ino
        os.close(fd1)
        
        fd2, lock_file2 = liveness.acquire_lock(self.output_dir)
        inode2 = os.stat(lock_file2).st_ino
        self.assertEqual(inode1, inode2)
        os.close(fd2)

    def test_arg_validation(self):
        with patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                liveness.parse_args(["--session", "not-a-uuid", "--output-dir", self.output_dir])
                
            with self.assertRaises(SystemExit):
                liveness.parse_args(["--session", self.session_uuid, "--output-dir", self.output_dir, "--duration", "-1"])

            with self.assertRaises(SystemExit):
                liveness.parse_args(["--session", self.session_uuid, "--output-dir", self.output_dir, "--duration", "inf"])

            with self.assertRaises(SystemExit):
                liveness.parse_args(["--session", self.session_uuid, "--output-dir", self.output_dir, "--duration", "5000"])

    @patch('subprocess.run')
    def test_hung_probe_timeout(self, mock_run):
        def side_effect(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)
            
        mock_run.side_effect = side_effect
        res_out, res_err, code, err_str = liveness.run_probe(["dummy"], 5.0)
        self.assertEqual(code, -1)
        self.assertEqual(err_str, "timeout")

class TestNativeContract(unittest.TestCase):
    SESSION = "12345678-1234-5678-1234-567812345678"

    def test_native_placement_and_unavailable_coverage(self):
        payload = {"id": self.SESSION, "phase": "running", "reported_state": "working",
                   "worker_alive": True, "worker_placement": {"placement": "login_session"},
                   "workload_placement": {"placement": "user_manager"}, "limits": {"pids": 100}}
        result = liveness.parse_status(self.SESSION, json.dumps(payload), "", 0, None, 2000)
        self.assertTrue(result["login_session_risk"])
        self.assertTrue(result["pids_limit_risk"])
        self.assertTrue(result["worker_alive"])
        unknown = liveness.parse_status(self.SESSION, "", "private stderr", 1, None, 2000)
        self.assertIsNone(unknown["dead"])
        self.assertNotIn("private", json.dumps(unknown))

    def test_all_cli_numeric_options_reject_invalid_values(self):
        with patch("sys.stderr"):
            for option in ("--interval", "--duration", "--probe-timeout", "--silence-timeout"):
                for value in ("nan", "inf", "0", "-1", "1000000"):
                    with self.subTest(option=option, value=value), self.assertRaises(SystemExit):
                        liveness.parse_args(["--session", self.SESSION, "--output-dir", "unused", option, value])
            with self.assertRaises(SystemExit):
                liveness.parse_args(["--session", self.SESSION.replace("-", ""), "--output-dir", "unused"])

    def test_malformed_roots_and_integer_activity(self):
        for value in ([], None, True, "text"):
            result = liveness.parse_status(self.SESSION, json.dumps(value), "", 0, None, 2000)
            self.assertFalse(result["available"])
        for value in (True, -1, 1.5, "1", [], float("nan")):
            result = liveness.parse_status(self.SESSION, json.dumps({"id": self.SESSION, "last_activity_ms": value}), "", 0, None, 2000)
            self.assertFalse(result["available"])

    def test_cards_override_busy_and_arbitrary_prose_stays_unknown(self):
        info = {"available": True, "reported_state": "working"}
        self.assertEqual(liveness.classify_state("[quota held]", info, 1000, 2000, 0), "quota-held")
        self.assertEqual(liveness.classify_state("Goal stalled", info, 1000, 2000, 0), "goal-stalled")
        self.assertEqual(liveness.classify_state("Our tests cover stalled calls", {"available": True}, 1000, 2000, 0), "unknown")

    def test_snapshot_existing_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "status.json")
            temporary = path + ".tmp"
            with open(temporary, "w") as f:
                f.write("old")
            os.chmod(temporary, 0o644)
            liveness.write_atomic_json(path, {"ok": True})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            os.symlink(path, temporary)
            with self.assertRaises(OSError):
                liveness.write_atomic_json(path, {"bad": True})
            with open(path) as f:
                self.assertEqual(json.load(f), {"ok": True})

    def test_probe_timeout_is_real_and_only_own_probe(self):
        started = time.monotonic()
        result = liveness.run_probe(["python3", "-c", "import time; time.sleep(10)"], 0.05)
        self.assertEqual(result[3], "timeout")
        self.assertLess(time.monotonic() - started, 1)

    def test_capture_failure_and_null_tool_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            info = json.dumps({"id": self.SESSION, "phase": "running", "reported_state": "working", "last_activity_ms": 1})
            with patch.object(liveness, "run_probe", side_effect=[(info, "", 0, None), ("private screen", "private error", -1, "timeout")]):
                liveness.run_observer(["--session", self.SESSION, "--output-dir", directory, "--once"])
            with open(os.path.join(directory, "status.json")) as f:
                result = json.load(f)
            self.assertFalse(result["screen_available"])
            self.assertEqual(result["state"], "working")
            self.assertTrue(result["silence_suspected"])
            self.assertIsNone(result["last_tool_output_time"])
            self.assertEqual(result["tool_telemetry_source"], "unavailable")
            self.assertNotIn("private", json.dumps(result))
            self.assertFalse(result["would_escalate"])

    def test_deadline_budget_is_recomputed_between_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            timeouts = []
            def probe(command, timeout):
                timeouts.append(timeout)
                if command[1] == "status":
                    time.sleep(0.08)
                    return json.dumps({"id": self.SESSION}), "", 0, None
                return "", "", -1, "timeout"
            with patch.object(liveness, "run_probe", side_effect=probe):
                liveness.run_observer(["--session", self.SESSION, "--output-dir", directory, "--duration", "0.12", "--once"])
            self.assertLess(timeouts[1], timeouts[0] / 2)


if __name__ == '__main__':
    unittest.main()
