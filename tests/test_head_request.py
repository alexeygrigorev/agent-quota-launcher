import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from launcher.cli import main

class TestHeadRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        self.config_dir = Path(self.tmp) / "config"
        self.config_dir.mkdir()
        
        # initialize store
        import subprocess
        subprocess.run(["git", "init"], cwd=str(self.repo), check=True, stdout=subprocess.DEVNULL)
        (self.repo / "test.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(self.repo), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.repo), check=True, stdout=subprocess.DEVNULL)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("launcher.cli.spawn_ql_controller")
    @patch("sys.argv")
    @patch("sys.stdout")
    def test_request_successful_admission(self, mock_stdout, mock_argv, mock_spawn):
        mock_spawn.return_value = {
            "controller_unit": "ql-ctl-task-test1.service",
            "state": "queued-or-starting"
        }
        
        # We need sys.argv to mock args
        args = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "test goal", "--cwd", str(self.repo),
            "--id", "task-test1"
        ]
        mock_argv.__getitem__.side_effect = lambda s: args[s] if isinstance(s, slice) else args[s]
        mock_argv.__iter__.side_effect = lambda: iter(args)
        mock_argv.copy.return_value = args
        # Actually patching sys.argv directly is better:
        
        with patch("sys.argv", args):
            import io
            import sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)
            finally:
                sys.stdout = sys.__stdout__
                
            out = json.loads(captured.getvalue())
            self.assertEqual(out["task_id"], "task-test1")
            self.assertEqual(out["controller_unit"], "ql-ctl-task-test1.service")
            self.assertEqual(out["status"], "queued-or-starting")
            self.assertEqual(out["profile"]["profile"], "default")

    @patch("launcher.cli.spawn_ql_controller")
    def test_request_source_validation_and_profile(self, mock_spawn):
        mock_spawn.return_value = {
            "controller_unit": "ql-ctl-auto.service",
            "state": "queued-or-starting"
        }
        args = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "test goal", "--cwd", str(self.repo),
            "--profile", "model-task", "--target-commit", "HEAD"
        ]
        with patch("sys.argv", args):
            import io
            import sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)
            finally:
                sys.stdout = sys.__stdout__
                
            out = json.loads(captured.getvalue())
            self.assertIn("task_id", out)
            self.assertEqual(out["profile"]["profile"], "model-task")
            self.assertEqual(out["profile"]["timeout"], 600.0)
            self.assertIn("source_receipt", out)
            self.assertIn("resolved_commit", out["source_receipt"])
            self.assertEqual(len(out["source_receipt"]["resolved_commit"]), 40)

    @patch("launcher.cli.spawn_ql_controller")
    def test_request_source_validation_fails(self, mock_spawn):
        args = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "test goal", "--cwd", str(self.repo),
            "--target-commit", "NONEXISTENT_REF"
        ]
        with patch("sys.argv", args):
            import io
            import sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 1)
            finally:
                sys.stdout = sys.__stdout__
                
            out = json.loads(captured.getvalue())
            self.assertIn("error", out)
            self.assertIn("Source validation failed", out["error"])

if __name__ == "__main__":
    unittest.main()
