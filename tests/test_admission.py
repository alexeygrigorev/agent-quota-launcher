import unittest
from datetime import datetime, timezone, timedelta
from launcher.admission import validate_quse

class TestAdmission(unittest.TestCase):
    def test_validate_quse(self):
        now = datetime.now(timezone.utc)
        future1 = (now + timedelta(days=1)).isoformat()
        future2 = (now + timedelta(days=2)).isoformat()
        past = (now - timedelta(days=1)).isoformat()
        
        data = {
            "grok": {
                "status": "ok",
                "details": {"has_grok_code_access": True},
                "windows": {"7d": {"percent_remaining": 50, "reset_at": future1}}
            }
        }
        valid, rejections = validate_quse(data)
        self.assertIn("grok", [v["name"] for v in valid])
        
        # limit reached
        data2 = {
            "grok": {
                "status": "ok",
                "details": {"limit_reached": True, "has_grok_code_access": True},
                "windows": {"7d": {"percent_remaining": 0, "reset_at": future1}}
            }
        }
        valid2, rej2 = validate_quse(data2)
        self.assertEqual(len(valid2), 0)
        self.assertIn("Limit reached", rej2.get("grok", ""))
        
        # stale evidence
        data3 = {
            "grok": {
                "status": "ok",
                "details": {"has_grok_code_access": True},
                "windows": {"7d": {"percent_remaining": 50, "reset_at": past}}
            }
        }
        valid3, rej3 = validate_quse(data3)
        self.assertEqual(len(valid3), 0)
        self.assertIn("Stale route evidence", rej3.get("grok", ""))

        # missing reset_at
        data4 = {
            "grok": {
                "status": "ok",
                "details": {"has_grok_code_access": True},
                "windows": {"7d": {"percent_remaining": 50}}
            }
        }
        valid4, rej4 = validate_quse(data4)
        self.assertEqual(len(valid4), 0)
        self.assertIn("Missing valid route evidence", rej4.get("grok", ""))
        
        # nonnumeric
        data5 = {
            "grok": {
                "status": "ok",
                "details": {"has_grok_code_access": True},
                "windows": {"7d": {"percent_remaining": "50%", "reset_at": future1}}
            }
        }
        valid5, rej5 = validate_quse(data5)
        self.assertEqual(len(valid5), 0)
        self.assertIn("Missing valid route evidence", rej5.get("grok", ""))

if __name__ == '__main__':
    unittest.main()
