"""Managed Claude falls back to Hub's fresh verdict when the native usage read is not ok (Hub intake bf0863c4).

Tabo Claude headless launches (f05a4ca4, ca52e88e/a24a35b1) failed
admission_quota_unverified: the per-token OAuth usage read is rate-limited
(Hub's own collector reads the same token), the host probe returned
status unknown with no source, and the plan rejected
"Untrusted or missing Claude quota source". Hub owns managed quota
(agent-hub 5814e109 I4), so a non-ok native reading asks Hub; Hub
refusals and stale or missing verdicts stay held.
"""
import json
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from launcher import admission as A

NOW = datetime(2026, 11, 2, 15, 56, 31, tzinfo=timezone.utc)
RATE_LIMITED = {"claude": {"status": "unknown", "error": "native_claude_rate_limited", "windows": {}}}


class ManagedDir:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "state.json").write_text(json.dumps([{"label": "hub-claude", "revision": "r1", "provider": "claude"}]))
        (root / "hub-claude-r1").mkdir()
        (root / "hub-claude-r1" / "bundle.json").write_text(json.dumps(
            {"label": "hub-claude", "provider": "claude", "revision": "r1", "args": [], "env": {}}))
        return root

    def __exit__(self, *exc):
        self.tmp.cleanup()


def verdict(**over):
    v = {"launch_allowed": True, "profile_supported": True, "account_label": "hub-claude",
         "observed_at": (NOW - timedelta(seconds=3)).isoformat().replace("+00:00", "Z")}
    v.update(over)
    return v


def probe_module(result):
    return types.SimpleNamespace(probe=lambda bundle, dest, requested_model=None: result)


def managed_reqs(root):
    return {"is_managed": True, "account_label": "hub-claude", "account_profile": "hub-hub-claude",
            "managed_dir": str(root), "model": "sonnet", "providers": ["claude"]}


class HubRouteTest(unittest.TestCase):
    def test_claude_verdict_is_keyed_claude(self):
        r = A.fetch_hub_admission_route("hub-claude", requested_model="sonnet", provider="claude",
                                        request=lambda m, p, b: verdict(), now=NOW)
        self.assertEqual(r["claude"]["status"], "ok")
        self.assertEqual(r["claude"]["details"]["source"], A.HUB_ADMISSION_SOURCE)

    def test_refused_stale_or_mismatched_verdicts_stay_held(self):
        for v, slug in ((verdict(launch_allowed=False, reason="quota_exhausted"), "hub_admission_quota_exhausted"),
                        (verdict(observed_at=(NOW - timedelta(minutes=5)).isoformat()), "hub_admission_stale"),
                        (verdict(account_label="other"), "hub_admission_account_mismatch")):
            r = A.fetch_hub_admission_route("hub-claude", requested_model="sonnet", provider="claude",
                                            request=lambda m, p, b, v=v: v, now=NOW)["claude"]
            self.assertEqual((r["status"], r["error"]), ("held", slug))

    def test_antigravity_default_is_unchanged(self):
        r = A.fetch_hub_admission_route("hub-antigravity", requested_model="gemini-4-argon-high",
                                        request=lambda m, p, b: verdict(account_label="hub-antigravity"), now=NOW)
        self.assertEqual(list(r), ["gemini"])


class ManagedFetchTest(unittest.TestCase):
    def fetch(self, native, hub=None):
        modules = {"native_claude_quota": native}
        with ManagedDir() as root, \
                patch.object(A, "_hub_host_module", side_effect=lambda name: modules[name]), \
                patch.object(A, "fetch_hub_admission_route", return_value=hub or {"claude": {"status": "ok"}}) as h:
            out = A.fetch_quse(account_label="hub-claude", provider="claude", requested_model="sonnet",
                               managed_dir=str(root))
        return out, h

    def test_ok_native_reading_is_used_without_asking_hub(self):
        ok = {"claude": {"status": "ok", "details": {"source": "native_claude_oauth_usage"}, "windows": {"5h": {}}}}
        out, hub = self.fetch(probe_module(ok))
        self.assertEqual(out, ok)
        hub.assert_not_called()

    def test_rate_limited_native_reading_falls_back_to_hub(self):
        out, hub = self.fetch(probe_module(RATE_LIMITED))
        self.assertEqual(out, {"claude": {"status": "ok"}})
        hub.assert_called_once_with("hub-claude", requested_model="sonnet", provider="claude")

    def test_crashed_native_probe_falls_back_to_hub(self):
        def boom(*a, **k):
            raise RuntimeError("probe crashed")
        out, hub = self.fetch(types.SimpleNamespace(probe=boom))
        hub.assert_called_once()


class ValidateTest(unittest.TestCase):
    def route(self, **details):
        d = {"source": A.HUB_ADMISSION_SOURCE, "account_label": "hub-claude", "model": "sonnet", "limit_reached": False}
        d.update(details)
        return {"status": "ok", "error": None, "details": d, "windows": {}}

    def test_hub_admitted_managed_claude_is_a_candidate(self):
        with ManagedDir() as root:
            valid, rejections = A.validate_quse({"claude": self.route()}, task_requirements=managed_reqs(root))
        self.assertEqual(rejections, {})
        [c] = valid
        self.assertEqual((c["provider"], c["hub_authorized"]), ("claude", True))
        self.assertIsNone(c["remaining_fraction"])

    def test_hub_verdict_for_another_account_is_rejected(self):
        with ManagedDir() as root:
            valid, rejections = A.validate_quse({"claude": self.route(account_label="shared-claude")},
                                                task_requirements=managed_reqs(root))
        self.assertEqual(valid, [])
        self.assertIn("account mismatch", " ".join(map(str, rejections.values())))

    def test_missing_source_still_reads_as_untrusted_quota_source(self):
        with ManagedDir() as root:
            valid, rejections = A.validate_quse({"claude": dict(self.route(), details={"model": "sonnet"})},
                                                task_requirements=managed_reqs(root))
        self.assertEqual(valid, [])
        self.assertIn("quota source", " ".join(map(str, rejections.values())))


if __name__ == "__main__":
    unittest.main()
