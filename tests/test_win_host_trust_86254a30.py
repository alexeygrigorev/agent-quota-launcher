"""Windows SID/DACL trust parity for AGENT_HUB_HOST_PATH (Hub intake 86254a30 AC5-8).

POSIX trusts the real resolved host directory and its agent_hub_host package
when owned by the current user or root and not group/world-writable. On
Windows the resolver used os.getuid()/mode bits, which do not exist there, so
every managed Claude/Gemini/Antigravity probe on a Windows host failed closed.
The Windows bar is the same: owner is the current user or SYSTEM/Administrators,
and no applicable allow ACE grants write-class rights to anyone else. Links are
followed exactly like POSIX resolve(); only the final targets are judged.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import win_trust as W
from launcher.launch import HubHostPathUntrusted, resolve_validated_hub_host_dir

USER = "S-1-5-21-1000-2000-3000-1001"
OTHER = "S-1-5-21-1000-2000-3000-1002"
EVERYONE, AUTH_USERS, USERS = "S-1-1-0", "S-1-5-11", "S-1-5-32-545"
FULL, READ_EXEC, MODIFY = 0x1F01FF, 0x1200A9, 0x1301BF
OI_CI = 0x03


class PureEvaluationTest(unittest.TestCase):
    def test_trusted_owner_and_writers(self):
        aces = [(W.SYSTEM_SID, 0, OI_CI, FULL), (W.ADMINISTRATORS_SID, 0, OI_CI, FULL),
                (W.OWNER_RIGHTS_SID, 0, OI_CI, FULL), (USER, 0, OI_CI, FULL), (USERS, 0, OI_CI, READ_EXEC)]
        for owner in (USER, W.SYSTEM_SID, W.ADMINISTRATORS_SID):
            with self.subTest(owner=owner):
                self.assertIsNone(W.untrusted_reason(owner, USER, aces))

    def test_foreign_owner_null_dacl_and_missing_identity_fail_closed(self):
        self.assertIn("owned by " + OTHER, W.untrusted_reason(OTHER, USER, []))
        self.assertIn("NULL DACL", W.untrusted_reason(USER, USER, None))
        self.assertIsNotNone(W.untrusted_reason(None, USER, []))
        self.assertIsNotNone(W.untrusted_reason(USER, None, []))

    def test_write_class_rights_for_others_fail_closed(self):
        for sid in (EVERYONE, AUTH_USERS, USERS, OTHER):
            for mask in (0x2, 0x4, 0x40, 0x10000, 0x40000, 0x80000, 0x40000000, 0x10000000, MODIFY):
                with self.subTest(sid=sid, mask=hex(mask)):
                    self.assertIn("writable by " + sid, W.untrusted_reason(USER, USER, [(sid, 0, OI_CI, mask)]))

    def test_read_deny_inherit_only_and_audit_aces_do_not_widen_or_break_trust(self):
        aces = [(EVERYONE, 0, OI_CI, READ_EXEC),            # read-only for everyone
                (EVERYONE, 1, OI_CI, FULL),                 # deny never widens trust
                (EVERYONE, 0, OI_CI | W.INHERIT_ONLY_ACE, FULL),  # applies to children only
                (None, 2, 0, FULL)]                          # audit ACE
        self.assertIsNone(W.untrusted_reason(USER, USER, aces))

    def test_unknown_allow_ace_type_with_write_fails_closed(self):
        self.assertIn("unsupported allow ACE type 5", W.untrusted_reason(USER, USER, [(None, 5, 0, MODIFY)]))
        self.assertIsNone(W.untrusted_reason(USER, USER, [(None, 5, 0, READ_EXEC)]))


# Real NTFS owner/DACL evidence exists only on Windows. The CI contract allows
# no skips outside the host-runtime allowlist, so on POSIX this class is not a
# TestCase (not collected) instead of skipping; the platform-neutral evaluation
# above always runs, and the POSIX resolver branch keeps its own tests.
_NT_TESTCASE = unittest.TestCase if os.name == "nt" else object


class RealWindowsResolverTest(_NT_TESTCASE):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def host(self, name="host"):
        host = self.root / name
        (host / "agent_hub_host").mkdir(parents=True)
        (host / "agent_hub_host" / "__init__.py").write_text("")
        return host

    def resolve(self, value):
        with patch.dict(os.environ, {"AGENT_HUB_HOST_PATH": str(value)}):
            return resolve_validated_hub_host_dir()

    def untrusted(self, value, needle):
        with self.assertRaises(HubHostPathUntrusted) as ctx:
            self.resolve(value)
        self.assertIn(needle, str(ctx.exception))

    def junction(self, link, target):
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)

    def test_trusted_owner_only_directory_is_accepted(self):
        host = self.host()
        self.assertEqual(os.path.normcase(self.resolve(host)), os.path.normcase(str(host.resolve())))

    def test_relative_broken_and_packageless_paths_fail_closed(self):
        self.untrusted("relative\\host", "absolute")
        self.untrusted(self.root / "missing", "not an existing directory")
        empty = self.root / "empty"
        empty.mkdir()
        self.untrusted(empty, "does not contain an agent_hub_host package")
        with patch.dict(os.environ, {"AGENT_HUB_HOST_PATH": ""}):
            with self.assertRaises(HubHostPathUntrusted):
                resolve_validated_hub_host_dir()

    def test_writable_host_or_package_fails_closed(self):
        host = self.host("w-host")
        subprocess.run(["icacls", str(host), "/grant", "*S-1-1-0:(OI)(CI)(W)"], check=True, capture_output=True)
        self.untrusted(host, "writable by S-1-1-0")
        pkg_host = self.host("w-pkg")
        subprocess.run(["icacls", str(pkg_host / "agent_hub_host"), "/grant", "*S-1-5-11:(M)"], check=True,
                       capture_output=True)
        self.untrusted(pkg_host, "agent_hub_host package is untrusted: writable by S-1-5-11")

    def test_foreign_owner_fails_closed(self):
        host = self.host()
        with patch.object(W, "current_user_sid", return_value=OTHER):
            self.untrusted(host, "owned by S-1-5-21")

    def test_link_to_trusted_target_resolves_like_posix(self):
        target = self.host("real")
        link = self.root / "link"
        self.junction(link, target)
        self.assertEqual(os.path.normcase(self.resolve(link)), os.path.normcase(str(target.resolve())))

    def test_link_to_untrusted_target_fails_closed(self):
        target = self.host("bad-real")
        subprocess.run(["icacls", str(target), "/grant", "*S-1-1-0:(OI)(CI)(M)"], check=True, capture_output=True)
        link = self.root / "bad-link"
        self.junction(link, target)
        self.untrusted(link, "writable by S-1-1-0")

    def test_security_read_failure_fails_closed(self):
        host = self.host()
        with patch.object(W, "read_security", side_effect=W.WindowsTrustUnavailable(5, "denied")):
            self.untrusted(host, "security descriptor unavailable")


if __name__ == "__main__":
    unittest.main()
