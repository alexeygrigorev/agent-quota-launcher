"""Actual-provider gates; no model, service or network is launched."""
import copy
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch

from launcher.provider_admission import (
    CommandIdentity, GO_MODEL, QuotaAcquisition, ACCOUNT_ENV_KEYS, fetch_task_unit_quota,
    identify_command, task_unit_environment, validate_acquisition, validate_command_environment, validate_provider_quota,
    DEFAULT_UNSET_ENV_KEYS, QUOTA_SOURCE_ENV_KEYS, QUOTA_SOURCE_POLICY,
    _collect_bounded_output,
    QUOTA_LIFECYCLE_HOLD, QUOTA_LIFECYCLE_PREREQUISITE, QuotaLifecycleUnavailable,
    require_quota_lifecycle,
)

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def quota(provider="claude", percent=45):
    names = {"claude": ("5h", "7d"), "opencode": ("5h", "7d", "monthly"),
             "codex": ("7d",), "grok": ("custom-hourly",)}[provider]
    route = {"status": "ok", "error": None, "details": {"limit_reached": False},
             "windows": {n: {"percent_remaining": percent,
                              "reset_at": (NOW + timedelta(days=2)).isoformat()}
                         for n in names}}
    if provider == "claude":
        route["details"]["observed_at"] = NOW.isoformat()
        route["windows"]["monthly"] = {"percent_remaining": None, "reset_at": None}
    if provider == "codex":
        for n in ("5h", "monthly"):
            route["windows"][n] = {"percent_remaining": None, "reset_at": None}
    if provider == "grok":
        route["details"]["has_grok_code_access"] = True
    return {"go" if provider == "opencode" else provider: route}


def model_command(provider):
    if provider == "claude":
        return ["/usr/bin/claude", "-p", "--model", "sonnet", "prompt"]
    if provider == "opencode":
        return ["/usr/bin/opencode", "run", "--standalone", "--auto", "--model", GO_MODEL, "prompt"]
    if provider == "codex":
        return ["/usr/bin/codex", "exec", "prompt"]
    from launcher.launch import ADAPTERS
    return list(ADAPTERS[provider]["argv"]) + ["prompt"]


def acquired(packet, provider="claude", command_argv=None):
    """Synthetic fixture for DATA validation only; never production acquisition.

    Deliberate reflection creates legacy shape for quota/account negatives.
    Production fetch is held, and this fixture cannot authorize model launch.
    """
    command = tuple(command_argv or model_command(provider))
    with patch("launcher.provider_admission.shutil.which", side_effect=lambda name, **kw: "/usr/bin/" + name):
        identity = identify_command(command)
    receipt = object.__new__(QuotaAcquisition)
    fields = dict(packet_json=json.dumps(packet), command_argv=command, identity=identity,
                  started_at=NOW, completed_at=NOW, duration_sec=0,
                  account_environment=tuple(sorted(task_unit_environment().items())),
                  quse_executable="/usr/bin/quse",
                  model_executable="/usr/bin/" + ("opencode" if provider == "opencode" else
                                                   "claude" if provider == "claude" else
                                                   "codex" if provider == "codex" else command[0].split("/")[-1]),
                  quota_source_environment=QUOTA_SOURCE_POLICY)
    # Maintained adapters may resolve through the system's actual path.
    from launcher.provider_admission import _model_executable
    fields["model_executable"] = _model_executable(command)
    for key, value in fields.items(): object.__setattr__(receipt, key, value)
    return receipt


class CommandBindingTests(unittest.TestCase):
    def setUp(self):
        self.which = patch("launcher.provider_admission.shutil.which",
                           side_effect=lambda name, **kwargs: "/usr/bin/" + name)
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_direct_claude_and_identity_unsets(self):
        for model in ("sonnet", "opus"):
            cmd = ["/usr/bin/env", "-u", "APLEXER_SESSION_ID", "-u", "APLEXER_TAG",
                   "-u", "APLEXER_WORKSPACE", "-u", "CLAUDE_SESSION_ID",
                   "/usr/bin/claude", "-p", "--model", model, "--effort", "high",
                   "--dangerously-skip-permissions", "--output-format", "stream-json",
                   "--verbose", "Review this"]
            self.assertEqual(identify_command(cmd), CommandIdentity("claude", model))
            self.assertEqual(identify_command(cmd, "claude", model).model, model)

    def test_go_explicit_route_only(self):
        cmd = ["/usr/bin/opencode", "run", "--standalone", "--auto", "--model", GO_MODEL,
               "--format", "json", "--dir", "/work", "--title", "task", "prompt"]
        for provider in (None, "go", "opencode", "opencode-go"):
            self.assertEqual(identify_command(cmd, provider), CommandIdentity("opencode", GO_MODEL))

    def test_provider_model_mismatch_and_unknown_models(self):
        claude = ["/usr/bin/claude", "-p", "--model", "sonnet", "prompt"]
        for provider, model in (("zai", None), ("opencode", None), ("claude", "opus"),
                                (True, None), ("", None)):
            with self.subTest(provider=provider, model=model), self.assertRaises(ValueError):
                identify_command(claude, provider, model)
        bad = [
            ["/usr/bin/claude", "-p", "prompt --model sonnet"],
            ["/usr/bin/claude", "-p", "--model", "haiku", "prompt"],
            ["/usr/bin/claude", "-p", "--model", "sonnet", "--model", "opus", "prompt"],
            ["/usr/bin/claude", "--model", "sonnet", "prompt"],
            ["/usr/bin/claude", "-p", "--model", "sonnet", "--fallback-model", "opus", "prompt"],
            ["/usr/bin/claude", "-p", "--model", "sonnet", "--settings", "rogue.json", "prompt"],
            ["/usr/bin/opencode", "run", "--standalone", "--auto", "--model", "zai/glm-5.3-flash", "prompt"],
            ["/usr/bin/opencode", "run", "--auto", "--model", GO_MODEL, "prompt"],
            ["/usr/bin/opencode", "run", "--pure", "--auto", "--model", GO_MODEL, "prompt"],
            ["/usr/bin/opencode", "run", "--standalone", "--auto", "--model", GO_MODEL,
             "--model", GO_MODEL, "prompt"],
        ]
        for cmd in bad:
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                identify_command(cmd)

    def test_prompt_and_option_values_are_not_provider_evidence(self):
        cmd = ["/usr/bin/claude", "-p", "--model", "sonnet", "--allowedTools",
               "--model opus"]
        with self.assertRaises(ValueError):
            identify_command(cmd)
        cmd = ["/usr/bin/opencode", "run", "--standalone", "--auto", "--title", "--model",
               GO_MODEL, "prompt"]
        with self.assertRaises(ValueError):
            identify_command(cmd)
        self.assertEqual(identify_command(["echo", "claude --model opus"]).provider, None)

    def test_opaque_wrappers_assignments_and_lookalikes_denied(self):
        bad = [
            ["sh", "-c", "claude -p --model sonnet prompt"],
            ["/bin/sh", "/work/launch-claude.sh"],
            ["python3", "launch.py"], ["node", "-e", "launch()"],
            ["env", "claude", "-p", "--model", "sonnet"],
            ["/usr/bin/env", "ANTHROPIC_BASE_URL=https://invalid", "/usr/bin/claude", "-p", "--model", "sonnet"],
            ["/usr/bin/env", "-u", "PATH", "/usr/bin/claude", "-p", "--model", "sonnet"],
            ["timeout", "60", "claude", "-p", "--model", "sonnet"],
            ["find", ".", "-exec", "claude", ";"],
            ["/tmp/claude", "-p", "--model", "sonnet"],
            ["/work/worker.py"], [], "claude -p --model sonnet", ["claude", None],
        ]
        for cmd in bad:
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                identify_command(cmd)
        for cmd in (["echo", "ok"], ["true"], ["false"], ["sleep", "0.1"]):
            self.assertIsNone(identify_command(cmd).provider)
        with self.assertRaises(ValueError):
            identify_command(["sleep", "1", "claude"])
        with self.assertRaises(ValueError):
            identify_command(["echo", "ok"], "claude")

    def test_subscription_route_backend_and_account_overrides_denied(self):
        identity = CommandIdentity("claude", "sonnet")
        keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
        for key in keys:
            with patch.dict("launcher.provider_admission.os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    validate_command_environment(identity, {key: "override"})
            with patch.dict("launcher.provider_admission.os.environ", {key: "override"}, clear=True):
                with self.assertRaises(ValueError):
                    validate_command_environment(identity)
        with patch.dict("launcher.provider_admission.os.environ", {}, clear=True):
            validate_command_environment(identity, {"TASK_ID": "task"})
            with self.assertRaises(ValueError):
                validate_command_environment(identity, {"CLAUDE_CONFIG_DIR": "/unverified"})
            for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR", "OPENCODE_GO_API_KEY"):
                with self.assertRaises(ValueError):
                    validate_command_environment(CommandIdentity("opencode", GO_MODEL), {key: "override"})

    def test_maintained_adapter_requires_exact_template_and_model(self):
        from launcher.launch import ADAPTERS
        for provider in ("grok", "zai", "antigravity"):
            cmd = list(ADAPTERS[provider]["argv"])
            if provider == "antigravity":
                cmd[cmd.index("--model") + 1] = "gemini-3.8-flash-high"
            self.assertEqual(identify_command(cmd + ["prompt"]).provider, provider)
            with self.assertRaises(ValueError):
                identify_command(cmd + ["--model", "rogue", "prompt"])


class ActualQuotaTests(unittest.TestCase):
    def setUp(self):
        which = patch("launcher.provider_admission.shutil.which",
                      side_effect=lambda name, **kwargs: "/usr/bin/" + name)
        which.start()
        self.addCleanup(which.stop)

    def identity(self, provider):
        return CommandIdentity(provider, {"claude": "sonnet", "opencode": GO_MODEL, "grok": "grok-4.6"}.get(provider))

    def validate(self, data, provider="claude", **kwargs):
        return validate_provider_quota(acquired(data, provider), self.identity(provider), now=NOW, **kwargs)

    def test_actual_claude_and_go_pass_exhausted_unrelated_routes(self):
        for provider in ("claude", "opencode"):
            data = quota(provider)
            data.update({"gemini": {"status": "ok", "details": {"limit_reached": True}},
                         "zai": {"status": "ok", "details": {"limit_reached": True}},
                         "grok": quota("grok", 5)["grok"]})
            self.assertEqual(self.validate(data, provider).provider, provider)

    def test_healthy_unrelated_route_never_rescues_bad_actual(self):
        for provider in ("claude", "opencode"):
            key = "go" if provider == "opencode" else provider
            data = quota(provider, 0)
            data["zai"] = {"status": "ok", "windows": {"7d": {
                "percent_remaining": 99, "reset_at": "2099-01-01T00:00:00Z"}}}
            variants = [data, {"zai": data["zai"]}]
            for broken in (None, {}, "route", {"status": "unknown"}):
                d = copy.deepcopy(data); d[key] = broken; variants.append(d)
            for packet in variants:
                with self.subTest(provider=provider, packet=packet), self.assertRaises(ValueError):
                    self.validate(packet, provider)

    def test_all_applicable_windows_fail_closed_on_invalid_readings(self):
        for provider in ("claude", "opencode", "codex", "grok"):
            for value in (None, True, False, float("nan"), float("inf"), -1, 101, "45"):
                data = quota(provider); key = "go" if provider == "opencode" else provider
                win = next(w for w in data[key]["windows"].values() if w["percent_remaining"] is not None)
                win["percent_remaining"] = value
                with self.subTest(provider=provider, value=value), self.assertRaisesRegex(ValueError, "reading"):
                    self.validate(data, provider)

    def test_required_missing_unknown_stale_and_timezone(self):
        for provider in ("claude", "opencode", "codex", "grok"):
            for reset in (None, "bogus", "2026-10-16T00:00:00", NOW.isoformat(),
                          (NOW - timedelta(seconds=1)).isoformat()):
                data = quota(provider); key = "go" if provider == "opencode" else provider
                win = next(w for w in data[key]["windows"].values() if w["percent_remaining"] is not None)
                win["reset_at"] = reset
                with self.subTest(provider=provider, reset=reset), self.assertRaisesRegex(ValueError, "timezone/reset|stale"):
                    self.validate(data, provider)
        for provider in ("claude", "opencode", "codex"):
            data = quota(provider); key = "go" if provider == "opencode" else provider
            del data[key]["windows"]["7d"]
            with self.assertRaisesRegex(ValueError, "required"):
                self.validate(data, provider)

    def test_route_details_window_errors_limits_stale_and_infinite(self):
        for provider in ("claude", "opencode", "codex", "grok"):
            for level in ("route", "details", "window"):
                for flag in ("error", "stale", "limit_reached", "infinite"):
                    data = quota(provider); key = "go" if provider == "opencode" else provider
                    record = data[key]
                    if level == "details": record = record["details"]
                    if level == "window": record = record["windows"][next(iter(record["windows"]))]
                    record[flag] = "unknown" if flag == "error" else True
                    record["present"] = False
                    with self.subTest(provider=provider, level=level, flag=flag), self.assertRaisesRegex(ValueError, flag):
                        self.validate(data, provider)
        for field, value in (("status", "stale"), ("status", None), ("details", None)):
            data = quota(); data["claude"][field] = value
            with self.assertRaises(ValueError): self.validate(data)

    def test_codex_absent_optional_is_not_unknown_but_execution_stays_held(self):
        data = quota("codex", 64)
        with self.assertRaisesRegex(ValueError, "protected wrapper"):
            self.validate(data, "codex")
        for optional in ("5h", "monthly"):
            data = quota("codex", 64)
            data["codex"]["windows"][optional]["present"] = False
            with self.assertRaisesRegex(ValueError, "protected wrapper"):
                self.validate(data, "codex")
            data["codex"]["windows"][optional]["error"] = "unknown"
            with self.assertRaisesRegex(ValueError, "error"):
                self.validate(data, "codex")
            data = quota("codex", 64)
            data["codex"]["windows"][optional]["reset_at"] = "2099-01-01T00:00:00Z"
            with self.assertRaisesRegex(ValueError, "reading"):
                self.validate(data, "codex")

    def test_codex_and_grok_cutoffs(self):
        for provider, cutoff in (("codex", 15), ("grok", 5)):
            for value in (0, cutoff - .01, cutoff):
                with self.assertRaisesRegex(ValueError, f"<= {cutoff}%"):
                    self.validate(quota(provider, value), provider)
        with self.assertRaisesRegex(ValueError, "protected wrapper"):
            self.validate(quota("codex", 15.01), "codex")
        self.validate(quota("grok", 5.01), "grok")

    def test_data_named_extra_windows_and_required_aliases(self):
        data = quota(); windows = data["claude"]["windows"]
        windows["five_hour"] = windows.pop("5h"); windows["seven_day"] = windows.pop("7d")
        self.validate(data)
        windows["regional-extra-week"] = {"percent_remaining": None, "reset_at": None}
        with self.assertRaisesRegex(ValueError, "reading"): self.validate(data)
        data = quota("codex"); data["codex"]["windows"]["weekly"] = data["codex"]["windows"].pop("7d")
        with self.assertRaisesRegex(ValueError, "protected wrapper"): self.validate(data, "codex")

    def test_present_false_cannot_hide_an_applicable_window(self):
        for provider in ("claude", "opencode", "codex"):
            data = quota(provider); key = "go" if provider == "opencode" else provider
            data[key]["windows"]["7d"]["present"] = False
            with self.assertRaisesRegex(ValueError, "absent"): self.validate(data, provider)

    def test_missing_details_unknown_limit_and_present_true_null_denied(self):
        for details in (None, {}, {"limit_reached": None}, {"limit_reached": 0}):
            data = quota(); data["claude"]["details"] = details
            with self.assertRaises(ValueError): self.validate(data)
        data = quota("codex")
        data["codex"]["windows"]["5h"]["present"] = True
        with self.assertRaisesRegex(ValueError, "reading"): self.validate(data, "codex")

    def test_observation_age_unknown_and_naive_fail_closed(self):
        for observed in (None, "bad", "2026-10-15T00:00:00", (NOW - timedelta(seconds=301)).isoformat(),
                         (NOW + timedelta(seconds=1)).isoformat()):
            data = quota(); data["claude"]["details"]["observed_at"] = observed
            with self.assertRaisesRegex(ValueError, "observation"): self.validate(data)

    def test_capacity_checks_actual_provider_even_with_healthy_unrelated(self):
        for provider in ("claude", "opencode", "grok"):
            with patch("launcher.capacity.check_provider_capacity", return_value=(False, "capacity full", {})) as check:
                with self.assertRaisesRegex(ValueError, "capacity"):
                    self.validate(quota(provider), provider, check_capacity=True, config_dir="/owned/config")
                check.assert_called_once_with(provider, config_dir="/owned/config")


class AcquisitionBindingTests(unittest.TestCase):
    def setUp(self):
        which = patch("launcher.provider_admission.shutil.which",
                      side_effect=lambda name, **kwargs: "/usr/bin/" + name)
        which.start(); self.addCleanup(which.stop)

    def test_missing_acquisition_raw_observed_packet_and_stamped_envelope_deny(self):
        for provider in ("claude", "opencode", "codex", "grok"):
            data = quota(provider)
            identity = identify_command(model_command(provider))
            for stamped in (data, {"packet": data, "acquired_at": NOW.isoformat()},
                            {"routes": data, "observed_at": NOW.isoformat()}):
                with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "acquisition receipt"):
                    validate_provider_quota(stamped, identity, now=NOW)
        with self.assertRaises(TypeError):
            QuotaAcquisition(packet_json=json.dumps(quota()), completed_at=NOW)

    def test_model_fetch_is_held_even_if_collector_is_patched_successful(self):
        for provider in ("claude", "opencode", "codex", "grok"):
            with self.subTest(provider=provider), patch("launcher.provider_admission._collect_bounded_output") as collect:
                collect.return_value = subprocess.CompletedProcess([], 0, json.dumps(quota(provider)), "")
                with self.assertRaisesRegex(QuotaLifecycleUnavailable, QUOTA_LIFECYCLE_HOLD):
                    fetch_task_unit_quota(model_command(provider))
                collect.assert_not_called()


    def test_recent_receipt_does_not_invent_missing_server_observation(self):
        data = quota(); data["claude"]["details"].pop("observed_at")
        receipt = acquired(data)
        self.assertNotIn("observed_at", json.loads(receipt.packet_json)["claude"]["details"])
        validate_provider_quota(receipt, receipt.identity, now=NOW)
        for now in (NOW + timedelta(seconds=301), NOW - timedelta(seconds=1), NOW.replace(tzinfo=None)):
            with self.subTest(now=now), self.assertRaises(ValueError):
                validate_provider_quota(receipt, receipt.identity, now=now)

    def test_invalid_timeout_denied_without_acquisition(self):
        for timeout in (True, 0, -1, 121, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), patch("launcher.provider_admission._collect_bounded_output") as collect:
                with self.assertRaises(ValueError):
                    fetch_task_unit_quota(model_command("claude"), timeout_sec=timeout)
                collect.assert_not_called()


    def test_home_config_and_executable_drift_after_acquisition_denied(self):
        for provider in ("claude", "opencode"):
            receipt = acquired(quota(provider), provider)
            for env in ({"HOME": "/tmp/unverified-account"}, {"XDG_CONFIG_HOME": "/tmp/config"},
                        {"CLAUDE_CONFIG_DIR": "/tmp/config"}, {"OPENCODE_GO_API_KEY": "unverified"}):
                with patch.dict(os.environ, env), self.assertRaisesRegex(ValueError, "HOME|account/backend"):
                    validate_acquisition(receipt, receipt.identity, now=NOW)
            with self.assertRaisesRegex(ValueError, "override"):
                validate_acquisition(receipt, receipt.identity, now=NOW, extra_env={"HOME": task_unit_environment()["HOME"]})
            changed = model_command(provider); changed[-1] = "different task"
            with self.assertRaisesRegex(ValueError, "argv mismatch"):
                validate_acquisition(receipt, receipt.identity, now=NOW, command_argv=changed)
            with patch("launcher.provider_admission.shutil.which", return_value="/tmp/other-cli"):
                with self.assertRaises(ValueError): validate_acquisition(receipt, receipt.identity, now=NOW)

    def test_bare_model_name_never_accepted_even_with_controller_path_selection(self):
        for provider, name in (("claude", "claude"), ("opencode", "opencode"), ("codex", "codex")):
            cmd = model_command(provider); cmd[0] = name
            with patch.dict(os.environ, {"PATH": "/tmp/controller-only"}), patch(
                "launcher.provider_admission.shutil.which", return_value="/tmp/controller-only/" + name
            ), self.assertRaises(ValueError): identify_command(cmd)

    def test_acquisition_packet_is_immutable_and_env_unsets_preserved(self):
        for provider in ("claude", "opencode"):
            packet = quota(provider)
            key = "claude" if provider == "claude" else "go"
            packet[key]["details"].pop("observed_at", None)
            command = ["/usr/bin/env", "-u", "APLEXER_SESSION_ID", "-u", "CLAUDE_SESSION_ID"] + model_command(provider)
            receipt = acquired(packet, provider, command)
            digest = receipt.packet_sha256
            packet[key]["windows"]["7d"]["percent_remaining"] = 0
            self.assertEqual(receipt.packet_sha256, digest)
            self.assertEqual(receipt.model_executable, "/usr/bin/" + ("claude" if provider == "claude" else "opencode"))
            validate_provider_quota(receipt, receipt.identity, now=NOW, command_argv=command)
            with self.assertRaises(AttributeError): receipt.completed_at = NOW + timedelta(days=1)

    def test_home_drift_blocks_acquisition_before_provider_call(self):
        for provider in ("claude", "opencode"):
            with patch.dict(os.environ, {"HOME": "/tmp/unverified-account"}), patch(
                "launcher.provider_admission._collect_bounded_output"
            ) as run:
                with self.assertRaisesRegex(ValueError, "HOME"):
                    fetch_task_unit_quota(model_command(provider))
                run.assert_not_called()


class QuotaSourceSelectorTests(unittest.TestCase):
    def setUp(self):
        which = patch("launcher.provider_admission.shutil.which", side_effect=lambda name, **kw: "/usr/bin/" + name)
        which.start(); self.addCleanup(which.stop)

    def test_all_installed_source_selectors_and_unrecognized_family_deny(self):
        selectors = QUOTA_SOURCE_ENV_KEYS | {"QUSE_FUTURE_SOURCE", "GROK_FUTURE_SOURCE", "AGY_FUTURE_SOURCE"}
        for provider in ("claude", "opencode"):
            receipt = acquired(quota(provider), provider)
            for key in selectors:
                with self.subTest(provider=provider, key=key), patch.dict(os.environ, {key: "/tmp/unverified-source"}), patch(
                    "launcher.provider_admission._collect_bounded_output"
                ) as collect:
                    with self.assertRaisesRegex(ValueError, "configuration override"):
                        fetch_task_unit_quota(model_command(provider))
                    with self.assertRaisesRegex(ValueError, "configuration override"):
                        validate_acquisition(receipt, receipt.identity, now=NOW)
                    collect.assert_not_called()
                with self.assertRaisesRegex(ValueError, "override"):
                    fetch_task_unit_quota(model_command(provider), extra_env={key: "override"})
        with patch.dict(os.environ, {"QUSE_CACHE_DIR": ""}), self.assertRaises(ValueError):
            fetch_task_unit_quota(model_command("claude"))

    def test_default_selector_context_recorded_without_environment_values(self):
        receipt = acquired(quota())
        self.assertEqual(receipt.quota_source_environment, QUOTA_SOURCE_POLICY)
        self.assertTrue(QUOTA_SOURCE_ENV_KEYS <= {key for key, value in receipt.quota_source_environment})
        self.assertTrue(all(value is None for key, value in receipt.quota_source_environment))
        with patch("launcher.provider_admission.QUOTA_SOURCE_POLICY", QUOTA_SOURCE_POLICY + (("future", None),)):
            with self.assertRaisesRegex(ValueError, "source/default policy changed"):
                validate_acquisition(receipt, receipt.identity, now=NOW)


class LifecycleUnavailableTests(unittest.TestCase):
    """Source/mocked-only proof of default deny; no OS lifecycle probes."""

    def test_private_collector_never_starts_process_or_scans_or_signals(self):
        with patch("subprocess.Popen") as start, patch("os.scandir") as scan, patch("os.killpg") as group:
            with self.assertRaisesRegex(QuotaLifecycleUnavailable, QUOTA_LIFECYCLE_HOLD):
                _collect_bounded_output(["/usr/bin/quse", "claude", "--json"],
                                        env={}, cwd="/tmp", timeout_sec=120)
            start.assert_not_called(); scan.assert_not_called(); group.assert_not_called()

    def test_platform_features_and_old_receipt_do_not_grant_lifecycle(self):
        receipt = acquired(quota())
        self.assertIsInstance(receipt, QuotaAcquisition)
        with patch("os.waitid") as wait, patch("os.pidfd_open") as open_fd:
            with self.assertRaisesRegex(QuotaLifecycleUnavailable, "kernel-enforced private process cohort"):
                require_quota_lifecycle()
            wait.assert_not_called(); open_fd.assert_not_called()

    def test_no_caller_enable_boolean_or_backend_label(self):
        for kwargs in ({"qualified": True}, {"backend": "private-cgroup"}, {"atomic": True}):
            with self.assertRaises(TypeError): require_quota_lifecycle(**kwargs)
        self.assertIn("non-escapable", QUOTA_LIFECYCLE_PREREQUISITE)

    def test_data_quota_eligibility_is_not_lifecycle_authority(self):
        with patch("launcher.provider_admission.shutil.which", side_effect=lambda name, **kw: "/usr/bin/" + name):
            for provider in ("claude", "opencode"):
                receipt = acquired(quota(provider), provider)
                self.assertEqual(validate_provider_quota(receipt, receipt.identity, now=NOW), receipt.identity)
                with self.assertRaises(QuotaLifecycleUnavailable): require_quota_lifecycle()


if __name__ == "__main__":
    unittest.main()
