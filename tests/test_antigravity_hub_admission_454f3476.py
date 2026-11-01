"""Managed Antigravity admission consumes Hub's fresh verdict (Hub intake 454f3476).

A Hub Antigravity launch on DESKTOP-FP4OP26 (probe 0fabbd43, offer 8d71d585)
exited 1 with an uncaught ValueError before the first launch record:
fetch_managed_quse special-cased only gemini/claude, so provider antigravity
ran `quse antigravity --json` -> `agy -p /quota` (OAuth browser tab loop,
agent-hub 09e74d4). The managed path must never run quse/agy, must take
Hub's account/model/admission verdict through the validated host package,
and must preserve refusals and stale or missing evidence. Standalone
(non-managed) admission is unchanged.
"""
import json
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from launcher import admission as A
from launcher.ranking import calculate_weight, select_candidate

MODEL = "gemini-4-argon-high"
NOW = datetime(2026, 11, 1, 9, 39, 37, tzinfo=timezone.utc)


def verdict(**over):
    v = {"launch_allowed": True, "profile_supported": True, "account_label": "hub-antigravity",
         "observed_at": (NOW - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")}
    v.update(over)
    return v


def no_spawn(*a, **k):
    raise AssertionError("managed Antigravity admission must not spawn quse/agy")


class ManagedDir:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "state.json").write_text(json.dumps(
            [{"label": "hub-antigravity", "revision": "rev1", "provider": "antigravity"}]))
        (root / "hub-antigravity-rev1").mkdir()
        (root / "hub-antigravity-rev1" / "bundle.json").write_text(json.dumps(
            {"label": "hub-antigravity", "provider": "antigravity", "revision": "rev1", "args": [], "env": {}}))
        self.root = root
        return root

    def __exit__(self, *exc):
        self.tmp.cleanup()


def managed_reqs(root):
    # The exact launcher payload shape Hub submitted for offer 8d71d585.
    payload = {"account_label": "hub-antigravity", "account_profile": "hub-hub-antigravity",
               "provider_profile": "hub-hub-antigravity", "managed_dir": str(root), "profile": "model-task",
               "model_requirements": {"models": [MODEL], "providers": ["antigravity"]}}
    reqs = A.task_requirements_for(payload)
    assert reqs["is_managed"] is True and reqs["managed_dir"] == str(root), reqs
    return reqs


class HubAdmissionRouteTest(unittest.TestCase):
    def route(self, v=None, exc=None, model=MODEL):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            if exc:
                raise exc
            return v

        out = A.fetch_hub_admission_route("hub-antigravity", requested_model=model, request=request, now=NOW)
        return out["gemini"], calls

    def test_fresh_allowed_verdict_is_ok_hub_admission_route(self):
        r, calls = self.route(verdict(quota_warning="quota_probe_unavailable"))
        self.assertEqual(calls, [("POST", "/api/engines/admission", {"label": "hub-antigravity"})])
        self.assertEqual((r["status"], r["error"], r["windows"]), ("ok", None, {}))
        self.assertEqual(r["details"]["source"], A.HUB_ADMISSION_SOURCE)
        self.assertEqual(r["details"]["model"], MODEL)
        self.assertEqual(r["details"]["quota_warning"], "quota_probe_unavailable")

    def test_hub_refusals_are_preserved_as_held(self):
        for reason in ("quota_exhausted", "credential_invalid", "credential_rejected",
                       "account_unmapped", "profile_not_installed"):
            with self.subTest(reason=reason):
                r, _ = self.route(verdict(launch_allowed=False, reason=reason))
                self.assertEqual((r["status"], r["error"]), ("held", "hub_admission_" + reason))
        r, _ = self.route(verdict(launch_allowed=False, reason="Free text <b>body</b>"))
        self.assertEqual(r["error"], "hub_admission_refused")  # never forward unbounded text
        r, _ = self.route(verdict(profile_supported=False))
        self.assertEqual(r["status"], "held")

    def test_stale_missing_and_mismatched_evidence_holds(self):
        stale = (NOW - timedelta(seconds=31)).isoformat()
        future = (NOW + timedelta(seconds=6)).isoformat()
        cases = {
            "hub_admission_stale": [verdict(observed_at=stale), verdict(observed_at=future)],
            "hub_admission_timestamp_missing": [verdict(observed_at=None), verdict(observed_at="yesterday")],
            "hub_admission_account_mismatch": [verdict(account_label="shared-gemini"), None, ["x"]],
        }
        for reason, vs in cases.items():
            for v in vs:
                with self.subTest(reason=reason, v=v):
                    r, _ = self.route(v)
                    self.assertEqual((r["status"], r["error"]), ("held", reason))

    def test_unreachable_untrusted_and_unsupported_model_hold_without_request(self):
        r, _ = self.route(exc=OSError("down"))
        self.assertEqual(r["error"], "hub_admission_unavailable")
        from launcher.launch import HubHostPathUntrusted
        r, _ = self.route(exc=HubHostPathUntrusted("bad host path"))
        self.assertEqual(r["error"], "hub_admission_host_untrusted")
        for model in (None, "", "gemini-9-unknown"):
            r, calls = self.route(verdict(), model=model)
            self.assertEqual((r["error"], calls), ("hub_admission_model_unsupported", []))

    def test_default_request_comes_only_from_validated_host_module(self):
        seen = []
        fake = types.SimpleNamespace(hub_request=lambda m, p, b: verdict(
            observed_at=datetime.now(timezone.utc).isoformat()))
        with patch.object(A, "_hub_host_module", side_effect=lambda name: seen.append(name) or fake):
            r = A.fetch_hub_admission_route("hub-antigravity", requested_model=MODEL)["gemini"]
        self.assertEqual(seen, ["native_admission"])
        self.assertEqual(r["status"], "ok")


class ManagedFetchTest(unittest.TestCase):
    def test_managed_antigravity_never_spawns_quse_or_agy(self):
        with ManagedDir() as root, \
                patch.object(subprocess, "run", side_effect=no_spawn), \
                patch.object(subprocess, "Popen", side_effect=no_spawn), \
                patch.object(A, "fetch_hub_admission_route",
                             return_value={"gemini": {"status": "ok"}}) as hub:
            out = A.fetch_quse(account_label="hub-antigravity", provider="antigravity",
                               requested_model=MODEL, managed_dir=str(root))
        self.assertEqual(out, {"gemini": {"status": "ok"}})
        hub.assert_called_once_with("hub-antigravity", requested_model=MODEL)

    def test_standalone_quse_path_unchanged(self):
        ok = subprocess.CompletedProcess(["quse", "--json"], 0, stdout='{"gemini": {}}', stderr="")
        with patch.object(subprocess, "run", return_value=ok) as run, \
                patch.object(A, "fetch_hub_admission_route", side_effect=AssertionError("no hub call")):
            self.assertEqual(A.fetch_quse(), {"gemini": {}})
        self.assertEqual(run.call_args[0][0], ["quse", "--json"])


class ValidateAndRankTest(unittest.TestCase):
    def ok_route(self, **details):
        d = {"source": A.HUB_ADMISSION_SOURCE, "account_label": "hub-antigravity", "model": MODEL,
             "limit_reached": False}
        d.update(details)
        return {"status": "ok", "error": None, "details": d, "windows": {}}

    def test_hub_admitted_managed_route_is_selected_without_invented_quota(self):
        with ManagedDir() as root:
            valid, rejections = A.validate_quse({"gemini": self.ok_route()}, task_requirements=managed_reqs(root))
        self.assertEqual(rejections, {})
        [c] = valid
        self.assertEqual((c["provider"], c["model"], c["hub_authorized"]), ("antigravity", MODEL, True))
        self.assertIsNone(c["remaining_fraction"])
        self.assertIsNone(c["hours_to_reset"])
        chosen, prov = select_candidate(valid, seed=1)
        self.assertEqual(chosen["provider"], "antigravity")
        self.assertEqual(prov["chosen"], "gemini")
        self.assertEqual(prov["candidates"][0]["reason"], "hub_admission_authorized")

    def test_held_mismatched_or_untrusted_evidence_is_rejected(self):
        with ManagedDir() as root:
            reqs = managed_reqs(root)
            held = A._hub_admission_held("hub_admission_quota_exhausted", {"account_label": "hub-antigravity", "model": MODEL})
            cases = {
                "held": held["gemini"],
                "wrong account": self.ok_route(account_label="other"),
                "wrong model": self.ok_route(model="gemini-3.8-flash-high"),
                "quse-style windows, no hub source": {"status": "ok", "error": None, "details": {},
                                                       "windows": {"w": {"percent_remaining": 90,
                                                                         "reset_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}},
                "limit reached": self.ok_route(limit_reached=True),
            }
            for label, r in cases.items():
                with self.subTest(label):
                    valid, rejections = A.validate_quse({"gemini": r}, task_requirements=reqs)
                    self.assertEqual(valid, [])
                    self.assertIn("gemini", rejections)
            self.assertIn("hub_admission_quota_exhausted", A.validate_quse({"gemini": held["gemini"]}, task_requirements=reqs)[1]["gemini"])

    def test_hub_evidence_cannot_authorize_a_non_managed_route(self):
        reqs = {"providers": ["antigravity"], "models": [MODEL], "kind": "general"}
        valid, rejections = A.validate_quse({"gemini": self.ok_route()}, task_requirements=reqs)
        self.assertEqual(valid, [])
        self.assertIn("managed", rejections["gemini"])

    def test_hub_authorized_weight_rules(self):
        self.assertEqual(calculate_weight(1.0, 1.0, None, None, 1.0, hub_authorized=True), (1.0, "hub_admission_authorized"))
        self.assertEqual(calculate_weight(1.0, 1.0, 0.5, None, 1.0, hub_authorized=True)[0], 0.0)
        self.assertEqual(calculate_weight(0.0, 1.0, None, None, 1.0, hub_authorized=True)[0], 0.0)
        self.assertEqual(calculate_weight(1.0, 1.0, None, None, 1.0)[0], 0.0)  # default unchanged


if __name__ == "__main__":
    unittest.main()
