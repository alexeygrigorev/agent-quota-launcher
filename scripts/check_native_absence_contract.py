#!/usr/bin/env python3
"""Host-only regression against installed aplexer; never starts/stops sessions.

Run separately from the hermetic suite, which does not install aplexer:
    python3 scripts/check_native_absence_contract.py
The temporary directory belongs to this check; all native queries are read-only.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# scripts/platform.py is a project command, not the stdlib platform module.
sys.path = [str(REPO)] + [entry for entry in sys.path
                           if Path(entry).resolve() != REPO / "scripts"]
from launcher.store import Store

LOCAL_TMP_DIR = REPO / ".local" / "tmp"


class NativeAbsenceCliContractTests(unittest.TestCase):
    """Exercise the installed CLI directly; missing paths do not prove death."""

    def test_real_missing_ancestor_status_and_successful_global_census(self):
        import uuid
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(LOCAL_TMP_DIR)) as directory:
            root = Path(directory)
            task_id = "hub-" + str(uuid.uuid4())
            missing = root / "absent-ancestor" / "absent-workspace"
            status = subprocess.run(["aplexer", "status", "--workspace", str(missing),
                                     "--tag", "task-" + task_id, "--json"],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(status.returncode, 1)
            self.assertIn("No such file or directory", status.stderr)
            self.assertNotIn("no matching session", status.stderr)
            existing = subprocess.run(["aplexer", "status", "--workspace", str(root),
                                       "--tag", "task-" + task_id, "--json"],
                                      capture_output=True, text=True, timeout=15)
            self.assertEqual(existing.returncode, 1)
            self.assertEqual(existing.stderr.strip(), "a: no matching session")
            # This is the replacement proof form, querying live sessions AND
            # retired tombstones without resolving the missing workspace.
            self.assertTrue(Store._native_census_absent(task_id))


if __name__ == "__main__":
    unittest.main()
