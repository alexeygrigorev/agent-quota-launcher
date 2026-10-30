"""Intake de54b9ae: a headless session that exits cleanly before the first-action
validator samples is a success with a native receipt — but only when it did real
work (a validated first action on a post-death re-check). A zero-work exit-0
death and a dirty death stay failed, and _run_with_host_hint must print the
receipt JSON so Hub records exited/done instead of launcher_command_failed.

aplexer reports the session workspace as the launched --cwd, so identity checks
accept a subdirectory cwd under the verified --workspace and nothing else."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from launcher.cli import _run_with_host_hint
from launcher.launch import clean_exit_receipt, validate_first_action, run_tag_for
from launcher.store import Store

SID = "ac90cc49-a6a8-4dfd-b487-df893a332d50"
CREATED = 1793034400000


def write_tomb(state_dir, tag="t1", workspace=..., **over):
    d = Path(state_dir) / "retired-sessions" / SID
    d.mkdir(parents=True)
    tomb = {"cause": "finished", "finished_at_ms": CREATED + 30000, "id": SID,
            "tag": tag, "workspace": workspace,
            "exit": {"code": 0, "exited_at_ms": CREATED + 30000, "oom_killed": False, "signal": None}}
    tomb.update(over)
    for k in [k for k, v in tomb.items() if v is ...]:
        del tomb[k]
    (d / "tombstone.json").write_text(json.dumps(tomb))


class CleanExitReceiptTest(unittest.TestCase):
    def receipt(self, ws="/tmp/ws-fastfinish", aliases=None, **over):
        with tempfile.TemporaryDirectory() as sd:
            write_tomb(sd, workspace=ws, **over)
            return clean_exit_receipt(SID, "t1", "/tmp/ws-fastfinish", CREATED,
                                      state_dir=Path(sd), workspace_aliases=aliases)

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
            self.assertIsNone(clean_exit_receipt(SID, "t1", "/tmp/ws-fastfinish", CREATED,
                                                 state_dir=Path(sd)))

    def test_cwd_subdir_tombstone_verifies_only_with_alias(self):
        # aplexer records the session workspace as the launched --cwd: a
        # tombstone naming a subdirectory of the verified workspace verifies
        # only when that cwd alias is passed (intake de54b9ae finding 3).
        with tempfile.TemporaryDirectory() as sd:
            ws = Path(sd) / "ws"
            cwd = ws / "docs"
            cwd.mkdir(parents=True)
            write_tomb(sd, workspace=str(cwd))
            self.assertIsNone(clean_exit_receipt(SID, "t1", str(ws), CREATED,
                                                 state_dir=Path(sd)))
            receipt = clean_exit_receipt(SID, "t1", str(ws), CREATED,
                                         state_dir=Path(sd), workspace_aliases=[str(cwd)])
            self.assertEqual(receipt["exit_code"], 0)

    def test_alias_outside_workspace_never_verifies(self):
        # No loosening: an alias that does not resolve under the verified
        # workspace is ignored even when the tombstone matches it exactly.
        with tempfile.TemporaryDirectory() as sd:
            ws = Path(sd) / "ws"
            elsewhere = Path(sd) / "elsewhere"
            elsewhere.mkdir(parents=True)
            write_tomb(sd, workspace=str(elsewhere))
            self.assertIsNone(clean_exit_receipt(SID, "t1", str(ws), CREATED,
                                                 state_dir=Path(sd),
                                                 workspace_aliases=[str(elsewhere)]))


class ValidateFirstActionWorkspaceTest(unittest.TestCase):
    """The incident shape: --workspace repo --cwd repo/docs makes a genuine
    live whoami report workspace=repo/docs; the validator must accept it via
    the cwd alias and nothing wider."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ws = Path(self.dir) / "repo"
        self.cwd = self.ws / "docs"
        self.cwd.mkdir(parents=True)
        self.fa = Path(self.dir) / "first-action-t1.json"
        self.min_mtime = time.time() - 60
        self.start = {"schema_version": 1, "id": "sess-ws", "tag": "task-ws",
                      "workspace": str(self.ws), "engine": "claude",
                      "phase": "starting", "command": ["claude", "-p"],
                      "created_at_ms": 1791116912591}
        self.now_ms = self.start["created_at_ms"] + 60_000

    def check(self, whoami_workspace, aliases):
        data = dict(self.start, workspace=whoami_workspace, phase="running",
                    created_at_ms=self.start["created_at_ms"] + 5000,
                    updated_at_ms=self.start["created_at_ms"] + 9000,
                    worker_pid=1234)
        self.fa.write_text(json.dumps(data))
        return validate_first_action(str(self.fa), self.start, self.min_mtime,
                                     now_ms=self.now_ms, workspace_aliases=aliases)

    def test_cwd_under_workspace_accepted_with_alias(self):
        self.assertTrue(self.check(str(self.cwd), [str(self.cwd)]))

    def test_cwd_workspace_rejected_without_alias(self):
        self.assertFalse(self.check(str(self.cwd), None))

    def test_root_workspace_accepted_without_alias(self):
        self.assertTrue(self.check(str(self.ws), None))

    def test_alias_outside_workspace_never_accepted(self):
        outside = Path(self.dir) / "elsewhere"
        outside.mkdir()
        self.assertFalse(self.check(str(outside), [str(outside)]))

    def test_unrelated_subdir_not_accepted(self):
        other = self.ws / "other"
        other.mkdir()
        self.assertFalse(self.check(str(other), [str(self.cwd)]))


class DoRunFastFinishTest(unittest.TestCase):
    """do_run death branch: the first action is re-checked after native death
    and clean_exit_receipt runs for real against a real tombstone (never
    mocked)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        (Path(self.dir) / ".local").mkdir()
        self.state_dir = tempfile.mkdtemp()
        self.db = str(Path(self.dir) / "store.db")
        self.store = Store(self.db)
        self.lock = str(Path(self.dir) / "launch.lock")
        self.tag = run_tag_for("task1")
        self.start = json.dumps({"id": SID, "tag": self.tag, "workspace": self.dir,
                                 "engine": "grok", "created_at_ms": CREATED})

    def run_do(self, validate_returns, payload_cwd=None, tomb_workspace=..., exit_code=0):
        payload = {"owner": "me", "cwd": payload_cwd or self.dir, "timeout": 60}
        self.store.submit_task("task1", "ikey", payload, [self.dir])
        if tomb_workspace is not ...:
            write_tomb(self.state_dir, tag=self.tag, workspace=tomb_workspace,
                       exit={"code": exit_code, "exited_at_ms": CREATED + 30000,
                             "oom_killed": False, "signal": None})
        if isinstance(validate_returns, list):
            validate_mock = MagicMock(side_effect=validate_returns)
        else:
            validate_mock = MagicMock(return_value=validate_returns)
        with patch.dict(os.environ, {"APLEXER_STATE_DIR": self.state_dir}), \
             patch("launcher.launch.fetch_quse"), \
             patch("launcher.launch.validate_quse", return_value=(["grok"], [])), \
             patch("launcher.launch.select_candidate", return_value=({"provider": "grok"}, "t")), \
             patch("launcher.launch.check_resources"), \
             patch("launcher.launch.subprocess.Popen") as popen, \
             patch("launcher.launch.native_status",
                   return_value=("dead", "phase=finished (retired tombstone cause=finished)")), \
             patch("launcher.launch.validate_first_action", validate_mock), \
             patch("launcher.launch.time.sleep"):
            popen.return_value.returncode = 0
            popen.return_value.communicate.return_value = (self.start, "")
            from launcher.launch import do_run
            rc = do_run(self.db, "task1", self.dir, None, self.lock)
        self.validate_mock = validate_mock
        return rc

    def record(self):
        return json.loads((Path(self.dir) / ".local" / "launch-task1.json").read_text())

    def test_fast_finish_with_first_action_is_success(self):
        # Wrote its first action between samples, then finished cleanly:
        # success with a native receipt (AC2).
        rc = self.run_do([False, True], tomb_workspace=self.dir)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(self.validate_mock.call_count, 2)  # post-death re-check ran
        self.assertEqual(self.store.get_task("task1")["state"], "completed-awaiting-review")
        rec = self.record()
        self.assertEqual(rec["outcome"], "completed-awaiting-review")
        self.assertEqual(rec["native_receipt"]["exit_code"], 0)
        self.assertEqual(rec["native_receipt"]["cause"], "finished")
        self.assertTrue(rec["first_action_validated_at"])

    def test_zero_work_clean_exit_stays_failed(self):
        # No first action at all: a matching finished tombstone with exit
        # code 0 is still no success (AC3, review finding 2).
        rc = self.run_do(False, tomb_workspace=self.dir, exit_code=0)
        self.assertEqual(rc, 1)
        self.assertEqual(self.store.get_task("task1")["state"], "failed")
        rec = self.record()
        self.assertEqual(rec["outcome"], "failed")
        self.assertNotIn("native_receipt", rec)
        self.assertNotIn("first_action_validated_at", rec)

    def test_real_work_dirty_death_stays_failed(self):
        rc = self.run_do([False, True], tomb_workspace=self.dir, exit_code=1)
        self.assertEqual(rc, 1)
        self.assertEqual(self.store.get_task("task1")["state"], "failed")
        self.assertEqual(self.record()["outcome"], "failed")

    def test_death_without_tombstone_still_fails(self):
        rc = self.run_do([False, True])
        self.assertEqual(rc, 1)
        self.assertEqual(self.store.get_task("task1")["state"], "failed")

    def test_cwd_subdir_launch_binds_and_succeeds(self):
        # The incident shape: --workspace root --cwd root/docs. The observed
        # workspace (whoami/tombstone) is the subdir; the launch succeeds and
        # the native binding records the observed workspace with no error.
        docs = Path(self.dir) / "docs"
        docs.mkdir()
        rc = self.run_do([False, True], payload_cwd=str(docs), tomb_workspace=str(docs))
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.get_task("task1")["state"], "completed-awaiting-review")
        rec = self.record()
        self.assertEqual(rec["native_workspace_aliases"], [str(docs)])
        self.assertNotIn("native_binding_error", rec)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute("SELECT workspace FROM task_native_bindings "
                               "WHERE task_id = 'task1'").fetchone()
        self.assertEqual(row[0], str(docs))


class RunWithHostHintReceiptTest(unittest.TestCase):
    """_run_with_host_hint stdout contract: Hub's run_json parses this stdout;
    a fast finish must print the verified exit receipt (review finding 1)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        (Path(self.dir) / ".local").mkdir()
        self.config_dir = tempfile.mkdtemp()
        self.start_doc = {"id": SID, "tag": "t1", "workspace": self.dir,
                          "engine": "claude", "phase": "running",
                          "profile": "hub-hub-claude", "account_label": "hub-claude"}
        (Path(self.dir) / ".local" / "launch-task1-start.json").write_text(
            json.dumps(self.start_doc))

    def write_record(self, **over):
        record = {"outcome": "running", "first_action_validated_at": "2026-10-30T15:07:30+00:00",
                  "observed_model": "sonnet", "requested_model": "sonnet",
                  "model_verified": True, "account_label": "hub-claude"}
        record.update(over)
        (Path(self.dir) / ".local" / "launch-task1.json").write_text(json.dumps(record))

    def invoke(self):
        args = SimpleNamespace(config_dir=self.config_dir, backend="aplexer",
                               id="task1", cwd=self.dir, tmpdir=None, resource_config=None)
        out = io.StringIO()
        with patch("launcher.cli.do_run", return_value=0), \
             contextlib.redirect_stdout(out):
            rc = _run_with_host_hint(args)
        return rc, out.getvalue()

    def test_fast_finish_prints_exited_receipt(self):
        self.write_record(outcome="completed-awaiting-review",
                          native_receipt={"native_id": SID, "cause": "finished",
                                          "exit_code": 0, "finished_at_ms": CREATED + 30000})
        rc, stdout = self.invoke()
        self.assertEqual(rc, 0)
        doc = json.loads(stdout)
        receipt = doc["receipt"]
        self.assertEqual(doc["backend"], "aplexer")
        self.assertEqual(doc["task_id"], "task1")
        self.assertEqual(receipt["session_id"], SID)
        self.assertEqual(receipt["state"], "exited")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["cause"], "finished")
        self.assertEqual(receipt["finished_at_ms"], CREATED + 30000)
        self.assertTrue(receipt["first_action_verified"])
        self.assertEqual(receipt["model"], "sonnet")
        self.assertEqual(receipt["requested_model"], "sonnet")
        self.assertTrue(receipt["model_verified"])
        self.assertEqual(receipt["tag"], "t1")
        self.assertEqual(receipt["workspace"], self.dir)
        self.assertEqual(receipt["engine"], "claude")
        self.assertEqual(receipt["profile"], "hub-hub-claude")
        self.assertEqual(receipt["account_label"], "hub-claude")
        self.assertNotIn("phase", receipt)

    def test_fast_finish_without_verified_exit_prints_nothing(self):
        self.write_record(outcome="completed-awaiting-review", native_receipt=None)
        rc, stdout = self.invoke()
        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "")

    def test_failed_outcome_prints_nothing(self):
        self.write_record(outcome="failed")
        rc, stdout = self.invoke()
        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "")

    def test_running_receipt_regression(self):
        self.write_record()
        rc, stdout = self.invoke()
        self.assertEqual(rc, 0)
        receipt = json.loads(stdout)["receipt"]
        self.assertEqual(receipt["state"], "running")
        self.assertNotIn("exit_code", receipt)
        self.assertNotIn("cause", receipt)

    def test_end_to_end_do_run_then_cli_receipt(self):
        # Real do_run (boundaries mocked, real tombstone) writes the evidence
        # files in the cwd-subdir incident shape; the CLI then prints the
        # exited receipt from exactly those files.
        harness = DoRunFastFinishTest()
        harness.setUp()
        try:
            docs = Path(harness.dir) / "docs"
            docs.mkdir()
            harness.run_do([False, True], payload_cwd=str(docs), tomb_workspace=str(docs))
            args = SimpleNamespace(config_dir=harness.dir, backend="aplexer",
                                   id="task1", cwd=harness.dir, tmpdir=None,
                                   resource_config=None)
            out = io.StringIO()
            with patch("launcher.cli.do_run", return_value=0), \
                 contextlib.redirect_stdout(out):
                rc = _run_with_host_hint(args)
            self.assertEqual(rc, 0)
            receipt = json.loads(out.getvalue())["receipt"]
            self.assertEqual(receipt["state"], "exited")
            self.assertEqual(receipt["exit_code"], 0)
            self.assertTrue(receipt["first_action_verified"])
            self.assertEqual(receipt["session_id"], SID)
        finally:
            harness.tearDown()


if __name__ == "__main__":
    unittest.main()
