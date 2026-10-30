"""Regression coverage for compile workloads launched through Hub."""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from launcher.task_resource_limits import (
    hub_headless_memory_mb, tasks_max_for, MAX_TASK_MEMORY_MB,
)
from launcher.task_profiles import resolve_task_bounds
from launcher.task_units import build_systemd_run_argv
from launcher.store import Store
from launcher.cli import task_unit_resource_context
from launcher.resources import check_resources
from tests.test_native_memory_policy import TestDoRunNativeMemoryArgv
from tests.pinned_runtime import pinned_runtime_present


class LimitsTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".local" / "tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.payload = dict(owner="hub-fleet", profile="model-task", cwd=str(self.cwd),
                            goal="compile", timeout=60, memory_estimate_mb=512)

    def test_compiler_cold_start_and_measured_growth(self):
        self.assertEqual(hub_headless_memory_mb(self.payload), 4096)
        (self.cwd / "Cargo.toml").write_text('[package]\nname="test"\n')
        self.assertEqual(hub_headless_memory_mb(self.payload), 8192)
        self.assertEqual(resolve_task_bounds(self.payload)["memory_mb"], 8192)
        self.payload["memory_estimate_mb"] = 12000
        self.assertEqual(hub_headless_memory_mb(self.payload), 12000)

    def test_foreign_contract_and_invalid_hints(self):
        self.assertIsNone(hub_headless_memory_mb(dict(self.payload, owner="other")))
        for key in ("memory_mb", "memory_estimate_mb"):
            for bad in (True, 0, -1, 32769, 8192.5, "8192"):
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    hub_headless_memory_mb(dict(self.payload, **{key: bad}))

    def test_thread_cap_is_bounded_and_scales(self):
        for memory, expected in ((256, 1024), (1500, 1024), (8192, 2048), (32768, 8192)):
            self.assertEqual(tasks_max_for(memory), expected)
            argv = build_systemd_run_argv("compile.service", ["/usr/bin/true"],
                                           memory, str(self.cwd))
            self.assertIn(f"MemoryMax={memory}M", argv)
            self.assertIn(f"TasksMax={expected}", argv)
        for bad in (0, -1, True, 32769):
            with self.assertRaises(ValueError):
                tasks_max_for(bad)

    def test_submit_and_start_admission_charge_compile_floor(self):
        (self.cwd / "Cargo.toml").touch()
        store = Store(str(self.cwd / "state.db"))
        store.submit_task("compile", "compile", self.payload, [])
        with store.get_conn() as conn:
            memory = conn.execute("SELECT memory_mb FROM task_resources").fetchone()[0]
        self.assertEqual(memory, 8192)
        fake = MagicMock()
        fake.freeze_cpu_start.return_value = dict(expected_cpu_cores=2, expected_cpu_source="repo_kind")
        fake.get_active_resources.return_value = (0, 0)
        args = argparse.Namespace(id="compile", cwd=str(self.cwd), tmpdir=None)
        with patch("launcher.cpu_resources.collect_admission_context", return_value=(None, None)), \
             patch("launcher.resources.check_resources") as check:
            context = task_unit_resource_context(fake, args, self.payload, memory)
        self.assertEqual(context["memory_estimate_mb"], 8192)
        self.assertEqual(context["expected_cpu_cores"], 2)
        self.assertEqual(check.call_args.kwargs["memory_ceiling_mb"], MAX_TASK_MEMORY_MB)

    def test_non_compile_admission_charges_measured_estimate_not_cap(self):
        # Intake 63a9ee3a: the 4096 MiB cold-start cap is not a reservation.
        store = Store(str(self.cwd / "state.db"))
        store.submit_task("t", "t", self.payload, [])
        with store.get_conn() as conn:
            memory = conn.execute("SELECT memory_mb FROM task_resources").fetchone()[0]
        self.assertEqual(memory, 4096)
        fake = MagicMock()
        fake.freeze_cpu_start.return_value = dict(expected_cpu_cores=1, expected_cpu_source="repo_kind")
        fake.get_active_resources.return_value = (0, 0)
        args = argparse.Namespace(id="t", cwd=str(self.cwd), tmpdir=None)
        with patch("launcher.cpu_resources.collect_admission_context", return_value=(None, None)), \
             patch("launcher.resources.check_resources"):
            context = task_unit_resource_context(fake, args, self.payload, memory)
        self.assertEqual(context["memory_estimate_mb"], 512)
        store.transition_task("t", "starting", ("queued",))
        row = store.resource_breakdown()[0]
        self.assertEqual((row["basis"], row["reserve_mb"]), ("start-ramp", 512))

    def test_compile_memory_is_charged_before_start(self):
        tmp = self.cwd / ".local" / "tmp"
        tmp.mkdir(parents=True)
        with patch("launcher.resources.get_mem_available", return_value=9 * 1024**3), \
             patch("shutil.disk_usage", return_value=type("Disk", (), {"free": 100 * 1024**3})()):
            with self.assertRaisesRegex(ValueError, "launcher_memory_reserve"):
                check_resources(8192, str(self.cwd), str(tmp), repo_root=str(self.cwd),
                                memory_estimate_mb=8192, memory_floor_mb=2048,
                                memory_ceiling_mb=MAX_TASK_MEMORY_MB)


class HubNativeTests(TestDoRunNativeMemoryArgv):
    @pinned_runtime_present()
    def test_native_hub_compile_receives_same_limits(self):
        (Path(self.dir) / "Cargo.toml").touch()
        cmd = self._run_with_provider("opencode", False,
            dict(owner="hub-fleet", profile="model-task", memory_estimate_mb=512))
        self.assertEqual(cmd[cmd.index("--memory") + 1], "8192M")
        self.assertEqual(cmd[cmd.index("--pids") + 1], "2048")

    def test_native_hub_measured_estimate_is_not_clamped_to_1500(self):
        cmd = self._run_with_provider("codex", True,
            dict(owner="hub-fleet", profile="model-task", memory_estimate_mb=12288))
        self.assertEqual(cmd[cmd.index("--memory") + 1], "12288M")
        self.assertEqual(cmd[cmd.index("--pids") + 1], "3072")
