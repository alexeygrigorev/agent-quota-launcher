"""Unit tests for hostwide provider capacity, ZAI ceiling, cooldowns, and fallback."""
import json
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from launcher.admission import validate_quse
from launcher.capacity import (
    DEFAULT_MAX_CONCURRENT_ZAI,
    CapacityError,
    ConcurrencyLimitExceeded,
    CooldownActive,
    check_cooldown,
    check_provider_capacity,
    get_live_zai_pids,
    is_genuine_zai_pid,
    provider_reservation,
    record_429_event,
    release_provider_slot,
    reserve_provider_slot,
)


class TestProviderCapacity(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.config_dir = self.tmp / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.future = "2099-01-01T00:00:00Z"
        self.sample_quse = {
            "zai": {
                "status": "ok",
                "windows": {
                    "5h": {"percent_remaining": 100.0, "reset_at": self.future},
                    "7d": {"percent_remaining": 50.0, "reset_at": self.future},
                },
            },
            "gemini": {
                "status": "ok",
                "windows": {
                    "5h": {"percent_remaining": 95.0, "reset_at": self.future},
                    "7d": {"percent_remaining": 60.0, "reset_at": self.future},
                },
            },
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("launcher.capacity.subprocess.run")
    @patch("launcher.capacity.is_genuine_zai_pid", return_value=True)
    def test_get_live_zai_pids_discovery(self, mock_is_genuine, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "1001\n1002\n1003\n"
        mock_run.return_value = mock_proc

        pids = get_live_zai_pids()
        self.assertEqual(pids, [1001, 1002, 1003])

    def test_is_genuine_zai_pid_comm_and_exe_validation(self):
        fake_proc = self.tmp / "proc"
        fake_proc.mkdir(parents=True, exist_ok=True)

        def make_proc(pid: int, comm: str, exe_target: str = None):
            pdir = fake_proc / str(pid)
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "comm").write_text(comm, encoding="utf-8")
            if exe_target:
                (pdir / "exe").symlink_to(exe_target)

        # Genuine processes
        make_proc(10, "zcode-cli")
        make_proc(11, "node", exe_target="/usr/bin/zcode-cli")

        # Wrapper processes matching cmdline but having wrapper comm
        make_proc(20, "bash")
        make_proc(21, "sh")
        make_proc(22, "python3")
        make_proc(23, "timeout")
        make_proc(24, "grep")
        make_proc(25, "pgrep")
        make_proc(26, "other_wrapper", exe_target="/bin/bash")

        self.assertTrue(is_genuine_zai_pid(10, proc_root=fake_proc))
        self.assertTrue(is_genuine_zai_pid(11, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(20, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(21, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(22, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(23, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(24, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(25, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(26, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(999, proc_root=fake_proc))  # non-existent

    def test_is_genuine_zai_pid_excludes_zombie_defunct_processes(self):
        fake_proc = self.tmp / "proc"
        fake_proc.mkdir(parents=True, exist_ok=True)

        def make_proc(pid: int, comm: str, status_state: str = None, stat_state: str = None):
            pdir = fake_proc / str(pid)
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "comm").write_text(comm, encoding="utf-8")
            if status_state:
                (pdir / "status").write_text(f"Name:\t{comm}\nState:\t{status_state}\n", encoding="utf-8")
            if stat_state:
                (pdir / "stat").write_text(f"{pid} ({comm}) {stat_state} 1 1 1 0 0\n", encoding="utf-8")

        # Live processes: S (sleeping) and R (running)
        make_proc(30, "zcode-cli", status_state="S (sleeping)", stat_state="S")
        make_proc(31, "zcode-cli", status_state="R (running)", stat_state="R")

        # Zombie processes with comm='zcode-cli' and state='Z'
        make_proc(40, "zcode-cli", status_state="Z (zombie)", stat_state="Z")
        make_proc(41, "zcode-cli", status_state="Z (zombie)")
        make_proc(42, "zcode-cli", stat_state="Z")

        self.assertTrue(is_genuine_zai_pid(30, proc_root=fake_proc))
        self.assertTrue(is_genuine_zai_pid(31, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(40, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(41, proc_root=fake_proc))
        self.assertFalse(is_genuine_zai_pid(42, proc_root=fake_proc))

    @patch("launcher.capacity.subprocess.run")
    def test_get_live_zai_pids_excludes_wrapper_processes(self, mock_run):
        fake_proc = self.tmp / "proc"
        fake_proc.mkdir(parents=True, exist_ok=True)

        def make_proc(pid: int, comm: str, exe_target: str = None, status_state: str = None):
            pdir = fake_proc / str(pid)
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "comm").write_text(comm, encoding="utf-8")
            if exe_target:
                (pdir / "exe").symlink_to(exe_target)
            if status_state:
                (pdir / "status").write_text(f"Name:\t{comm}\nState:\t{status_state}\n", encoding="utf-8")

        # 4 false wrapper matches from pgrep cmdline search
        make_proc(201, "bash")
        make_proc(202, "python3")
        make_proc(203, "sh")
        make_proc(204, "timeout")

        # 2 genuine zcode-cli processes
        make_proc(205, "zcode-cli")
        make_proc(206, "zcode-cli")

        # 1 zombie process with comm='zcode-cli'
        make_proc(207, "zcode-cli", status_state="Z (zombie)")

        # pgrep returns all 7 because cmdline matches 'zcode-cli'
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "201\n202\n203\n204\n205\n206\n207\n"
        mock_run.return_value = mock_proc

        pids = get_live_zai_pids(proc_root=fake_proc)
        # Only genuine live 205 and 206 should be discovered; zombie 207 and wrappers excluded
        self.assertEqual(pids, [205, 206])

    @patch("launcher.capacity.subprocess.run")
    def test_get_live_zai_pids_iterdir_fallback_excludes_wrappers(self, mock_run):
        # Simulate pgrep failure/empty
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_run.return_value = mock_proc

        fake_proc = self.tmp / "proc"
        fake_proc.mkdir(parents=True, exist_ok=True)

        def make_proc(pid: int, comm: str):
            pdir = fake_proc / str(pid)
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "comm").write_text(comm, encoding="utf-8")

        make_proc(301, "bash")
        make_proc(302, "zcode-cli")
        make_proc(303, "python3")
        make_proc(304, "sh")

        pids = get_live_zai_pids(proc_root=fake_proc)
        self.assertEqual(pids, [302])

    @patch("launcher.capacity.get_live_zai_pids")
    def test_check_provider_capacity_under_ceiling(self, mock_pids):
        mock_pids.return_value = [101, 102, 103]  # 3 live processes
        can_admit, reason, info = check_provider_capacity("zai", config_dir=self.config_dir)
        self.assertTrue(can_admit)
        self.assertIsNone(reason)
        self.assertEqual(info["live_count"], 3)
        self.assertEqual(info["total_active"], 3)
        self.assertEqual(info["headroom"], DEFAULT_MAX_CONCURRENT_ZAI - 3)

    @patch("launcher.capacity.get_live_zai_pids")
    def test_check_provider_capacity_at_ceiling(self, mock_pids):
        # 26 processes = at ceiling
        mock_pids.return_value = list(range(100, 126))
        can_admit, reason, info = check_provider_capacity("zai", config_dir=self.config_dir)
        self.assertFalse(can_admit)
        self.assertIn("zai hostwide capacity ceiling (26) reached", reason)
        self.assertEqual(info["live_count"], 26)
        self.assertEqual(info["headroom"], 0)

    @patch("launcher.capacity.get_live_zai_pids")
    def test_check_provider_capacity_outside_project_occupancy_affects_admission(self, mock_pids):
        # Outside project (e.g. ai-shipping-labs) running 28 backends
        mock_pids.return_value = list(range(200, 228))
        can_admit, reason, info = check_provider_capacity("zai", config_dir=self.config_dir)
        self.assertFalse(can_admit)
        self.assertIn("total 28 >= 26", reason)
        self.assertEqual(info["live_count"], 28)

    def test_429_cooldown_lifecycle(self):
        # Record cooldown of 10s
        record_429_event("zai", retry_after_sec=10.0, reason="Rate limited", config_dir=self.config_dir)
        is_cooling, remaining = check_cooldown("zai", config_dir=self.config_dir)
        self.assertTrue(is_cooling)
        self.assertGreater(remaining, 5.0)

        # check_provider_capacity must reject
        can_admit, reason, info = check_provider_capacity("zai", config_dir=self.config_dir)
        self.assertFalse(can_admit)
        self.assertIn("429 cooldown active", reason)

        # reserve_provider_slot must raise CooldownActive
        with self.assertRaises(CooldownActive):
            reserve_provider_slot("zai", "task-fail", config_dir=self.config_dir)

    @patch("launcher.capacity.get_live_zai_pids", return_value=[1, 2])
    def test_atomic_reservation_and_release(self, mock_pids):
        tok1 = reserve_provider_slot("zai", "task-1", config_dir=self.config_dir)
        self.assertTrue(tok1.startswith("slot-zai-"))

        # Verify active count is 2 live + 1 reserved = 3
        _, _, info = check_provider_capacity("zai", config_dir=self.config_dir)
        self.assertEqual(info["reserved_count"], 1)
        self.assertEqual(info["total_active"], 3)

        # Release
        release_provider_slot(tok1, config_dir=self.config_dir)
        _, _, info2 = check_provider_capacity("zai", config_dir=self.config_dir)
        self.assertEqual(info2["reserved_count"], 0)
        self.assertEqual(info2["total_active"], 2)

    @patch("launcher.capacity.get_live_zai_pids", return_value=[1, 2])
    def test_provider_reservation_context_manager_cleans_up_on_error(self, mock_pids):
        try:
            with provider_reservation("zai", "task-err", config_dir=self.config_dir) as token:
                _, _, info = check_provider_capacity("zai", config_dir=self.config_dir)
                self.assertEqual(info["reserved_count"], 1)
                raise RuntimeError("simulated task error")
        except RuntimeError:
            pass

        # Verify slot released after exception
        _, _, info2 = check_provider_capacity("zai", config_dir=self.config_dir)
        self.assertEqual(info2["reserved_count"], 0)

    @patch("launcher.capacity.get_live_zai_pids")
    def test_reservation_exceeds_ceiling_raises(self, mock_pids):
        mock_pids.return_value = list(range(26))  # 26 live
        with self.assertRaises(ConcurrencyLimitExceeded):
            reserve_provider_slot("zai", "task-over", config_dir=self.config_dir)

    @patch("launcher.capacity.get_live_zai_pids")
    def test_validate_quse_capacity_check_excludes_capped_zai_and_keeps_antigravity(self, mock_pids):
        # 31 live outside processes
        mock_pids.return_value = list(range(31))
        valid_routes, rejections = validate_quse(
            self.sample_quse,
            check_capacity=True,
            config_dir=self.config_dir,
        )

        providers = [r["provider"] for r in valid_routes]
        self.assertNotIn("zai", providers)
        self.assertIn("antigravity", providers)
        self.assertIn("zai", rejections)
        self.assertIn("capacity ceiling (26) reached", rejections["zai"])

    def test_default_codex_capacity_unconstrained_by_arbitrary_ceiling(self):
        can_admit, reason, info = check_provider_capacity("codex", config_dir=self.config_dir)
        self.assertTrue(can_admit)
        self.assertIsNone(reason)
        self.assertTrue(info.get("admitted"))

    def test_explicit_max_cap_under_and_at_ceiling(self):
        # When caller/task explicitly specifies max_cap, it is strictly enforced
        can_admit, reason, info = check_provider_capacity("codex", max_cap=5, config_dir=self.config_dir)
        self.assertTrue(can_admit)
        self.assertEqual(info["ceiling"], 5)
        self.assertEqual(info["headroom"], 5)

        tokens = []
        for i in range(5):
            tok = reserve_provider_slot("codex", f"task-codex-{i}", max_cap=5, config_dir=self.config_dir)
            tokens.append(tok)

        # 6th attempt rejected
        can_admit, reason, info = check_provider_capacity("codex", max_cap=5, config_dir=self.config_dir)
        self.assertFalse(can_admit)
        self.assertIn("ceiling (5) reached", reason)

        with self.assertRaises(ConcurrencyLimitExceeded):
            reserve_provider_slot("codex", "task-codex-overflow", max_cap=5, config_dir=self.config_dir)

        release_provider_slot(tokens[0], config_dir=self.config_dir)
        can_admit, _, info = check_provider_capacity("codex", max_cap=5, config_dir=self.config_dir)
        self.assertTrue(can_admit)
        self.assertEqual(info["headroom"], 1)


if __name__ == "__main__":
    unittest.main()
