import unittest
import tempfile
import os
from pathlib import Path
from launcher.store import Store

class TestStore(unittest.TestCase):
    def setUp(self):
        self.fd, self.db_path = tempfile.mkstemp()
        self.store = Store(self.db_path)

    def tearDown(self):
        os.close(self.fd)
        os.remove(self.db_path)

    def test_submit_idempotent(self):
        payload = {"goal": "test", "owner": "test", "cwd": ".", "timeout": 300}
        task_id = "t1"
        res1 = self.store.submit_task(task_id, "key1", payload, [])
        self.assertEqual(res1, "t1")
        
        # Exact same payload -> returns original task_id
        res2 = self.store.submit_task("t2", "key1", payload, [])
        self.assertEqual(res2, "t1")
        
        # Different payload -> conflict
        payload_diff = {"goal": "different", "owner": "test", "cwd": ".", "timeout": 300}
        with self.assertRaises(ValueError):
            self.store.submit_task("t3", "key1", payload_diff, [])

    def test_path_overlap(self):
        payload = {"goal": "test", "owner": "test", "cwd": ".", "timeout": 300}
        with tempfile.TemporaryDirectory() as d:
            dir_path = Path(d)
            p1 = dir_path / "a" / "b"
            p1.mkdir(parents=True)
            p2 = dir_path / "a"
            
            # Submit first task owning p1
            self.store.submit_task("t1", "key1", payload, [str(p1)])
            
            # Submit task owning p2 (which is parent of p1)
            with self.assertRaises(ValueError) as ctx:
                self.store.submit_task("t2", "key2", payload, [str(p2)])
            self.assertIn("Path overlap", str(ctx.exception))
            
            # Submitting task owning same path p1
            with self.assertRaises(ValueError):
                self.store.submit_task("t3", "key3", payload, [str(p1)])

            # Submitting task owning sibling path p3 should be fine
            p3 = dir_path / "a" / "c"
            p3.mkdir()
            self.store.submit_task("t4", "key4", payload, [str(p3)])

if __name__ == '__main__':
    unittest.main()
