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
                    worker_pid=1234)

    def test_rich_native_whoami_accepted(self):
        # command/phase/parent_session/schema_version keys are expected on a
        # genuine whoami and must NOT be blacklisted (Principal C1450).
        self.write(self.rich_whoami())
        self.assertTrue(self.check())

    def test_minimal_identity_plus_timestamp_accepted(self):
        self.write({"id": "sess-1", "tag": "task-t1", "workspace": "/repo",
                    "timestamp": "2026-10-04T12:00:05Z"})
        self.assertTrue(self.check())

    def test_start_record_copy_rejected(self):
        # Byte-identical wrapper start JSON is not a first action...
        self.fa.write_text(json.dumps(self.start))
        self.assertFalse(self.check())

    def test_reformatted_start_record_copy_rejected(self):
        # ...and neither is the same record re-serialized with other whitespace.
        self.fa.write_text(json.dumps(self.start, indent=2, sort_keys=True))
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
