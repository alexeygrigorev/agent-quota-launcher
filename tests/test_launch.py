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

    def write(self, obj):
        self.fa.write_text(json.dumps(obj))

    def genuine(self):
        return {"id": "sess-1", "tag": "task-t1",
                "workspace": "/repo", "timestamp": "2026-10-04T12:00:00Z"}

    def test_genuine_whoami_shape_accepted(self):
        self.write(self.genuine())
        self.assertTrue(validate_first_action(str(self.fa), "sess-1", "task-t1",
                                              "/repo", self.min_mtime))

    def test_wrapper_start_json_rejected(self):
        # The rejected core-2 case: aplexer wrapper start record with phase.
        wrapper = {"schema_version": 1, "id": "sess-1", "tag": "task-t1",
                   "workspace": "/repo", "phase": "starting",
                   "command": ["zcodex", "exec"], "parent_session": "p",
                   "created_at_ms": 1}
        self.write(wrapper)
        self.assertFalse(validate_first_action(str(self.fa), "sess-1", "task-t1",
                                               "/repo", self.min_mtime))

    def test_engine_start_record_rejected(self):
        record = dict(self.genuine(), phase="running", command=["aplexer"])
        self.write(record)
        self.assertFalse(validate_first_action(str(self.fa), "sess-1", "task-t1",
                                               "/repo", self.min_mtime))

    def test_missing_timestamp_rejected(self):
        data = self.genuine()
        del data["timestamp"]
        self.write(data)
        self.assertFalse(validate_first_action(str(self.fa), "sess-1", "task-t1",
                                               "/repo", self.min_mtime))

    def test_wrong_identity_rejected(self):
        self.write(self.genuine())
        self.assertFalse(validate_first_action(str(self.fa), "other", "task-t1",
                                               "/repo", self.min_mtime))

    def test_unparsable_timestamp_rejected(self):
        self.write(dict(self.genuine(), timestamp="not-a-time"))
        self.assertFalse(validate_first_action(str(self.fa), "sess-1", "task-t1",
                                               "/repo", self.min_mtime))

    def test_extra_keys_rejected(self):
        self.write(dict(self.genuine(), extra="smuggled"))
        self.assertFalse(validate_first_action(str(self.fa), "sess-1", "task-t1",
                                               "/repo", self.min_mtime))

    def test_missing_file_rejected(self):
        self.assertFalse(validate_first_action(str(self.fa), "sess-1", "task-t1",
                                               "/repo", self.min_mtime))

    def test_stale_file_rejected(self):
        # A pre-existing artifact from before the launch must not satisfy it.
        self.write(self.genuine())
        old = time.time() - 3600
        os.utime(self.fa, (old, old))
        self.assertFalse(validate_first_action(str(self.fa), "sess-1", "task-t1",
                                               "/repo", time.time() - 60))

class TestAdapters(unittest.TestCase):
    def test_grok_argv_exact(self):
        argv = build_adapter_argv("grok", "goal text")
        self.assertEqual(argv, ["grok", "-p", "--model", "grok-4.6", "--effort",
                                "high", "--permission-mode", "auto", "goal text"])

    def test_antigravity_strips_api_keys_and_sets_print_timeout(self):
        argv = build_adapter_argv("antigravity", "goal")
        self.assertEqual(argv[0], "env")
        self.assertIn("-u", argv)
        self.assertLess(argv.index("GEMINI_API_KEY"), argv.index("agy"))
        self.assertLess(argv.index("GOOGLE_API_KEY"), argv.index("agy"))
        self.assertIn("--print-timeout", argv)
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "0")
        self.assertIn("-p", argv)
        self.assertNotIn("sh", argv)
        self.assertNotIn("-c", argv[:argv.index("agy")] + ["sh"])

    def test_zai_uses_exact_zcodex_invocation(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertEqual(argv[0], "/home/alexey/.local/bin/zcodex")
        self.assertEqual(argv[1:4], ["exec", "--model", "glm-5.3-flash"])
        self.assertIn("--json", argv)
        self.assertEqual(ADAPTERS["zai"]["env"].get("ZCODE_CJS"),
                         "/opt/ZCode/resources/glm/zcode.cjs")
        self.assertEqual(argv[-1], "goal")

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

if __name__ == '__main__':
    unittest.main()
