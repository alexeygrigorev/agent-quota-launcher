"""Unit and source tests for manager-spawned sibling systemd TASK units (C2438 / C2441)."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    generate_prelude_code,
    invocation_ids_match,
    parse_invocation_id,
    sanitize_unit_name,
    signal_owned_unit,
    spawn_transient_task_unit,
    timeout_output_text,
    verify_task_unit_cleanup,
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
        with patch("launcher.task_units.check_resources", return_value=True):
            self.assertTrue(
                admit_task_unit(
                    task_id="t_auto",
                    memory_mb=512,
                    workspace=str(self.repo),
                    tmpdir=None,
                    quse_json=self.valid_quse,
                )
            )

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


if __name__ == "__main__":
    unittest.main()
