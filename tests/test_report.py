import json
import tempfile
import os
import unittest
from datetime import datetime, timezone

from launcher.cli import build_report
from launcher.store import Store, StateTransitionError

PAYLOAD = {"goal": "test", "owner": "head-x", "cwd": ".", "timeout": 600}

class TestReportContract(unittest.TestCase):
    def test_usage_null_and_quota_separate(self):
        fd, db = tempfile.mkstemp()
        os.close(fd)
        try:
            store = Store(db)
            store.submit_task("t1", "key1", PAYLOAD, [])
            projection = build_report(store, now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc))
            self.assertEqual(projection["timezone"], "Europe/Berlin")
            found = None
            for tasks in projection["buckets"].values():
                for t in tasks:
                    if t["id"] == "t1":
                        found = t
            self.assertIsNotNone(found)
            # Unproven usage stays null, never zero-fabricated.
            self.assertIsNone(found["usage"]["input_tokens"])
            self.assertIsNone(found["usage"]["output_tokens"])
            self.assertEqual(found["usage"]["source"], "unproven")
            # Quota is a separate object from usage.
            self.assertIn("quota", found)
            self.assertIsNone(found["quota"]["percent_delta"])
            # Coverage and gaps explicit.
            self.assertEqual(projection["coverage"]["window_hours"], 24)
            self.assertIsInstance(projection["coverage"]["gaps_within_last_24h"], list)
        finally:
            os.remove(db)

class TestCliAcceptOnQueued(unittest.TestCase):
    def test_accept_on_queued_via_store_guard(self):
        fd, db = tempfile.mkstemp()
        os.close(fd)
        try:
            store = Store(db)
            store.submit_task("t1", "key1", PAYLOAD, [])
            with self.assertRaises(StateTransitionError) as ctx:
                store.accept_task("t1", "head-x")
            self.assertIn("cannot transition to accepted", str(ctx.exception))
            # state unchanged
            self.assertEqual(store.get_task("t1")["state"], "queued")
        finally:
            os.remove(db)

if __name__ == '__main__':
    unittest.main()
