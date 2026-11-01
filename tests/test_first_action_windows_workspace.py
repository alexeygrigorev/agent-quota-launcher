"""Windows first-action identity ignores the mutable native workspace (Hub intake bf0863c4).

aplexer 0.1.23 on Windows reports the daemon's initial cwd (%TEMP%) as the
session workspace in whoami, so a genuine first action never matched the
start record and every Windows headless launch exited 1 (DESKTOP-FP4OP26
46a7bc7b, LAPTOP-PHA6TCNC 202195f1). Identity there rests on id + tag.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher.launch import build_managed_profile_argv, native_command_model, validate_first_action


class TestWindowsFirstActionWorkspace(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fa = Path(self.dir) / "first-action-t1.json"
        self.start = {"schema_version": 1, "id": "0f8c2a5e-3d1b-4e8f-9a7c-1b2c3d4e5f60",
                      "tag": "task-t1", "workspace": "C:\\Users\\User\\work\\bf0863c4\\agent-hub",
                      "engine": "claude", "phase": "starting", "command": ["claude"],
                      "created_at_ms": 1793525215675}

    def whoami(self, **overrides):
        data = dict(self.start, phase="running", worker_pid=4242,
                    updated_at_ms=self.start["created_at_ms"] + 9000,
                    workspace="C:\\Users\\User\\AppData\\Local\\Temp")
        data.update(overrides)
        self.fa.write_text(json.dumps(data))

    def check(self, platform):
        with patch("launcher.launch.sys.platform", platform):
            return validate_first_action(str(self.fa), self.start, time.time() - 60,
                                         now_ms=self.start["created_at_ms"] + 60_000)

    def test_windows_accepts_temp_workspace_with_matching_id_and_tag(self):
        self.whoami()
        self.assertTrue(self.check("win32"))

    def test_other_platforms_still_require_the_start_workspace(self):
        self.whoami()
        self.assertFalse(self.check("linux"))

    def test_windows_still_rejects_wrong_id_or_tag(self):
        self.whoami(id="11111111-2222-4333-8444-555555555555")
        self.assertFalse(self.check("win32"))
        self.whoami(tag="task-other")
        self.assertFalse(self.check("win32"))

    def test_windows_rejects_non_drive_workspace(self):
        for ws in ("/other", "", None, "relative\\dir"):
            self.whoami(workspace=ws)
            self.assertFalse(self.check("win32"), ws)

    def test_windows_start_record_clone_is_still_rejected(self):
        clone = dict(self.start, phase="running", worker_pid=4242)
        self.fa.write_text(json.dumps(clone))
        self.assertFalse(self.check("win32"))


class TestManagedAntigravityArgv(unittest.TestCase):
    """Managed hub-antigravity on Windows raised 'not supported for managed
    headless execution' at dispatch_headless (DESKTOP cae30722, LAPTOP eb6d4514)."""

    def test_managed_antigravity_uses_the_adapter_flags_and_routed_model(self):
        with patch("launcher.launch.resolve_hub_path", return_value="/trusted/host"):
            argv = build_managed_profile_argv("antigravity", "do it", "hub-antigravity", "gemini-4-argon-high")
        self.assertEqual(native_command_model(argv), "gemini-4-argon-high")
        self.assertEqual(argv[3], "hub-antigravity")
        self.assertEqual(argv[4:], ["--model", "gemini-4-argon-high", "--effort", "high",
                                    "--dangerously-skip-permissions", "--print-timeout", "0",
                                    "--output-format", "stream-json", "-p", "do it"])


if __name__ == "__main__":
    unittest.main()
