"""Tests for root-run profile admission and pre-action receiver guard."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from launcher.task_profiles import get_profile, resolve_task_bounds
from launcher.root_profile import (
    ROOT_PROFILE_NAME,
    ROOT_MAX_TIMEOUT_SEC,
    ROOT_MAX_MEMORY_MB,
    UNSUPPORTED_RECEIVER_REASON,
    RootJobSpec,
    RootAdmissionError,
    RootExecutionDeniedError,
    RootSecretLeakError,
    ProductionRootReceiver,
    IsolatedTestReceiver,
    check_root_pre_action,
    generate_root_dependency_proof,
    prepare_root_environment_file,
    get_active_receiver,
    set_active_receiver,
)


class TestRootProfileAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        set_active_receiver(None)

    def test_root_profile_registered_in_task_profiles(self):
        prof = get_profile(ROOT_PROFILE_NAME)
        self.assertEqual(prof["timeout"], ROOT_MAX_TIMEOUT_SEC)
        self.assertEqual(prof["memory_mb"], ROOT_MAX_MEMORY_MB)
        self.assertIn("Bounded root-run execution profile", prof["description"])

    def test_resolve_task_bounds_root_run_explicit(self):
        payload = {"profile": "root-run"}
        bounds = resolve_task_bounds(payload)
        self.assertEqual(bounds["profile"], "root-run")
        self.assertEqual(bounds["timeout"], 1500.0)
        self.assertEqual(bounds["memory_mb"], 1500)

    def test_resolve_task_bounds_root_run_clamping(self):
        payload = {"profile": "root-run", "timeout": 3600.0, "memory_mb": 2048}
        bounds = resolve_task_bounds(payload)
        self.assertEqual(bounds["profile"], "root-run")
        self.assertEqual(bounds["timeout"], 1500.0)  # clamped to 1500s ceiling
        self.assertEqual(bounds["memory_mb"], 1500)  # clamped to 1500MiB ceiling

    def test_resolve_task_bounds_root_run_autodetect(self):
        payload = {"id": "task-root-run-repair-59", "command": "python3 repair.py"}
        bounds = resolve_task_bounds(payload)
        self.assertEqual(bounds["profile"], "root-run")
        self.assertEqual(bounds["timeout"], 1500.0)

    def test_root_job_spec_valid(self):
        payload = {
            "id": "t-root-1",
            "host": "test-host",
            "session_id": "sess-uuid-1",
            "run_id": "run-tag-1",
            "lease_epoch": 42,
            "workspace": str(self.ws),
            "timeout": 1200,
            "memory_mb": 1024,
            "private_env": {"API_TOKEN": "secret-xyz-12345"},
            "command": ["python3", "worker.py", "--mode", "offline"],
        }
        spec = RootJobSpec.from_payload(payload)
        self.assertEqual(spec.job_id, "t-root-1")
        self.assertEqual(spec.host, "test-host")
        self.assertEqual(spec.session_id, "sess-uuid-1")
        self.assertEqual(spec.run_id, "run-tag-1")
        self.assertEqual(spec.lease_epoch, 42)
        self.assertEqual(spec.timeout_sec, 1200.0)
        self.assertEqual(spec.memory_mb, 1024)
        self.assertEqual(spec.private_env, {"API_TOKEN": "secret-xyz-12345"})

    def test_root_job_spec_missing_tokens_fail_closed(self):
        base = {
            "id": "t-root-1",
            "host": "test-host",
            "session_id": "sess-uuid-1",
            "run_id": "run-tag-1",
            "lease_epoch": 1,
            "workspace": str(self.ws),
        }
        # Missing job_id
        with self.assertRaises(RootAdmissionError):
            RootJobSpec.from_payload(dict(base, id=""))
        # Missing host
        with self.assertRaises(RootAdmissionError):
            RootJobSpec.from_payload(dict(base, host=""))
        # Missing session_id
        with self.assertRaises(RootAdmissionError):
            RootJobSpec.from_payload(dict(base, session_id=""))
        # Missing run_id
        with self.assertRaises(RootAdmissionError):
            p = dict(base)
            del p["run_id"]
            p["tag"] = ""
            p["id"] = ""
            RootJobSpec.from_payload(p)

    def test_root_job_spec_secret_in_argv_rejected(self):
        payload = {
            "id": "t-root-secret",
            "host": "test-host",
            "session_id": "sess-uuid-1",
            "run_id": "run-tag-1",
            "lease_epoch": 1,
            "workspace": str(self.ws),
            "private_env": {"MY_SECRET": "top-secret-password-xyz"},
            "command": ["python3", "run.py", "--key", "top-secret-password-xyz"],
        }
        with self.assertRaises(RootSecretLeakError):
            RootJobSpec.from_payload(payload)

    def test_root_job_spec_generic_secret_flags_rejected(self):
        payload = {
            "id": "t-root-secret-flag",
            "host": "test-host",
            "session_id": "sess-uuid-1",
            "run_id": "run-tag-1",
            "lease_epoch": 1,
            "workspace": str(self.ws),
            "command": ["python3", "run.py", "--password=supersecret"],
        }
        with self.assertRaises(RootSecretLeakError):
            RootJobSpec.from_payload(payload)

    def test_production_receiver_unqualified_fails_closed(self):
        receiver = ProductionRootReceiver()
        self.assertFalse(receiver.is_operationally_qualified())
        self.assertIn("No independently qualified trusted receiver", receiver.unsupported_reason())

        spec = RootJobSpec.from_payload({
            "id": "t-prod-1",
            "host": "host-1",
            "session_id": "sess-1",
            "run_id": "run-1",
            "lease_epoch": 10,
            "workspace": str(self.ws),
        })
        valid, reason, details = receiver.validate_pre_action(spec)
        self.assertFalse(valid)
        self.assertFalse(details["qualified"])

        with self.assertRaises(RootExecutionDeniedError) as cm:
            check_root_pre_action(spec, receiver=receiver)
        self.assertIn("Root execution denied", str(cm.exception))
        self.assertIn("No independently qualified trusted receiver", str(cm.exception))

    def test_isolated_test_receiver_cannot_enable_operational_support(self):
        fixture = IsolatedTestReceiver(
            expected_job_id="t-fix-1",
            expected_host="fix-host",
            expected_session_id="sess-fix",
            expected_run_id="run-fix",
            expected_epoch=99,
        )
        # Permamently unqualified operationally
        self.assertFalse(fixture.is_operationally_qualified())

        spec = RootJobSpec.from_payload({
            "id": "t-fix-1",
            "host": "fix-host",
            "session_id": "sess-fix",
            "run_id": "run-fix",
            "lease_epoch": 99,
            "workspace": str(self.ws),
        })

        # When allow_test_fixture is False (default): fails closed
        with self.assertRaises(RootExecutionDeniedError) as cm:
            check_root_pre_action(spec, receiver=fixture, allow_test_fixture=False)
        self.assertIn("cannot enable operational root execution", str(cm.exception))

    def test_isolated_test_receiver_offline_verification(self):
        fixture = IsolatedTestReceiver(
            expected_job_id="t-fix-1",
            expected_host="fix-host",
            expected_session_id="sess-fix",
            expected_run_id="run-fix",
            expected_epoch=99,
        )
        spec_match = RootJobSpec.from_payload({
            "id": "t-fix-1",
            "host": "fix-host",
            "session_id": "sess-fix",
            "run_id": "run-fix",
            "lease_epoch": 99,
            "workspace": str(self.ws),
        })
        # Explicit test fixture pass
        res = check_root_pre_action(spec_match, receiver=fixture, allow_test_fixture=True)
        self.assertFalse(res["denied"])
        self.assertTrue(res["test_fixture"])
        self.assertFalse(res["operational_root_qualified"])

        # Mismatch in epoch fails
        spec_mismatch = RootJobSpec.from_payload({
            "id": "t-fix-1",
            "host": "fix-host",
            "session_id": "sess-fix",
            "run_id": "run-fix",
            "lease_epoch": 100,  # mismatch
            "workspace": str(self.ws),
        })
        with self.assertRaises(RootExecutionDeniedError) as cm:
            check_root_pre_action(spec_mismatch, receiver=fixture, allow_test_fixture=True)
        self.assertIn("Token mismatch", str(cm.exception))

    def test_prepare_root_environment_file_mode_0600(self):
        spec = RootJobSpec.from_payload({
            "id": "t-env-test",
            "host": "host-1",
            "session_id": "sess-1",
            "run_id": "run-1",
            "lease_epoch": 1,
            "workspace": str(self.ws),
            "private_env": {
                "SECRET_KEY": "supersecretkey123",
                "DB_PASS": "passwordsafe456",
            },
        })
        env_file = prepare_root_environment_file(spec, self.ws, "unit-test")
        self.assertTrue(env_file.exists())
        mode = stat.S_IMODE(env_file.stat().st_mode)
        self.assertEqual(mode, 0o600)

        content = env_file.read_text()
        self.assertIn("SECRET_KEY=supersecretkey123", content)
        self.assertIn("DB_PASS=passwordsafe456", content)

    def test_generate_root_dependency_proof(self):
        spec = RootJobSpec.from_payload({
            "id": "t-proof-1",
            "host": "host-1",
            "session_id": "sess-1",
            "run_id": "run-1",
            "lease_epoch": 5,
            "workspace": str(self.ws),
            "private_env": {"AUTH": "val"},
        })
        proof = generate_root_dependency_proof(spec)
        self.assertEqual(proof["profile"], "root-run")
        self.assertTrue(proof["admitted"])
        self.assertTrue(proof["preclaim_hold"])
        self.assertFalse(proof["operational_root_qualified"])
        self.assertIn(UNSUPPORTED_RECEIVER_REASON, proof["unsupported_reason"])
        self.assertEqual(proof["immutable_tokens"]["job_id"], "t-proof-1")
        self.assertEqual(proof["transport"]["mode"], "0600")


class TestRootProfileArgonNegativeRepairs(unittest.TestCase):
    """Reproduces and verifies fixes for F1, F2, F4, F5 in root_profile."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        set_active_receiver(None)

    def tearDown(self):
        set_active_receiver(None)
        self.tmp.cleanup()

    def test_f1_duck_typed_and_protocol_forgeries_rejected(self):
        spec = RootJobSpec.from_payload({
            "id": "root-f1-1",
            "host": "hetzner-1",
            "session_id": "sess-1",
            "run_id": "run-1",
            "lease_epoch": 1,
            "workspace": str(self.ws),
        })
        from types import SimpleNamespace
        forged = SimpleNamespace(
            is_test_fixture=False,
            is_operationally_qualified=lambda: True,
            validate_pre_action=lambda s: (True, "forged", {"forged": True}),
            unsupported_reason=lambda: "none",
        )
        # Production execution (allow_test_fixture=False) MUST unconditionally deny
        with self.assertRaises(RootExecutionDeniedError):
            check_root_pre_action(spec, receiver=forged, allow_test_fixture=False)

        # Even with allow_test_fixture=True, non-IsolatedTestReceiver is rejected
        with self.assertRaises(RootExecutionDeniedError):
            check_root_pre_action(spec, receiver=forged, allow_test_fixture=True)

    def test_f1_monkeypatched_receiver_cannot_enable_operational_root(self):
        spec = RootJobSpec.from_payload({
            "id": "root-f1-2",
            "host": "hetzner-1",
            "session_id": "sess-1",
            "run_id": "run-1",
            "lease_epoch": 1,
            "workspace": str(self.ws),
        })
        fixture = IsolatedTestReceiver("root-f1-2", "hetzner-1", "sess-1", "run-1", 1)
        fixture.is_operationally_qualified = lambda: True  # type: ignore[method-assign]
        with self.assertRaises(RootExecutionDeniedError):
            check_root_pre_action(spec, receiver=fixture, allow_test_fixture=False)

        res = check_root_pre_action(spec, receiver=fixture, allow_test_fixture=True)
        self.assertFalse(res["denied"])
        self.assertFalse(res["operational_root_qualified"])
        self.assertTrue(res["test_fixture"])

    def test_f2_omitted_host_run_id_lease_epoch_rejected(self):
        # Omitted host
        with self.assertRaises(RootAdmissionError) as cm:
            RootJobSpec.from_payload({
                "id": "job-omit-1",
                "session_id": "sess-1",
                "run_id": "run-1",
                "lease_epoch": 1,
                "workspace": str(self.ws),
            })
        self.assertIn("host must be provided", str(cm.exception))

        # Omitted run_id
        with self.assertRaises(RootAdmissionError) as cm:
            RootJobSpec.from_payload({
                "id": "job-omit-2",
                "host": "h1",
                "session_id": "sess-1",
                "lease_epoch": 1,
                "workspace": str(self.ws),
            })
        self.assertIn("run_id must be provided", str(cm.exception))

        # Omitted lease_epoch
        with self.assertRaises(RootAdmissionError) as cm:
            RootJobSpec.from_payload({
                "id": "job-omit-3",
                "host": "h1",
                "session_id": "sess-1",
                "run_id": "run-1",
                "workspace": str(self.ws),
            })
        self.assertIn("lease_epoch must be provided", str(cm.exception))

    def test_f2_out_of_bounds_timeout_and_memory_rejected_without_clamping(self):
        # Excessive timeout not silently clamped
        with self.assertRaises(RootAdmissionError) as cm:
            RootJobSpec.from_payload({
                "id": "job-clamp-1",
                "host": "h1",
                "session_id": "s1",
                "run_id": "r1",
                "lease_epoch": 1,
                "workspace": str(self.ws),
                "timeout": 99999,
            })
        self.assertIn("outside bounded range", str(cm.exception))

        # Excessive memory not silently clamped
        with self.assertRaises(RootAdmissionError) as cm:
            RootJobSpec.from_payload({
                "id": "job-clamp-2",
                "host": "h1",
                "session_id": "s1",
                "run_id": "r1",
                "lease_epoch": 1,
                "workspace": str(self.ws),
                "memory_mb": 99999,
            })
        self.assertIn("outside bounded range", str(cm.exception))

    def test_f2_invalid_lease_epochs_rejected(self):
        for invalid_epoch in (0, -5, False, True, "not-an-int"):
            with self.assertRaises(RootAdmissionError):
                RootJobSpec.from_payload({
                    "id": "job-ep",
                    "host": "h1",
                    "session_id": "s1",
                    "run_id": "r1",
                    "lease_epoch": invalid_epoch,
                    "workspace": str(self.ws),
                })

    def test_f2_isolated_test_receiver_token_validations(self):
        # Empty expectations rejected
        with self.assertRaises(ValueError):
            IsolatedTestReceiver()

        fixture = IsolatedTestReceiver("job-tok", "h1", "s1", "r1", 5)

        # Expired / revoked rejected
        expired_spec = RootJobSpec.from_payload({
            "id": "job-tok",
            "host": "h1",
            "session_id": "s1",
            "run_id": "r1",
            "lease_epoch": 5,
            "workspace": str(self.ws),
            "expires_at": "2020-01-01T00:00:00Z",
            "expired": True,
        })
        with self.assertRaises(RootExecutionDeniedError):
            check_root_pre_action(expired_spec, receiver=fixture, allow_test_fixture=True)

        revoked_spec = RootJobSpec.from_payload({
            "id": "job-tok",
            "host": "h1",
            "session_id": "s1",
            "run_id": "r1",
            "lease_epoch": 5,
            "workspace": str(self.ws),
            "revoked": True,
            "revoked_at": "2026-10-13T00:00:00Z",
        })
        with self.assertRaises(RootExecutionDeniedError):
            check_root_pre_action(revoked_spec, receiver=fixture, allow_test_fixture=True)

        # Stale epoch rejected
        stale_spec = RootJobSpec.from_payload({
            "id": "job-tok",
            "host": "h1",
            "session_id": "s1",
            "run_id": "r1",
            "lease_epoch": 4,  # < 5
            "workspace": str(self.ws),
        })
        with self.assertRaises(RootExecutionDeniedError):
            check_root_pre_action(stale_spec, receiver=fixture, allow_test_fixture=True)

        # Valid spec accepted on first check, but replayed check is rejected
        valid_spec = RootJobSpec.from_payload({
            "id": "job-tok",
            "host": "h1",
            "session_id": "s1",
            "run_id": "r1",
            "lease_epoch": 5,
            "workspace": str(self.ws),
        })
        res1 = check_root_pre_action(valid_spec, receiver=fixture, allow_test_fixture=True)
        self.assertFalse(res1["denied"])
        self.assertFalse(res1["operational_root_qualified"])

        with self.assertRaises(RootExecutionDeniedError) as cm_rep:
            check_root_pre_action(valid_spec, receiver=fixture, allow_test_fixture=True)
        self.assertIn("Token replay detected", str(cm_rep.exception))

    def test_f4_space_separated_credential_flags_and_hub_tokens_rejected(self):
        with self.assertRaises(RootSecretLeakError) as cm:
            RootJobSpec.from_payload({
                "id": "t-space-leak",
                "host": "h1",
                "session_id": "s1",
                "run_id": "r1",
                "lease_epoch": 1,
                "workspace": str(self.ws),
                "command": ["python3", "worker.py", "--token", "hubt_supersecret999"],
            })
        self.assertIn("<redacted>", str(cm.exception))
        self.assertNotIn("hubt_supersecret999", str(cm.exception))

    def test_f4_secret_in_goal_or_prompt_rejected(self):
        with self.assertRaises(RootSecretLeakError) as cm:
            RootJobSpec.from_payload({
                "id": "t-goal-leak",
                "host": "h1",
                "session_id": "s1",
                "run_id": "r1",
                "lease_epoch": 1,
                "workspace": str(self.ws),
                "private_env": {"HUB_JOB_TOKEN": "hubt_secret_value_abc"},
                "goal": "Run with token hubt_secret_value_abc",
            })
        self.assertIn("Secret value for 'HUB_JOB_TOKEN' detected in task goal/prompt", str(cm.exception))

    def test_f4_error_message_redacts_credential_secrets(self):
        with self.assertRaises(RootSecretLeakError) as cm:
            RootJobSpec.from_payload({
                "id": "t-err-leak",
                "host": "h1",
                "session_id": "s1",
                "run_id": "r1",
                "lease_epoch": 1,
                "workspace": str(self.ws),
                "command": ["python3", "--token=mysecret1234"],
            })
        self.assertIn("<redacted>", str(cm.exception))
        self.assertNotIn("mysecret1234", str(cm.exception))

    def test_f5_unsupported_provider_codex_rejected(self):
        with self.assertRaises(RootAdmissionError) as cm:
            RootJobSpec.from_payload({
                "id": "t-codex-root",
                "host": "h1",
                "session_id": "s1",
                "run_id": "r1",
                "lease_epoch": 1,
                "workspace": str(self.ws),
                "provider": "codex",
            })
        self.assertIn("Provider 'codex' is unsupported for root-run profile", str(cm.exception))

    def test_f5_dependency_proof_safely_handles_invalid_payloads_and_codex(self):
        # Invalid payload does not crash generate_root_dependency_proof
        proof_invalid = generate_root_dependency_proof({"id": "t-inv", "provider": "codex"})
        self.assertFalse(proof_invalid["admitted"])
        self.assertTrue(proof_invalid["preclaim_hold"])
        self.assertIn("codex", proof_invalid["unsupported_reason"])

    def test_f5_root_run_profile_task_fit_and_admission(self):
        import datetime
        from launcher.admission import (
            PROFILE_TASK_KINDS,
            PROVIDER_TASK_KINDS,
            task_requirements_for,
            validate_quse,
        )
        from launcher.ranking import select_candidate

        # 1. Profile mapping and catalog
        self.assertIn("root-run", PROFILE_TASK_KINDS)
        self.assertEqual(PROFILE_TASK_KINDS["root-run"], "root-run")
        for prov in ("grok", "antigravity", "zai"):
            self.assertIn("root-run", PROVIDER_TASK_KINDS.get(prov, ()))

        # 2. task_requirements_for implies kind "root-run"
        reqs = task_requirements_for({"profile": "root-run"})
        self.assertEqual(reqs, {"kind": "root-run"})

        # Preserves explicit model requirements while injecting kind
        reqs_with_prov = task_requirements_for({
            "profile": "root-run",
            "model_requirements": {"providers": ["zai"]},
        })
        self.assertEqual(reqs_with_prov, {"providers": ["zai"], "kind": "root-run"})

        # 3. validate_quse and candidate selection
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)).isoformat()
        quse_data = {
            "zai": {"status": "ok", "windows": {"7d": {"percent_remaining": 80.0, "reset_at": future}}},
        }
        valid_routes, rejections = validate_quse(quse_data, task_requirements=reqs)
        self.assertEqual(len(valid_routes), 1)
        self.assertEqual(valid_routes[0]["provider"], "zai")
        self.assertEqual(valid_routes[0]["task_fit"], 1.0)

        chosen, _ = select_candidate(valid_routes, seed=0)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.get("provider"), "zai")

        # 4. Codex provider remains unselectable / rejected
        quse_codex = {
            "codex": {
                "status": "ok",
                "windows": {
                    "7d": {"percent_remaining": 80.0, "reset_at": future},
                },
            },
        }
        valid_codex, codex_rejections = validate_quse(quse_codex, task_requirements=reqs)
        self.assertEqual(len(valid_codex), 0)
        self.assertIn("codex", codex_rejections)


if __name__ == "__main__":
    unittest.main()

