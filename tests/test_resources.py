import unittest
from unittest.mock import patch
from launcher.resources import check_resources

class TestResources(unittest.TestCase):
    @patch('launcher.resources.get_mem_available')
    @patch('shutil.disk_usage')
    def test_check_resources(self, mock_disk_usage, mock_mem):
        mock_mem.return_value = 15 * 1024 * 1024 * 1024 # 15 GiB
        
        class DiskUsage:
            free = 100 * 1024 * 1024 * 1024 # 100 GiB
        mock_disk_usage.return_value = DiskUsage()
        
        # Should pass
        self.assertTrue(check_resources(1000, ".", ".local/tmp/1"))
        
        # Test memory limit > 1500
        with self.assertRaisesRegex(ValueError, "requested worker memory > 1500MiB"):
            check_resources(1600, ".", ".local/tmp/1")
            
        # Test MemAvailable < 10GiB
        mock_mem.return_value = 10.5 * 1024 * 1024 * 1024 # 10.5 GiB
        with self.assertRaisesRegex(ValueError, "host MemAvailable < 10GiB"):
            check_resources(1000, ".", ".local/tmp/1")
            
        # Test tmpdir path reject /tmp
        mock_mem.return_value = 15 * 1024 * 1024 * 1024 # 15 GiB
        with self.assertRaisesRegex(ValueError, "reject /tmp"):
            check_resources(1000, ".", "/tmp/foo")
            
        # Test tmpdir needs .local/tmp
        with self.assertRaisesRegex(ValueError, "TMPDIR under owned .local/tmp on root required"):
            check_resources(1000, ".", "/var/tmp")

if __name__ == '__main__':
    unittest.main()
