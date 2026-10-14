import unittest
from launcher.launch import build_adapter_argv


class GlmRoutingTest(unittest.TestCase):
    def test_glm_refused_off_zai(self):
        for prov in ("grok", "antigravity", "opencode"):
            with self.assertRaises(ValueError):
                build_adapter_argv(prov, "x", model="zai-coding-plan/glm-5.2")

    def test_non_glm_model_allowed(self):
        argv = build_adapter_argv("grok", "x", model="grok-4.6")
        self.assertIn("grok-4.6", argv)


if __name__ == "__main__":
    unittest.main()
