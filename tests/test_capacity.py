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
    def test_get_live_zai_pids_discovery(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "1001\n1002\n1003\n"
        mock_run.return_value = mock_proc

        with patch("os.path.exists", return_value=True):
            pids = get_live_zai_pids()
            self.assertEqual(pids, [1001, 1002, 1003])

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


if __name__ == "__main__":
    unittest.main()
