"""OpenCode Go Muse Spark route tests (platform-muse-brief slice).

Covers: quse go/opencode admission mapping to provider opencode with the exact
installed subscription model, exact adapter argv (separate prompt argv, no
shell interpolation), runtime validation, GLM refusal on opencode, and
do_run --engine opencode wiring. Fixtures only; no real launches.
"""
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from launcher.admission import validate_quse, ADAPTER_MODELS, ADAPTER_SUPPORTED_MODELS
from launcher.launch import (
    ADAPTERS, OPENCODE_BIN, OPENCODE_MODEL,
    build_adapter_argv, validate_opencode_runtime,
)


def route(windows, status="ok", error=None, details=None):
    return {"status": status, "error": error, "details": details or {}, "windows": windows}


class TestOpencodeAdmission(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.future1 = (now + timedelta(days=1)).isoformat()
        self.future2 = (now + timedelta(days=2)).isoformat()

    def test_models_pin_muse_spark(self):
        self.assertEqual(ADAPTER_MODELS["opencode"], OPENCODE_MODEL)
        self.assertEqual(OPENCODE_MODEL, "opencode-go/muse-spark-1.3-contributor")
        self.assertEqual(ADAPTER_SUPPORTED_MODELS["opencode"],
                         ("opencode-go/muse-spark-1.3-contributor",))

    def test_go_maps_to_opencode(self):
        data = {"go": route({
            "5h": {"percent_remaining": 54, "reset_at": self.future1},
            "7d": {"percent_remaining": 58, "reset_at": self.future2},
            "monthly": {"percent_remaining": 55, "reset_at": self.future2},
        })}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["name"], "go")
        self.assertEqual(valid[0]["provider"], "opencode")
        self.assertEqual(valid[0]["model"], OPENCODE_MODEL)
        self.assertNotIn("go", rej)

    def test_opencode_key_admitted(self):
        data = {"opencode": route({
            "5h": {"percent_remaining": 90, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(valid[0]["provider"], "opencode")
        self.assertEqual(valid[0]["model"], OPENCODE_MODEL)
        self.assertNotIn("opencode", rej)

    def test_opencode_task_fit_model_pin(self):
        data = {"go": route({
            "5h": {"percent_remaining": 90, "reset_at": self.future1}})}
        valid, _ = validate_quse(
            data, task_requirements={"providers": ["opencode"],
                                     "models": [OPENCODE_MODEL]})
        self.assertEqual(valid[0]["task_fit"], 1.0)
        self.assertEqual(valid[0]["model"], OPENCODE_MODEL)
        # Unsupported model on the pinned provider: fit 0.0 (ranking weights
        # it 0, never fabricated to 1.0); the route stays listed, not admitted
        # for that model.
        valid2, _ = validate_quse(
            data, task_requirements={"providers": ["opencode"],
                                     "models": ["opencode-go/space-bunny-free"]})
        self.assertEqual(len(valid2), 1)
        self.assertEqual(valid2[0]["task_fit"], 0.0)
        self.assertEqual(valid2[0]["model"], OPENCODE_MODEL)

    def test_free_model_not_mapped(self):
        # Go remaining never proves free opencode/* routes; only the exact
        # subscription model is admitted.
        self.assertNotIn("opencode-go/space-bunny-free",
                         ADAPTER_SUPPORTED_MODELS["opencode"])


class TestOpencodeAdapter(unittest.TestCase):
    def test_argv_exact_separate_prompt(self):
        with patch("launcher.launch.validate_opencode_runtime", return_value=OPENCODE_BIN):
            argv = build_adapter_argv("opencode", "do the brief thing $(not interpolated)")
        self.assertEqual(argv, [
            OPENCODE_BIN, "run", "--pure", "--auto", "--model", OPENCODE_MODEL,
            "do the brief thing $(not interpolated)",
        ])
        self.assertEqual(argv[0], "/home/alexey/.nvm/versions/node/v24.13.1/bin/opencode")
        # Prompt is a separate argv element (no shell interpolation possible).
        self.assertEqual(len(argv), 7)
        self.assertNotIn("go", ADAPTERS)

    def test_default_model_is_pinned(self):
        with patch("launcher.launch.validate_opencode_runtime", return_value=OPENCODE_BIN):
            argv = build_adapter_argv("opencode", "goal", model=None)
            argv2 = build_adapter_argv("opencode", "goal", model=OPENCODE_MODEL)
        self.assertIn(OPENCODE_MODEL, argv)
        self.assertIn(OPENCODE_MODEL, argv2)

    def test_other_models_refused_no_fallback(self):
        with patch("launcher.launch.validate_opencode_runtime", return_value=OPENCODE_BIN):
            for bad in ("opencode-go/space-bunny-free", "grok-4.6",
                        "glm-5.3-flash", "opencode-go/glm-5.3-flash",
                        "zai-coding-plan/glm-5.3-flash"):
                with self.subTest(model=bad):
                    with self.assertRaises(ValueError):
                        build_adapter_argv("opencode", "goal", model=bad)

    def test_glm_never_via_opencode(self):
        with patch("launcher.launch.validate_opencode_runtime", return_value=OPENCODE_BIN):
            with self.assertRaisesRegex(ValueError, "only allowed on the zai"):
                build_adapter_argv("opencode", "goal", model="glm-5.3-flash")

    def test_runtime_missing_or_unexecutable_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope-opencode")
            with self.assertRaises(FileNotFoundError):
                validate_opencode_runtime(missing)
            probe = os.path.join(tmp, "opencode-probe")
            with open(probe, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(probe, 0o644)  # not executable
            with self.assertRaises(PermissionError):
                validate_opencode_runtime(probe)
            os.chmod(probe, 0o755)
            self.assertEqual(validate_opencode_runtime(probe), probe)

    def test_installed_runtime_is_valid(self):
        # Fresh evidence: the exact required binary exists and executes.
        self.assertTrue(os.path.isfile(OPENCODE_BIN))
        self.assertTrue(os.access(OPENCODE_BIN, os.X_OK))
        self.assertEqual(validate_opencode_runtime(), OPENCODE_BIN)

    def test_other_providers_unaffected(self):
        grok_argv = build_adapter_argv("grok", "grok goal")
        self.assertEqual(grok_argv[-1], "grok goal")
        with self.assertRaisesRegex(ValueError, "Unsupported provider"):
            build_adapter_argv("codex", "goal")


class TestOpencodeDoRunWiring(unittest.TestCase):
    def test_do_run_uses_engine_opencode(self):
        # do_run must request genuine native engine=opencode for opencode
        # provider while preserving other providers' argv (no --engine).
        import json
        import tempfile
        from pathlib import Path
        from launcher.store import Store
        from launcher import launch as launch_mod

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.db")
            store = Store(db)
            task_id = "task-opencode-wiring"
            payload = {"owner": "me", "cwd": tmp, "timeout": 60,
                       "goal": "wire engine",
                       "model_requirements": {"providers": ["opencode"],
                                              "models": [OPENCODE_MODEL]}}
            store.submit_task(task_id, "k1", payload, [])
            lock = str(Path(tmp) / "launch.lock")
            seen = {}

            class Proc:
                returncode = 0
                def communicate(self, timeout=None):
                    return ('{"id":"x"}', "")

            def fake_popen(cmd, **kw):
                seen["cmd"] = list(cmd)
                return Proc()

            with patch.object(launch_mod, "fetch_quse", return_value={"ok": True}), \
                 patch.object(launch_mod, "validate_quse",
                              return_value=([{"provider": "opencode",
                                              "model": OPENCODE_MODEL}], [])), \
                 patch.object(launch_mod, "select_candidate",
                              return_value=({"provider": "opencode",
                                             "model": OPENCODE_MODEL}, {})), \
                 patch.object(launch_mod, "check_resources", return_value=None), \
                 patch.object(launch_mod, "subprocess") as mock_sp, \
                 patch.object(launch_mod, "native_status",
                              return_value=("dead", "fixture dead")), \
                 patch.object(launch_mod, "validate_first_action",
                              return_value=False), \
                 patch.object(launch_mod, "validate_opencode_runtime",
                              return_value=OPENCODE_BIN):
                mock_sp.Popen.side_effect = fake_popen
                # Force early uncertain via bad start JSON to avoid first-action wait.
                try:
                    launch_mod.do_run(db, task_id, tmp, None, lock)
                except Exception:
                    pass
            cmd = seen.get("cmd", [])
            self.assertIn("--engine", cmd)
            idx = cmd.index("--engine")
            self.assertEqual(cmd[idx + 1], "opencode")
            # Exact adapter argv follows `--` with prompt separate.
            self.assertIn("--", cmd)
            tail = cmd[cmd.index("--") + 1:]
            self.assertEqual(tail[:6],
                             [OPENCODE_BIN, "run", "--pure", "--auto",
                              "--model", OPENCODE_MODEL])
            # Arbitrary brief stays verbatim at the head; the first-action
            # instruction (with the exact artifact path) is appended so the
            # native worker can prove liveness.
            self.assertTrue(tail[6].startswith("wire engine"))
            self.assertIn("aplexer whoami --json", tail[6])
            self.assertIn("first-action-task-opencode-wiring.json", tail[6])
            self.assertEqual(len(tail), 7)

    def test_do_run_appends_first_action_instruction_verbatim(self):
        import tempfile
        from pathlib import Path
        from launcher.store import Store
        from launcher import launch as launch_mod

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.db")
            store = Store(db)
            task_id = "task-opencode-brief"
            brief = 'arbitrary brief $(keep) "quoted"'
            payload = {"owner": "me", "cwd": tmp, "timeout": 60,
                       "goal": brief,
                       "model_requirements": {"providers": ["opencode"],
                                              "models": [OPENCODE_MODEL]}}
            store.submit_task(task_id, "k1", payload, [])
            lock = str(Path(tmp) / "launch.lock")
            seen = {}

            class Proc:
                returncode = 0
                def communicate(self, timeout=None):
                    return ('{"id":"x"}', "")

            def fake_popen(cmd, **kw):
                seen["cmd"] = list(cmd)
                return Proc()

            with patch.object(launch_mod, "fetch_quse", return_value={"ok": True}), \
                  patch.object(launch_mod, "validate_quse",
                               return_value=([{"provider": "opencode",
                                               "model": OPENCODE_MODEL}], [])), \
                  patch.object(launch_mod, "select_candidate",
                               return_value=({"provider": "opencode",
                                              "model": OPENCODE_MODEL}, {})), \
                  patch.object(launch_mod, "check_resources", return_value=None), \
                  patch.object(launch_mod, "subprocess") as mock_sp, \
                  patch.object(launch_mod, "native_status",
                               return_value=("dead", "fixture dead")), \
                  patch.object(launch_mod, "validate_first_action",
                               return_value=False), \
                  patch.object(launch_mod, "validate_opencode_runtime",
                               return_value=OPENCODE_BIN):
                mock_sp.Popen.side_effect = fake_popen
                try:
                    launch_mod.do_run(db, task_id, tmp, None, lock)
                except Exception:
                    pass
            tail = seen["cmd"][seen["cmd"].index("--") + 1:]
            sent_goal = tail[-1]
            self.assertTrue(sent_goal.startswith(brief))
            self.assertIn(str(Path(tmp) / ".local"
                              / "first-action-task-opencode-brief.json"),
                          sent_goal)
            self.assertIn("aplexer whoami --json", sent_goal)

    def test_do_run_other_providers_keep_brief_untouched(self):
        # The first-action instruction is opencode-scoped; other providers
        # preserve exact existing goal semantics.
        import tempfile
        from pathlib import Path
        from launcher.store import Store
        from launcher import launch as launch_mod

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.db")
            store = Store(db)
            task_id = "task-grok-brief"
            payload = {"owner": "me", "cwd": tmp, "timeout": 60,
                       "goal": "wire engine"}
            store.submit_task(task_id, "k1", payload, [])
            lock = str(Path(tmp) / "launch.lock")
            seen = {}

            class Proc:
                returncode = 0
                def communicate(self, timeout=None):
                    return ('{"id":"x"}', "")

            def fake_popen(cmd, **kw):
                seen["cmd"] = list(cmd)
                return Proc()

            with patch.object(launch_mod, "fetch_quse", return_value={"ok": True}), \
                  patch.object(launch_mod, "validate_quse",
                               return_value=([{"provider": "grok",
                                               "model": "grok-4.6"}], [])), \
                  patch.object(launch_mod, "select_candidate",
                               return_value=({"provider": "grok",
                                              "model": "grok-4.6"}, {})), \
                  patch.object(launch_mod, "check_resources", return_value=None), \
                  patch.object(launch_mod, "subprocess") as mock_sp, \
                  patch.object(launch_mod, "native_status",
                               return_value=("dead", "fixture dead")), \
                  patch.object(launch_mod, "validate_first_action",
                               return_value=False):
                mock_sp.Popen.side_effect = fake_popen
                try:
                    launch_mod.do_run(db, task_id, tmp, None, lock)
                except Exception:
                    pass
            tail = seen["cmd"][seen["cmd"].index("--") + 1:]
            self.assertEqual(tail[-1], "wire engine")
            self.assertNotIn("--engine", seen["cmd"])


class TestOpencodeCliReceipt(unittest.TestCase):
    def test_aplexer_backend_prints_native_receipt_on_success(self):
        import io
        import json as jsonlib
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from launcher import cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg"
            cfg.mkdir()
            (cfg / "state.db").touch()
            cwd = Path(tmp) / "work"
            (cwd / ".local").mkdir(parents=True)
            start = {"id": "native-uuid-123", "tag": "task-t1",
                     "workspace": str(cwd), "engine": "opencode",
                     "phase": "running"}
            record = {"outcome": "running",
                      "first_action_validated_at": "2026-10-17T15:00:00Z",
                      "session_id": "native-uuid-123"}
            (cwd / ".local" / "launch-t1-start.json").write_text(
                jsonlib.dumps(start))
            (cwd / ".local" / "launch-t1.json").write_text(
                jsonlib.dumps(record))
            args = SimpleNamespace(config_dir=str(cfg), backend="aplexer",
                                   id="t1", cwd=str(cwd), tmpdir=None,
                                   as_controller=False)
            with patch.object(cli_mod, "do_run", return_value=0), \
                 patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = cli_mod.run(args)
            self.assertEqual(rc, 0)
            doc = jsonlib.loads(out.getvalue())
            self.assertEqual(doc["backend"], "aplexer")
            self.assertEqual(doc["receipt"]["session_id"], "native-uuid-123")
            self.assertEqual(doc["receipt"]["state"], "running")
            self.assertTrue(doc["receipt"]["first_action_verified"])
            self.assertEqual(doc["receipt"]["engine"], "opencode")

    def test_aplexer_backend_no_fake_receipt_without_evidence(self):
        import io
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from launcher import cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg"
            cfg.mkdir()
            (cfg / "state.db").touch()
            cwd = Path(tmp) / "work"
            cwd.mkdir(parents=True)
            args = SimpleNamespace(config_dir=str(cfg), backend="aplexer",
                                   id="missing", cwd=str(cwd), tmpdir=None,
                                   as_controller=False)
            with patch.object(cli_mod, "do_run", return_value=0), \
                 patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = cli_mod.run(args)
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), "")

    def test_aplexer_backend_stdout_is_exactly_one_json_document(self):
        # The Hub fleet run_json caller parses stdout as exactly one JSON
        # receipt: no log noise, no second line, native ID live while the
        # worker runs, mirroring the genuine start record.
        import io
        import json as jsonlib
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from launcher import cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg"
            cfg.mkdir()
            (cfg / "state.db").touch()
            cwd = Path(tmp) / "work"
            (cwd / ".local").mkdir(parents=True)
            start = {"id": "native-uuid-456", "tag": "task-hub-offer1",
                     "workspace": str(cwd), "engine": "opencode",
                     "phase": "running"}
            record = {"outcome": "running",
                      "first_action_validated_at": "2026-10-17T15:00:00Z",
                      "session_id": "native-uuid-456"}
            (cwd / ".local" / "launch-t2-start.json").write_text(
                jsonlib.dumps(start))
            (cwd / ".local" / "launch-t2.json").write_text(
                jsonlib.dumps(record))
            args = SimpleNamespace(config_dir=str(cfg), backend="aplexer",
                                   id="t2", cwd=str(cwd), tmpdir=None,
                                   as_controller=False)
            with patch.object(cli_mod, "do_run", return_value=0), \
                  patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = cli_mod.run(args)
            self.assertEqual(rc, 0)
            lines = out.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 1)
            doc = jsonlib.loads(lines[0])
            self.assertEqual(doc["receipt"]["session_id"], "native-uuid-456")
            self.assertEqual(doc["receipt"]["tag"], "task-hub-offer1")
            self.assertEqual(doc["receipt"]["workspace"], str(cwd))
            self.assertEqual(doc["receipt"]["engine"], "opencode")
            self.assertEqual(doc["receipt"]["phase"], "running")
            self.assertEqual(doc["receipt"]["state"], "running")
            self.assertTrue(doc["receipt"]["first_action_verified"])

    def test_aplexer_backend_failure_prints_nothing(self):
        import io
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from launcher import cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg"
            cfg.mkdir()
            args = SimpleNamespace(config_dir=str(cfg), backend="aplexer",
                                   id="t1", cwd=tmp, tmpdir=None,
                                   as_controller=False)
            with patch.object(cli_mod, "do_run", return_value=1), \
                 patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = cli_mod.run(args)
            self.assertEqual(rc, 1)
            self.assertEqual(out.getvalue().strip(), "")


if __name__ == "__main__":
    unittest.main()
