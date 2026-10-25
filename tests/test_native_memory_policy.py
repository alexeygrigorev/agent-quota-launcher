"""Narrow managed native Codex memory policy (Hub midrun death fix).

Kernel proved native Codex killed at 1500MiB midrun (Hub intake14453011).
Policy: managed native Codex defaults to 4096MiB; every other provider
stays 1500MiB. Host 10GiB / disk / quota gates unchanged. Startup
30s handshake must never become a runtime cap.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from launcher.resources import (
    check_resources,
    native_memory_request_for,
    is_codex_managed_native,
    DEFAULT_NATIVE_MEMORY_MB,
    CODEX_MANAGED_NATIVE_MEMORY_MB,
)

GiB = 1024 * 1024 * 1024
LOCAL_TMP_DIR = Path(__file__).resolve().parent.parent / ".local" / "tmp"


class DiskUsage:
    def __init__(self, free):
        self.free = free


class TestNativeMemoryPolicy(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(native_memory_request_for("codex", True), 4096)
        self.assertEqual(native_memory_request_for("openai", True), 4096)
        self.assertEqual(native_memory_request_for("codex", False), 1500)
        self.assertEqual(native_memory_request_for("openai", False), 1500)
        for prov in ("grok", "antigravity", "zai", "opencode", "gemini", "claude", None, ""):
            self.assertEqual(native_memory_request_for(prov, False), 1500,
                             f"provider {prov!r} must stay 1500")
            self.assertEqual(native_memory_request_for(prov, True), 1500,
                             f"managed {prov!r} must stay 1500")
        self.assertEqual(DEFAULT_NATIVE_MEMORY_MB, 1500)
        self.assertEqual(CODEX_MANAGED_NATIVE_MEMORY_MB, 4096)

    def test_predicate_narrow(self):
        self.assertTrue(is_codex_managed_native("codex", True))
        self.assertTrue(is_codex_managed_native("CODEX", True))
        self.assertTrue(is_codex_managed_native("openai", True))
        self.assertFalse(is_codex_managed_native("codex", False))
        self.assertFalse(is_codex_managed_native("zai", True))
        self.assertFalse(is_codex_managed_native("opencode", True))
        self.assertFalse(is_codex_managed_native(None, True))
        self.assertFalse(is_codex_managed_native("codex", None))

    def test_explicit_bounds(self):
        # Managed codex honors smaller explicit requests up to its ceiling.
        self.assertEqual(native_memory_request_for("codex", True, 2000), 2000)
        self.assertEqual(native_memory_request_for("codex", True, 4096), 4096)
        with self.assertRaisesRegex(ValueError, "4096"):
            native_memory_request_for("codex", True, 4097)
        with self.assertRaisesRegex(ValueError, "4096"):
            native_memory_request_for("codex", True, 8192)
        # All others keep the 1500 ceiling: no blanket increase.
        with self.assertRaisesRegex(ValueError, "1500MiB"):
            native_memory_request_for("grok", False, 1600)
        with self.assertRaisesRegex(ValueError, "1500MiB"):
            native_memory_request_for("codex", False, 1600)
        with self.assertRaisesRegex(ValueError, "1500MiB"):
            native_memory_request_for("zai", True, 1600)
        with self.assertRaisesRegex(ValueError, "1500MiB"):
            native_memory_request_for("opencode", True, 4096)
        self.assertEqual(native_memory_request_for("grok", False, 1500), 1500)

    def test_no_blanket_cap_increase_via_check_resources(self):
        import shutil
        repo = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        owned = Path(repo) / ".local" / "tmp" / "r1"
        owned.mkdir(parents=True)
        with patch("launcher.resources.get_mem_available", return_value=30 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            # Managed codex 4096 admitted when host has room.
            self.assertTrue(check_resources(4096, repo, str(owned), repo_root=repo,
                                            provider="codex", is_managed=True))
            # Same 4096 for unmanaged codex or other providers is refused.
            with self.assertRaisesRegex(ValueError, "1500MiB"):
                check_resources(4096, repo, str(owned), repo_root=repo,
                                provider="codex", is_managed=False)
            with self.assertRaisesRegex(ValueError, "1500MiB"):
                check_resources(4096, repo, str(owned), repo_root=repo,
                                provider="zai", is_managed=True)
            with self.assertRaisesRegex(ValueError, "1500MiB"):
                check_resources(1600, repo, str(owned), repo_root=repo)


class TestCodexAdmissionRoom(unittest.TestCase):
    def setUp(self):
        import shutil
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.repo = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.owned = Path(self.repo) / ".local" / "tmp" / "r1"
        self.owned.mkdir(parents=True)

    def test_host_floor_accounts_for_4096(self):
        # 13GiB available: 1500MiB request leaves >10GiB, 4096MiB does not.
        # This is the midrun-kill regression: 1500-passing hosts must not
        # silently admit a 4096 Codex request without room.
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=13 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            self.assertTrue(check_resources(1500, self.repo, str(self.owned),
                                            repo_root=self.repo))
            with self.assertRaisesRegex(ValueError, "host MemAvailable < 10GiB"):
                check_resources(4096, self.repo, str(self.owned), repo_root=self.repo,
                                provider="codex", is_managed=True)

    def test_disk_and_tmp_gates_preserved_for_codex(self):
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=30 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(21 * GiB)):
            with self.assertRaisesRegex(ValueError, "free < 20GiB"):
                check_resources(4096, self.repo, str(self.owned), repo_root=self.repo,
                                provider="codex", is_managed=True,
                                active_disk_mb=1500)
        with patch.object(res, "get_mem_available", return_value=30 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            with self.assertRaisesRegex(ValueError, "reject /tmp"):
                check_resources(4096, "/tmp/foo", "/tmp/foo", repo_root=self.repo,
                                provider="codex", is_managed=True)


class TestSubmitReservationPolicy(unittest.TestCase):
    def setUp(self):
        import shutil
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        from launcher.store import Store
        self.db = str(Path(self.dir) / "s.db")
        self.store = Store(self.db)

    def _mem(self, task_id):
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT memory_mb FROM task_resources WHERE task_id=?",
                               (task_id,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_managed_codex_submit_reserves_4096(self):
        payload = {"owner": "o", "cwd": self.dir, "timeout": 600,
                   "provider": "codex",
                   "account_label": "hubacc", "account_profile": "hub-hubacc",
                   "profile": "model-task",
                   "goal": "g"}
        # Managed state not required for reservation inference: explicit
        # account_profile hub- pair counts as managed attempt; but the strict
        # managed predicate needs managed_dir. Use provider+profile label form
        # that is_managed_profile_payload accepts without managed_dir.
        tid = self.store.submit_task("t-codex-m", "k1", payload, [])
        self.assertEqual(self._mem("t-codex-m"), 4096)

    def test_unmanaged_codex_submit_stays_1500(self):
        payload = {"owner": "o", "cwd": self.dir, "timeout": 600,
                   "provider": "codex", "goal": "g"}
        self.store.submit_task("t-codex-u", "k2", payload, [])
        self.assertEqual(self._mem("t-codex-u"), 1500)

    def test_other_provider_submit_stays_1500(self):
        payload = {"owner": "o", "cwd": self.dir, "timeout": 600,
                   "provider": "zai", "goal": "g"}
        self.store.submit_task("t-zai", "k3", payload, [])
        self.assertEqual(self._mem("t-zai"), 1500)

    def test_explicit_over_ceiling_rejected_at_submit(self):
        payload = {"owner": "o", "cwd": self.dir, "timeout": 600,
                   "provider": "codex",
                   "account_label": "hubacc", "account_profile": "hub-hubacc",
                   "profile": "model-task",
                   "memory_mb": 8192, "goal": "g"}
        with self.assertRaisesRegex(ValueError, "4096"):
            self.store.submit_task("t-big", "k4", payload, [])
        payload2 = {"owner": "o", "cwd": self.dir, "timeout": 600,
                    "provider": "grok", "memory_mb": 1600, "goal": "g"}
        with self.assertRaisesRegex(ValueError, "1500MiB"):
            self.store.submit_task("t-big2", "k5", payload2, [])


class TestDoRunNativeMemoryArgv(unittest.TestCase):
    def setUp(self):
        import shutil
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (Path(self.dir) / ".local").mkdir(exist_ok=True)
        from launcher.store import Store
        self.db = str(Path(self.dir) / "store.db")
        self.store = Store(self.db)
        self.lock = str(Path(self.dir) / "launch.lock")

    def _run_with_provider(self, provider, is_managed, payload_extra=None):
        from launcher.launch import do_run
        task_id = f"t-{provider}-{'m' if is_managed else 'u'}"
        payload = {"owner": "me", "cwd": self.dir, "timeout": 60, "goal": "g x"}
        if is_managed and provider == "codex":
            # Managed path uses build_managed_profile_argv (no wrapper
            # needed) and the 4096MiB policy.
            payload.update({"account_label": "hubacc",
                            "account_profile": "hub-hubacc",
                            "profile": "model-task"})
        if payload_extra:
            payload.update(payload_extra)
        self.store.submit_task(task_id, f"ik-{task_id}", payload, [])
        fake_quse = {"codex": {"status": "ok", "windows": {
            "weekly": {"percent_remaining": 80.0, "reset_at": "2026-11-01T00:00:00+00:00"}}},
            "go": {"status": "ok", "windows": {
                "monthly": {"percent_remaining": 80.0, "reset_at": "2026-11-01T00:00:00+00:00"}}}}
        captured = {}
        outer_dir = self.dir

        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""
            def communicate(self, timeout=None):
                start = {"schema_version": 1,
                         "id": "11111111-1111-4111-8111-111111111111",
                         "tag": f"task-{task_id}",
                         "workspace": str(Path(outer_dir).resolve()),
                         "engine": "codex" if provider == "codex" else "opencode",
                         "command": ["native", "--model",
                                     "gpt-6.1-sol" if provider == "codex"
                                     else "opencode-go/muse-spark-1.3-contributor"],
                         "phase": "running", "worker_pid": 99,
                         "created_at_ms": 1000000, "updated_at_ms": 1000500}
                return json.dumps(start), ""

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeProc()

        chosen = {"provider": provider,
                  "model": "gpt-6.1-sol" if provider == "codex"
                  else "opencode-go/muse-spark-1.3-contributor"}
        with patch("launcher.launch.fetch_quse", return_value=fake_quse), \
             patch("launcher.launch.validate_quse", return_value=([{"name": provider,
                  "provider": provider}], [])), \
             patch("launcher.launch.select_candidate", return_value=(chosen, {})), \
             patch("launcher.launch.subprocess.Popen", side_effect=fake_popen), \
             patch("launcher.launch.validate_first_action", return_value=True), \
             patch("launcher.resources.get_mem_available", return_value=30 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            rc = do_run(self.db, task_id, self.dir, None, self.lock)
        self.assertEqual(rc, 0)
        return captured["cmd"]

    def test_codex_managed_argv_4096_and_startup_only(self):
        cmd = self._run_with_provider("codex", True)
        self.assertIn("--memory", cmd)
        self.assertEqual(cmd[cmd.index("--memory") + 1], "4096M")
        # Startup handshake stays 30s and never becomes a runtime cap.
        self.assertIn("--startup-timeout-ms", cmd)
        self.assertEqual(cmd[cmd.index("--startup-timeout-ms") + 1], "30000")
        self.assertNotIn("--timeout", cmd)
        self.assertNotIn("--runtime-timeout-ms", cmd)

    def test_other_provider_argv_stays_1500(self):
        cmd = self._run_with_provider("opencode", False)
        self.assertEqual(cmd[cmd.index("--memory") + 1], "1500M")
        self.assertEqual(cmd[cmd.index("--startup-timeout-ms") + 1], "30000")


class TestResourceStatusCodexRoom(unittest.TestCase):
    def test_reports_both_gates(self):
        import tempfile, json
        from launcher.cli import resource_status
        from launcher.store import Store
        d = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(__import__("shutil").rmtree, d, ignore_errors=True)
        cfg = Path(d) / "cfg"
        cfg.mkdir()
        Store(str(cfg / "state.db"))

        class Args:
            config_dir = str(cfg)
            dry_run = True

        with patch("launcher.resources.get_mem_available", return_value=30 * GiB):
            with patch("builtins.print") as pr:
                self.assertEqual(resource_status(Args()), 0)
                doc = json.loads(pr.call_args.args[0])
        self.assertIn("launch_allowed_codex_managed", doc)
        self.assertIn("codex_managed_memory_mb", doc)
        self.assertEqual(doc["codex_managed_memory_mb"], 4096)
        self.assertEqual(doc["default_memory_mb"], 1500)
        # 30GiB host: both gates pass.
        self.assertTrue(doc["launch_allowed"])
        self.assertTrue(doc["launch_allowed_codex_managed"])

    def test_tight_host_blocks_codex_but_not_default(self):
        import tempfile, json
        from launcher.cli import resource_status
        from launcher.store import Store
        d = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(__import__("shutil").rmtree, d, ignore_errors=True)
        cfg = Path(d) / "cfg"
        cfg.mkdir()
        Store(str(cfg / "state.db"))

        class Args:
            config_dir = str(cfg)
            dry_run = True

        # 13GiB: default 1500 passes, codex 4096 must not.
        with patch("launcher.resources.get_mem_available", return_value=13 * GiB):
            with patch("builtins.print") as pr:
                self.assertEqual(resource_status(Args()), 0)
                doc = json.loads(pr.call_args.args[0])
        self.assertTrue(doc["launch_allowed"])
        self.assertFalse(doc["launch_allowed_codex_managed"])
        self.assertEqual(doc["reason_codex_managed"],
                         "launcher_memory_reserve_codex_4096")


if __name__ == "__main__":
    unittest.main()
