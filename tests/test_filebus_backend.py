import json
import tempfile
import unittest
from pathlib import Path

from launcher.filebus_backend import (
    BLOCKED_REASON,
    FileBusBackendError,
    admit_headless,
    attach_filebus_identity,
    attach_filebus_live_identity,
    dispatch_headless_task,
    is_native_whoami,
    plan_filebus_task,
    validate_filebus_identity,
    validate_filebus_live_identity,
)
from launcher.store import Store


REPO = Path("/home/alexey/git/agent-quota-launcher")


class FileBusIdentityTests(unittest.TestCase):
    def test_native_whoami_is_rejected(self):
        rec = {
            "id": "6be4c247-4410-4bdb-968e-7fc2d5844941",
            "tag": "quota-launcher-head",
            "workspace": str(REPO),
            "worker_cgroup": "/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-6be4c247-4410-4bdb-968e-7fc2d5844941.scope",
            "socket_path": "/run/user/1000/aplexer/x.sock",
        }
        self.assertTrue(is_native_whoami(rec))
        self.assertFalse(validate_filebus_identity(rec))

    def test_complete_filebus_identity_accepted(self):
        rec = {
            "task_id": "t-filebus-review-20261005T071104Z-b1a35f",
            "bus_identity": "dogfood-model-reviewer",
            "task_message_id": "f5c30541-db73-4972-962d-5c4112871d75",
            "ack_id": "read-ack-example",
            "reply_id": "edeb1734-192e-4e11-8430-632816a986c3",
            "outcome": "accepted",
            "provider": "grok",
        }
        self.assertTrue(validate_filebus_identity(rec))

    def test_live_identity_rejects_invented_ack(self):
        live = {
            "task_id": "t1",
            "bus_identity": "w1",
            "task_message_id": "m1",
            "provider": "grok",
        }
        self.assertTrue(validate_filebus_live_identity(live))
        invented = dict(live, ack_id="invented-ack", reply_id="invented-reply",
                        outcome="accepted")
        self.assertFalse(validate_filebus_live_identity(invented))
        self.assertTrue(validate_filebus_identity(invented))

    def test_missing_ack_rejected(self):
        rec = {
            "task_id": "t1",
            "bus_identity": "w1",
            "task_message_id": "m1",
            "ack_id": "",
            "reply_id": "r1",
            "outcome": "accepted",
            "provider": "grok",
        }
        self.assertFalse(validate_filebus_identity(rec))


class FileBusDispatchTests(unittest.TestCase):
    def test_dispatch_does_not_call_aplexer(self):
        with self.assertRaises(FileBusBackendError) as ctx:
            dispatch_headless_task({"backend": "filebus", "goal": "census"})
        self.assertIn("live filebus dispatch held", str(ctx.exception))
        self.assertNotIn("aplexer start", str(ctx.exception))

    def test_wrong_backend_rejected(self):
        with self.assertRaises(FileBusBackendError):
            dispatch_headless_task({"backend": "aplexer"})


class FileBusStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "tasks.sqlite"))
        self.payload = {
            "backend": "filebus",
            "owner": "quota-launcher-head",
            "cwd": str(REPO),
            "timeout": 600,
            "goal": "independent-census",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_queues_blocked_without_spawn(self):
        tid = plan_filebus_task(
            self.store, "t-fb-1", "idem-fb-1", self.payload,
            [str(Path(self.tmp.name) / "evidence")],
        )
        row = self.store.get_task(tid)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["payload"]["backend"], "filebus")
        with self.store.get_conn() as conn:
            reason = conn.execute("SELECT reason FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        self.assertEqual(reason, BLOCKED_REASON)

    def test_attach_identity_roundtrip(self):
        tid = plan_filebus_task(
            self.store, "t-fb-2", "idem-fb-2", self.payload,
            [str(Path(self.tmp.name) / "ev2")],
        )
        rec = {
            "task_id": tid,
            "bus_identity": "dogfood-model-reviewer",
            "task_message_id": "f5c30541-db73-4972-962d-5c4112871d75",
            "ack_id": "ack-1",
            "reply_id": "reply-1",
            "outcome": "accepted",
            "provider": "grok",
        }
        path = Path(self.tmp.name) / "receipt.json"
        attach_filebus_identity(self.store, tid, rec, str(path))
        saved = json.loads(path.read_text())
        self.assertEqual(saved["task_message_id"], rec["task_message_id"])

    def test_attach_live_rejects_invented_terminal(self):
        tid = plan_filebus_task(
            self.store, "t-fb-live", "idem-fb-live", self.payload,
            [str(Path(self.tmp.name) / "ev-live")],
        )
        with self.assertRaises(FileBusBackendError):
            attach_filebus_live_identity(
                self.store, tid,
                {
                    "task_id": tid,
                    "bus_identity": "w",
                    "task_message_id": "m",
                    "provider": "grok",
                    "ack_id": "invented",
                    "reply_id": "invented",
                    "outcome": "accepted",
                },
                str(Path(self.tmp.name) / "live.json"),
            )

    def test_attach_rejects_native_whoami(self):
        tid = plan_filebus_task(
            self.store, "t-fb-3", "idem-fb-3", self.payload,
            [str(Path(self.tmp.name) / "ev3")],
        )
        with self.assertRaises(FileBusBackendError):
            attach_filebus_identity(
                self.store, tid,
                {
                    "task_id": tid,
                    "bus_identity": "x",
                    "task_message_id": "m",
                    "ack_id": "a",
                    "reply_id": "r",
                    "outcome": "accepted",
                    "provider": "grok",
                    "worker_cgroup": "/user.slice/fake",
                },
                str(Path(self.tmp.name) / "bad.json"),
            )


class FileBusAdmissionTests(unittest.TestCase):
    def test_tmp_not_under_repo_rejected(self):
        quse = {
            "ok": True,
            "provider": "grok",
            "remaining_pct": 65.0,
            "windows": [{"name": "7d", "remaining_pct": 65.0}],
        }
        with self.assertRaises(Exception):
            admit_headless(quse, 1500, str(REPO), "/tmp", 0, 0)


if __name__ == "__main__":
    unittest.main()
