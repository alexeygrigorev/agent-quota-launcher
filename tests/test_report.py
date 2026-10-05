import json
import io
import shutil
import tempfile
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from launcher.cli import build_report, report
from launcher.store import Store, StateTransitionError

PAYLOAD = {"goal": "test", "owner": "head-x", "cwd": ".", "timeout": 600}

def argparse_ns(**kw):
    return SimpleNamespace(**kw)

class TestReportContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.store = Store(self.db)
        # as_of on an hour boundary minus 14 minutes: the window
        # [as_of-24h, as_of) then covers exactly 24 hourly buckets,
        # 11:00Z on the previous day through 11:00Z on as_of's day.
        self.now = datetime(2026, 10, 4, 12, 14, tzinfo=timezone.utc)
        self.store.submit_task("t1", "key1", PAYLOAD, [])
        # Pin created_at inside the frozen [as_of-24h, as_of) window.
        with self.store.transaction() as conn:
            conn.execute("UPDATE tasks SET created_at = '2026-10-04 12:00:00' "
                         "WHERE id = 't1'")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def projection(self, **kw):
        return build_report(self.store, now=self.now, **kw)

    def test_usage_null_and_quota_separate(self):
        projection = self.projection(project_id="proj-x")
        self.assertEqual(projection["timezone"], "UTC")
        self.assertEqual(projection["project_id"], "proj-x")
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

    def test_utc_z_window_is_half_open_24h(self):
        projection = self.projection()
        cov = projection["coverage"]
        self.assertEqual(cov["window_hours"], 24)
        self.assertEqual(cov["window_start"], "2026-10-03T12:14:00Z")
        self.assertEqual(cov["window_end"], "2026-10-04T12:14:00Z")
        self.assertEqual(cov["window_half_open"], "[window_start, window_end)")
        # Every bucket label is UTC Z; tile hours lie within [window_start, now)
        for label in cov["gaps_within_window"]:
            self.assertTrue(label.endswith("Z"), label)
            h = datetime.strptime(label, "%Y-%m-%dT%H:00:00Z").replace(tzinfo=timezone.utc)
            self.assertGreaterEqual(h, self.now - timedelta(hours=24))
            self.assertLess(h, self.now)
        # Non-hour as_of (12:14): exact 24 hourly buckets on half-open [as_of-24h, as_of) Z
        self.assertNotIn("2026-10-03T12:00:00Z", cov["gaps_within_window"])
        self.assertIn("2026-10-03T13:00:00Z", cov["gaps_within_window"])
        self.assertEqual(len(cov["gaps_within_window"]) + len(projection["buckets"]), 24)

    def test_default_project_id_is_quota_launcher(self):
        # Default project_id is quota-launcher (not cwd basename agent-quota-launcher)
        projection = self.projection()
        self.assertEqual(projection["project_id"], "quota-launcher")

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = report(argparse_ns(jsonl=True, config_dir=self.tmp))
        self.assertEqual(rc, 0)
        lines = [json.loads(l) for l in buf.getvalue().strip().splitlines() if l]
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual(line["project_id"], "quota-launcher")

    def test_exact_24_buckets_on_hour_boundary(self):
        now_hour = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        proj = build_report(self.store, now=now_hour)
        cov = proj["coverage"]
        self.assertEqual(len(cov["gaps_within_window"]) + len(proj["buckets"]), 24)
        self.assertEqual(cov["window_start"], "2026-10-03T12:00:00Z")
        self.assertEqual(cov["window_end"], "2026-10-04T12:00:00Z")

    def test_offset_timestamps_converted_not_relabelled(self):
        # +02:00 wall time 14:00 is 12:00Z: it must land in the 12:00Z bucket
        # (same hour as t1), never at its naive wall-clock label.
        self.store.submit_task("t2", "key2", PAYLOAD, [])
        with self.store.transaction() as conn:
            conn.execute("UPDATE tasks SET created_at = '2026-10-04 14:00:00+02:00' "
                         "WHERE id = 't2'")
        projection = self.projection()
        bucket_ids = [t["id"] for t in projection["buckets"]["2026-10-04T12:00:00Z"]]
        self.assertIn("t2", bucket_ids)
        self.assertEqual(projection["coverage"]["tasks_outside_window"], 0)

    def test_dst_offset_timestamp_lands_on_utc_instant(self):
        # Berlin summer offset +02:00: 15:30+02:00 is 13:30Z on the previous
        # day — inside the window, in the 13:00Z bucket. A naive
        # replace(tzinfo=UTC) would have produced the 15:00Z label instead.
        self.store.submit_task("t3", "key3", PAYLOAD, [])
        with self.store.transaction() as conn:
            conn.execute("UPDATE tasks SET created_at = '2026-10-03 15:30:00+02:00' "
                         "WHERE id = 't3'")
        projection = self.projection()
        self.assertIn("2026-10-03T13:00:00Z", projection["buckets"])
        bucket_ids = [t["id"] for t in projection["buckets"]["2026-10-03T13:00:00Z"]]
        self.assertIn("t3", bucket_ids)

    def test_malformed_created_at_is_invalid_not_outside(self):
        self.store.submit_task("t4", "key4", PAYLOAD, [])
        with self.store.transaction() as conn:
            conn.execute("UPDATE tasks SET created_at = 'not-a-timestamp' "
                         "WHERE id = 't4'")
        projection = self.projection()
        cov = projection["coverage"]
        self.assertEqual(cov["created_at_invalid"], 1)
        self.assertEqual(cov["tasks_outside_window"], 0)
        self.assertNotIn("t4", json.dumps(projection["buckets"]))

    def test_task_outside_window_counted_not_emitted(self):
        with self.store.transaction() as conn:
            conn.execute("UPDATE tasks SET created_at = '2026-09-01 08:00:00' "
                         "WHERE id = 't1'")
        projection = self.projection()
        self.assertEqual(projection["coverage"]["tasks_outside_window"], 1)
        self.assertIsNone(projection["coverage"]["first_bucket"])
        self.assertNotIn("t1", json.dumps(projection["buckets"]))

    def test_first_partial_hour_task_placed_in_first_bucket_not_outside(self):
        # Window start is 2026-10-03T12:14:00Z. A task at 12:20:00 is within
        # [window_start, window_end) but before the first clock-hour boundary 13:00:00Z.
        # It must land in the first bucket (13:00:00Z) and NOT be counted outside window.
        self.store.submit_task("t_partial", "key_partial", PAYLOAD, [])
        with self.store.transaction() as conn:
            conn.execute("UPDATE tasks SET created_at = '2026-10-03 12:20:00' "
                         "WHERE id = 't_partial'")
        projection = self.projection()
        cov = projection["coverage"]
        self.assertEqual(cov["tasks_outside_window"], 0)
        self.assertIn("2026-10-03T13:00:00Z", projection["buckets"])
        bucket_ids = [t["id"] for t in projection["buckets"]["2026-10-03T13:00:00Z"]]
        self.assertIn("t_partial", bucket_ids)

    def test_task_before_window_start_counted_outside_window(self):
        # Window start is 2026-10-03T12:14:00Z. A task at 12:10:00 is 4 minutes
        # before the window and must be counted outside window and not emitted.
        self.store.submit_task("t_early", "key_early", PAYLOAD, [])
        with self.store.transaction() as conn:
            conn.execute("UPDATE tasks SET created_at = '2026-10-03 12:10:00' "
                         "WHERE id = 't_early'")
        projection = self.projection()
        cov = projection["coverage"]
        self.assertEqual(cov["tasks_outside_window"], 1)
        all_emitted_ids = [t["id"] for bucket in projection["buckets"].values() for t in bucket]
        self.assertNotIn("t_early", all_emitted_ids)

    def test_jsonl_lines_carry_project_id_and_coverage_last(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = report(argparse_ns(project_id="proj-x", jsonl=True,
                                    config_dir=self.tmp))
        self.assertEqual(rc, 0)
        lines = [json.loads(l) for l in buf.getvalue().strip().splitlines() if l]
        self.assertTrue(lines)
        for line in lines[:-1]:
            self.assertEqual(line["project_id"], "proj-x")
            self.assertIn("bucket", line)
            self.assertIn("tasks", line)
        last = lines[-1]
        self.assertEqual(last["project_id"], "proj-x")
        self.assertIn("coverage", last)
        self.assertNotIn("bucket", last)
        self.assertEqual([l["bucket"] for l in lines[:-1]],
                         sorted(l["bucket"] for l in lines[:-1]))

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
