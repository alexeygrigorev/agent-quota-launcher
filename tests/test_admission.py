import unittest
from datetime import datetime, timezone, timedelta
from launcher.admission import validate_quse, codex_gate_reason, fetch_quse

def route(windows, status="ok", error=None, details=None):
    return {"status": status, "error": error, "details": details or {}, "windows": windows}

def grok_route(windows, status="ok", error=None, details=None):
    merged = {"has_grok_code_access": True}
    merged.update(details or {})
    return route(windows, status=status, error=error, details=merged)

def grok_route(windows, status="ok", error=None, details=None):
    merged = {"has_grok_code_access": True}
    merged.update(details or {})
    return route(windows, status=status, error=error, details=merged)

class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.future1 = (self.now + timedelta(days=1)).isoformat()
        self.future2 = (self.now + timedelta(days=2)).isoformat()
        self.past = (self.now - timedelta(days=1)).isoformat()

    def test_valid_route(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 50, "reset_at": self.future1}})}
        valid, rejections = validate_quse(data)
        self.assertIn("grok", [v["name"] for v in valid])
        self.assertEqual(valid[0]["provider"], "grok")
        self.assertEqual(valid[0]["model"], "grok-4.6")

    def test_limit_reached(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 0, "reset_at": self.future1}},
                              details={"limit_reached": True})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Limit reached", rej.get("grok", ""))

    def test_stale_evidence(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 50, "reset_at": self.past}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Stale route evidence", rej.get("grok", ""))

    def test_missing_reset_at(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 50}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Missing valid route evidence", rej.get("grok", ""))

    def test_nonnumeric_percent(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": "50%", "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Missing valid route evidence", rej.get("grok", ""))

    def test_nan_percent_rejected(self):
        # NaN admits a route at fabricated 100% headroom if not rejected.
        data = {"grok": grok_route({"7d": {"percent_remaining": float("nan"),
                                      "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Missing valid route evidence", rej.get("grok", ""))

    def test_inf_percent_rejected(self):
        data = {"zai": route({"5h": {"percent_remaining": float("inf"),
                                     "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Missing valid route evidence", rej.get("zai", ""))

    def test_naive_reset_at_no_crash_and_fail_closed(self):
        # One naive reset_at must neither crash other routes nor admit its own.
        data = {
            "grok": grok_route({"7d": {"percent_remaining": 50,
                                  "reset_at": "2026-10-05T14:00:00"}}),  # naive
            "zai": route({"5h": {"percent_remaining": 90, "reset_at": self.future1}}),
        }
        valid, rej = validate_quse(data)
        self.assertEqual([v["name"] for v in valid], ["zai"])
        self.assertIn("Missing valid route evidence", rej.get("grok", ""))

    def test_per_route_exception_isolation(self):
        # A malformed route must not kill validation of the others.
        data = {
            "grok": {"status": "ok", "windows": "not-a-dict"},
            "zai": route({"5h": {"percent_remaining": 90, "reset_at": self.future1}}),
        }
        valid, rej = validate_quse(data)
        self.assertEqual([v["name"] for v in valid], ["zai"])
        self.assertIn("grok", rej)

    def test_exhausted_window(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 0, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Quota exhausted", rej.get("grok", ""))

    def test_codex_at_15_remaining_rejected(self):
        data = {"codex": route({"7d": {"percent_remaining": 15, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("<= 15", rej.get("codex", ""))

    def test_codex_unknown_window_fail_closed(self):
        data = {"codex": route({"5h": {"percent_remaining": None, "reset_at": None},
                                "7d": {"percent_remaining": 80, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("fail-closed: unknown window reading", rej.get("codex", ""))

    def test_codex_gate_helper(self):
        ok = {"windows": {"7d": {"percent_remaining": 60,
                                 "reset_at": (self.now + timedelta(days=3)).isoformat()}}}
        self.assertIsNone(codex_gate_reason(ok, self.now))
        low = {"windows": {"7d": {"percent_remaining": 10,
                                  "reset_at": (self.now + timedelta(days=3)).isoformat()}}}
        self.assertIn("<= 15", codex_gate_reason(low, self.now))

    def test_codex_unsupported_even_when_ample(self):
        data = {"codex": route({"7d": {"percent_remaining": 80, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("unsupported in launcher v0.1", rej.get("codex", ""))

    def test_unsupported_routes_blocked(self):
        data = {
            "claude": route({"5h": {"percent_remaining": 90, "reset_at": self.future1}}),
            "copilot": route({"monthly": {"percent_remaining": 100,
                                          "reset_at": self.future1}}),
            "go": route({"5h": {"percent_remaining": 100, "reset_at": self.future1}}),
        }
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        for name in ("claude", "copilot", "go"):
            self.assertIn("Unsupported route", rej.get(name, ""))

    def test_grok_entitlement_required(self):
        # quota availability alone is insufficient for grok
        data = {"grok": grok_route({"7d": {"percent_remaining": 70, "reset_at": self.future1}},
                                   details={"has_grok_code_access": False})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("has_grok_code_access", rej.get("grok", ""))
        ok = {"grok": route({"7d": {"percent_remaining": 70, "reset_at": self.future1}},
                            details={"has_grok_code_access": True})}
        valid2, _ = validate_quse(ok)
        self.assertEqual([v["name"] for v in valid2], ["grok"])

    def test_task_fit_known_against_requirements(self):
        reqs = {"providers": ["zai"], "models": ["glm-5.3-flash"]}
        data = {"zai": route({"5h": {"percent_remaining": 90, "reset_at": self.future1}})}
        valid, _ = validate_quse(data, task_requirements=reqs)
        self.assertEqual(valid[0]["task_fit"], 1.0)

        reqs_other = {"providers": ["grok"]}
        valid2, _ = validate_quse(data, task_requirements=reqs_other)
        self.assertEqual(valid2[0]["task_fit"], 0.0)

    def test_task_fit_unknown_without_requirements(self):
        data = {"zai": route({"5h": {"percent_remaining": 90, "reset_at": self.future1}})}
        valid, _ = validate_quse(data)
        self.assertIsNone(valid[0]["task_fit"])       # unknown -> weight 0, not fabricated
        self.assertEqual(valid[0]["health"], 1.0)     # measured from quse status ok

    def test_gemini_maps_to_antigravity(self):
        data = {"gemini": route({"5h": {"percent_remaining": 82.5,
                                        "reset_at": self.future1}})}
        valid, _ = validate_quse(data)
        self.assertEqual(valid[0]["provider"], "antigravity")

    def test_fetch_quse_rejects_truncated_json(self):
        import subprocess
        from unittest.mock import patch
        fake = subprocess.CompletedProcess(args=[], returncode=0)
        fake.stdout = '{"grok": {"status": "ok"'  # truncated by a crash mid-print
        with patch("launcher.admission.subprocess.run", return_value=fake):
            with self.assertRaisesRegex(ValueError, "Invalid quse JSON"):
                fetch_quse()

    def test_fetch_quse_salvages_trailing_garbage(self):
        import subprocess
        from unittest.mock import patch
        fake = subprocess.CompletedProcess(args=[], returncode=0)
        fake.stdout = '{"grok": {"status": "ok", "windows": {}}}\nsp=0x... Go panic trace'
        with patch("launcher.admission.subprocess.run", return_value=fake):
            data = fetch_quse()
        self.assertIn("grok", data)

if __name__ == '__main__':
    unittest.main()
