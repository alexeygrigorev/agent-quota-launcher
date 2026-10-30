"""Intake de54b9ae: a headless session that exits cleanly before the first-action
validator samples is a success with a native receipt; a real early death is not."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher.launch import clean_exit_receipt
from launcher.store import Store

SID = "ac90cc49-a6a8-4dfd-b487-df893a332d50"
WS = "/tmp/ws-fastfinish"
CREATED = 1793034400000


def write_tomb(state_dir, **over):
    d = Path(state_dir) / "retired-sessions" / SID
    d.mkdir(parents=True)
    tomb = {"cause": "finished", "finished_at_ms": CREATED + 30000, "id": SID,
            "tag": "t1", "workspace": WS,
            "exit": {"code": 0, "exited_at_ms": CREATED + 30000, "oom_killed": False, "signal": None}}
    tomb.update(over)
    for k in [k for k, v in tomb.items() if v is ...]:
        del tomb[k]
    (d / "tombstone.json").write_text(json.dumps(tomb))


class CleanExitReceiptTest(unittest.TestCase):
    def receipt(self, **over):
        with tempfile.TemporaryDirectory() as sd:
            write_tomb(sd, **over)
            return clean_exit_receipt(SID, "t1", WS, CREATED, state_dir=Path(sd))

    def test_clean_exit_is_receipt(self):
        self.assertEqual(self.receipt()["exit_code"], 0)

    def test_missing_exit_record_is_not_success(self):
        self.assertIsNone(self.receipt(exit=...))

    def test_nonzero_exit_is_not_success(self):
        self.assertIsNone(self.receipt(exit={"code": 1, "signal": None, "oom_killed": False}))

    def test_signal_or_oom_is_not_success(self):
        self.assertIsNone(self.receipt(exit={"code": 0, "signal": 9, "oom_killed": False}))
        self.assertIsNone(self.receipt(exit={"code": 0, "signal": None, "oom_killed": True}))

    def test_other_cause_is_not_success(self):
        self.assertIsNone(self.receipt(cause="killed"))

    def test_identity_mismatch_is_not_success(self):
        self.assertIsNone(self.receipt(tag="other"))

    def test_no_tombstone(self):
        with tempfile.TemporaryDirectory() as sd:
            self.assertIsNone(clean_exit_receipt(SID, "t1", WS, CREATED, state_dir=Path(sd)))


class DoRunFastFinishTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        (Path(self.dir) / ".local").mkdir()
        self.db = str(Path(self.dir) / "store.db")
        self.store = Store(self.db)
        self.store.submit_task("task1", "ikey", {"owner": "me", "cwd": self.dir, "timeout": 60}, [self.dir])
        self.lock = str(Path(self.dir) / "launch.lock")

    def run_with(self, receipt):
        start = json.dumps({"id": SID, "tag": "t1", "workspace": self.dir, "engine": "grok",
                            "created_at_ms": CREATED})
        with patch("launcher.launch.fetch_quse"), \
             patch("launcher.launch.validate_quse", return_value=(["grok"], [])), \
             patch("launcher.launch.select_candidate", return_value=({"provider": "grok"}, "t")), \
             patch("launcher.launch.check_resources"), \
             patch("launcher.launch.subprocess.Popen") as popen, \
             patch("launcher.launch.native_status", return_value=("dead", "phase=finished")), \
             patch("launcher.launch.validate_first_action", return_value=False), \
             patch("launcher.launch.clean_exit_receipt", return_value=receipt), \
             patch("launcher.launch.time.sleep"):
            popen.return_value.returncode = 0
            popen.return_value.communicate.return_value = (start, "")
            from launcher.launch import do_run
            return do_run(self.db, "task1", self.dir, None, self.lock)

    def test_clean_finish_before_first_action_is_success(self):
        self.assertEqual(self.run_with({"native_id": SID, "exit_code": 0}), 0)
        self.assertEqual(self.store.get_task("task1")["state"], "completed-awaiting-review")

    def test_death_without_receipt_still_fails(self):
        self.assertEqual(self.run_with(None), 1)
        self.assertEqual(self.store.get_task("task1")["state"], "failed")


if __name__ == "__main__":
    unittest.main()
