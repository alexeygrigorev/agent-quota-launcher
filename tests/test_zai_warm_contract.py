"""Issue69 regression: headless ZAI tools permission contract is the warm bridge.

Installed contract (external-model-agents SKILL.md, ZCodex section):
  env ZCODE_WARM=1 ZCODE_WARM_MODE=yolo \
    zcodex --dangerously-bypass-approvals-and-sandbox exec \
    --disable shell_snapshot ...

The warm bridge executes tools in its own YOLO loop exactly once. The cold
bridge (`-s workspace-write` with no permission client) reports
`No permission client configured` while the outer harness executes emitted
Bash calls; enabling inner YOLO on the cold bridge would execute streamed
mutations twice. These tests pin the warm shape and forbid the cold shape.
"""
import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from launcher.launch import (
    ADAPTERS,
    ZCODE_CJS,
    ZCODE_NODE,
    ZCODEX_BIN,
    build_adapter_argv,
)


class TestZaiWarmContract(unittest.TestCase):
    def test_adapter_env_carries_warm_bridge(self):
        env = ADAPTERS["zai"]["env"]
        self.assertEqual(env.get("ZCODE_WARM"), "1")
        self.assertEqual(env.get("ZCODE_WARM_MODE"), "yolo")
        # Runtime pins preserved alongside warm flags.
        self.assertEqual(env.get("ZCODE_NODE"), ZCODE_NODE)
        self.assertEqual(env.get("ZCODE_CJS"), ZCODE_CJS)

    def test_argv_emits_warm_env_assignments_before_binary(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertEqual(argv[0], "/usr/bin/env")
        for key in (f"ZCODE_NODE={ZCODE_NODE}", f"ZCODE_CJS={ZCODE_CJS}",
                    "ZCODE_WARM=1", "ZCODE_WARM_MODE=yolo"):
            self.assertIn(key, argv)
        bin_idx = argv.index(ZCODEX_BIN)
        for key in ("ZCODE_WARM=1", "ZCODE_WARM_MODE=yolo"):
            self.assertLess(argv.index(key), bin_idx)

    def test_argv_warm_yolo_ordering_single_execution(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertEqual(argv.count(ZCODEX_BIN), 1)
        self.assertEqual(argv.count("exec"), 1)
        self.assertEqual(argv.count("--dangerously-bypass-approvals-and-sandbox"), 1)
        self.assertEqual(argv.count("--disable"), 1)
        self.assertEqual(argv.count("shell_snapshot"), 1)
        self.assertEqual(argv.count("--model"), 1)
        # --dangerously-... is an outer zcodex flag: before exec.
        # --disable shell_snapshot is an exec flag: after exec.
        outer = argv.index("--dangerously-bypass-approvals-and-sandbox")
        exec_idx = argv.index("exec")
        dis_idx = argv.index("--disable")
        self.assertLess(outer, exec_idx)
        self.assertLess(exec_idx, dis_idx)
        self.assertEqual(argv[dis_idx + 1], "shell_snapshot")
        # Exactly one prompt, last, never shell-interpolated.
        self.assertEqual(argv[-1], "goal")
        self.assertEqual(argv.count("goal"), 1)
        self.assertNotIn("sh", argv)
        self.assertNotIn("bash", argv)

    def test_cold_bridge_never_reappears(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertNotIn("-s", argv)
        self.assertNotIn("workspace-write", argv)
        self.assertNotIn("-a", argv)
        self.assertNotIn("--ask-for-approval", argv)

    def test_exact_model_preserved(self):
        argv = build_adapter_argv("zai", "goal")
        self.assertEqual(argv[argv.index("--model") + 1], "glm-5.3-flash")
        # Override still routes through zai (GLM routing rule enforced elsewhere).
        argv2 = build_adapter_argv("zai", "goal", model="glm-5.3-flash")
        self.assertEqual(argv2[argv2.index("--model") + 1], "glm-5.3-flash")

    def test_validated_runtime_propagates_warm(self):
        with tempfile.NamedTemporaryFile() as node_f, \
             tempfile.NamedTemporaryFile() as zbin_f, \
             tempfile.NamedTemporaryFile() as cjs_f:
            import os
            for f in (node_f, zbin_f):
                os.chmod(f.name, 0o755)
            with patch("launcher.launch.ZCODE_NODE", node_f.name), \
                 patch("launcher.launch.ZCODE_CJS", cjs_f.name), \
                 patch("launcher.launch.ZCODEX_BIN", zbin_f.name):
                argv = build_adapter_argv("zai", "goal")
                self.assertIn(f"ZCODE_NODE={node_f.name}", argv)
                self.assertIn(f"ZCODE_CJS={cjs_f.name}", argv)
                self.assertIn("ZCODE_WARM=1", argv)
                self.assertIn("ZCODE_WARM_MODE=yolo", argv)
                z_idx = argv.index(zbin_f.name)
                self.assertEqual(argv[z_idx + 1], "--dangerously-bypass-approvals-and-sandbox")
                self.assertNotIn("-s", argv)

    def test_warm_argv_matches_maintained_adapter_identity(self):
        from launcher.provider_admission import identify_command
        argv = build_adapter_argv("zai", "warm probe")
        ident = identify_command(argv)
        self.assertEqual(ident.provider, "zai")
        self.assertEqual(ident.model, "glm-5.3-flash")

    def test_do_run_emits_single_warm_execution_with_warm_env(self):
        from launcher.launch import do_run
        from launcher.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / ".local").mkdir()
            db = str(d / "store.db")
            store = Store(db)
            task_id = "t-warm-once"
            store.submit_task(task_id, "ikey-warm",
                              {"owner": "me", "cwd": str(d), "timeout": 60,
                               "model_requirements": {"providers": ["zai"],
                                                      "models": ["glm-5.3-flash"]}},
                              [str(d)])
            lock = str(d / "launch.lock")
            calls = []

            class FakeProc:
                returncode = 0
                def communicate(self, timeout=None):
                    start = {"id": "11111111-1111-4111-8111-111111111111",
                             "tag": f"task-{task_id}", "workspace": str(d),
                             "engine": "zcodex",
                             "command": ["native", "--model", "glm-5.3-flash"],
                             "phase": "running", "worker_pid": 4242,
                             "created_at_ms": 1000000, "updated_at_ms": 1000500}
                    return json.dumps(start), ""

            def fake_popen(cmd, **kwargs):
                calls.append(list(cmd))
                return FakeProc()

            fake_quse = {"zai": {"status": "ok", "windows": {
                "weekly": {"percent_remaining": 80.0,
                           "reset_at": "2026-11-01T00:00:00+00:00"}}}}
            with patch("launcher.launch.fetch_quse", return_value=fake_quse), \
                 patch("launcher.launch.validate_quse", return_value=(["zai"], [])), \
                 patch("launcher.launch.select_candidate",
                       return_value=({"provider": "zai", "model": "glm-5.3-flash"}, {})), \
                 patch("launcher.launch.check_resources", return_value=True), \
                 patch("launcher.launch.subprocess.Popen", side_effect=fake_popen), \
                 patch("launcher.launch.validate_first_action", return_value=True):
                rc = do_run(db, task_id, str(d), None, lock)
            self.assertEqual(rc, 0)
            # No duplicate outer execution: exactly one native spawn.
            self.assertEqual(len(calls), 1)
            cmd = calls[0]
            dash = cmd.index("--")
            adapter = cmd[dash + 1:]
            self.assertEqual(adapter.count("exec"), 1)
            self.assertEqual(adapter.count("--dangerously-bypass-approvals-and-sandbox"), 1)
            self.assertNotIn("-s", adapter)
            self.assertNotIn("workspace-write", adapter)
            self.assertIn("ZCODE_WARM=1", adapter)
            self.assertIn("ZCODE_WARM_MODE=yolo", adapter)
            env_flags = [cmd[i + 1] for i, t in enumerate(cmd[:dash])
                         if t == "--env" and i + 1 < dash]
            env_map = dict(e.split("=", 1) for e in env_flags if "=" in e)
            self.assertEqual(env_map.get("ZCODE_WARM"), "1")
            self.assertEqual(env_map.get("ZCODE_WARM_MODE"), "yolo")
            self.assertIn("ZCODE_NODE", env_map)
            self.assertIn("ZCODE_CJS", env_map)


if __name__ == "__main__":
    unittest.main()
