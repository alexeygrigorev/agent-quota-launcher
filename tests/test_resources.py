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

    def test_repo_root_required(self):
        with self.assertRaisesRegex(ValueError, "repo root required"):
            check_resources(1000, self.repo, str(self.owned_tmp), repo_root=None)

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
