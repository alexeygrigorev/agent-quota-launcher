import unittest
from launcher.ranking import calculate_weight, select_candidate

class TestRanking(unittest.TestCase):
    def test_weight_zero_if_unhealthy(self):
        w = calculate_weight(task_fit=1.0, health=0.0, remaining_fraction=1.0, hours_to_reset=1.0, promo_multiplier=1.0)
        self.assertEqual(w, 0.0)

    def test_weight_zero_if_no_quota(self):
        w = calculate_weight(task_fit=1.0, health=1.0, remaining_fraction=0.0, hours_to_reset=1.0, promo_multiplier=1.0)
        self.assertEqual(w, 0.0)

    def test_weight_calculation(self):
        # High remaining, low hours -> gets saturated bonus
        w1 = calculate_weight(task_fit=1.0, health=1.0, remaining_fraction=0.9, hours_to_reset=0.1, promo_multiplier=1.0)
        # Bonus = 1 + min(5.0, 0.9/0.1) = 1 + min(5.0, 9.0) = 6.0
        # Weight = 1.0 * 1.0 * 0.9 * 6.0 * 1.0 = 5.4
        self.assertAlmostEqual(w1, 5.4)

    def test_selection(self):
        candidates = [
            {"id": "c1", "task_fit": 1.0, "health": 1.0, "remaining_fraction": 0.0, "hours_to_reset": 1.0}, # exhausted
            {"id": "c2", "task_fit": 1.0, "health": 1.0, "remaining_fraction": 0.9, "hours_to_reset": 0.1}, # bonus
            {"id": "c3", "task_fit": 1.0, "health": 1.0, "remaining_fraction": 0.9, "hours_to_reset": 100}, # no bonus
        ]
        
        chosen, eligible, weights = select_candidate(candidates, seed=42)
        self.assertIsNotNone(chosen)
        self.assertEqual(len(eligible), 2)
        # c2 should have higher weight than c3
        self.assertGreater(weights[0], weights[1])

if __name__ == '__main__':
    unittest.main()
