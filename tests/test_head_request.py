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

    def _run_cli(self, arg_list):
        import io
        captured = io.StringIO()
        code = 0
        with patch("sys.argv", arg_list), patch("sys.stdout", captured):
            try:
                code = main() or 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        return code, captured.getvalue()

    def _store(self):
        from launcher.store import Store
        return Store(str(self.config_dir / "state.db"))

    @patch("launcher.cli.spawn_ql_controller")
    def test_request_goal_autodetects_model_review_profile(self, mock_spawn):
        # C3048 regression (review probe P3): a review goal submitted through
        # request() must resolve to model-review bounds, not the 300s default.
        mock_spawn.return_value = {
            "controller_unit": "ql-ctl-task-rev1.service",
            "state": "queued-or-starting",
        }
        args = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "perform full code review of the diff and report findings",
            "--cwd", str(self.repo), "--id", "task-rev1",
        ]
        code, out = self._run_cli(args)
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["profile"]["profile"], "model-review")
        self.assertEqual(result["profile"]["timeout"], 900.0)
        task = self._store().get_task("task-rev1")
        self.assertEqual(task["payload"]["profile"], "model-review")
        self.assertEqual(task["payload"]["timeout"], 900.0)

    @patch("launcher.cli.spawn_ql_controller")
    def test_request_unknown_profile_emits_structured_error(self, mock_spawn):
        args = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "test goal", "--cwd", str(self.repo),
            "--profile", "definitely-not-a-profile", "--id", "task-badprof",
        ]
        code, out = self._run_cli(args)
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertIn("error", result)
        self.assertIn("Unknown task profile", result["error"])
        mock_spawn.assert_not_called()
        self.assertEqual(
            [t for t in self._store().list_tasks() if t["id"] == "task-badprof"], []
        )

    @patch("launcher.cli.spawn_ql_controller")
    def test_request_duplicate_id_emits_structured_error(self, mock_spawn):
        mock_spawn.return_value = {
            "controller_unit": "ql-ctl-task-dup1.service",
            "state": "queued-or-starting",
        }
        base = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "test goal", "--cwd", str(self.repo),
            "--id", "task-dup1",
        ]
        code1, _ = self._run_cli(base)
        self.assertEqual(code1, 0)
        # No --key: a fresh idempotency key each run, so the retry hits the
        # duplicate-id UNIQUE constraint and must fail closed with JSON, no spawn.
        code2, out2 = self._run_cli(base)
        self.assertEqual(code2, 1)
        result = json.loads(out2)
        self.assertIn("error", result)
        self.assertIn("duplicate id", result["error"])
        mock_spawn.assert_called_once()
        tasks = [t for t in self._store().list_tasks() if t["id"] == "task-dup1"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["state"], "queued")

    @patch("launcher.cli.spawn_ql_controller")
    def test_request_idempotent_resubmission_with_same_key(self, mock_spawn):
        mock_spawn.return_value = {
            "controller_unit": "ql-ctl-task-idem1.service",
            "state": "queued-or-starting",
        }
        args = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "idempotent goal", "--cwd", str(self.repo),
            "--id", "task-idem1", "--key", "idem-key-1",
        ]
        code1, _ = self._run_cli(args)
        code2, _ = self._run_cli(args)
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        tasks = [t for t in self._store().list_tasks() if t["id"] == "task-idem1"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["state"], "queued")

    @patch("launcher.cli.spawn_ql_controller")
    def test_request_conflicting_key_payload_emits_structured_error(self, mock_spawn):
        mock_spawn.return_value = {
            "controller_unit": "ql-ctl-task-conf1.service",
            "state": "queued-or-starting",
        }
        first = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "first goal", "--cwd", str(self.repo),
            "--id", "task-conf1", "--key", "conf-key-1",
        ]
        second = [
            "launcher", "--config-dir", str(self.config_dir),
            "request", "--goal", "different goal", "--cwd", str(self.repo),
            "--id", "task-conf2", "--key", "conf-key-1",
        ]
        code1, _ = self._run_cli(first)
        self.assertEqual(code1, 0)
        code2, out2 = self._run_cli(second)
        self.assertEqual(code2, 1)
        result = json.loads(out2)
        self.assertIn("error", result)
        self.assertIn("Task submission failed", result["error"])
        mock_spawn.assert_called_once()

if __name__ == "__main__":
    unittest.main()
