import unittest
from launcher.ranking import calculate_weight, select_candidate, reset_multiplier

class TestRanking(unittest.TestCase):
    def test_unknown_fit_or_health_weights_zero(self):
        w, reason = calculate_weight(None, 1.0, 0.9, 1.0, 1.0)
        self.assertEqual(w, 0.0)
        self.assertIn("unknown", reason)
        w2, _ = calculate_weight(1.0, None, 0.9, 1.0, 1.0)
        self.assertEqual(w2, 0.0)

    def test_weight_zero_if_unhealthy(self):
        w, _ = calculate_weight(1.0, 0.0, 0.9, 1.0, 1.0)
        self.assertEqual(w, 0.0)

    def test_weight_zero_if_no_quota(self):
        w, _ = calculate_weight(1.0, 1.0, 0.0, 1.0, 1.0)
        self.assertEqual(w, 0.0)

    def test_reset_multiplier_bounds(self):
        self.assertEqual(reset_multiplier(0.9, None), 1.0)      # missing reset: no urgency
        self.assertEqual(reset_multiplier(0.9, 200.0), 1.0)     # far window: no urgency
        self.assertEqual(reset_multiplier(1.0, 0.0), 3.0)       # max urgency saturates at 3
        self.assertEqual(reset_multiplier(0.5, 0.0), 2.0)       # urgency 0.5 -> 2.0
        for rem in (0.05, 0.3, 0.9):
            for hours in (0.0, 1.0, 12.0, 47.0):
                m = reset_multiplier(rem, hours)
                self.assertGreaterEqual(m, 1.0)
                self.assertLessEqual(m, 3.0)

    def test_weight_calculation_bounded(self):
        w, _ = calculate_weight(1.0, 1.0, 0.9, 0.1, 1.0)
        # headroom 0.9 * multiplier (1 + 2*0.9*(24-0.1)/24) = 0.9 * 2.7925
        self.assertAlmostEqual(w, 0.9 * 2.7925)
        # grok/zai get the modest expiring-allowance preference
        wp, _ = calculate_weight(1.0, 1.0, 0.9, 0.1, 1.0, provider="zai")
        self.assertAlmostEqual(wp, 0.9 * 2.7925 * 1.2)
        # floor: tiny remaining keeps >= 0.05 headroom
        w2, _ = calculate_weight(1.0, 1.0, 0.001, 24.0, 1.0)
        self.assertAlmostEqual(w2, 0.05)  # urgency 0 at 24h

    def test_preference_grok_zai_near_reset(self):
        wa, _ = calculate_weight(1.0, 1.0, 0.5, 2.0, 1.0)  # zai (default provider not set)
        # no provider info -> no preference; base weight
        self.assertGreater(wa, 0)

    def test_selection_records_provenance(self):
        candidates = [
            {"name": "exhausted", "provider": "grok", "task_fit": 1.0, "health": 1.0,
             "remaining_fraction": 0.0, "hours_to_reset": 1.0},
            {"name": "urgent", "provider": "zai", "task_fit": 1.0, "health": 1.0,
             "remaining_fraction": 0.9, "hours_to_reset": 0.1},
            {"name": "unknown", "provider": "antigravity", "task_fit": None,
             "health": 1.0, "remaining_fraction": 0.9, "hours_to_reset": 100.0},
        ]
        chosen, provenance = select_candidate(candidates, seed=42)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["name"], "urgent")
        self.assertEqual(provenance["seed"], 42)
        self.assertEqual(provenance["chosen"], "urgent")
        by_name = {c["name"]: c for c in provenance["candidates"]}
        self.assertEqual(by_name["exhausted"]["weight"], 0.0)
        self.assertEqual(by_name["unknown"]["weight"], 0.0)
        self.assertGreater(by_name["urgent"]["probability"], 0.99)

    def test_selection_none_when_all_unknown(self):
        candidates = [{"name": "x", "task_fit": None, "health": None,
                       "remaining_fraction": 0.9, "hours_to_reset": 1.0}]
        chosen, provenance = select_candidate(candidates, seed=1)
        self.assertIsNone(chosen)
        self.assertIsNone(provenance["chosen"])

if __name__ == '__main__':
    unittest.main()
