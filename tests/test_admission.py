from unittest.mock import patch
import unittest
from datetime import datetime, timezone, timedelta
from launcher.admission import validate_quse, codex_gate_reason, fetch_quse

def route(windows, status="ok", error=None, details=None):
    return {"status": status, "error": error, "details": details or {}, "windows": windows}

def grok_route(windows, status="ok", error=None, details=None):
    merged = {"has_grok_code_access": True}
    merged.update(details or {})
    return route(windows, status=status, error=error, details=merged)

class TestAdmission(unittest.TestCase):
    def test_named_codex_model_task_has_task_fit(self):
        from launcher.admission import _eval_task_fit, task_requirements_for
        payload = {"profile": "model-task", "account_profile": "hub-second-codex",
                   "model_requirements": {"providers": ["codex"], "models": ["gpt-6.1-sol"]}}
        fit, model = _eval_task_fit("codex", task_requirements_for(payload))
        self.assertEqual(fit, 1.0)
        self.assertEqual(model, "gpt-6.1-sol")

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
        data = {"codex": route({"weekly": {"percent_remaining": 15, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("<= 15", rej.get("codex", ""))

    def test_codex_unknown_window_fail_closed(self):
        data = {"codex": route({"daily": {"percent_remaining": None, "reset_at": None},
                                "weekly": {"percent_remaining": 80, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("fail-closed: unknown window reading", rej.get("codex", ""))

    def test_codex_gate_helper(self):
        ok = {"windows": {"weekly": {"percent_remaining": 60,
                                 "reset_at": (self.now + timedelta(days=3)).isoformat()}}}
        self.assertIsNone(codex_gate_reason(ok, self.now))
        low = {"windows": {"weekly": {"percent_remaining": 10,
                                  "reset_at": (self.now + timedelta(days=3)).isoformat()}}}
        self.assertIn("<= 15", codex_gate_reason(low, self.now))

    def test_codex_unsupported_even_when_ample(self):
        data = {"codex": route({"weekly": {"percent_remaining": 80, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("unsupported in launcher v0.1", rej.get("codex", ""))

    def test_unsupported_routes_blocked(self):
        data = {
            "claude": route({"5h": {"percent_remaining": 90, "reset_at": self.future1}}),
            "copilot": route({"monthly": {"percent_remaining": 100,
                                          "reset_at": self.future1}}),
        }
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        for name in ("claude", "copilot"):
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

    def test_grok_at_exactly_5_percent_rejected(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 5.0, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Grok window <= 5% remaining (cutoff policy)", rej.get("grok", ""))

    def test_grok_below_5_percent_rejected(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 4.9, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Grok window <= 5% remaining (cutoff policy)", rej.get("grok", ""))

    def test_grok_above_5_percent_admitted(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 5.1, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["provider"], "grok")

    def test_grok_exhausted_rejected(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 0.0, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Quota exhausted", rej.get("grok", ""))

    def test_grok_limit_reached_rejected(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 50, "reset_at": self.future1}},
                                   details={"limit_reached": True})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Limit reached", rej.get("grok", ""))

    def test_grok_failed_reading_rejected(self):
        data = {"grok": grok_route({"7d": {"percent_remaining": 50, "reset_at": self.future1}},
                                   status="error", error="provider timeout")}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("Error: provider timeout", rej.get("grok", ""))

    def test_grok_absent_window_not_interpreted_as_zero(self):
        # 5h window absent: 7d window at 50% is valid evidence, not treated as 0%
        data = {"grok": grok_route({"7d": {"percent_remaining": 50, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["remaining_fraction"], 0.5)


    @patch("launcher.capacity.check_provider_capacity")
    def test_zai_concurrency_bound_enforced(self, mock_check_cap):
        # When ZAI concurrency bound <= 26 is reached, ensure it is rejected
        mock_check_cap.return_value = (False, "zai hostwide capacity ceiling (26) reached: 26 live processes", {})
        data = {"zai": route({"5h": {"percent_remaining": 90, "reset_at": self.future1}})}
        valid, rej = validate_quse(data, check_capacity=True)
        self.assertEqual(len(valid), 0)
        self.assertIn("capacity ceiling (26) reached", rej.get("zai", ""))

    
    def test_codex_above_15_percent_passes_gate(self):
        data = {"codex": route({"weekly": {"percent_remaining": 15.1, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        # It passes the 15% gate and hits the unsupported wrapper error instead.
        self.assertIn("unsupported in launcher v0.1", rej.get("codex", ""))

    def test_codex_optional_absent_window_admitted(self):
        # A missing secondary window (present: false) does not block admission if the required 'weekly' window is valid
        data = {"codex": route({"weekly": {"percent_remaining": 80, "reset_at": self.future1},
                                "daily": {"present": False}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        # Should hit unsupported wrapper error instead of window rejection
        self.assertIn("unsupported in launcher v0.1", rej.get("codex", ""))

    def test_codex_present_true_unknown_reading_rejected(self):
        # If a secondary window is present: true but missing percent_remaining, it's rejected
        data = {"codex": route({"weekly": {"percent_remaining": 80, "reset_at": self.future1},
                                "daily": {"present": True, "reset_at": self.future1}})}
        valid, rej = validate_quse(data)
        self.assertEqual(len(valid), 0)
        self.assertIn("codex fail-closed: unknown window reading", rej.get("codex", ""))

    def test_gemini_default_model_is_flash_3_8_high(self):
        data = {"gemini": route({"5h": {"percent_remaining": 82.5, "reset_at": self.future1}})}
        valid, rejections = validate_quse(data)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["provider"], "antigravity")
        self.assertEqual(valid[0]["model"], "gemini-3.8-flash-high")
        self.assertIsNone(valid[0]["task_fit"])

    def test_gemini_explicit_argon_selection(self):
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        reqs = {"models": ["gemini-4-argon-high"]}
        valid, rejections = validate_quse(data, task_requirements=reqs)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["provider"], "antigravity")
        self.assertEqual(valid[0]["model"], "gemini-4-argon-high")
        self.assertEqual(valid[0]["task_fit"], 1.0)

    def test_gemini_explicit_argon_selection_via_singular_model(self):
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        reqs = {"model": "gemini-4-argon-high"}
        valid, rejections = validate_quse(data, task_requirements=reqs)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["provider"], "antigravity")
        self.assertEqual(valid[0]["model"], "gemini-4-argon-high")
        self.assertEqual(valid[0]["task_fit"], 1.0)

    def test_gemini_explicit_flash_selection(self):
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        reqs = {"models": ["gemini-3.8-flash-high"]}
        valid, rejections = validate_quse(data, task_requirements=reqs)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["provider"], "antigravity")
        self.assertEqual(valid[0]["model"], "gemini-3.8-flash-high")
        self.assertEqual(valid[0]["task_fit"], 1.0)

    def test_gemini_obsolete_pro_rejected_task_fit_zero(self):
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        reqs = {"models": ["gemini-3.1-pro-high"]}
        valid, rejections = validate_quse(data, task_requirements=reqs)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["task_fit"], 0.0)

    def test_gemini_obsolete_pro_rejected_singular_model_task_fit_zero(self):
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        reqs = {"model": "gemini-3.1-pro-high"}
        valid, rejections = validate_quse(data, task_requirements=reqs)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["task_fit"], 0.0)

    def test_gemini_obsolete_pro_not_chosen_in_ranking(self):
        from launcher.ranking import select_candidate
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        reqs = {"models": ["gemini-3.1-pro-high"]}
        valid, _ = validate_quse(data, task_requirements=reqs)
        chosen, prov = select_candidate(valid, seed=42)
        self.assertIsNone(chosen)
        self.assertEqual(prov["candidates"][0]["weight"], 0.0)
        self.assertEqual(prov["candidates"][0]["reason"], "task fit or health is zero")

    def test_gemini_unknown_model_rejected_task_fit_zero(self):
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        reqs = {"models": ["gemini-unknown-model-99"]}
        valid, rejections = validate_quse(data, task_requirements=reqs)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["task_fit"], 0.0)

    def test_gemini_supported_order_selection_consistent(self):
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        # Argon first in requested models -> argon selected
        reqs_argon_first = {"models": ["gemini-4-argon-high", "gemini-3.8-flash-high"]}
        valid1, _ = validate_quse(data, task_requirements=reqs_argon_first)
        self.assertEqual(valid1[0]["model"], "gemini-4-argon-high")
        self.assertEqual(valid1[0]["task_fit"], 1.0)

        # Flash first in requested models -> flash selected
        reqs_flash_first = {"models": ["gemini-3.8-flash-high", "gemini-4-argon-high"]}
        valid2, _ = validate_quse(data, task_requirements=reqs_flash_first)
        self.assertEqual(valid2[0]["model"], "gemini-3.8-flash-high")
        self.assertEqual(valid2[0]["task_fit"], 1.0)

        # Obsolete pro first, argon second -> pro skipped, argon selected
        reqs_pro_argon = {"models": ["gemini-3.1-pro-high", "gemini-4-argon-high"]}
        valid3, _ = validate_quse(data, task_requirements=reqs_pro_argon)
        self.assertEqual(valid3[0]["model"], "gemini-4-argon-high")
        self.assertEqual(valid3[0]["task_fit"], 1.0)

    def test_gemini_fails_closed_on_stale_or_missing_or_nonfinite_quota(self):
        # Exhausted
        data_exhausted = {"gemini": route({"5h": {"percent_remaining": 0, "reset_at": self.future1}})}
        v, rej = validate_quse(data_exhausted)
        self.assertEqual(len(v), 0)
        self.assertIn("Quota exhausted", rej.get("gemini", ""))

        # Stale evidence
        data_stale = {"gemini": route({"5h": {"percent_remaining": 50, "reset_at": self.past}})}
        v, rej = validate_quse(data_stale)
        self.assertEqual(len(v), 0)
        self.assertIn("Stale route evidence", rej.get("gemini", ""))

        # Missing reset_at
        data_missing = {"gemini": route({"5h": {"percent_remaining": 50}})}
        v, rej = validate_quse(data_missing)
        self.assertEqual(len(v), 0)
        self.assertIn("Missing valid route evidence", rej.get("gemini", ""))

        # Nonfinite percent
        data_nan = {"gemini": route({"5h": {"percent_remaining": float("nan"), "reset_at": self.future1}})}
        v, rej = validate_quse(data_nan)
        self.assertEqual(len(v), 0)
        self.assertIn("Missing valid route evidence", rej.get("gemini", ""))

    def test_negative_probes_conflicting_dual_model_fails_closed(self):
        from launcher.ranking import select_candidate
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        conflicts = [
            {"models": ["gemini-3.8-flash-high"], "model": "gemini-3.1-pro-high"},
            {"models": ["gemini-3.8-flash-high"], "model": "gemini-4-argon-high"},
            {"models": ["gemini-4-argon-high"], "model": "gemini-3.8-flash-high"},
            {"models": ["gemini-3.8-flash-high", "gemini-4-argon-high"], "model": "gemini-unknown"},
        ]
        for req in conflicts:
            valid, _ = validate_quse(data, task_requirements=req)
            self.assertEqual(len(valid), 1, f"Failed on {req}")
            self.assertEqual(valid[0]["task_fit"], 0.0, f"Failed on {req}")
            self.assertEqual(valid[0]["model"], "gemini-3.8-flash-high", f"Failed on {req}")
            chosen, prov = select_candidate(valid, seed=42)
            self.assertIsNone(chosen, f"Should not choose conflicting req {req}")
            self.assertEqual(prov["candidates"][0]["weight"], 0.0)

    def test_negative_probes_malformed_model_types_fail_closed(self):
        from launcher.ranking import select_candidate
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        malformed = [
            {"model": None},
            {"model": 123},
            {"model": {"name": "gemini-3.1-pro-high"}},
            {"model": True},
            {"model": False},
            {"model": ["gemini-4-argon-high"]},
            {"model": ""},
            {"model": "   "},
        ]
        for req in malformed:
            valid, _ = validate_quse(data, task_requirements=req)
            self.assertEqual(len(valid), 1, f"Failed on {req}")
            self.assertEqual(valid[0]["task_fit"], 0.0, f"Failed on {req}")
            self.assertEqual(valid[0]["model"], "gemini-3.8-flash-high", f"Failed on {req}")
            chosen, prov = select_candidate(valid, seed=42)
            self.assertIsNone(chosen, f"Should not choose malformed req {req}")
            self.assertEqual(prov["candidates"][0]["weight"], 0.0)

    def test_negative_probes_malformed_models_types_fail_closed(self):
        from launcher.ranking import select_candidate
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        malformed = [
            {"models": "gemini-3.1-pro-high"},
            {"models": None},
            {"models": 123},
            {"models": True},
            {"models": False},
            {"models": {}},
            {"models": []},
            {"models": ()},
            {"models": ["gemini-3.8-flash-high", 123]},
            {"models": ["gemini-3.8-flash-high", None]},
            {"models": ["gemini-3.8-flash-high", True]},
            {"models": ["gemini-3.8-flash-high", ""]},
            {"models": ["gemini-3.8-flash-high", "   "]},
            {"models": ["   "]},
            {"models": [""]},
        ]
        for req in malformed:
            valid, _ = validate_quse(data, task_requirements=req)
            self.assertEqual(len(valid), 1, f"Failed on {req}")
            self.assertEqual(valid[0]["task_fit"], 0.0, f"Failed on {req}")
            self.assertEqual(valid[0]["model"], "gemini-3.8-flash-high", f"Failed on {req}")
            chosen, prov = select_candidate(valid, seed=42)
            self.assertIsNone(chosen, f"Should not choose malformed req {req}")
            self.assertEqual(prov["candidates"][0]["weight"], 0.0)

    def test_negative_probes_malformed_provider_and_providers_fail_closed(self):
        from launcher.ranking import select_candidate
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        malformed = [
            {"provider": None},
            {"provider": 123},
            {"provider": True},
            {"provider": ""},
            {"provider": "   "},
            {"providers": None},
            {"providers": 123},
            {"providers": "antigravity"},
            {"providers": []},
            {"providers": ["antigravity", 123]},
            {"providers": ["antigravity", None]},
            {"providers": ["antigravity", ""]},
            {"providers": ["   "]},
            {"provider": "antigravity", "providers": ["grok"]},
        ]
        for req in malformed:
            valid, _ = validate_quse(data, task_requirements=req)
            self.assertEqual(len(valid), 1, f"Failed on {req}")
            self.assertEqual(valid[0]["task_fit"], 0.0, f"Failed on {req}")
            self.assertEqual(valid[0]["model"], "gemini-3.8-flash-high", f"Failed on {req}")
            chosen, prov = select_candidate(valid, seed=42)
            self.assertIsNone(chosen, f"Should not choose malformed req {req}")
            self.assertEqual(prov["candidates"][0]["weight"], 0.0)

    def test_negative_probes_empty_and_unknown_only_requirements(self):
        from launcher.ranking import select_candidate
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        for req in ({}, {"unknown_key": "some_value"}, {"foo": 1, "bar": [2]}):
            valid, _ = validate_quse(data, task_requirements=req)
            self.assertEqual(len(valid), 1)
            self.assertIsNone(valid[0]["task_fit"])
            self.assertEqual(valid[0]["model"], "gemini-3.8-flash-high")
            chosen, prov = select_candidate(valid, seed=42)
            self.assertIsNone(chosen)
            self.assertEqual(prov["candidates"][0]["weight"], 0.0)
            self.assertIn("unknown task_fit/health", prov["candidates"][0]["reason"])

    def test_negative_probes_unallowlisted_models_fail_closed(self):
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        probes = [
            {"models": ["gemini-unknown-model-99"]},
            {"models": ["gemini-4-argon-medium"]},
            {"model": "gemini-4-argon-low"},
            {"model": "gemini-3.8-flash-medium"},
        ]
        for req in probes:
            valid, _ = validate_quse(data, task_requirements=req)
            self.assertEqual(len(valid), 1)
            self.assertEqual(valid[0]["task_fit"], 0.0)
            self.assertEqual(valid[0]["model"], "gemini-3.8-flash-high")

    def test_valid_provider_only_declarations_preserve_default_model(self):
        from launcher.ranking import select_candidate
        data = {
            "gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}}),
            "grok": grok_route({"7d": {"percent_remaining": 80.0, "reset_at": self.future1}}),
        }
        req_anti = {"provider": "antigravity"}
        valid, _ = validate_quse(data, task_requirements=req_anti)
        anti = [r for r in valid if r["provider"] == "antigravity"][0]
        grok = [r for r in valid if r["provider"] == "grok"][0]
        self.assertEqual(anti["task_fit"], 1.0)
        self.assertEqual(anti["model"], "gemini-3.8-flash-high")
        self.assertEqual(grok["task_fit"], 0.0)

        req_anti_list = {"providers": ["antigravity"]}
        valid2, _ = validate_quse(data, task_requirements=req_anti_list)
        anti2 = [r for r in valid2 if r["provider"] == "antigravity"][0]
        grok2 = [r for r in valid2 if r["provider"] == "grok"][0]
        self.assertEqual(anti2["task_fit"], 1.0)
        self.assertEqual(anti2["model"], "gemini-3.8-flash-high")
        self.assertEqual(grok2["task_fit"], 0.0)

        chosen, _ = select_candidate(valid, seed=42)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["provider"], "antigravity")

    def test_compatible_dual_model_specifications(self):
        from launcher.ranking import select_candidate
        data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future1}})}
        # Compatible: both specify Argon
        req_argon_dual = {
            "models": ["gemini-3.8-flash-high", "gemini-4-argon-high"],
            "model": "gemini-4-argon-high",
        }
        valid, _ = validate_quse(data, task_requirements=req_argon_dual)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["task_fit"], 1.0)
        self.assertEqual(valid[0]["model"], "gemini-4-argon-high")

        # Compatible: both specify Flash
        req_flash_dual = {
            "models": ["gemini-3.8-flash-high"],
            "model": "gemini-3.8-flash-high",
        }
        valid2, _ = validate_quse(data, task_requirements=req_flash_dual)
        self.assertEqual(len(valid2), 1)
        self.assertEqual(valid2[0]["task_fit"], 1.0)
        self.assertEqual(valid2[0]["model"], "gemini-3.8-flash-high")

        # Compatible matching obsolete pro -> both agree on obsolete pro, rejected task_fit 0.0
        req_pro_dual = {
            "models": ["gemini-3.1-pro-high"],
            "model": "gemini-3.1-pro-high",
        }
        valid3, _ = validate_quse(data, task_requirements=req_pro_dual)
        self.assertEqual(len(valid3), 1)
        self.assertEqual(valid3[0]["task_fit"], 0.0)
        self.assertEqual(valid3[0]["model"], "gemini-3.8-flash-high")

if __name__ == '__main__':
    unittest.main()


class TestCodexNoFiveHourWindow(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.reset = (self.now + timedelta(days=5)).isoformat()

    def live_shape(self, pct=90.0):
        return {"windows": {"5h": {"percent_remaining": None, "reset_at": None, "rolling": False},
                            "7d": {"percent_remaining": pct, "reset_at": self.reset},
                            "monthly": {"percent_remaining": None, "reset_at": None}}}

    def test_codex_without_5h_judged_on_7d(self):
        self.assertIsNone(codex_gate_reason(self.live_shape(90.0), self.now))
        self.assertIn("<= 15", codex_gate_reason(self.live_shape(10.0), self.now))

    def test_codex_missing_7d_still_fails_closed(self):
        r = self.live_shape()
        r["windows"]["7d"] = {"percent_remaining": None, "reset_at": self.reset}
        self.assertIn("unknown window reading (7d)", codex_gate_reason(r, self.now))
        del r["windows"]["7d"]
        self.assertIn("missing required weekly window", codex_gate_reason(r, self.now))

    def test_codex_5h_with_reset_but_no_reading_is_unknown(self):
        r = self.live_shape()
        r["windows"]["5h"] = {"percent_remaining": None, "reset_at": self.reset}
        self.assertIn("unknown window reading (5h)", codex_gate_reason(r, self.now))


class TestTaskKindFit(unittest.TestCase):
    def setUp(self):
        self.reset = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.data = {"zai": route({"7d": {"percent_remaining": 80, "reset_at": self.reset}})}

    def test_zai_fits_general_kinds(self):
        from launcher.ranking import calculate_weight
        for kind in ("coding", "implementation", "docs", "review"):
            valid, _ = validate_quse(self.data, task_requirements={"kind": kind})
            self.assertEqual(valid[0]["task_fit"], 1.0, kind)
            c = valid[0]
            w, _ = calculate_weight(c["task_fit"], c["health"], c["remaining_fraction"],
                                    c["hours_to_reset"], c["promo_multiplier"], provider="zai")
            self.assertGreater(w, 0)

    def test_profile_implies_kind(self):
        from launcher.admission import task_requirements_for
        reqs = task_requirements_for({"profile": "model-task"})
        valid, _ = validate_quse(self.data, task_requirements=reqs)
        self.assertEqual(valid[0]["task_fit"], 1.0)
        self.assertIsNone(task_requirements_for({}))

    def test_real_exclusions_still_apply(self):
        for reqs in ({"kind": "implementation", "providers": ["antigravity"]},
                     {"kind": "implementation", "models": ["gemini-3.8-flash-high"]},
                     {"kind": "video-editing"}):
            valid, _ = validate_quse(self.data, task_requirements=reqs)
            self.assertEqual(valid[0]["task_fit"], 0.0, reqs)


class TestNativeGeminiAdmission(unittest.TestCase):
    def setUp(self):
        self.future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.data = {"gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future}})}

    def test_gemini_native_route_default_model(self):
        valid, _ = validate_quse(self.data, task_requirements={"provider": "gemini"})
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["provider"], "gemini")
        self.assertEqual(valid[0]["model"], "gemini-2.5-pro")
        self.assertEqual(valid[0]["task_fit"], 1.0)

    def test_gemini_native_supported_model_override(self):
        for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite", "auto-gemini-2.5"):
            valid, _ = validate_quse(self.data, task_requirements={"provider": "gemini", "model": model})
            self.assertEqual(len(valid), 1)
            self.assertEqual(valid[0]["provider"], "gemini")
            self.assertEqual(valid[0]["model"], model)
            self.assertEqual(valid[0]["task_fit"], 1.0)

    def test_gemini_antigravity_model_rejected_for_native_gemini(self):
        # Antigravity models (e.g. gemini-3.8-flash-high) must NOT be accepted for native gemini
        valid, _ = validate_quse(self.data, task_requirements={"provider": "gemini", "model": "gemini-3.8-flash-high"})
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["provider"], "gemini")
        self.assertEqual(valid[0]["task_fit"], 0.0)

    def test_task_requirements_for_malformed_input_fails_closed(self):
        from launcher.admission import task_requirements_for
        self.assertIsNone(task_requirements_for(None))
        self.assertIsNone(task_requirements_for("string"))
        self.assertIsNone(task_requirements_for([]))

        for malformed in (123, "antigravity", ["a"], True):
            reqs = task_requirements_for({"model_requirements": malformed})
            self.assertIsNotNone(reqs)
            valid, _ = validate_quse(self.data, task_requirements=reqs)
            self.assertEqual(len(valid), 1)
            self.assertEqual(valid[0]["task_fit"], 0.0)


class TestNativeClaudeAdmission(unittest.TestCase):
    def setUp(self):
        self.future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.data = {"claude": route(
            {"5h": {"percent_remaining": 80.0, "reset_at": self.future},
             "7d": {"percent_remaining": 90.0, "reset_at": self.future}},
            details={"source": "native_claude_oauth_usage", "managed": True},
        )}
        self.managed_reqs = {
            "provider": "claude",
            "is_managed": True,
            "account_label": "my-claude",
            "account_profile": "hub-my-claude",
            "managed_dir": "/tmp/managed",
        }

    def test_unmanaged_claude_rejected(self):
        valid, rejections = validate_quse(self.data, task_requirements={"provider": "claude"})
        self.assertEqual(len(valid), 0)
        self.assertIn("Claude route requires managed profile", rejections.get("claude", ""))

    def test_managed_claude_without_explicit_provider_rejected(self):
        reqs = dict(self.managed_reqs)
        del reqs["provider"]
        valid, rejections = validate_quse(self.data, task_requirements=reqs)
        self.assertEqual(len(valid), 0)
        self.assertIn("Claude route requires explicit provider request", rejections.get("claude", ""))

    def test_managed_claude_admitted_with_default_model(self):
        valid, _ = validate_quse(self.data, task_requirements=self.managed_reqs)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["provider"], "claude")
        self.assertEqual(valid[0]["model"], "claude-sonnet-4-6")
        self.assertEqual(valid[0]["task_fit"], 1.0)

    def test_managed_claude_supported_model_override(self):
        for model in ("claude-sonnet-5-5", "claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5", "opus"):
            reqs = dict(self.managed_reqs)
            reqs["model"] = model
            valid, _ = validate_quse(self.data, task_requirements=reqs)
            self.assertEqual(len(valid), 1)
            self.assertEqual(valid[0]["provider"], "claude")
            self.assertEqual(valid[0]["model"], model)
            self.assertEqual(valid[0]["task_fit"], 1.0)

    def test_hub_fleet_launch_model_requirements_shape_and_extraction(self):
        from launcher.admission import extract_requested_model
        # Exact Hub fleet_launch.py payload shape:
        hub_payload = {
            "account_label": "claude-prod",
            "account_profile": "hub-claude-prod",
            "managed_dir": "/managed",
            "model_requirements": {
                "models": ["claude-sonnet-4-6"],
                "providers": ["claude"],
            }
        }
        extracted = extract_requested_model(hub_payload, provider="claude")
        self.assertEqual(extracted, "claude-sonnet-4-6")

        # Ambiguous multiple models for same provider fails closed:
        ambiguous_payload = {
            "model_requirements": {
                "models": ["claude-sonnet-4-6", "claude-opus-4-8"],
                "providers": ["claude"],
            }
        }
        self.assertIsNone(extract_requested_model(ambiguous_payload, provider="claude"))

        # Multi-provider models list resolves to provider-specific match:
        cross_payload = {
            "model_requirements": {
                "models": ["gemini-2.5-pro", "claude-sonnet-4-6"],
                "providers": ["claude"],
            }
        }
        self.assertEqual(extract_requested_model(cross_payload, provider="claude"), "claude-sonnet-4-6")


class TestManagedProfileQualification(unittest.TestCase):
    def setUp(self):
        self.future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.data = {
            "gemini": route({"5h": {"percent_remaining": 80.0, "reset_at": self.future}}),
            "codex": {
                "status": "ok",
                "windows": {
                    "primary": {"percent_remaining": 70.0, "reset_at": self.future},
                    "weekly": {"percent_remaining": 70.0, "reset_at": self.future},
                },
            },
        }

    def test_forged_flag_does_not_manufacture_managed_authority(self):
        from launcher.admission import task_requirements_for
        payload = {"model_requirements": {"is_managed": True, "provider": "codex"}}
        reqs = task_requirements_for(payload)
        self.assertNotIn("is_managed", reqs)
        # Without valid managed authority or wrapper, codex route is rejected
        valid, rejections = validate_quse(self.data, task_requirements=reqs)
        codex_routes = [r for r in valid if r["provider"] == "codex"]
        self.assertEqual(len(codex_routes), 0)

    def test_missing_account_label_rejected(self):
        from launcher.admission import task_requirements_for
        payload = {"profile": "hub-alpha", "model_requirements": {"provider": "codex"}}
        reqs = task_requirements_for(payload)
        self.assertIn("managed_rejected", reqs)
        valid, _ = validate_quse(self.data, task_requirements=reqs)
        self.assertEqual(len(valid), 0)

    def test_label_profile_mismatch_rejected(self):
        from launcher.admission import task_requirements_for
        payload = {
            "account_label": "alpha",
            "account_profile": "hub-beta",
            "model_requirements": {"provider": "codex"},
        }
        reqs = task_requirements_for(payload)
        self.assertIn("managed_rejected", reqs)
        valid, _ = validate_quse(self.data, task_requirements=reqs)
        self.assertEqual(len(valid), 0)

    def test_removed_or_stale_bundle_rejected(self):
        import tempfile, json
        from pathlib import Path
        from launcher.admission import task_requirements_for
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            # state.json without matching account or bundle missing
            (tmp_p / "state.json").write_text(json.dumps([{"label": "other", "revision": "rev1"}]))
            payload = {
                "account_label": "alpha",
                "account_profile": "hub-alpha",
                "managed_dir": str(tmp_p),
                "model_requirements": {"provider": "codex"},
            }
            reqs = task_requirements_for(payload)
            self.assertIn("managed_rejected", reqs)
            valid, _ = validate_quse(self.data, task_requirements=reqs)
            self.assertEqual(len(valid), 0)

    def test_legacy_shared_gemini_unchanged(self):
        from launcher.admission import task_requirements_for
        payload = {"profile": "shared-gemini", "model_requirements": {"provider": "gemini"}}
        reqs = task_requirements_for(payload)
        self.assertNotIn("is_managed", reqs or {})
        self.assertNotIn("managed_rejected", reqs or {})
        valid, _ = validate_quse(self.data, task_requirements=reqs)
        gemini_routes = [r for r in valid if r["provider"] == "gemini"]
        self.assertEqual(len(gemini_routes), 1)
        self.assertEqual(gemini_routes[0]["task_fit"], 1.0)

    def test_valid_managed_profile_qualified(self):
        import tempfile, json
        from pathlib import Path
        from launcher.admission import task_requirements_for
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            (tmp_p / "state.json").write_text(json.dumps([{"label": "alpha", "revision": "rev1", "provider": "codex"}]))
            bundle_dir = tmp_p / "alpha-rev1"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(json.dumps({"label": "alpha", "revision": "rev1", "provider": "codex"}))

            payload = {
                "account_label": "alpha",
                "account_profile": "hub-alpha",
                "managed_dir": str(tmp_p),
                "model_requirements": {"provider": "codex"},
            }
            reqs = task_requirements_for(payload)
            self.assertTrue(reqs.get("is_managed"))
            self.assertEqual(reqs.get("account_label"), "alpha")
            self.assertEqual(reqs.get("account_profile"), "hub-alpha")

            valid, _ = validate_quse(self.data, task_requirements=reqs)
            codex_routes = [r for r in valid if r["provider"] == "codex"]
            self.assertEqual(len(codex_routes), 1)

    def test_managed_claude_requires_trusted_source(self):
        import tempfile, json
        from pathlib import Path
        from launcher.admission import task_requirements_for, validate_quse
        now = datetime.now(timezone.utc)
        fut = (now + timedelta(days=2)).isoformat()

        reqs = {
            "is_managed": True,
            "account_label": "claude-managed",
            "account_profile": "hub-claude-managed",
            "managed_dir": "/nonexistent",
            "provider": "claude",
        }

        # Untrusted source rejected
        untrusted_data = {
            "claude": {
                "status": "ok",
                "details": {"source": "ambient_quse", "managed": True},
                "windows": {"5h": {"percent_remaining": 80.0, "reset_at": fut}},
            }
        }
        valid, rejections = validate_quse(untrusted_data, task_requirements=reqs)
        self.assertEqual(len(valid), 0)
        self.assertIn("Untrusted or missing Claude quota source", rejections.get("claude", ""))

        # Trusted source native_claude_oauth_usage accepted
        trusted_data = {
            "claude": {
                "status": "ok",
                "details": {"source": "native_claude_oauth_usage", "managed": True},
                "windows": {"5h": {"percent_remaining": 80.0, "reset_at": fut}},
            }
        }
        valid2, _ = validate_quse(trusted_data, task_requirements=reqs)
        self.assertEqual(len(valid2), 1)
        self.assertEqual(valid2[0]["provider"], "claude")

    def test_gemini_failure_never_fabricates_100_percent(self):
        import tempfile, json
        from pathlib import Path
        from launcher.admission import fetch_managed_quse
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            (tmp_p / "state.json").write_text(json.dumps([{"label": "g-fail", "revision": "rev1", "provider": "gemini"}]))
            bundle_dir = tmp_p / "g-fail-rev1"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(json.dumps({"label": "g-fail", "revision": "rev1", "provider": "gemini"}))

            res = fetch_managed_quse("g-fail", provider="gemini", managed_dir=tmp_p)
            self.assertIn("gemini", res)
            # The optional native Hub probe may report unknown when no
            # OAuth evidence exists; an unavailable adapter reports held.
            # Both must remain fail-closed with no fabricated quota window.
            self.assertIn(res["gemini"]["status"], ("held", "unknown"))
            self.assertEqual(res["gemini"]["windows"], {})

    def test_claude_model_task_profile_resolves_implementation_and_matches_provider_task_kinds(self):
        import tempfile, json
        from pathlib import Path
        from launcher.admission import task_requirements_for, validate_quse
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            (tmp_p / "state.json").write_text(json.dumps([{"label": "my-claude", "revision": "r1", "provider": "claude"}]))
            b_dir = tmp_p / "my-claude-r1"
            b_dir.mkdir()
            (b_dir / "bundle.json").write_text(json.dumps({
                "label": "my-claude",
                "revision": "r1",
                "provider": "claude",
                "args": ["--model", "claude-sonnet-4-6"],
            }))

            payload = {
                "account_label": "my-claude",
                "account_profile": "hub-my-claude",
                "managed_dir": str(tmp_p),
                "profile": "model-task",
                "model_requirements": {
                    "models": ["claude-sonnet-4-6"],
                    "providers": ["claude"],
                },
            }
            reqs = task_requirements_for(payload)
            self.assertEqual(reqs.get("kind"), "implementation")
            self.assertIn("claude", reqs.get("providers", []))
            self.assertTrue(reqs.get("is_managed"))

            now = datetime.now(timezone.utc)
            fut = (now + timedelta(days=2)).isoformat()
            quse_data = {
                "claude": {
                    "status": "ok",
                    "details": {
                        "source": "native_claude_oauth_usage",
                        "managed": True,
                        "model": "claude-sonnet-4-6",
                    },
                    "windows": {
                        "5h": {"percent_remaining": 100.0, "reset_at": None, "inactive": True},
                        "7d": {"percent_remaining": 80.0, "reset_at": fut},
                    },
                }
            }
            valid, rejections = validate_quse(quse_data, task_requirements=reqs)
            self.assertEqual(len(valid), 1)
            self.assertEqual(valid[0]["provider"], "claude")
            self.assertEqual(valid[0]["model"], "claude-sonnet-4-6")
            self.assertEqual(valid[0]["task_fit"], 1.0)

    def test_hub_fleet_launch_plan_full_admission(self):
        import tempfile, json
        from pathlib import Path
        from launcher.cli import plan
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            (tmp_p / "state.json").write_text(json.dumps([{"label": "prod-c", "revision": "r1", "provider": "claude"}]))
            b_dir = tmp_p / "prod-c-r1"
            b_dir.mkdir()
            (b_dir / "bundle.json").write_text(json.dumps({
                "label": "prod-c",
                "revision": "r1",
                "provider": "claude",
                "args": ["--model", "claude-sonnet-4-6"],
                "env": {"CLAUDE_CODE_OAUTH_TOKEN": "token-test"},
            }))

            now = datetime.now(timezone.utc)
            fut = (now + timedelta(days=3)).isoformat()
            fake_quse = {
                "claude": {
                    "status": "ok",
                    "details": {
                        "source": "native_claude_oauth_usage",
                        "managed": True,
                        "model": "claude-sonnet-4-6",
                    },
                    "windows": {
                        "5h": {"percent_remaining": 100.0, "reset_at": None, "inactive": True},
                        "7d": {"percent_remaining": 85.0, "reset_at": fut},
                    },
                }
            }

            payload = {
                "account_label": "prod-c",
                "account_profile": "hub-prod-c",
                "managed_dir": str(tmp_p),
                "profile": "model-task",
                "model_requirements": {
                    "models": ["claude-sonnet-4-6"],
                    "providers": ["claude"],
                },
            }

            class FakeArgs:
                def __init__(self, p, cdir):
                    self.id = None
                    self.payload = p
                    self.config_dir = cdir

            with patch("launcher.admission.fetch_quse", return_value=fake_quse), \
                 patch("sys.stdout") as mock_stdout:
                rc = plan(FakeArgs(json.dumps(payload), str(tmp_p)))
                self.assertEqual(rc, 0)






class HubHostPathProbeTests(unittest.TestCase):
    """Hub intake 798f0f8c: the native probe is importable only via AGENT_HUB_HOST_PATH."""

    def test_managed_claude_probe_imports_from_trusted_host_path(self):
        import json, os, sys, tempfile
        from pathlib import Path
        from launcher.admission import fetch_managed_quse
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            pkg = tmp_p / "host" / "agent_hub_host"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "native_claude_quota.py").write_text(
                "def probe(bundle, dest, requested_model=None):\n"
                "    return {'claude': {'status': 'ok', 'details': {'source': 'native_claude_oauth_usage', 'model': requested_model}, 'windows': {}}}\n")
            managed = tmp_p / "managed"
            (managed / "c-rev1").mkdir(parents=True)
            (managed / "state.json").write_text(json.dumps([{"label": "c", "revision": "rev1", "provider": "claude"}]))
            (managed / "c-rev1" / "bundle.json").write_text(json.dumps({"provider": "claude"}))
            saved_path = list(sys.path)
            saved_mods = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "agent_hub_host" or k.startswith("agent_hub_host.")}
            try:
                with patch.dict(os.environ, {"AGENT_HUB_HOST_PATH": str(tmp_p / "host")}):
                    res = fetch_managed_quse("c", provider="claude", requested_model="opus", managed_dir=managed)
            finally:
                sys.path[:] = saved_path
                for k in [k for k in sys.modules if k == "agent_hub_host" or k.startswith("agent_hub_host.")]:
                    sys.modules.pop(k)
                sys.modules.update(saved_mods)
            self.assertEqual(res["claude"]["status"], "ok")
            self.assertEqual(res["claude"]["details"]["source"], "native_claude_oauth_usage")
            self.assertEqual(res["claude"]["details"]["model"], "opus")
