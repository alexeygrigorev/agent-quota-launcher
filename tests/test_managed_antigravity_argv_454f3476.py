"""Managed Antigravity headless argv (Hub intake 454f3476).

After cf593b1 fixed managed Antigravity admission, real Hub Antigravity
launches on DESKTOP-FP4OP26 and LAPTOP-PHA6TCNC (launcher a8dddab, host
ff295a5) still exited 1 before native bind: build_managed_profile_argv had
no antigravity branch and raised "Provider 'antigravity' is not supported for
managed headless execution". The managed argv now mirrors the unmanaged
antigravity adapter's non-interactive contract through Hub's execute wrapper.
"""
import unittest
from unittest.mock import patch

from launcher.admission import ADAPTER_MODELS
from launcher.launch import ADAPTERS, build_managed_profile_argv


class ManagedAntigravityArgvTest(unittest.TestCase):
    def build(self, model):
        with patch("launcher.launch.resolve_hub_path", return_value="/trusted/agent-hub/host"):
            return build_managed_profile_argv("antigravity", "do work", "hub-antigravity", model,
                                              managed_dir="/managed")

    def test_runs_through_hub_execute_wrapper_with_requested_model(self):
        argv = self.build("gemini-4-argon-high")
        self.assertIn("from agent_hub_host.provisioning import execute; execute()", argv[2])
        self.assertIn("AGENT_HUB_MANAGED_DIR", argv[2])
        self.assertEqual(argv[3], "hub-antigravity")
        rest = argv[4:]
        self.assertEqual(rest[rest.index("--model") + 1], "gemini-4-argon-high")
        self.assertEqual(rest.count("--model"), 1)
        self.assertEqual(rest[-2:], ["-p", "do work"])

    def test_matches_unmanaged_adapter_non_interactive_contract(self):
        rest = self.build("gemini-4-argon-high")[4:]
        unmanaged = ADAPTERS["antigravity"]["argv"]
        for flag in ("--dangerously-skip-permissions", "--print-timeout", "--output-format", "--effort"):
            self.assertIn(flag, rest)
            self.assertEqual(rest[rest.index(flag) + 1:rest.index(flag) + 2],
                             unmanaged[unmanaged.index(flag) + 1:unmanaged.index(flag) + 2])
        # No API-key environment or raw agy path is embedded in managed argv.
        self.assertFalse(any("GEMINI_API_KEY" in a or "GOOGLE_API_KEY" in a or a.endswith("/agy") for a in rest))

    def test_default_model_is_the_adapter_default(self):
        rest = self.build(None)[4:]
        self.assertEqual(rest[rest.index("--model") + 1], ADAPTER_MODELS["antigravity"])

    def test_unknown_provider_still_refused(self):
        with patch("launcher.launch.resolve_hub_path", return_value="/trusted/agent-hub/host"):
            with self.assertRaises(ValueError):
                build_managed_profile_argv("grok", "do work", "hub-grok", None)


if __name__ == "__main__":
    unittest.main()
