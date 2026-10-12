"""Regression checks for task-unit process and control-socket containment."""

import os
import unittest
from unittest.mock import patch

from launcher.pid_isolation import manager_properties, wrap_command
from launcher.task_units import build_systemd_run_argv


class PidIsolationTests(unittest.TestCase):
    def test_command_is_wrapped_before_provider_runs(self):
        command = ["/usr/bin/python3", "worker.py"]
        wrapped = wrap_command(command, "/usr/bin/python3")
        self.assertEqual(wrapped[:7], [
            "/usr/bin/unshare", "--user", "--map-root-user", "--pid",
            "--fork", "--mount-proc", "--kill-child=KILL",
        ])
        self.assertIn("os.getpid()!=1", wrapped[10])
        self.assertEqual(wrapped[-2:], command)
        self.assertEqual(wrapped[11], os.readlink("/proc/self/ns/pid"))

    def test_missing_unshare_fails_before_payload(self):
        with patch("launcher.pid_isolation.os.path.isfile", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "no unsandboxed fallback"):
                wrap_command(["/usr/bin/true"], "/usr/bin/python3")
        with self.assertRaises(ValueError):
            wrap_command([], "/usr/bin/python3")

    def test_manager_masks_native_control_sockets(self):
        props = manager_properties(1000)
        self.assertIn("InaccessiblePaths=-/run/user/1000/bus", props[1])
        self.assertIn("-/run/user/1000/aplexer", props[1])
        self.assertIn("-/run/systemd/private", props[1])
        self.assertIn("NoNewPrivileges=yes", props)
        self.assertIn("ProtectControlGroups=yes", props)
        for uid in (True, -1, "1000"):
            with self.assertRaises(ValueError):
                manager_properties(uid)

    def test_all_task_unit_argv_uses_isolation(self):
        argv = build_systemd_run_argv(
            "agent-task-isolation-test.service", ["/usr/bin/true"], 128,
            "/home/alexey/git/agent-quota-launcher/.local/tmp",
        )
        separator = argv.index("--")
        self.assertEqual(argv[separator + 1], "/usr/bin/unshare")
        self.assertEqual(argv[-1], "/usr/bin/true")
        self.assertIn("--kill-child=KILL", argv)
        self.assertIn("NoNewPrivileges=yes", argv)
        self.assertIn("ProtectControlGroups=yes", argv)


if __name__ == "__main__":
    unittest.main()
