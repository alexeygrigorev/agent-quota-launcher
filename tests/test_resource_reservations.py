"""Live resident memory credit retains full worker growth and host headroom."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from launcher.memory_accounting import resident_credit_mb, MIB
from launcher.resources import check_resources, MIN_MEM_AVAILABLE_BYTES
from launcher.store import Store


class ResidentReservationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cgroot = self.root / "cgroup"
        self.proc = self.root / "proc"
        self.native_id = "11111111-2222-4333-8444-555555555555"
        self.relative = f"/app.slice/aplexer-workload-{self.native_id}.scope"
        self.cg = self.cgroot / self.relative.lstrip("/")
        self.cg.mkdir(parents=True)
        self.pidroot = self.proc / "123"
        self.pidroot.mkdir(parents=True)
        (self.pidroot / "cgroup").write_text(f"0::{self.relative}\n")
        (self.pidroot / "stat").write_text("123 (test (worker)) S " + "0 " * 18 + "987 0\n")
        self.files = {
            "memory.max": str(1500 * MIB), "memory.current": str(1000 * MIB),
            "memory.stat": f"anon {800 * MIB}\nfile {200 * MIB}\n",
            "cgroup.events": "populated 1\nfrozen 0\n", "cgroup.procs": "123\n",
        }
        for name, value in self.files.items():
            (self.cg / name).write_text(value)
        self.binding = dict(native_id=self.native_id, tag="task-t1", workspace=str(self.root),
                            engine="codex", incarnation=1000)
        self.identity = {"boot_id": "test-boot"}
        self.info = dict(id=self.native_id, tag="task-t1", workspace=str(self.root), engine="codex",
                         created_at_ms=1000, phase="running", containment_empty=False,
                         workload_pid=123, workload_cgroup=self.relative,
                         containment_cgroup=str(self.cg), containment_cgroup_identity=self.identity)
        for name, value in (("CGROUP_ROOT", self.cgroot), ("PROC_ROOT", self.proc)):
            patcher = patch(f"launcher.memory_accounting.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("launcher.memory_accounting._kernel_identity", return_value=self.identity)
        self.kernel_identity = patcher.start()
        self.addCleanup(patcher.stop)

    def credit(self, info=None):
        return resident_credit_mb(self.binding, self.info if info is None else info, 1500)

    def test_credit_uses_real_anon_capped_by_current_and_rounds_down(self):
        self.assertEqual(self.credit(), 800)
        (self.cg / "memory.stat").write_text(f"anon {800 * MIB + MIB - 1}\n")
        self.assertEqual(self.credit(), 800)

    def test_unknown_identity_and_starting_remain_fully_reserved(self):
        changes = {"id": "other", "tag": "other", "workspace": "/other", "engine": "other",
                   "created_at_ms": 1001, "phase": "starting", "containment_empty": True,
                   "workload_pid": None, "workload_cgroup": "/", "containment_cgroup": "/",
                   "containment_cgroup_identity": {"boot_id": "other"}}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.assertEqual(self.credit(dict(self.info, **{key: value})), 0)
                missing = dict(self.info)
                missing.pop(key)
                self.assertEqual(self.credit(missing), 0)

    def test_unknown_unbounded_invalid_and_empty_cgroup_remain_fully_reserved(self):
        changes = {"memory.max": ["max", str(1501 * MIB), "0"],
                   "memory.current": ["invalid", "-1", str(1501 * MIB)],
                   "memory.stat": ["file 10\n", "anon -1\n", f"anon {1001 * MIB}\n"],
                   "cgroup.events": ["populated 0\n", ""], "cgroup.procs": ["124\n", ""]}
        for name, values in changes.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    (self.cg / name).write_text(value)
                    self.assertEqual(self.credit(), 0)
            (self.cg / name).write_text(self.files[name])

    def test_missing_symlink_and_foreign_cgroup_fail_closed(self):
        (self.cg / "memory.current").unlink()
        self.assertEqual(self.credit(), 0)
        target = self.root / "fake-memory"
        target.write_text(str(1000 * MIB))
        (self.cg / "memory.current").symlink_to(target)
        self.assertEqual(self.credit(), 0)

    def test_process_move_exit_reuse_and_kernel_identity_races_fail_closed(self):
        with patch("launcher.memory_accounting._pid_identity", side_effect=[987, 988]):
            self.assertEqual(self.credit(), 0)
        with patch("launcher.memory_accounting._pid_identity", side_effect=[987, FileNotFoundError()]):
            self.assertEqual(self.credit(), 0)
        self.kernel_identity.side_effect = [self.identity, {"boot_id": "changed"}]
        self.assertEqual(self.credit(), 0)
        self.kernel_identity.side_effect = None
        (self.pidroot / "cgroup").write_text("0::/other\n")
        self.assertEqual(self.credit(), 0)
        (self.pidroot / "cgroup").write_text(f"0::{self.relative}\n")
        (self.pidroot / "stat").write_text("123 (test) Z " + "0 " * 18 + "987 0\n")
        self.assertEqual(self.credit(), 0)

    def test_cgroup_replacement_during_sampling_fails_closed(self):
        from launcher.memory_accounting import _read_at
        replaced = False

        def replacing(fd, name):
            nonlocal replaced
            value = _read_at(fd, name)
            if not replaced:
                replaced = True
                self.cg.rename(self.cg.with_name("retired.scope"))
                self.cg.mkdir()
                for field, content in self.files.items():
                    (self.cg / field).write_text(content)
            return value

        with patch("launcher.memory_accounting._read_at", side_effect=replacing):
            self.assertEqual(self.credit(), 0)

    def test_kernel_and_process_read_errors_fail_closed(self):
        for error in (PermissionError(), FileNotFoundError(), ValueError()):
            with self.subTest(error=type(error).__name__):
                self.kernel_identity.side_effect = error
                self.assertEqual(self.credit(), 0)
        self.kernel_identity.side_effect = None
        (self.pidroot / "stat").unlink()
        self.assertEqual(self.credit(), 0)

    def test_shrinking_memory_uses_lower_observation(self):
        from launcher.memory_accounting import _read_at
        anon_reads = 0

        def shrinking(fd, name):
            nonlocal anon_reads
            if name == "memory.stat":
                anon_reads += 1
                return f"anon {(800 if anon_reads == 1 else 700) * MIB}\n"
            return _read_at(fd, name)

        with patch("launcher.memory_accounting._read_at", side_effect=shrinking):
            self.assertEqual(self.credit(), 700)

    def store(self):
        patcher = patch.dict(os.environ, {"LAUNCHER_HOST_FENCING_DIR": str(self.root / "fencing")})
        patcher.start()
        self.addCleanup(patcher.stop)
        store = Store(self.root / "state.db")
        store.submit_task("t1", "key-t1", {"goal": "test", "owner": "head", "cwd": str(self.root),
                                          "timeout": 60, "provider": "codex"}, [])
        store.record_native_binding("t1", **{k: v for k, v in self.binding.items()})
        return store

    def test_store_preserves_disk_leases_queued_unknown_and_task_exclusion(self):
        store = self.store()
        with patch("launcher.native_lifecycle.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps(self.info), "")
            self.assertEqual(store.get_active_resources(), (1500, 512))
            run.assert_not_called()
            store.transition_task("t1", "running", ("queued",))
            self.assertEqual(store.get_active_resources(), (700, 512))
            self.assertEqual(store.get_active_resources(exclude_task_id="t1"), (0, 0))
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps(dict(self.info, id="wrong")), "")
            self.assertEqual(store.get_active_resources(), (1500, 512))
            self.assertEqual(store.get_task("t1")["state"], "running")

    def test_corrected_growth_reservation_preserves_exact_ten_gib_floor(self):
        store = self.store()
        store.transition_task("t1", "running", ("queued",))
        with patch("launcher.native_lifecycle.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(self.info), "")):
            active_mem, active_disk = store.get_active_resources()
        self.assertEqual(active_mem, 700)
        disk = type("Disk", (), {"free": 100 * 1024 ** 3})()
        available = MIN_MEM_AVAILABLE_BYTES + (700 + 1500) * MIB
        kwargs = dict(requested_memory_mb=1500, requested_cwd=self.root,
                      requested_tmpdir=self.root / ".local/tmp", repo_root=self.root,
                      active_mem_mb=active_mem, active_disk_mb=active_disk, ignore_ram=False)
        with patch("launcher.resources._get_disk_usage", return_value=disk):
            with patch("launcher.resources.get_mem_available", return_value=available):
                self.assertTrue(check_resources(**kwargs))
            with patch("launcher.resources.get_mem_available", return_value=available - 1):
                with self.assertRaisesRegex(ValueError, "10GiB"):
                    check_resources(**kwargs)
