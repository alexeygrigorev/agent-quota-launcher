"""Unit and source tests for manager-spawned sibling systemd TASK units (C2438 / C2441)."""
from __future__ import annotations

import datetime
import hashlib
import argparse
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from launcher.cli import run_task_units
from launcher.store import Store

from launcher.task_units import (
    HEAD_SCOPE_FORBIDDEN_MARKERS,
    MAX_MEMORY_MB,
    TASKS_MAX,
    TaskUnitAdmissionError,
    TaskUnitCleanupError,
    TaskUnitExecutionError,
    TaskUnitTimeoutError,
    _is_cgroup_dissolved_or_empty,
    admit_task_unit,
    assert_cgroup_outside_head,
    build_systemd_run_argv,
    execute_transient_task_unit,
    find_enclosing_git_root,
    generate_prelude_code,
    invocation_ids_match,
    parse_invocation_id,
    sanitize_unit_name,
    signal_owned_unit,
    spawn_transient_task_unit,
    timeout_output_text,
    verify_task_unit_cleanup,
    parse_tool_events,
)


class TestTaskUnitNamingAndIsolation(unittest.TestCase):
    def test_sanitize_unit_name_valid(self):
        self.assertEqual(sanitize_unit_name("task-123"), "agent-task-task-123.service")
        self.assertEqual(sanitize_unit_name("Task_456-abc"), "agent-task-Task_456-abc.service")

    def test_sanitize_unit_name_rejects_path_traversal_and_invalid_chars(self):
        for bad in ["task/with spaces!", "task/1", "task\\1", "../task", "task..name", "task@bad"]:
            with self.assertRaises(TaskUnitAdmissionError):
                sanitize_unit_name(bad)

    def test_sanitize_unit_name_invalid_raises(self):
        with self.assertRaises(TaskUnitAdmissionError):
            sanitize_unit_name("")
        with self.assertRaises(TaskUnitAdmissionError):
            sanitize_unit_name("   ")
        with self.assertRaises(TaskUnitAdmissionError):
            sanitize_unit_name(None)  # type: ignore

    def test_assert_cgroup_outside_head_passes_sibling(self):
        sibling_cg = "/user.slice/user-1000.slice/user@1000.service/app.slice/agent-task-123.service"
        self.assertTrue(assert_cgroup_outside_head(sibling_cg))

    def test_assert_cgroup_outside_head_rejects_head_scope(self):
        head_cg = "/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-6be4c247-4410-4bdb-968e-7fc2d5844941.scope"
        with self.assertRaises(TaskUnitExecutionError) as ctx:
            assert_cgroup_outside_head(head_cg)
        self.assertIn("nested under forbidden head scope", str(ctx.exception))

    def test_assert_cgroup_outside_head_rejects_any_aplexer_workload_scope(self):
        other_workload = "/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-any.scope"
        with self.assertRaises(TaskUnitExecutionError):
            assert_cgroup_outside_head(other_workload)


class TestTaskUnitAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        self.allowed_tmp = self.repo / ".local" / "tmp" / "task1"
        self.allowed_tmp.mkdir(parents=True)
        self.valid_quse = {
            "zai": {
                "status": "ok",
                "windows": {
                    "7d": {
                        "percent_remaining": 65.0,
                        "reset_at": "2099-01-01T00:00:00Z",
                    }
                },
            }
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_admit_task_unit_valid(self):
        # Passes with 512 MB, isolated repo tmp, and valid quse
        with patch("launcher.task_units.check_resources", return_value=True):
            self.assertTrue(
                admit_task_unit(
                    task_id="t1",
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=str(self.allowed_tmp),
                    quse_json=self.valid_quse,
                )
            )


    def test_admit_task_unit_auto_provisions_tmpdir_when_none(self):
        with patch("launcher.task_units.check_resources", return_value=True) as mock_check:
            self.assertTrue(
                admit_task_unit(
                    task_id="t_auto",
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=None,
                    quse_json=self.valid_quse,
                )
            )
            self.assertTrue(mock_check.called)
            called_tmpdir = mock_check.call_args[0][2]
            expected_tmpdir = str(self.repo / ".local" / "tmp" / "t_auto")
            self.assertEqual(called_tmpdir, expected_tmpdir)


    def test_admit_task_unit_missing_quse_rejected(self):
        with self.assertRaises(TaskUnitAdmissionError) as ctx:
            admit_task_unit(
                task_id="t1",
                memory_mb=512,
                workspace=str(self.repo),
                tmpdir=str(self.allowed_tmp),
                quse_json=None,
            )
        self.assertIn("fresh quse_json evidence is required", str(ctx.exception))

    def test_admit_task_unit_invalid_quota_route_rejected(self):
        exhausted_quse = {
            "zai": {
                "status": "ok",
                "windows": {
                    "7d": {
                        "percent_remaining": 0.0,
                        "reset_at": "2099-01-01T00:00:00Z",
                    }
                },
            }
        }
        with self.assertRaises(TaskUnitAdmissionError) as ctx:
            admit_task_unit(
                task_id="t1",
                memory_mb=512,
                workspace=str(self.repo),
                tmpdir=str(self.allowed_tmp),
                quse_json=exhausted_quse,
            )
        self.assertIn("no valid quota route available", str(ctx.exception))

    def test_admit_task_unit_requested_provider_quota_rejected(self):
        with self.assertRaises(TaskUnitAdmissionError) as ctx:
            admit_task_unit(
                task_id="t1",
                memory_mb=512,
                workspace=str(self.repo),
                tmpdir=str(self.allowed_tmp),
                quse_json=self.valid_quse,
                provider="grok",
            )
        self.assertIn("requested provider 'grok' quota rejected", str(ctx.exception))

    def test_admit_task_unit_memory_ceiling_exceeded_rejected(self):
        with self.assertRaises(TaskUnitAdmissionError) as ctx:
            admit_task_unit(
                task_id="t1",
                memory_mb=1501,
                workspace=str(self.repo),
                tmpdir=str(self.allowed_tmp),
                quse_json=self.valid_quse,
            )
        self.assertIn("exceeds maximum allowed ceiling", str(ctx.exception))

    def test_admit_task_unit_zero_or_negative_memory_rejected(self):
        with self.assertRaises(TaskUnitAdmissionError):
            admit_task_unit(
                task_id="t1",
                memory_mb=0,
                workspace=str(self.repo),
                tmpdir=str(self.allowed_tmp),
                quse_json=self.valid_quse,
            )

    def test_admit_task_unit_tmpdir_not_under_repo_local_tmp_rejected(self):
        outside_tmp = Path(self.tmp) / "outside_tmp"
        outside_tmp.mkdir()
        with patch("launcher.task_units.check_resources", return_value=True):
            with self.assertRaises(TaskUnitAdmissionError) as ctx:
                admit_task_unit(
                    task_id="t1",
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=str(outside_tmp),
                    quse_json=self.valid_quse,
                )
            self.assertIn("must be located under", str(ctx.exception))

    def test_admit_task_unit_system_tmp_rejected(self):
        with patch("launcher.task_units.check_resources", return_value=True):
            with self.assertRaises(TaskUnitAdmissionError):
                admit_task_unit(
                    task_id="t1",
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir="/tmp/test_task",
                    quse_json=self.valid_quse,
                )

    def test_admit_task_unit_data_destination_rejected(self):
        with patch("launcher.task_units.check_resources", return_value=True):
            with self.assertRaises(TaskUnitAdmissionError) as ctx:
                admit_task_unit(
                    task_id="t1",
                    memory_mb=512,
                    workspace="/data/projects/repo",
                    tmpdir="/data/projects/repo/.local/tmp/task",
                    quse_json=self.valid_quse,
                )
            self.assertIn("destination under /data is denied", str(ctx.exception))


class TestTaskUnitCommandAndPrelude(unittest.TestCase):
    def test_build_systemd_run_argv_properties(self):
        cmd = build_systemd_run_argv(
            unit_name="agent-task-t1.service",
            command_argv=["python3", "worker.py", "--arg"],
            memory_mb=768,
            tmpdir="/repo/.local/tmp/t1",
        )
        self.assertEqual(cmd[0], "systemd-run")
        self.assertIn("--user", cmd)
        self.assertIn("--unit=agent-task-t1.service", cmd)
        self.assertIn("--slice=app.slice", cmd)
        self.assertNotIn("--remain-after-exit", cmd)
        self.assertIn("--collect", cmd)
        self.assertIn("--expand-environment=no", cmd)
        self.assertIn("--pipe", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("MemoryMax=768M", cmd)
        self.assertIn("TasksMax=100", cmd)
        self.assertIn("-E", cmd)
        self.assertIn("TMPDIR=/repo/.local/tmp/t1", cmd)
        self.assertTrue(any(arg.startswith("PATH=") for arg in cmd))
        self.assertTrue(any(arg.startswith("HOME=") for arg in cmd))
        self.assertTrue(any(arg.startswith("USER=") for arg in cmd))
        self.assertIn("--", cmd)
        self.assertEqual(cmd[-3:], ["python3", "worker.py", "--arg"])

        # Crucial architectural assertion: NEVER use --scope!
        self.assertNotIn("--scope", cmd)

    def test_build_systemd_run_argv_propagates_extra_env(self):
        tmp_ws = tempfile.mkdtemp()
        try:
            cmd = build_systemd_run_argv(
                unit_name="agent-task-t1.service",
                command_argv=["python3", "worker.py"],
                memory_mb=768,
                tmpdir=f"{tmp_ws}/tmp",
                extra_env={"CUSTOM_KEY": "CUSTOM_VAL", "FOO": "BAR"},
            )
            # Secrets MUST NOT leak via command-line arguments (-E)
            self.assertNotIn("CUSTOM_KEY=CUSTOM_VAL", cmd)
            self.assertNotIn("FOO=BAR", cmd)
            # Environment variables must be passed securely via EnvironmentFile
            env_file_arg = f"EnvironmentFile={tmp_ws}/tmp/agent-task-t1.service.env"
            self.assertIn(env_file_arg, cmd)
            # Verify the env file was created with 0600 mode and correct content
            env_path = Path(f"{tmp_ws}/tmp/agent-task-t1.service.env")
            self.assertTrue(env_path.exists())
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            content = env_path.read_text(encoding="utf-8")
            self.assertIn("CUSTOM_KEY=CUSTOM_VAL\n", content)
            self.assertIn("FOO=BAR\n", content)
        finally:
            shutil.rmtree(tmp_ws, ignore_errors=True)

    def test_build_systemd_run_argv_strict_path_whitelist(self):
        # Even if os.environ has an unvetted path, it must not propagate
        old_path = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = f"/malicious/unvetted/bin:{old_path}"
            cmd = build_systemd_run_argv(
                unit_name="agent-task-t1.service",
                command_argv=["python3", "worker.py"],
                memory_mb=768,
                tmpdir="/repo/.local/tmp/t1",
            )
            path_arg = next(arg for arg in cmd if arg.startswith("PATH="))
            self.assertNotIn("/malicious/unvetted/bin", path_arg)
        finally:
            os.environ["PATH"] = old_path

    def test_build_systemd_run_argv_requires_service_suffix(self):
        with self.assertRaises(TaskUnitAdmissionError):
            build_systemd_run_argv(
                unit_name="agent-task-t1.scope",
                command_argv=["true"],
                memory_mb=512,
                tmpdir="/tmp",
            )

    def test_build_systemd_run_argv_with_workspace(self):
        tmp_ws = tempfile.mkdtemp()
        try:
            cmd = build_systemd_run_argv(
                unit_name="agent-task-t1.service",
                command_argv=["python3", "worker.py"],
                memory_mb=768,
                tmpdir=f"{tmp_ws}/tmp",
                workspace=tmp_ws,
            )
            self.assertIn("-p", cmd)
            self.assertIn(f"WorkingDirectory={tmp_ws}", cmd)
        finally:
            shutil.rmtree(tmp_ws, ignore_errors=True)

    def test_build_systemd_run_argv_invalid_workspace_raises(self):
        with self.assertRaises(TaskUnitAdmissionError) as ctx:
            build_systemd_run_argv(
                unit_name="agent-task-t1.service",
                command_argv=["python3", "worker.py"],
                memory_mb=768,
                tmpdir="/tmp",
                workspace="/nonexistent/path/that/cannot/exist/12345",
            )
        self.assertIn("workspace path does not exist", str(ctx.exception))

    def test_generate_prelude_code_contains_head_scope_guard(self):
        code = generate_prelude_code("agent-task-t1.service")
        self.assertIn("aplexer-workload-6be4c247", code)
        self.assertIn("/proc/self/cgroup", code)
        self.assertIn("sys.exit(96)", code)
        self.assertIn("sys.exit(97)", code)
        self.assertIn("sys.exit(98)", code)
        self.assertIn("app.slice", code)

    def test_generate_prelude_code_contains_workspace_guard(self):
        code = generate_prelude_code("agent-task-t1.service", expected_workspace="/home/alexey/git/test")
        self.assertIn("expected_ws = '/home/alexey/git/test'", code)
        self.assertIn("current_cwd != os.path.realpath(expected_ws)", code)
        self.assertIn("sys.exit(99)", code)

    def test_build_systemd_run_argv_mount_isolation_flags(self):
        cmd = build_systemd_run_argv(
            unit_name="agent-task-t1.service",
            command_argv=["python3", "worker.py"],
            memory_mb=768,
            tmpdir="/repo/.local/tmp/t1",
            protect_system="strict",
            read_only_paths=["/repo"],
            read_write_paths=["/repo/work", "/repo/tmp"],
        )
        self.assertIn("-p", cmd)
        pairs = list(zip(cmd[:-1], cmd[1:]))
        self.assertIn(("-p", "ProtectSystem=strict"), pairs)
        self.assertIn(("-p", "ReadOnlyPaths=/repo"), pairs)
        self.assertIn(("-p", "ReadWritePaths=/repo/work"), pairs)
        self.assertIn(("-p", "ReadWritePaths=/repo/tmp"), pairs)

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {"ControlGroup": "app.slice/agent-task-t1.service", "ActiveState": "inactive", "SubState": "dead", "InvocationID": "inv12345"}))
    @patch("subprocess.Popen")
    @patch("launcher.task_units.build_systemd_run_argv")
    def test_execute_transient_task_unit_mount_isolation_propagation(self, mock_build, mock_popen, mock_cleanup, mock_res):
        proc_inst = MagicMock()
        proc_inst.returncode = 0
        proc_inst.communicate.return_value = ("Running as unit: agent-task-t1.service; invocation ID: inv12345\n", "")
        mock_popen.return_value = proc_inst
        mock_build.return_value = ["systemd-run", "--unit=agent-task-t1.service", "--"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            work_dir = repo / "work"
            work_dir.mkdir()
            tmpdir = repo / ".local" / "tmp" / "t1"
            tmpdir.mkdir(parents=True)
            valid_quse = {
                "zai": {
                    "status": "ok",
                    "windows": {
                        "7d": {
                            "percent_remaining": 65.0,
                            "reset_at": "2099-01-01T00:00:00Z",
                        }
                    },
                }
            }

            receipt = execute_transient_task_unit(
                task_id="t1",
                command_argv=["echo", "hello"],
                memory_mb=512,
                workspace=str(repo),
                tmpdir=str(tmpdir),
                quse_json=valid_quse,
                protect_system="strict",
                read_only_paths=[str(repo)],
                read_write_paths=[str(work_dir), str(tmpdir)],
            )

            mock_build.assert_called_once()
            _, kwargs = mock_build.call_args
            self.assertEqual(kwargs.get("protect_system"), "strict")
            self.assertEqual(kwargs.get("read_only_paths"), [str(repo)])
            self.assertEqual(kwargs.get("read_write_paths"), [str(work_dir), str(tmpdir)])

            self.assertEqual(receipt.get("protect_system"), "strict")
            self.assertEqual(receipt.get("read_only_paths"), [str(repo)])
            self.assertEqual(receipt.get("read_write_paths"), [str(work_dir), str(tmpdir)])

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("subprocess.run")
    @patch("launcher.task_units.build_systemd_run_argv")
    def test_spawn_transient_task_unit_mount_isolation_propagation(self, mock_build, mock_run, mock_res):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Running as unit: agent-task-t1.service; invocation ID: inv12345\n"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc
        mock_build.return_value = ["systemd-run", "--unit=agent-task-t1.service", "--"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            work_dir = repo / "work"
            work_dir.mkdir()
            tmpdir = repo / ".local" / "tmp" / "t1"
            tmpdir.mkdir(parents=True)
            valid_quse = {
                "zai": {
                    "status": "ok",
                    "windows": {
                        "7d": {
                            "percent_remaining": 65.0,
                            "reset_at": "2099-01-01T00:00:00Z",
                        }
                    },
                }
            }

            receipt = spawn_transient_task_unit(
                task_id="t1",
                command_argv=["echo", "hello"],
                memory_mb=512,
                workspace=str(repo),
                tmpdir=str(tmpdir),
                quse_json=valid_quse,
                protect_system="strict",
                read_only_paths=[str(repo)],
                read_write_paths=[str(work_dir), str(tmpdir)],
            )

            mock_build.assert_called_once()
            _, kwargs = mock_build.call_args
            self.assertEqual(kwargs.get("protect_system"), "strict")
            self.assertEqual(kwargs.get("read_only_paths"), [str(repo)])
            self.assertEqual(kwargs.get("read_write_paths"), [str(work_dir), str(tmpdir)])

            self.assertEqual(receipt.get("protect_system"), "strict")
            self.assertEqual(receipt.get("read_only_paths"), [str(repo)])
            self.assertEqual(receipt.get("read_write_paths"), [str(work_dir), str(tmpdir)])

    def test_find_enclosing_git_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir).resolve()
            repo = tmp_root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            nested = repo / "a" / "b" / "c"
            nested.mkdir(parents=True)

            # Verifies returns repository root for nested paths
            self.assertEqual(find_enclosing_git_root(nested), repo)
            self.assertEqual(find_enclosing_git_root(str(nested)), repo)
            self.assertEqual(find_enclosing_git_root(repo), repo)

            # Falls back to path if no .git exists up to filesystem root
            orig_exists = Path.exists

            def no_git_exists(self_path):
                if self_path.name == ".git":
                    return False
                return orig_exists(self_path)

            no_git = tmp_root / "no_git_dir" / "sub"
            no_git.mkdir(parents=True)
            with patch.object(Path, "exists", no_git_exists):
                self.assertEqual(find_enclosing_git_root(no_git), no_git)
                self.assertEqual(find_enclosing_git_root(str(no_git)), no_git)

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {"ControlGroup": "app.slice/agent-task-t_default.service", "ActiveState": "inactive", "SubState": "dead", "InvocationID": "inv12345"}))
    @patch("subprocess.Popen")
    @patch("launcher.task_units.build_systemd_run_argv")
    def test_execute_transient_task_unit_derives_mandatory_isolation_policy(self, mock_build, mock_popen, mock_cleanup, mock_res):
        proc_inst = MagicMock()
        proc_inst.returncode = 0
        proc_inst.communicate.return_value = ("Running as unit: agent-task-t_default.service; invocation ID: inv12345\n", "")
        mock_popen.return_value = proc_inst
        mock_build.return_value = ["systemd-run", "--unit=agent-task-t_default.service", "--"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir).resolve()
            repo = tmp_root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            workspace = repo / "workspace"
            workspace.mkdir()
            tmpdir = workspace / ".local" / "tmp" / "t_default"
            tmpdir.mkdir(parents=True)
            valid_quse = {
                "zai": {
                    "status": "ok",
                    "windows": {
                        "7d": {
                            "percent_remaining": 65.0,
                            "reset_at": "2099-01-01T00:00:00Z",
                        }
                    },
                }
            }

            receipt = execute_transient_task_unit(
                task_id="t_default",
                command_argv=["echo", "hello"],
                memory_mb=512,
                workspace=str(workspace),
                tmpdir=str(tmpdir),
                quse_json=valid_quse,
            )

            expected_git_root = find_enclosing_git_root(workspace)
            self.assertEqual(expected_git_root, repo)
            self.assertEqual(receipt.get("protect_system"), "strict")
            self.assertEqual(receipt.get("read_only_paths"), [str(find_enclosing_git_root(workspace))])
            self.assertEqual(receipt.get("read_write_paths"), [str(workspace), str(tmpdir)])

            mock_build.assert_called_once()
            _, kwargs = mock_build.call_args
            self.assertEqual(kwargs.get("protect_system"), "strict")
            self.assertEqual(kwargs.get("read_only_paths"), [str(find_enclosing_git_root(workspace))])
            self.assertEqual(kwargs.get("read_write_paths"), [str(workspace), str(tmpdir)])

    @patch("launcher.task_units.check_resources", return_value=True)
    def test_execute_transient_task_unit_rejects_escaping_read_write_paths(self, mock_res):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir).resolve()
            repo = tmp_root / "repo"
            repo.mkdir()
            tmpdir = repo / ".local" / "tmp" / "t_esc"
            tmpdir.mkdir(parents=True)
            valid_quse = {
                "zai": {
                    "status": "ok",
                    "windows": {
                        "7d": {
                            "percent_remaining": 65.0,
                            "reset_at": "2099-01-01T00:00:00Z",
                        }
                    },
                }
            }

            for escaping_path in ["/etc", "/home/alexey"]:
                with self.assertRaises(TaskUnitAdmissionError) as ctx:
                    execute_transient_task_unit(
                        task_id="t_esc",
                        command_argv=["echo", "hello"],
                        memory_mb=512,
                        workspace=str(repo),
                        tmpdir=str(tmpdir),
                        quse_json=valid_quse,
                        read_write_paths=[escaping_path],
                    )
                self.assertIn(
                    f"read_write_paths contains unauthorized escaping path outside workspace: '{escaping_path}'",
                    str(ctx.exception),
                )



class TestTaskUnitEnvironmentExpansionLiteralPreservation(unittest.TestCase):
    """Tests reproducing the issue66 systemd environment-expansion hazard and verifying literal preservation."""

    def test_build_systemd_run_argv_places_expand_environment_before_separator(self):
        cmd = build_systemd_run_argv(
            unit_name="agent-task-literal.service",
            command_argv=["node", "-e", "console.log('test')"],
            memory_mb=512,
            tmpdir="/repo/.local/tmp/literal",
            wait=True,
        )
        self.assertIn("--expand-environment=no", cmd)
        sep_index = cmd.index("--")
        flag_index = cmd.index("--expand-environment=no")
        self.assertLess(flag_index, sep_index)

    def test_build_systemd_run_argv_preserves_typescript_template_literals(self):
        # Exact mismatch expressions from parent receipt / review transport:
        fragment1 = "r === `machine:${caller.machineId}`) return true;\n        if (act"
        fragment2 = "ey = `root-run:${slotStartUtc}`;\n      const existing = this.sql."
        command = [
            "node",
            "-e",
            fragment1,
            "--extra",
            fragment2,
        ]
        cmd = build_systemd_run_argv(
            unit_name="agent-task-ts.service",
            command_argv=command,
            memory_mb=512,
            tmpdir="/repo/.local/tmp/ts",
        )
        # Ensure --expand-environment=no is enabled for systemd-run
        self.assertIn("--expand-environment=no", cmd)
        # Ensure command arguments remain completely unchanged in argv after --
        sep = cmd.index("--")
        payload_args = cmd[sep + 1 :]
        self.assertIn(fragment1, payload_args)
        self.assertIn(fragment2, payload_args)

    def test_build_systemd_run_argv_preserves_sentinels_and_special_characters(self):
        # Benign fixed sentinels, known environment names, quotes, backticks, multiline, unicode
        sentinel_dollar = "${BENIGN_SENTINEL_TOKEN_123}"
        known_env_expr = "$USER:$HOME:${PATH}:${TMPDIR}"
        complex_quoted = '`template ${var}` and "double" and \'single\''
        multiline_str = "first line\nsecond line with ${VAR}\nthird line"
        unicode_str = "λ x → x + 1 // 🌟 🚀 unicode"

        test_args = [
            "python3",
            "eval_script.py",
            sentinel_dollar,
            known_env_expr,
            complex_quoted,
            multiline_str,
            unicode_str,
        ]

        cmd = build_systemd_run_argv(
            unit_name="agent-task-sentinel.service",
            command_argv=test_args,
            memory_mb=768,
            tmpdir="/repo/.local/tmp/sentinel",
        )

        self.assertIn("--expand-environment=no", cmd)
        sep = cmd.index("--")
        after_sep = cmd[sep + 1 :]
        # All passed arguments are preserved verbatim
        for arg in test_args:
            self.assertIn(arg, after_sep)

    def test_expansion_hazard_regression_contrast(self):
        """Simulate and document systemd-run expansion behavior:
        Without --expand-environment=no, systemd expands ${var} references against unit env,
        collapsing undefined variables (e.g. ${caller.machineId}) to empty string.
        With --expand-environment=no, systemd disables this expansion and passes args literally.
        """
        def simulate_systemd_arg_processing(argv: List[str], env: Dict[str, str]) -> List[str]:
            sep = argv.index("--")
            options = argv[:sep]
            payload = argv[sep + 1 :]

            if "--expand-environment=no" in options:
                # Literal mode: no variable expansion
                return list(payload)

            # Default systemd-run mode (--expand-environment=yes):
            expanded = []
            for arg in payload:
                # systemd expands ${NAME} to env[NAME] or "" if unset
                import re
                def repl(m):
                    var_name = m.group(1)
                    return env.get(var_name, "")
                expanded_arg = re.sub(r"\$\{([a-zA-Z0-9_\.]+)\}", repl, arg)
                expanded.append(expanded_arg)
            return expanded

        sample_arg = "r === `machine:${caller.machineId}`"
        mock_env = {"USER": "testuser"}

        # 1. Unrepaired command (hazard): lacks --expand-environment=no
        unrepaired_cmd = [
            "systemd-run", "--user", "--unit=bad.service", "--collect", "--",
            "node", "-e", sample_arg
        ]
        hazard_result = simulate_systemd_arg_processing(unrepaired_cmd, mock_env)
        # Without flag, ${caller.machineId} is expanded away, reproducing review-transport-dollar-mismatch:
        self.assertEqual(hazard_result[-1], "r === `machine:`")
        self.assertNotEqual(hazard_result[-1], sample_arg)

        # 2. Repaired command: includes --expand-environment=no
        repaired_cmd = build_systemd_run_argv(
            unit_name="agent-task-fixed.service",
            command_argv=["node", "-e", sample_arg],
            memory_mb=512,
            tmpdir="/repo/.local/tmp/fixed",
        )
        self.assertIn("--expand-environment=no", repaired_cmd)
        fixed_result = simulate_systemd_arg_processing(repaired_cmd, mock_env)
        # With flag, sample_arg is preserved verbatim
        self.assertEqual(fixed_result[-1], sample_arg)

    def test_controls_and_invariants_unchanged(self):
        """Verify that adding --expand-environment=no does not alter any existing gates/controls."""
        cmd_wait = build_systemd_run_argv(
            unit_name="agent-task-ctrl.service",
            command_argv=["python3", "task.py"],
            memory_mb=1024,
            tmpdir="/repo/.local/tmp/ctrl",
            wait=True,
            protect_system="strict",
            read_only_paths=["/repo"],
            read_write_paths=["/repo/.local/tmp"],
        )
        self.assertNotIn("--scope", cmd_wait)
        self.assertIn("--expand-environment=no", cmd_wait)
        self.assertIn("--wait", cmd_wait)
        self.assertNotIn("--pipe", cmd_wait)
        self.assertIn("-p", cmd_wait)
        self.assertTrue(any("MemoryMax=1024M" in x for x in cmd_wait))
        self.assertTrue(any("TasksMax=100" in x for x in cmd_wait))
        self.assertTrue(any("ProtectSystem=strict" in x for x in cmd_wait))
        self.assertTrue(any("ReadOnlyPaths=/repo" in x for x in cmd_wait))
        self.assertTrue(any("ReadWritePaths=/repo/.local/tmp" in x for x in cmd_wait))

        cmd_pipe = build_systemd_run_argv(
            unit_name="agent-task-pipe.service",
            command_argv=["python3", "task.py"],
            memory_mb=512,
            tmpdir="/repo/.local/tmp/ctrl",
            wait=False,
        )
        self.assertIn("--pipe", cmd_pipe)
        self.assertNotIn("--wait", cmd_pipe)
        self.assertIn("--expand-environment=no", cmd_pipe)
class TestCGroupDissolutionAndCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cgroup_dissolved_when_populated_zero_and_no_procs(self):
        cg_dir = Path(self.tmp) / "app.slice" / "agent-task-1.service"
        cg_dir.mkdir(parents=True)
        (cg_dir / "cgroup.events").write_text("populated 0\nfrozen 0\n")
        (cg_dir / "cgroup.procs").write_text("")

        with patch("launcher.task_units.Path", return_value=Path(self.tmp)):
            # Mocking Path("/sys/fs/cgroup")
            pass

    def test_verify_task_unit_cleanup_success_when_dead(self):
        fake_stdout = "ActiveState=inactive\nSubState=dead\nControlGroup=\nInvocationID=123\nExecMainPID=0\n"
        mock_proc = MagicMock()
        mock_proc.stdout = fake_stdout
        with patch("subprocess.run", return_value=mock_proc):
            cleaned, props = verify_task_unit_cleanup("agent-task-t1.service", timeout_sec=0.1)
            self.assertTrue(cleaned)
            self.assertEqual(props["ActiveState"], "inactive")
            self.assertEqual(props["SubState"], "dead")

    def test_verify_task_unit_cleanup_fails_when_lingering_active(self):
        fake_stdout = "ActiveState=active\nSubState=running\nControlGroup=app.slice/agent-task-t1.service\n"
        mock_proc = MagicMock()
        mock_proc.stdout = fake_stdout
        calls = []

        def mock_sub_run(cmd, **kwargs):
            calls.append(cmd)
            return mock_proc

        with patch("subprocess.run", side_effect=mock_sub_run):
            cleaned, props = verify_task_unit_cleanup("agent-task-t1.service", timeout_sec=0.1)
            self.assertFalse(cleaned)
            stop_calls = [c for c in calls if "stop" in c]
            self.assertEqual(len(stop_calls), 0)

    def test_verify_task_unit_cleanup_mismatched_invocation_id_fails_closed(self):
        fake_stdout = "ActiveState=active\nSubState=running\nControlGroup=app.slice/agent-task-t1.service\nInvocationID=other-id-999\n"
        mock_proc = MagicMock()
        mock_proc.stdout = fake_stdout
        with patch("subprocess.run", return_value=mock_proc):
            cleaned, props = verify_task_unit_cleanup(
                "agent-task-t1.service",
                timeout_sec=0.1,
                expected_invocation_id="my-expected-id-111",
            )
            self.assertFalse(cleaned)

    def test_verify_task_unit_cleanup_collected_unit_with_empty_metadata_passes(self):
        # When systemd --collect erases metadata from dead unit, cleanup still succeeds
        fake_stdout = "ActiveState=inactive\nSubState=dead\nControlGroup=\nInvocationID=\nExecMainPID=0\n"
        mock_proc = MagicMock()
        mock_proc.stdout = fake_stdout
        with patch("subprocess.run", return_value=mock_proc):
            with patch("launcher.task_units._is_cgroup_dissolved_or_empty", return_value=True):
                cleaned, props = verify_task_unit_cleanup(
                    "agent-task-t1.service",
                    timeout_sec=0.1,
                    expected_invocation_id="my-expected-id-111",
                    expected_cgroup="app.slice/agent-task-t1.service",
                )
                self.assertTrue(cleaned)
                self.assertEqual(props["ActiveState"], "inactive")
                self.assertEqual(props["SubState"], "dead")

    def test_verify_task_unit_cleanup_foreign_active_never_stopped(self):
        # If another invocation has reused the unit name, never issue systemctl stop
        fake_stdout = "ActiveState=active\nSubState=running\nControlGroup=app.slice/agent-task-t1.service\nInvocationID=foreign-id-999\n"
        mock_proc = MagicMock()
        mock_proc.stdout = fake_stdout
        calls = []
        def mock_sub_run(cmd, **kwargs):
            calls.append(cmd)
            return mock_proc

        with patch("subprocess.run", side_effect=mock_sub_run):
            cleaned, props = verify_task_unit_cleanup(
                "agent-task-t1.service",
                timeout_sec=0.1,
                expected_invocation_id="my-expected-id-111",
            )
            self.assertFalse(cleaned)
            # Ensure "stop" command was NEVER called
            stop_calls = [c for c in calls if "stop" in c]
            self.assertEqual(len(stop_calls), 0)


class TestTaskUnitExecutionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        self.tmpdir = self.repo / ".local" / "tmp" / "t1"
        self.tmpdir.mkdir(parents=True)
        self.valid_quse = {
            "zai": {
                "status": "ok",
                "windows": {
                    "7d": {
                        "percent_remaining": 65.0,
                        "reset_at": "2099-01-01T00:00:00Z",
                    }
                },
            }
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup")
    @patch("subprocess.Popen")
    def test_execute_transient_task_unit_success(self, mock_popen, mock_cleanup, mock_res):
        mock_cleanup.return_value = (True, {"ControlGroup": "app.slice/agent-task-t1.service", "ActiveState": "inactive", "SubState": "dead", "InvocationID": "inv12345"})
        
        proc_inst = MagicMock()
        proc_inst.returncode = 0
        proc_inst.communicate.return_value = ("Running as unit: agent-task-t1.service; invocation ID: inv12345\n", "")
        mock_popen.return_value = proc_inst

        receipt = execute_transient_task_unit(
            task_id="t1",
            command_argv=["echo", "hello"],
            memory_mb=512,
            workspace=str(self.repo),
            tmpdir=str(self.tmpdir),
            quse_json=self.valid_quse,
        )

        self.assertEqual(receipt["task_id"], "t1")
        self.assertEqual(receipt["unit_name"], "agent-task-t1.service")
        self.assertEqual(receipt["invocation_id"], "inv12345")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["workspace"], str(self.repo))
        self.assertEqual(receipt["working_directory"], str(self.repo))
        self.assertEqual(receipt["timeout_sec"], 1200.0)

    @patch("launcher.task_units.show_unit_props")
    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup")
    @patch("subprocess.Popen")
    def test_execute_transient_task_unit_telemetry_capture(self, mock_popen, mock_cleanup, mock_res, mock_show):
        mock_cleanup.return_value = (True, {"ActiveState": "inactive", "SubState": "dead", "InvocationID": "inv12345"})
        
        proc_inst = MagicMock()
        proc_inst.returncode = 0
        proc_inst.communicate.return_value = ("Running as unit: agent-task-t2.service; invocation ID: inv12345\n", "")
        # Allow thread to poll once then stop
        proc_inst.poll.side_effect = [None, 0]
        mock_popen.return_value = proc_inst
        
        mock_show.return_value = {
            "ControlGroup": "app.slice/agent-task-t2.service",
            "MemoryPeak": "1048576",
            "CPUUsageNSec": "5000000"
        }

        receipt = execute_transient_task_unit(
            task_id="t2",
            command_argv=["echo", "telemetry"],
            memory_mb=512,
            workspace=str(self.repo),
            tmpdir=str(self.tmpdir),
            quse_json=self.valid_quse,
        )

        self.assertEqual(receipt["task_id"], "t2")
        self.assertEqual(receipt["cgroup"], "app.slice/agent-task-t2.service")
        self.assertEqual(receipt["memory_peak_bytes"], 1048576)
        self.assertEqual(receipt["cpu_usage_nsec"], 5000000)
        self.assertEqual(len(receipt["module_sha256"]), 64)
        self.assertTrue(receipt["cleanup_verified"])
        self.assertEqual(receipt["memory_max_mb"], 512)
        self.assertEqual(receipt["tasks_max"], 100)
        self.assertTrue(Path(receipt["stdout_log"]).exists())


    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {}))
    @patch("subprocess.Popen")
    def test_execute_transient_task_unit_auto_provisions_tmpdir(self, mock_popen, mock_cleanup, mock_res):
        proc_inst = MagicMock()
        proc_inst.returncode = 0
        proc_inst.communicate.return_value = ("Running as unit: agent-task-t-auto.service; invocation ID: inv12345\n", "")
        mock_popen.return_value = proc_inst

        receipt = execute_transient_task_unit(
            task_id="t-auto",
            command_argv=["echo", "hello"],
            memory_mb=512,
            workspace=str(self.repo),
            tmpdir=None,
            quse_json=self.valid_quse,
        )
        
        expected_tmp = str(Path(self.repo).resolve() / ".local" / "tmp" / "t-auto")
        
        # Verify that subprocess.Popen was called with expected_tmp in env
        call_kwargs = mock_popen.call_args[1]
        env = call_kwargs.get("env", {})
        self.assertEqual(env.get("TMPDIR"), expected_tmp)
        self.assertEqual(env.get("TEMP"), expected_tmp)
        self.assertEqual(env.get("TMP"), expected_tmp)
        
        # Verify the directory was created
        self.assertTrue(Path(expected_tmp).exists())
        import stat
        self.assertEqual(stat.S_IMODE(Path(expected_tmp).stat().st_mode), 0o700)

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup")
    @patch("subprocess.Popen")
    def test_execute_transient_task_unit_cleanup_failure_raises(self, mock_popen, mock_cleanup, mock_res):
        mock_cleanup.return_value = (False, {"ActiveState": "failed", "SubState": "failed"})
        
        proc_inst = MagicMock()
        proc_inst.returncode = 1
        proc_inst.communicate.return_value = ("", "failure")
        mock_popen.return_value = proc_inst

        with self.assertRaises(TaskUnitCleanupError):
            execute_transient_task_unit(
                task_id="t2",
                command_argv=["false"],
                memory_mb=512,
                workspace=str(self.repo),
                tmpdir=str(self.tmpdir),
                quse_json=self.valid_quse,
            )

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("subprocess.run")
    def test_spawn_transient_task_unit_async(self, mock_run, mock_res):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Running as unit: agent-task-async1.service; invocation ID: async_inv_999\n"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        receipt = spawn_transient_task_unit(
            task_id="async1",
            command_argv=["echo", "async"],
            memory_mb=512,
            workspace=str(self.repo),
            tmpdir=str(self.tmpdir),
            quse_json=self.valid_quse,
        )
        self.assertEqual(receipt["task_id"], "async1")
        self.assertEqual(receipt["unit_name"], "agent-task-async1.service")
        self.assertEqual(receipt["invocation_id"], "async_inv_999")
        self.assertEqual(receipt["workspace"], str(self.repo))
        self.assertEqual(receipt["working_directory"], str(self.repo))
        self.assertEqual(len(receipt["module_sha256"]), 64)
        self.assertEqual(receipt["memory_max_mb"], 512)
        self.assertTrue(Path(receipt["stdout_log"]).name.endswith("-stdout.log"))

    def test_invocation_ids_match_requires_both_nonempty_and_equal(self):
        self.assertTrue(invocation_ids_match("abc", "abc"))
        self.assertFalse(invocation_ids_match("", ""))
        self.assertFalse(invocation_ids_match("abc", ""))
        self.assertFalse(invocation_ids_match("", "abc"))
        self.assertFalse(invocation_ids_match(None, "abc"))
        self.assertFalse(invocation_ids_match("abc", "xyz"))

    def test_timeout_output_text_decodes_bytes(self):
        self.assertEqual(timeout_output_text(b"invocation ID: abc\n"), "invocation ID: abc\n")
        self.assertEqual(timeout_output_text("abc"), "abc")
        self.assertEqual(timeout_output_text(None), "")
        self.assertEqual(parse_invocation_id("Running as unit: x; invocation ID: wit-1\n"), "wit-1")
        self.assertIsNone(parse_invocation_id(""))

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {}))
    @patch("subprocess.Popen")
    def test_timeout_without_invocation_does_not_kill_or_stop(self, mock_popen, mock_cleanup, mock_res):
        proc_inst = MagicMock()
        proc_inst.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["systemd-run"], timeout=1, output="", stderr=""
        )
        mock_popen.return_value = proc_inst
        runs = []

        def mock_run(cmd, **kwargs):
            runs.append(list(cmd))
            mock = MagicMock()
            mock.stdout = "InvocationID=\nControlGroup=\nMainPID=0\nLoadState=not-found\n"
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=mock_run):
            with self.assertRaises(TaskUnitTimeoutError) as ctx:
                execute_transient_task_unit(
                    task_id="t-timeout-noid",
                    command_argv=["sleep", "9"],
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=str(self.tmpdir),
                    timeout_sec=0.01,
                    quse_json=self.valid_quse,
                )
        self.assertFalse(ctx.exception.identity.get("killed"))
        self.assertEqual(ctx.exception.identity.get("expected_invocation_id"), "")
        kill_or_stop = [c for c in runs if "kill" in c or "stop" in c]
        self.assertEqual(kill_or_stop, [])

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {}))
    @patch("subprocess.Popen")
    def test_timeout_empty_live_invocation_does_not_kill(self, mock_popen, mock_cleanup, mock_res):
        proc_inst = MagicMock()
        proc_inst.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["systemd-run"], timeout=1,
            output="Running as unit: agent-task-t-timeout-empty.service; invocation ID: expected-inv\n",
            stderr="",
        )
        mock_popen.return_value = proc_inst
        runs = []

        def mock_run(cmd, **kwargs):
            runs.append(list(cmd))
            mock = MagicMock()
            mock.stdout = "InvocationID=\nControlGroup=\nMainPID=0\nLoadState=loaded\n"
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=mock_run):
            with self.assertRaises(TaskUnitTimeoutError) as ctx:
                execute_transient_task_unit(
                    task_id="t-timeout-empty",
                    command_argv=["sleep", "9"],
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=str(self.tmpdir),
                    timeout_sec=0.01,
                    quse_json=self.valid_quse,
                )
        self.assertFalse(ctx.exception.identity.get("killed"))
        kill_or_stop = [c for c in runs if "kill" in c or "stop" in c]
        self.assertEqual(kill_or_stop, [])

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {}))
    @patch("subprocess.Popen")
    def test_timeout_bytes_stdout_without_invocation_does_not_kill(self, mock_popen, mock_cleanup, mock_res):
        proc_inst = MagicMock()
        proc_inst.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["systemd-run"], timeout=1, output=b"", stderr=b"timeout\n"
        )
        mock_popen.return_value = proc_inst
        runs = []

        def mock_run(cmd, **kwargs):
            runs.append(list(cmd))
            mock = MagicMock()
            mock.stdout = "InvocationID=\nControlGroup=\nMainPID=0\nLoadState=not-found\n"
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=mock_run):
            with self.assertRaises(TaskUnitTimeoutError) as ctx:
                execute_transient_task_unit(
                    task_id="t-timeout-bytes-noid",
                    command_argv=["sleep", "9"],
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=str(self.tmpdir),
                    timeout_sec=0.01,
                    quse_json=self.valid_quse,
                )
        self.assertFalse(ctx.exception.identity.get("killed"))
        self.assertEqual(ctx.exception.identity.get("expected_invocation_id"), "")
        kill_or_stop = [c for c in runs if "kill" in c or "stop" in c]
        self.assertEqual(kill_or_stop, [])

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {}))
    @patch("subprocess.Popen")
    def test_timeout_bytes_stdout_matched_invocation_kills(self, mock_popen, mock_cleanup, mock_res):
        proc_inst = MagicMock()
        proc_inst.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["systemd-run"], timeout=1,
            output=b"Running as unit: agent-task-t-timeout-bytes.service; invocation ID: matched-inv\n",
            stderr=b"",
        )
        mock_popen.return_value = proc_inst
        runs = []

        def mock_run(cmd, **kwargs):
            runs.append(list(cmd))
            mock = MagicMock()
            mock.stdout = (
                "InvocationID=matched-inv\n"
                "ControlGroup=app.slice/agent-task-t-timeout-bytes.service\n"
                "MainPID=4242\nLoadState=loaded\n"
            )
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=mock_run):
            with self.assertRaises(TaskUnitTimeoutError) as ctx:
                execute_transient_task_unit(
                    task_id="t-timeout-bytes",
                    command_argv=["sleep", "9"],
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=str(self.tmpdir),
                    timeout_sec=0.01,
                    quse_json=self.valid_quse,
                )
        self.assertTrue(ctx.exception.identity.get("killed"))
        self.assertEqual(ctx.exception.identity.get("expected_invocation_id"), "matched-inv")
        kill_calls = [c for c in runs if "kill" in c]
        self.assertTrue(any("--signal=SIGKILL" in c for c in kill_calls))

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {}))
    @patch("subprocess.Popen")
    def test_timeout_matched_invocation_kills(self, mock_popen, mock_cleanup, mock_res):
        proc_inst = MagicMock()
        proc_inst.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["systemd-run"], timeout=1,
            output="Running as unit: agent-task-t-timeout-match.service; invocation ID: matched-inv\n",
            stderr="",
        )
        mock_popen.return_value = proc_inst
        runs = []

        def mock_run(cmd, **kwargs):
            runs.append(list(cmd))
            mock = MagicMock()
            mock.stdout = (
                "InvocationID=matched-inv\n"
                "ControlGroup=app.slice/agent-task-t-timeout-match.service\n"
                "MainPID=4242\nLoadState=loaded\n"
            )
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=mock_run):
            with self.assertRaises(TaskUnitTimeoutError) as ctx:
                execute_transient_task_unit(
                    task_id="t-timeout-match",
                    command_argv=["sleep", "9"],
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=str(self.tmpdir),
                    timeout_sec=0.01,
                    quse_json=self.valid_quse,
                )
        self.assertTrue(ctx.exception.identity.get("killed"))
        kill_calls = [c for c in runs if "kill" in c]
        self.assertTrue(any("--signal=SIGKILL" in c for c in kill_calls))

    @patch("launcher.task_units.check_resources", return_value=True)
    @patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {}))
    @patch("subprocess.Popen")
    def test_exception_without_invocation_does_not_stop(self, mock_popen, mock_cleanup, mock_res):
        mock_popen.side_effect = OSError("systemd-run missing")
        runs = []

        def mock_run(cmd, **kwargs):
            runs.append(list(cmd))
            mock = MagicMock()
            mock.stdout = "InvocationID=\nControlGroup=\nMainPID=0\nLoadState=not-found\n"
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=mock_run):
            with self.assertRaises(TaskUnitExecutionError):
                execute_transient_task_unit(
                    task_id="t-exc-noid",
                    command_argv=["true"],
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=str(self.tmpdir),
                    quse_json=self.valid_quse,
                )
        kill_or_stop = [c for c in runs if "kill" in c or "stop" in c]
        self.assertEqual(kill_or_stop, [])



class TestParseToolEvents(unittest.TestCase):
    def test_parse_tool_events_deduplicates_paired_events(self):
        events = [
            {"step_update": {"step_type": "tool", "tool_name": "t1", "state": "ACTIVE", "step_index": 1}},
            {"step_update": {"step_type": "tool", "tool_name": "t1", "state": "DONE", "step_index": 1, "duration_seconds": 1.5}},
            {"step_update": {"step_type": "tool", "tool_name": "t2", "state": "ACTIVE", "step_index": 2}},
        ]
        distinct, raw_count = parse_tool_events(events)
        self.assertEqual(raw_count, 3)
        self.assertEqual(len(distinct), 2)
        self.assertEqual(distinct[0]["tool_name"], "t1")
        self.assertEqual(distinct[0]["state"], "DONE")
        self.assertEqual(distinct[0]["duration_seconds"], 1.5)
        self.assertEqual(distinct[1]["tool_name"], "t2")
        self.assertEqual(distinct[1]["state"], "ACTIVE")

    def test_parse_tool_events_handles_single_tool_use(self):
        events = [
            {"type": "tool_use", "name": "t3", "input": {}},
            {"step_update": {"step_type": "tool", "tool_name": "t4", "state": "DONE", "step_index": 5}}
        ]
        distinct, raw_count = parse_tool_events(events)
        self.assertEqual(raw_count, 2)
        self.assertEqual(len(distinct), 2)
        self.assertEqual(distinct[0]["tool_name"], "t3")
        self.assertEqual(distinct[0]["state"], "DONE")
        self.assertEqual(distinct[1]["tool_name"], "t4")
        self.assertEqual(distinct[1]["step_index"], 5)

    def test_parse_tool_events_handles_malformed_and_unindexed_streams(self):
        events = [
            {"step_update": {"step_type": "tool", "tool_name": "t1", "state": "ACTIVE"}},
            {"step_update": {"step_type": "tool", "tool_name": "t1", "state": "DONE"}},
            {"step_update": {"step_type": "tool", "tool_name": "t2", "state": "ACTIVE"}},
            {"step_update": {"step_type": "tool", "tool_name": "t2", "state": "ACTIVE"}},
            {"unknown_format": True}
        ]
        distinct, raw_count = parse_tool_events(events)
        self.assertEqual(raw_count, 4)
        self.assertEqual(len(distinct), 3)
        self.assertEqual(distinct[0]["tool_name"], "t1")
        self.assertEqual(distinct[0]["state"], "DONE")
        self.assertEqual(distinct[1]["tool_name"], "t2")
        self.assertEqual(distinct[1]["state"], "ACTIVE")
        self.assertEqual(distinct[2]["tool_name"], "t2")
        self.assertEqual(distinct[2]["state"], "ACTIVE")


class TestTaskUnitsLifecycleTransitions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name)
        self.store = Store(str(self.cfg / "state.db"))
        self.task_id = "t-lifecycle-1"
        self.store.submit_task(
            self.task_id,
            "k-lifecycle-1",
            {"owner": "ql", "cwd": self.tmp.name, "timeout": 60, "goal": "test goal", "provider": "grok"},
            [str(self.cfg / "p-1")],
        )
        self.args = argparse.Namespace(
            id=self.task_id,
            cwd=self.tmp.name,
            tmpdir=str(self.cfg / "tmp"),
            backend="task-units",
            config_dir=str(self.cfg),
            as_controller=True,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_task_units_transitions_queued_starting_running_to_completed(self):
        observed_states = []

        def fake_execute(*args, **kwargs):
            current_state = self.store.get_task(self.task_id)["state"]
            observed_states.append(current_state)
            return {"exit_code": 0, "unit": "agent-task-test.service", "invocation_id": "inv-1"}

        transitions = []
        orig_transition = self.store.transition_task

        def spy_transition(tid, new_state, expected_states, **kwargs):
            transitions.append((new_state, expected_states))
            return orig_transition(tid, new_state, expected_states, **kwargs)

        with patch("launcher.admission.fetch_quse", return_value={"ok": True}), \
             patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
             patch("launcher.task_units.execute_transient_task_unit", side_effect=fake_execute), \
             patch.object(self.store, "transition_task", side_effect=spy_transition), \
             patch("launcher.cli.get_store", return_value=self.store), \
             patch("launcher.watch.watch_loop", return_value=None):
            rc = run_task_units(self.args)

        self.assertEqual(rc, 0)
        self.assertEqual(observed_states, ["running"])
        self.assertEqual(
            transitions,
            [
                ("starting", ("queued",)),
                ("running", ("starting",)),
                ("completed-awaiting-review", ("starting", "running")),
            ],
        )
        final_task = self.store.get_task(self.task_id)
        self.assertEqual(final_task["state"], "completed-awaiting-review")

    def test_run_task_units_transitions_running_to_failed_on_nonzero_exit(self):
        observed_states = []

        def fake_execute(*args, **kwargs):
            current_state = self.store.get_task(self.task_id)["state"]
            observed_states.append(current_state)
            return {"exit_code": 1, "unit": "agent-task-test.service", "invocation_id": "inv-1"}

        transitions = []
        orig_transition = self.store.transition_task

        def spy_transition(tid, new_state, expected_states, **kwargs):
            transitions.append((new_state, expected_states))
            return orig_transition(tid, new_state, expected_states, **kwargs)

        with patch("launcher.admission.fetch_quse", return_value={"ok": True}), \
             patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
             patch("launcher.task_units.execute_transient_task_unit", side_effect=fake_execute), \
             patch.object(self.store, "transition_task", side_effect=spy_transition), \
             patch("launcher.cli.get_store", return_value=self.store):
            rc = run_task_units(self.args)

        self.assertEqual(rc, 1)
        self.assertEqual(observed_states, ["running"])
        self.assertEqual(
            transitions,
            [
                ("starting", ("queued",)),
                ("running", ("starting",)),
                ("failed", ("starting", "running")),
            ],
        )
        final_task = self.store.get_task(self.task_id)
        self.assertEqual(final_task["state"], "failed")

    def test_run_task_units_transitions_running_to_failed_on_execution_exception(self):
        observed_states = []

        def fake_execute(*args, **kwargs):
            current_state = self.store.get_task(self.task_id)["state"]
            observed_states.append(current_state)
            raise RuntimeError("transient unit execution failed")

        transitions = []
        orig_transition = self.store.transition_task

        def spy_transition(tid, new_state, expected_states, **kwargs):
            transitions.append((new_state, expected_states))
            return orig_transition(tid, new_state, expected_states, **kwargs)

        with patch("launcher.admission.fetch_quse", return_value={"ok": True}), \
             patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
             patch("launcher.task_units.execute_transient_task_unit", side_effect=fake_execute), \
             patch.object(self.store, "transition_task", side_effect=spy_transition), \
             patch("launcher.cli.get_store", return_value=self.store):
            rc = run_task_units(self.args)

        self.assertEqual(rc, 1)
        self.assertEqual(observed_states, ["running"])
        self.assertEqual(
            transitions,
            [
                ("starting", ("queued",)),
                ("running", ("starting",)),
                ("failed", ("starting", "running")),
            ],
        )
        final_task = self.store.get_task(self.task_id)
        self.assertEqual(final_task["state"], "failed")

    def test_run_task_units_transitions_starting_to_failed_if_reservation_fails(self):
        transitions = []
        orig_transition = self.store.transition_task

        def spy_transition(tid, new_state, expected_states, **kwargs):
            transitions.append((new_state, expected_states))
            return orig_transition(tid, new_state, expected_states, **kwargs)

        with patch("launcher.admission.fetch_quse", return_value={"ok": True}), \
             patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
             patch("launcher.capacity.provider_reservation", side_effect=RuntimeError("capacity full")), \
             patch.object(self.store, "transition_task", side_effect=spy_transition), \
             patch("launcher.cli.get_store", return_value=self.store):
            rc = run_task_units(self.args)

        self.assertEqual(rc, 1)
        self.assertEqual(
            transitions,
            [
                ("starting", ("queued",)),
                ("failed", ("starting", "running")),
            ],
        )
        final_task = self.store.get_task(self.task_id)
        self.assertEqual(final_task["state"], "failed")

    def test_store_complete_task_succeeds_on_running_task(self):
        # Verify that entering 'running' allows Store.complete_task to succeed
        def fake_execute(*args, **kwargs):
            # In running state, Store.complete_task succeeds
            self.store.complete_task(
                self.task_id, reviewer="reviewer-head", reason="manual reviewer completion"
            )
            return {"exit_code": 0, "unit": "agent-task-test.service", "invocation_id": "inv-1"}

        with patch("launcher.admission.fetch_quse", return_value={"ok": True}), \
             patch("launcher.launch.build_adapter_argv", return_value=["/bin/true"]), \
             patch("launcher.task_units.execute_transient_task_unit", side_effect=fake_execute), \
             patch("launcher.cli.get_store", return_value=self.store), \
             patch("launcher.watch.watch_loop", return_value=None):
            # Since complete_task already moved it to completed-awaiting-review,
            # run_task_units will try to transition from starting/running to completed-awaiting-review.
            # That will fail because task is already completed-awaiting-review.
            pass

        # Direct verification on store:
        # submit -> starting -> running -> complete_task
        task_id = "t-complete-test"
        self.store.submit_task(
            task_id, "k-complete",
            {"owner": "ql", "cwd": self.tmp.name, "timeout": 60, "goal": "x", "provider": "grok"},
            [str(self.cfg / "p-c")],
        )
        self.store.transition_task(task_id, "starting", ("queued",))
        self.store.transition_task(task_id, "running", ("starting",))
        self.store.complete_task(task_id, reviewer="reviewer-head", reason="completed")
        self.assertEqual(self.store.get_task(task_id)["state"], "completed-awaiting-review")


class TestTaskUnitsEnvironmentTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_systemd_run_argv_extra_env_creates_0600_file(self):
        from launcher.task_units import build_systemd_run_argv
        import stat

        unit = "test-env.service"
        extra = {"MY_SECRET_VAR": "secret12345", "DB_USER": "admin"}
        argv = build_systemd_run_argv(
            unit_name=unit,
            command_argv=["python3", "script.py"],
            memory_mb=512,
            tmpdir=str(self.ws),
            workspace=str(self.ws),
            extra_env=extra,
        )
        env_file = self.ws / f"{unit}.env"
        self.assertTrue(env_file.exists())
        mode = stat.S_IMODE(env_file.stat().st_mode)
        self.assertEqual(mode, 0o600)

        content = env_file.read_text()
        self.assertIn("MY_SECRET_VAR=secret12345", content)
        self.assertIn("DB_USER=admin", content)

        # Check EnvironmentFile parameter in argv
        self.assertIn(f"EnvironmentFile={env_file}", argv)
        # Ensure secret itself is NOT passed in argv
        self.assertNotIn("secret12345", " ".join(argv))

    def test_build_systemd_run_argv_rejects_secret_leak_in_argv(self):
        from launcher.task_units import build_systemd_run_argv, TaskUnitAdmissionError

        unit = "test-leak.service"
        extra = {"API_KEY": "leaked_secret_val"}
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name=unit,
                command_argv=["python3", "script.py", "--key", "leaked_secret_val"],
                memory_mb=512,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                extra_env=extra,
            )
        self.assertIn("Secret value for 'API_KEY' detected in command argv", str(cm.exception))

    def test_build_systemd_run_argv_rejects_generic_secret_flags(self):
        from launcher.task_units import build_systemd_run_argv, TaskUnitAdmissionError

        unit = "test-flag.service"
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name=unit,
                command_argv=["python3", "script.py", "--token=xyz12345"],
                memory_mb=512,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                extra_env={"OTHER": "val"},
            )
        self.assertIn("Credential argument detected in command argv", str(cm.exception))
        self.assertIn("<redacted>", str(cm.exception))
        self.assertNotIn("xyz12345", str(cm.exception))

    def test_f4_build_systemd_run_argv_scans_credentials_with_empty_or_none_extra_env(self):
        from launcher.task_units import build_systemd_run_argv, TaskUnitAdmissionError

        unit = "test-empty-extra.service"
        for extra in (None, {}):
            with self.assertRaises(TaskUnitAdmissionError) as cm:
                build_systemd_run_argv(
                    unit_name=unit,
                    command_argv=["python3", "worker.py", "--token=supersecret12345"],
                    memory_mb=512,
                    tmpdir=str(self.ws),
                    workspace=str(self.ws),
                    extra_env=extra,
                )
            self.assertIn("<redacted>", str(cm.exception))
            self.assertNotIn("supersecret12345", str(cm.exception))

    def test_f4_build_systemd_run_argv_space_separated_and_hub_tokens_rejected(self):
        from launcher.task_units import build_systemd_run_argv, TaskUnitAdmissionError

        unit = "test-space.service"
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name=unit,
                command_argv=["python3", "worker.py", "--token", "hubt_supersecret999"],
                memory_mb=512,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                extra_env=None,
            )
        self.assertIn("<redacted>", str(cm.exception))
        self.assertNotIn("hubt_supersecret999", str(cm.exception))

    def test_f4_execute_transient_task_unit_cleans_up_env_file_in_finally(self):
        import datetime
        from unittest.mock import MagicMock, patch
        from launcher.task_units import _raw_execute_transient_task_unit

        tmpdir = self.ws / ".local" / "tmp" / "t-env-cleanup"
        tmpdir.mkdir(parents=True, exist_ok=True)
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)).isoformat()
        valid_quse = {"zai": {"status": "ok", "windows": {"7d": {"percent_remaining": 80.0, "reset_at": future}}}}
        proc_inst = MagicMock(returncode=0)
        proc_inst.communicate.return_value = (
            "Running as unit: agent-task-t-env-clean.service; invocation ID: inv1\n",
            "",
        )
        with patch("launcher.task_units.check_resources", return_value=True), \
             patch("launcher.task_units.verify_task_unit_cleanup", return_value=(True, {})), \
             patch("subprocess.Popen", return_value=proc_inst):
            _raw_execute_transient_task_unit(
                task_id="t-env-clean",
                command_argv=["echo", "ok"],
                memory_mb=512,
                workspace=str(self.ws),
                tmpdir=str(tmpdir),
                quse_json=valid_quse,
                extra_env={"HUB_JOB_TOKEN": "hubt_secret_must_be_deleted"},
            )
        env_file = tmpdir / "agent-task-t-env-clean.service.env"
        # Verify env file was cleanly unlinked in finally:
        self.assertFalse(env_file.exists())

    def test_f3_run_task_units_non_controller_denies_root_before_spawning_ql_ctl(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from unittest.mock import MagicMock, patch
        from launcher.store import Store
        from launcher.cli import run_task_units

        store = Store(str(self.ws / "state.db"))
        store.submit_task(
            "t-root-ctl-test",
            "k-root-ctl-test",
            {
                "id": "t-root-ctl-test",
                "profile": "root-run",
                "goal": "root check",
                "owner": "head",
                "cwd": str(self.ws),
                "timeout": 600,
            },
            [],
        )
        args_non_ctl = argparse.Namespace(
            id="t-root-ctl-test",
            cwd=str(self.ws),
            tmpdir=None,
            backend="task-units",
            config_dir=str(self.ws),
            as_controller=False,
        )
        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("launcher.cli.subprocess.run", return_value=mock_proc) as mock_sub_run:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = run_task_units(args_non_ctl)
        # Denied with rc=1 and NO systemd-run controller spawned!
        self.assertEqual(rc, 1)
        self.assertFalse(mock_sub_run.called)
        self.assertEqual(store.get_task("t-root-ctl-test")["state"], "blocked")

    def test_f3_resolve_task_bounds_prioritizes_root_run_over_review_and_cleanup(self):
        from launcher.task_profiles import resolve_task_bounds

        payload = {
            "id": "hub-job-1",
            "root_run": True,
            "goal": "You are root: review principal health and recover if needed",
        }
        bounds = resolve_task_bounds(payload)
        self.assertEqual(bounds["profile"], "root-run")
        self.assertEqual(bounds["timeout"], 1500.0)


class TestRootRunNamespaceAndEnvTransport(unittest.TestCase):
    """Source-only guards for the root-only namespace policy and private env transport.

    Offline only: systemd/subprocess mocked, no Root launch, no service, no provider.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        os.chmod(self.ws, 0o700)
        self.script = self.ws / "headless-root-run.sh"
        self.script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(self.script, 0o700)
        self.script_sha = hashlib.sha256(self.script.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _evidence(self, **overrides):
        from launcher.task_units import TrustedRootJobEvidence

        fields = {
            "job_id": "hub-job-1",
            "run_id": "run-1",
            "lease_epoch": 2,
            "host_id": "host-1",
            "session_id": "session-1",
            "script_path": str(self.script),
            "script_sha256": self.script_sha,
            "source_commit": "e" * 40,
            "auth_role": "root",
            "expires_at": (
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)
            ).isoformat(),
        }
        fields.update(overrides)
        return TrustedRootJobEvidence(**fields)

    def _private_env(self, **overrides):
        env = {"HR_RUN_ID": "run-1", "HR_HUB_JOB_ID": "hub-job-1", "HR_HUB_LEASE_EPOCH": "2"}
        env.update(overrides)
        return env

    def _generic_argv(self):
        from launcher.task_units import build_systemd_run_argv

        return build_systemd_run_argv(
            unit_name="agent-task-gen.service",
            command_argv=["/bin/sh", str(self.script)],
            memory_mb=512,
            tmpdir=str(self.ws),
            workspace=str(self.ws),
            wait=True,
        )

    def test_generic_argv_keeps_masking_and_private_pid_namespace(self):
        argv = self._generic_argv()
        joined = " ".join(argv)
        self.assertIn("InaccessiblePaths=", joined)
        self.assertIn("-/run/user/%d/aplexer" % os.getuid(), joined)
        self.assertIn("/usr/bin/unshare", argv)
        self.assertIn("NoNewPrivileges=yes", joined)
        self.assertIn("ProtectControlGroups=yes", joined)

    def test_root_run_profile_without_evidence_produces_identical_generic_argv(self):
        from launcher.task_units import build_systemd_run_argv

        masked = build_systemd_run_argv(
            unit_name="agent-task-gen.service",
            command_argv=["/bin/sh", str(self.script)],
            memory_mb=512,
            tmpdir=str(self.ws),
            workspace=str(self.ws),
            wait=True,
            profile="root-run",
        )
        self.assertEqual(masked, self._generic_argv())

    def test_env_file_is_0600_and_removed_on_success(self):
        import stat as stat_mod
        from launcher.task_units import build_systemd_run_argv

        argv = build_systemd_run_argv(
            unit_name="agent-task-env.service",
            command_argv=["/bin/sh", str(self.script)],
            memory_mb=512,
            tmpdir=str(self.ws),
            workspace=str(self.ws),
            wait=True,
            extra_env={"SAFE_VAR": "value-1234"},
        )
        env_file = self.ws / "agent-task-env.service.env"
        self.assertEqual(stat_mod.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertIn("SAFE_VAR=value-1234", env_file.read_text(encoding="utf-8"))
        self.assertIn(f"EnvironmentFile={env_file}", argv)
        env_file.unlink()

    def test_env_file_removed_on_failure_path(self):
        from unittest.mock import MagicMock, patch
        from launcher.task_units import _raw_execute_transient_task_unit

        tmpdir = self.ws / ".local" / "tmp" / "t-fail-cleanup"
        tmpdir.mkdir(parents=True, exist_ok=True)
        proc_inst = MagicMock(returncode=0)
        proc_inst.communicate.side_effect = RuntimeError("spawn blew up")
        future = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
        ).isoformat()
        valid_quse = {"zai": {"status": "ok", "windows": {"7d": {"percent_remaining": 80.0,
                                                                  "reset_at": future}}}}
        with patch("launcher.task_units.check_resources", return_value=True), patch(
            "subprocess.Popen", return_value=proc_inst
        ):
            with self.assertRaises(Exception):
                _raw_execute_transient_task_unit(
                    task_id="t-fail-cleanup",
                    command_argv=["echo", "ok"],
                    memory_mb=512,
                    workspace=str(self.ws),
                    tmpdir=str(tmpdir),
                    quse_json=valid_quse,
                    extra_env={"SAFE_VAR": "value-1234"},
                )
        self.assertFalse((tmpdir / "agent-task-t-fail-cleanup.service.env").exists())

    def test_reserved_key_injection_refused(self):
        from launcher.task_units import build_systemd_run_argv, TaskUnitAdmissionError

        for key in ("LD_PRELOAD", "BASH_ENV", "PYTHONPATH", "PATH"):
            with self.subTest(key=key):
                with self.assertRaises(TaskUnitAdmissionError):
                    build_systemd_run_argv(
                        unit_name="agent-task-inject.service",
                        command_argv=["/bin/sh", str(self.script)],
                        memory_mb=512,
                        tmpdir=str(self.ws),
                        workspace=str(self.ws),
                        wait=True,
                        extra_env={key: "/tmp/evil"},
                    )

    def test_newline_value_is_refused_not_silently_escaped(self):
        from launcher.task_units import build_systemd_run_argv, TaskUnitAdmissionError

        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name="agent-task-nl.service",
                command_argv=["/bin/sh", str(self.script)],
                memory_mb=512,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                wait=True,
                extra_env={"SAFE_VAR": "a\nPATH=/evil"},
            )
        self.assertIn("Newline", str(cm.exception))
        self.assertNotIn("/evil", str(cm.exception))

    def test_trusted_root_env_requires_allowlist_and_own_identity(self):
        # HELD (F2): self-minted evidence is refused before allowlist checks;
        # the allowlist itself is still unit-tested via _validated_env_lines.
        from launcher.task_units import (
            build_systemd_run_argv,
            TaskUnitAdmissionError,
            _validated_env_lines,
            ROOT_TRUSTED_NAMESPACE_POLICY,
        )

        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name="agent-task-root-env.service",
                command_argv=[str(self.script)],
                memory_mb=1500,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                wait=True,
                profile="root-run",
                trusted_root_evidence=self._evidence(),
                extra_env=self._private_env(HR_BORROWED="1"),
            )
        self.assertIn("trusted root admission refused", str(cm.exception))
        with self.assertRaises(TaskUnitAdmissionError) as cm2:
            _validated_env_lines({"HR_BORROWED": "1"}, ROOT_TRUSTED_NAMESPACE_POLICY)
        self.assertIn("allowlist", str(cm2.exception))

    def test_trusted_root_expired_evidence_refused_without_spawning(self):
        from unittest.mock import patch
        from launcher.task_units import _raw_execute_transient_task_unit, TaskUnitAdmissionError

        future = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
        ).isoformat()
        valid_quse = {"zai": {"status": "ok", "windows": {"7d": {"percent_remaining": 80.0,
                                                                  "reset_at": future}}}}
        expired = self._evidence(expires_at="2020-01-01T00:00:00+00:00")
        tmpdir = self.ws / ".local" / "tmp" / "t-root-expired"
        tmpdir.mkdir(parents=True, exist_ok=True)
        with patch("launcher.task_units.check_resources", return_value=True), patch(
            "subprocess.Popen"
        ) as mock_popen:
            with self.assertRaises(TaskUnitAdmissionError) as cm:
                _raw_execute_transient_task_unit(
                    task_id="t-root-expired",
                    command_argv=["/bin/sh", str(self.script)],
                    memory_mb=1500,
                    workspace=str(self.ws),
                    tmpdir=str(tmpdir),
                    quse_json=valid_quse,
                    profile="root-run",
                    trusted_root_evidence=expired,
                    extra_env=self._private_env(),
                )
        self.assertIn("trusted root admission refused", str(cm.exception))
        self.assertFalse(mock_popen.called)

    def test_spawn_path_never_reaches_the_trusted_root_namespace(self):
        from launcher.task_units import resolve_namespace_policy

        policy = resolve_namespace_policy(
            "root-run", trusted_root_evidence=self._evidence(), require_synchronous_wait=False
        )
        self.assertEqual(policy.policy, "root-trusted-masked")
        self.assertTrue(policy.isolated_pid and policy.mask_native_services)


if __name__ == "__main__":
    unittest.main()
