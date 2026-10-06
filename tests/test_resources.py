import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from launcher.resources import (
    MIN_DISK_FREE_BYTES,
    WARN_DISK_FREE_BYTES,
    check_disk_pressure,
    check_resources,
)

GiB = 1024 * 1024 * 1024

LOCAL_TMP_DIR = Path(__file__).resolve().parent.parent / ".local" / "tmp"


class DiskUsage:
    def __init__(self, free):
        self.free = free

class TestResources(unittest.TestCase):
    def setUp(self):
        import shutil
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.repo = tempfile.mkdtemp(dir=LOCAL_TMP_DIR)
        self.owned_tmp = Path(self.repo) / ".local" / "tmp" / "run1"
        self.owned_tmp.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.mem_patch = patch('launcher.resources.get_mem_available',
                               return_value=15 * GiB)
        self.mem_patch.start()
        self.disk_patch = patch('shutil.disk_usage',
                                return_value=DiskUsage(100 * GiB))
        self.disk_patch.start()
        self.addCleanup(self.mem_patch.stop)
        self.addCleanup(self.disk_patch.stop)

    def test_owned_tmpdir_passes(self):
        self.assertTrue(check_resources(1000, self.repo, str(self.owned_tmp),
                                        repo_root=self.repo))

    def test_memory_over_1500(self):
        with self.assertRaisesRegex(ValueError, "requested worker memory > 1500MiB"):
            check_resources(1600, self.repo, str(self.owned_tmp), repo_root=self.repo)

    def test_memavailable_floor(self):
        import launcher.resources as res
        with patch.object(res, 'get_mem_available', return_value=10.5 * GiB):
            with self.assertRaisesRegex(ValueError, "host MemAvailable < 10GiB"):
                check_resources(1000, self.repo, str(self.owned_tmp), repo_root=self.repo)

    def test_active_reservations_counted(self):
        # 11 GiB available, 1.5 GiB already reserved -> below floor after request.
        import launcher.resources as res
        with patch.object(res, 'get_mem_available', return_value=11 * GiB):
            with self.assertRaisesRegex(ValueError, "host MemAvailable < 10GiB"):
                check_resources(1500, self.repo, str(self.owned_tmp),
                                active_mem_mb=1500, repo_root=self.repo)

    def test_reject_tmp(self):
        with self.assertRaisesRegex(ValueError, "reject /tmp"):
            check_resources(1000, self.repo, "/tmp/foo", repo_root=self.repo)

    def test_reject_tmp_subdir_path_trick(self):
        # /tmp/.local/tmp must not pass a substring check either.
        with self.assertRaisesRegex(ValueError, "reject /tmp"):
            check_resources(1000, self.repo, "/tmp/.local/tmp/x", repo_root=self.repo)

    def test_reject_tmpdir_outside_owned_root(self):
        with tempfile.TemporaryDirectory(dir=LOCAL_TMP_DIR) as other:
            with self.assertRaisesRegex(ValueError, "TMPDIR must resolve under owned"):
                check_resources(1000, self.repo, other, repo_root=self.repo)

    def test_reject_sibling_repo_local_tmp(self):
        # A sibling checkout's .local/tmp must not satisfy containment.
        with tempfile.TemporaryDirectory(dir=LOCAL_TMP_DIR) as sibling:
            sib_tmp = Path(sibling) / ".local" / "tmp"
            sib_tmp.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "TMPDIR must resolve under owned"):
                check_resources(1000, self.repo, str(sib_tmp), repo_root=self.repo)

    def test_non_existent_contained_tmpdir_passes(self):
        # non-existent declared scratch path under repo_root/.local/tmp passes admission
        non_existent_tmp = self.owned_tmp / "does_not_exist_yet"
        self.assertFalse(non_existent_tmp.exists())
        self.assertTrue(check_resources(1000, self.repo, str(non_existent_tmp), repo_root=self.repo))

    def test_non_existent_uncontained_tmpdir_rejected(self):
        # non-existent scratch path outside repo_root/.local/tmp is rejected without mkdir outside taskscope
        outside_tmp = Path(self.repo) / "other_dir" / "does_not_exist_yet"
        self.assertFalse(outside_tmp.exists())
        with self.assertRaisesRegex(ValueError, "TMPDIR must resolve under owned"):
            check_resources(1000, self.repo, str(outside_tmp), repo_root=self.repo)
        self.assertFalse(outside_tmp.exists())

    def test_repo_root_required(self):
        with self.assertRaisesRegex(ValueError, "repo root required"):
            check_resources(1000, self.repo, str(self.owned_tmp), repo_root=None)


    def test_estimate_stage_memory_bytes(self):
        from launcher.resources import estimate_stage_memory_bytes
        # 350 + 140 = 490
        # 10 * 490 = 4900 MiB = 5138022400 bytes
        self.assertEqual(estimate_stage_memory_bytes(10), 10 * (350 + 140) * 1024 * 1024)
        self.assertEqual(estimate_stage_memory_bytes(25), 25 * (350 + 140) * 1024 * 1024)
        self.assertEqual(estimate_stage_memory_bytes(50), 50 * (350 + 140) * 1024 * 1024)
        
        # 768 + 140 = 908
        self.assertEqual(estimate_stage_memory_bytes(10, peak=True), 10 * (768 + 140) * 1024 * 1024)
        self.assertEqual(estimate_stage_memory_bytes(50, peak=True), 50 * (768 + 140) * 1024 * 1024)

    def test_evaluate_stage_capacity(self):
        from launcher.resources import evaluate_stage_capacity
        # 28 GiB available
        mem_28 = 28 * 1024 * 1024 * 1024
        
        # Stage 50 typical: 50 * 490 MiB = 24500 MiB = 23.92 GiB
        # Remaining = 28 - 23.92 = 4.08 GiB > 3 GiB (feasible)
        res_50 = evaluate_stage_capacity(50, mem_available_bytes=mem_28)
        self.assertTrue(res_50["feasible"])
        self.assertEqual(res_50["target_stage"], 50)
        
        # Stage 50 peak: 50 * 908 MiB = 45400 MiB = 44.33 GiB > 28 GiB (not feasible)
        res_50_peak = evaluate_stage_capacity(50, mem_available_bytes=mem_28, peak=True)
        self.assertFalse(res_50_peak["feasible"])
        
        # Evaluate throttling with lower mem available (e.g. 15 GiB)
        mem_15 = 15 * 1024 * 1024 * 1024
        # Stage 25 typical: 25 * 490 MiB = 12250 MiB = 11.96 GiB
        # Remaining = 15 - 11.96 = 3.04 GiB > 3 GiB (feasible)
        res_25 = evaluate_stage_capacity(25, mem_available_bytes=mem_15)
        self.assertTrue(res_25["feasible"])
        
        # Stage 25 peak: 25 * 908 MiB = 22700 MiB = 22.16 GiB (not feasible)
        res_25_peak = evaluate_stage_capacity(25, mem_available_bytes=mem_15, peak=True)
        self.assertFalse(res_25_peak["feasible"])
        
        # current_active adjustment
        # Want stage 50, but 30 already active, so we only need 20 more.
        # 20 * 490 MiB = 9800 MiB = 9.57 GiB
        # Remaining from 15 = 15 - 9.57 = 5.43 GiB (feasible)
        res_active = evaluate_stage_capacity(50, current_active=30, mem_available_bytes=mem_15)
        self.assertTrue(res_active["feasible"])

    def test_get_max_feasible_stage(self):
        from launcher.resources import get_max_feasible_stage
        
        mem_28 = 28 * 1024 * 1024 * 1024
        self.assertEqual(get_max_feasible_stage(mem_available_bytes=mem_28), 50)
        
        mem_15 = 15 * 1024 * 1024 * 1024
        self.assertEqual(get_max_feasible_stage(mem_available_bytes=mem_15), 25)
        
        mem_8 = 8 * 1024 * 1024 * 1024
        self.assertEqual(get_max_feasible_stage(mem_available_bytes=mem_8), 10)
        
        mem_2 = 2 * 1024 * 1024 * 1024
        self.assertEqual(get_max_feasible_stage(mem_available_bytes=mem_2), 0)

    def test_check_resources_target_stage(self):
        from launcher.resources import check_resources
        
        # 15 GiB MemAvailable. Can support 25 typical, but NOT 50 typical.
        # Wait, check_resources doesn't pass current_active to evaluate_stage_capacity.
        
        # Should pass for target_stage 25
        # mem_patch is 15 GiB in setUp
        self.assertTrue(check_resources(1000, self.repo, str(self.owned_tmp), repo_root=self.repo, target_stage=25))
        
        # Should fail for target_stage 50
        with self.assertRaisesRegex(ValueError, "stage 50 capacity exceeded"):
            check_resources(1000, self.repo, str(self.owned_tmp), repo_root=self.repo, target_stage=50)

    def test_disk_floor(self):
        with patch('shutil.disk_usage', return_value=DiskUsage(21 * GiB)):
            with self.assertRaisesRegex(ValueError, "free < 20GiB"):
                check_resources(1000, self.repo, str(self.owned_tmp),
                                active_disk_mb=1500, repo_root=self.repo)

    def test_disk_pressure_hard_floor_exceeded(self):
        with patch('shutil.disk_usage', return_value=DiskUsage(19 * GiB)):
            res = check_disk_pressure(self.repo, str(self.owned_tmp))
            self.assertEqual(res["status"], "hard_floor_exceeded")
            self.assertTrue(res["pressure"])
            self.assertEqual(res["free_bytes"], 19 * GiB)
            self.assertFalse(res["eligible_continue"])

    def test_disk_pressure_at_29_gib(self):
        with patch('shutil.disk_usage', return_value=DiskUsage(29 * GiB)):
            res = check_disk_pressure(self.repo, str(self.owned_tmp))
            self.assertEqual(res["status"], "pressure")
            self.assertTrue(res["pressure"])
            self.assertEqual(res["free_bytes"], 29 * GiB)
            self.assertTrue(res["eligible_continue"])
            self.assertTrue(res["enqueue_cleanup"])

    def test_disk_pressure_at_30_gib_ok_and_rearm(self):
        with patch('shutil.disk_usage', return_value=DiskUsage(30 * GiB)):
            res = check_disk_pressure(self.repo, str(self.owned_tmp))
            self.assertEqual(res["status"], "ok")
            self.assertFalse(res["pressure"])
            self.assertEqual(res["free_bytes"], 30 * GiB)
            self.assertTrue(res["eligible_continue"])
            self.assertFalse(res["enqueue_cleanup"])
            self.assertTrue(res["rearm"])

    def test_disk_pressure_episode_deduplication_and_rearm(self):
        episode_state = {}
        # 1. First probe at 29 GiB: pressure detected, enqueues cleanup
        with patch('shutil.disk_usage', return_value=DiskUsage(29 * GiB)):
            res1 = check_disk_pressure(self.repo, str(self.owned_tmp), episode_state=episode_state)
            self.assertEqual(res1["status"], "pressure")
            self.assertTrue(res1["enqueue_cleanup"])
            self.assertTrue(episode_state["in_episode"])
            self.assertEqual(episode_state["episode_id"], 1)

        # 2. Second probe at 29 GiB (same episode): deduplication suppresses cleanup
        with patch('shutil.disk_usage', return_value=DiskUsage(29 * GiB)):
            res2 = check_disk_pressure(self.repo, str(self.owned_tmp), episode_state=episode_state)
            self.assertEqual(res2["status"], "pressure")
            self.assertFalse(res2["enqueue_cleanup"])
            self.assertTrue(res2["eligible_continue"])
            self.assertTrue(episode_state["in_episode"])

        # 3. Third probe recovers to 35 GiB: pressure cleared, rearm enabled
        with patch('shutil.disk_usage', return_value=DiskUsage(35 * GiB)):
            res3 = check_disk_pressure(self.repo, str(self.owned_tmp), episode_state=episode_state)
            self.assertEqual(res3["status"], "ok")
            self.assertFalse(res3["pressure"])
            self.assertTrue(res3["rearm"])
            self.assertFalse(episode_state["in_episode"])

        # 4. Fourth probe drops to 28 GiB: new episode triggered, enqueues cleanup for episode 2
        with patch('shutil.disk_usage', return_value=DiskUsage(28 * GiB)):
            res4 = check_disk_pressure(self.repo, str(self.owned_tmp), episode_state=episode_state)
            self.assertEqual(res4["status"], "pressure")
            self.assertTrue(res4["enqueue_cleanup"])
            self.assertTrue(episode_state["in_episode"])
            self.assertEqual(episode_state["episode_id"], 2)

if __name__ == '__main__':
    unittest.main()
