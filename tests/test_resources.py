import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from launcher.resources import check_resources

GiB = 1024 * 1024 * 1024

class DiskUsage:
    def __init__(self, free):
        self.free = free

class TestResources(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.owned_tmp = Path(self.repo) / ".local" / "tmp" / "run1"
        self.owned_tmp.mkdir(parents=True)
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
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaisesRegex(ValueError, "TMPDIR must resolve under owned"):
                check_resources(1000, self.repo, other, repo_root=self.repo)

    def test_reject_sibling_repo_local_tmp(self):
        # A sibling checkout's .local/tmp must not satisfy containment.
        with tempfile.TemporaryDirectory() as sibling:
            sib_tmp = Path(sibling) / ".local" / "tmp"
            sib_tmp.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "TMPDIR must resolve under owned"):
                check_resources(1000, self.repo, str(sib_tmp), repo_root=self.repo)

    def test_repo_root_required(self):
        with self.assertRaisesRegex(ValueError, "repo root required"):
            check_resources(1000, self.repo, str(self.owned_tmp), repo_root=None)

    def test_disk_floor(self):
        with patch('shutil.disk_usage', return_value=DiskUsage(51 * GiB)):
            with self.assertRaisesRegex(ValueError, "free < 50GiB"):
                check_resources(1000, self.repo, str(self.owned_tmp),
                                active_disk_mb=1500, repo_root=self.repo)

if __name__ == '__main__':
    unittest.main()
