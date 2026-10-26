"""CPU V2 choking-only admission (Hub umbrella 4fcd3139, USER t1511u).

Heavy utilization is fine (busy 90%+ admits). Only fresh actual choking
refuses: PSI CPU FULL > cap (cpu_pressure), PSI memory FULL > cap
(memory_pressure), fresh OOM delta > 0 (host_oom), fresh trusted host
runtime evidence (host_runtime_choking/host_oom). Loadavg, busy%,
predicted load, and unknown telemetry NEVER refuse.
"""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from launcher import cpu_resources as cpu
from launcher.cpu_resources import (
    check_cpu_choking,
    cpu_verdict,
    format_cpu_refusal,
    oom_delta,
    ramping_cpu_cores,
    resolve_expected_cpu,
    sanitize_cpu_admission,
    sanitize_cpu_usage,
    validate_resource_choking,
)
from launcher.resource_policy import RESOURCE_DEFAULTS, load_resource_policy
from launcher.resources import check_resources

GiB = 1024 * 1024 * 1024
MiB = 1024 * 1024
LOCAL_TMP_DIR = Path(__file__).resolve().parent.parent / ".local" / "tmp"


def now_iso():
    return datetime.fromtimestamp(time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def usage(cores=12, busy_percent=100.0, psi_some=99.0, psi_full=0.0,
          mem_full=0.0, oom=0, interval=0.2):
    busy_cores = busy_percent * cores / 100
    return {"busy_percent": busy_percent, "busy_cores": busy_cores,
            "cores": cores, "psi_avg10": psi_some,
            "psi_full_avg10": psi_full,
            "memory_psi_full_avg10": mem_full,
            "oom_kills_delta": oom, "observed_at": now_iso(),
            "interval_s": interval}


def policy(**over):
    base = dict(RESOURCE_DEFAULTS)
    base.update(over)
    return base


class ChokingGateTests(unittest.TestCase):
    def test_full_busy_heavy_expected_high_some_admits(self):
        # 100% busy, heavy expected, SOME 99, but FULL 0 => admit.
        u = usage(cores=12, busy_percent=100.0, psi_some=99.0, psi_full=0.0)
        allowed, reason, admission = check_cpu_choking(
            cpu_usage=u, policy=policy(), cores=12,
            expected_cpu_cores=8.0, ramping_cpu_cores=4.0)
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        self.assertIsNotNone(admission)

    def test_high_loadavg_iowait_idle_admits(self):
        # Loadavg is not even an input; idle busy with high SOME admits.
        u = usage(cores=12, busy_percent=2.0, psi_some=80.0, psi_full=0.0)
        allowed, reason, _ = check_cpu_choking(
            cpu_usage=u, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_unknown_telemetry_admits_with_advisory(self):
        allowed, reason, admission = check_cpu_choking(
            cpu_usage=None, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0)
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        # Unknown stays null, never fake-cleared.
        self.assertIsNone(admission["current_cpu_cores"])
        malformed = {"busy_percent": "hot", "cores": 12,
                     "observed_at": now_iso(), "interval_s": 0.2}
        allowed2, reason2, _ = check_cpu_choking(
            cpu_usage=malformed, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0)
        self.assertTrue(allowed2)
        self.assertIsNone(reason2)

    def test_predicted_over_target_is_advisory_only(self):
        # Busy 12 + expected 8 + ramping 4 >> limit 12: still admits.
        u = usage(cores=12, busy_percent=100.0, psi_full=0.0, mem_full=0.0)
        verdict = cpu_verdict(u, 12, 8.0, 4.0, policy())
        self.assertTrue(verdict["predicted_over_target"])
        self.assertTrue(verdict["admitted"])
        self.assertIsNone(verdict["reason"])

    def test_full_cpu_refuses_cpu_pressure(self):
        u = usage(cores=12, busy_percent=100.0, psi_full=6.0)
        allowed, reason, admission = check_cpu_choking(
            cpu_usage=u, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "cpu_pressure")
        text = format_cpu_refusal(reason, admission)
        slug, payload = text.split("\n", 1)
        self.assertEqual(slug, "cpu_pressure")
        self.assertTrue(payload.startswith("CPU_ADMISSION_JSON "))
        self.assertEqual(admission["psi_full_avg10"], 6.0)

    def test_full_memory_refuses_memory_pressure(self):
        u = usage(cores=12, busy_percent=10.0, psi_full=0.0, mem_full=2.5)
        allowed, reason, admission = check_cpu_choking(
            cpu_usage=u, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "memory_pressure")
        self.assertEqual(admission["memory_psi_full_avg10"], 2.5)

    def test_oom_delta_refuses_host_oom(self):
        u = usage(cores=12, busy_percent=10.0, oom=2)
        allowed, reason, admission = check_cpu_choking(
            cpu_usage=u, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "host_oom")
        self.assertEqual(admission["oom_kills_delta"], 2)

    def test_trusted_runtime_hint_refuses(self):
        hint = {"reason": "host_runtime_choking", "observed_at": now_iso()}
        allowed, reason, admission = check_cpu_choking(
            cpu_usage=None, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0,
            trusted_choking=hint)
        self.assertFalse(allowed)
        self.assertEqual(reason, "host_runtime_choking")

    def test_stale_hint_ignored(self):
        old = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat().replace("+00:00", "Z")
        hint = {"reason": "host_runtime_choking", "observed_at": old}
        allowed, reason, _ = check_cpu_choking(
            cpu_usage=None, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0,
            trusted_choking=hint)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_stale_telemetry_unknown_admits(self):
        old = datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat().replace("+00:00", "Z")
        u = usage(psi_full=90.0)
        u["observed_at"] = old
        allowed, reason, _ = check_cpu_choking(
            cpu_usage=u, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_choking_with_unknown_busy_still_refuses(self):
        # FULL PSI/OOM signals are independent of busy counters: known fresh
        # choking refuses even when busy is unknown/reset.
        u = {"busy_percent": None, "busy_cores": None, "cores": 12,
             "psi_avg10": None, "psi_full_avg10": 9.0,
             "memory_psi_full_avg10": 0.0, "oom_kills_delta": None,
             "observed_at": now_iso(), "interval_s": 0.2}
        allowed, reason, _ = check_cpu_choking(
            cpu_usage=u, policy=policy(), cores=12,
            expected_cpu_cores=1.0, ramping_cpu_cores=0.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "cpu_pressure")

    def test_refusal_format_exact(self):
        u = usage(psi_full=7.0)
        allowed, reason, admission = check_cpu_choking(
            cpu_usage=u, policy=policy(), cores=12,
            expected_cpu_cores=2.0, ramping_cpu_cores=1.0)
        text = format_cpu_refusal(reason, admission)
        lines = text.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "cpu_pressure")
        body = json.loads(lines[1].split("CPU_ADMISSION_JSON ", 1)[1])
        for key in ("current_cpu_cores", "expected_cpu_cores",
                    "ramping_cpu_cores", "limit_cpu_cores",
                    "target_utilization", "psi_avg10", "psi_full_avg10",
                    "memory_psi_full_avg10", "oom_kills_delta", "observed_at"):
            self.assertIn(key, body)


class ExpectedCpuTests(unittest.TestCase):
    def test_payload_authoritative_all_sources(self):
        for source in ("repo_kind", "repo", "global", "default"):
            cores, src = resolve_expected_cpu(
                {"expected_cpu_cores": 2.5, "expected_cpu_source": source})
            self.assertEqual((cores, src), (2.5, source))

    def test_no_history_defaults_conservative(self):
        cores, src = resolve_expected_cpu({}, policy=policy())
        self.assertEqual((cores, src), (1.0, "default"))
        cores, src = resolve_expected_cpu(None, policy=policy())
        self.assertEqual((cores, src), (1.0, "default"))

    def test_invalid_payload_fails_closed(self):
        with self.assertRaises(ValueError):
            resolve_expected_cpu({"expected_cpu_cores": -1,
                                  "expected_cpu_source": "repo"})
        with self.assertRaises(ValueError):
            resolve_expected_cpu({"expected_cpu_cores": 2,
                                  "expected_cpu_source": "guess"})


class RampingTests(unittest.TestCase):
    def test_queued_zero_stable_zero_young_credit(self):
        entries = [
            {"state": "queued", "expected_cpu_cores": 4.0,
             "current_cpu_cores": None, "age_seconds": 5},
            {"state": "running", "expected_cpu_cores": 4.0,
             "current_cpu_cores": 1.5, "age_seconds": 300},
            {"state": "starting", "expected_cpu_cores": 2.0,
             "current_cpu_cores": 0.5, "age_seconds": 10},
            {"state": "running", "expected_cpu_cores": 3.0,
             "current_cpu_cores": None, "age_seconds": 10},
        ]
        total = ramping_cpu_cores(entries, ramp_seconds=180)
        self.assertAlmostEqual(total, (2.0 - 0.5) + 3.0)

    def test_unknown_age_retains_prediction(self):
        entries = [{"state": "starting", "expected_cpu_cores": 2.0,
                    "current_cpu_cores": None, "age_seconds": None}]
        self.assertAlmostEqual(ramping_cpu_cores(entries), 2.0)


class IdentityFreshnessTests(unittest.TestCase):
    def test_first_sample_unknown(self):
        self.assertIsNone(cpu.cpu_current_cores(None, None, 5.0, 1000))

    def test_reset_unknown(self):
        self.assertIsNone(cpu.cpu_current_cores(10.0, 1000, 9.0, 2000))

    def test_out_of_order_unknown(self):
        self.assertIsNone(cpu.cpu_current_cores(5.0, 2000, 6.0, 1000))

    def test_proven_credit(self):
        self.assertAlmostEqual(cpu.cpu_current_cores(5.0, 1000, 7.0, 3000), 1.0)

    def test_oom_first_unknown_then_delta(self):
        self.assertIsNone(oom_delta(None, 3))
        self.assertEqual(oom_delta(3, 5), 2)
        self.assertIsNone(oom_delta(5, 4))

    def test_choking_hint_validation(self):
        good = validate_resource_choking(
            {"reason": "host_oom", "observed_at": now_iso()})
        self.assertEqual(good["reason"], "host_oom")
        self.assertIsNone(validate_resource_choking(
            {"reason": "busy", "observed_at": now_iso()}))
        self.assertIsNone(validate_resource_choking(None))
        # Task text is never a hint: only the validated dict shape passes.
        self.assertIsNone(validate_resource_choking("host is choking badly"))


class AdmissionIntegrationTests(unittest.TestCase):
    def setUp(self):
        import shutil
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.repo = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.owned = Path(self.repo) / ".local" / "tmp" / "r1"
        self.owned.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def check(self, **kw):
        base = dict(requested_memory_mb=1500, requested_cwd=self.repo,
                    requested_tmpdir=str(self.owned), repo_root=self.repo)
        base.update(kw)
        return check_resources(**base)

    def mem(self):
        import launcher.resources as res
        return (patch.object(res, "get_mem_available", return_value=30 * GiB),
                patch("shutil.disk_usage",
                      return_value=type("D", (), {"free": 100 * GiB})()))

    def test_busy100_admits_and_choking_queues(self):
        m1, m2 = self.mem()
        calm = usage(cores=12, busy_percent=100.0, psi_some=99.0, psi_full=0.0)
        hot = usage(cores=12, busy_percent=100.0, psi_some=99.0, psi_full=9.0)
        with m1, m2:
            self.assertTrue(self.check(cpu_usage=calm, _cpu_count=12))
            with self.assertRaisesRegex(ValueError, "cpu_pressure\nCPU_ADMISSION_JSON"):
                self.check(cpu_usage=hot, _cpu_count=12)

    def test_memory_floor_still_applies(self):
        import launcher.resources as res
        calm = usage(cores=12, busy_percent=100.0, psi_full=0.0)
        with patch.object(res, "get_mem_available", return_value=1 * GiB), \
             patch("shutil.disk_usage",
                   return_value=type("D", (), {"free": 100 * GiB})()):
            with self.assertRaisesRegex(ValueError, "launcher_memory_reserve"):
                self.check(cpu_usage=calm, _cpu_count=12,
                           memory_estimate_mb=64)

    def test_native_taskunits_filebus_same_policy(self):
        from launcher.task_units import admit_task_unit
        from launcher.filebus_backend import admit_headless
        m1, m2 = self.mem()
        calm = usage(cores=12, busy_percent=100.0, psi_full=0.0)
        hot = usage(cores=12, busy_percent=100.0, psi_full=9.0)
        quse = {"ok": True}
        argv = ["echo", "hello"]
        with m1, m2, \
             patch("launcher.task_units.validate_quse",
                   return_value=([{"provider": "zai", "name": "zai"}], {})), \
             patch("launcher.filebus_backend.validate_quse",
                   return_value=([{"provider": "zai", "name": "zai"}], {})):
            self.assertTrue(admit_task_unit(
                "t1", 1500, self.repo, str(self.owned), quse_json=quse,
                provider=None, command_argv=argv, cpu_usage=calm,
                expected_cpu_cores=1.0))
            with self.assertRaisesRegex(Exception, "cpu_pressure"):
                admit_task_unit(
                    "t2", 1500, self.repo, str(self.owned), quse_json=quse,
                    provider=None, command_argv=argv, cpu_usage=hot,
                    expected_cpu_cores=1.0)
            self.assertTrue(admit_headless(
                quse, 1500, self.repo, str(self.owned), cpu_usage=calm,
                expected_cpu_cores=1.0))
            with self.assertRaisesRegex(Exception, "cpu_pressure"):
                admit_headless(quse, 1500, self.repo, str(self.owned),
                               cpu_usage=hot, expected_cpu_cores=1.0)


class StoreCpuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict(os.environ,
                             {"LAUNCHER_HOST_FENCING_DIR": str(self.root / "fencing")})
        patcher.start()
        self.addCleanup(patcher.stop)
        from launcher.store import Store
        self.store = Store(str(self.root / "state.db"))

    def test_freeze_is_stable_and_explainable(self):
        self.store.submit_task("task-1", "k1",
                               {"goal": "g", "owner": "o", "cwd": "/tmp",
                                "timeout": 60}, [], 1500, 0)
        frozen = self.store.freeze_cpu_start("task-1", 2.5, "repo_kind")
        self.assertEqual(frozen, {"expected_cpu_cores": 2.5,
                                  "expected_cpu_source": "repo_kind"})
        # Second freeze with different values keeps the first (frozen).
        frozen2 = self.store.freeze_cpu_start("task-1", 9.0, "global")
        self.assertEqual(frozen2["expected_cpu_cores"], 2.5)
        self.assertEqual(frozen2["expected_cpu_source"], "repo_kind")
        self.assertEqual(self.store.get_cpu_start("task-1")["expected_cpu_source"],
                         "repo_kind")

    def test_dry_run_never_writes(self):
        self.store.submit_task("task-dry", "kdry",
                               {"goal": "g", "owner": "o", "cwd": "/tmp",
                                "timeout": 60}, [], 1500, 0)
        before = self.store.get_cpu_start("task-dry")
        self.assertIsNone(before)
        self.store.freeze_cpu_start("task-dry", 2.0, "repo", dry_run=True)
        self.store.record_choking_event("host_oom", now_iso(), dry_run=True)
        self.store.record_cpu_sample("task-dry", 1.0, now_iso(), "n:1", 5.0,
                                     1000, dry_run=True)
        self.store.set_oom_baseline("/proc", 7, dry_run=True)
        self.assertIsNone(self.store.get_cpu_start("task-dry"))
        self.assertIsNone(self.store.get_fresh_choking())
        self.assertIsNone(self.store.get_oom_baseline("/proc"))

    def test_choking_cooldown_evidence(self):
        self.assertIsNone(self.store.get_fresh_choking(cooldown_s=300))
        self.store.record_choking_event("host_oom", now_iso())
        fresh = self.store.get_fresh_choking(cooldown_s=300)
        self.assertEqual(fresh["reason"], "host_oom")


class SanitizeTests(unittest.TestCase):
    def test_admission_dto_bounded(self):
        good = {"expected_cpu_cores": 2.0, "ramping_cpu_cores": 1.0,
                "limit_cpu_cores": 12.0, "target_utilization": 1.0,
                "current_cpu_cores": None, "psi_avg10": 99.0,
                "psi_full_avg10": 0.0, "memory_psi_full_avg10": 0.0,
                "oom_kills_delta": 0, "observed_at": now_iso()}
        self.assertIsNotNone(sanitize_cpu_admission(good))
        bad = dict(good, psi_full_avg10=101.0)
        self.assertIsNone(sanitize_cpu_admission(bad))
        bad2 = dict(good, oom_kills_delta=1.5)
        self.assertIsNone(sanitize_cpu_admission(bad2))

    def test_usage_requires_consistent_busy(self):
        u = usage(cores=4, busy_percent=50.0)
        self.assertIsNotNone(sanitize_cpu_usage(u)["busy_cores"])
        bad = dict(u, busy_cores=999.0)
        self.assertIsNone(sanitize_cpu_usage(bad)["busy_cores"])


class TrustedHostCliBridgeTests(unittest.TestCase):
    def test_bad_explicit_hint_does_not_clear_valid_inherited_evidence(self):
        from types import SimpleNamespace
        from launcher import cli
        hint={"reason":"host_oom","observed_at":datetime.now(timezone.utc).isoformat()}
        with patch.dict(os.environ,{"LAUNCHER_TRUSTED_CHOKING_JSON":json.dumps(hint)}), \
             patch.object(cli,"_run_with_host_hint",side_effect=lambda args: cpu.trusted_choking_from_env()):
            self.assertEqual(cli.run(SimpleNamespace(trusted_choking_json="bad")),hint)

    def test_native_and_task_units_receive_explicit_hint_without_task_payload_trust(self):
        from types import SimpleNamespace
        from launcher import cli
        at = datetime.now(timezone.utc).isoformat()
        hint = {"reason": "host_runtime_choking", "observed_at": at}
        for backend, function in (("aplexer", "do_run"), ("task-units", "run_task_units")):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(dir=LOCAL_TMP_DIR) as tmp:
                args = SimpleNamespace(config_dir=tmp, backend=backend, id="proof", cwd=tmp,
                                       tmpdir=tmp, trusted_choking_json=json.dumps(hint))
                def checked(*a, **kw):
                    self.assertEqual(cpu.trusted_choking_from_env(), hint)
                    return 1
                with patch.dict(os.environ, {"LAUNCHER_TRUSTED_CHOKING_JSON": "prior"}), \
                     patch.object(cli, function, side_effect=checked):
                    self.assertEqual(cli.run(args), 1)
                    self.assertEqual(os.environ["LAUNCHER_TRUSTED_CHOKING_JSON"], "prior")

    def test_malformed_unknown_and_unbounded_flags_remain_advisory_and_restore_environment(self):
        from types import SimpleNamespace
        from launcher import cli
        for raw in ("bad-json", "x" * 513, '{"reason":"host_oom","observed_at":"2020-01-01T00:00:00Z"}',
                    json.dumps({"reason":"host_oom", "observed_at":datetime.now(timezone.utc).isoformat(), "extra":"text"})):
            with self.subTest(raw=raw[:25]), patch.object(cli, "_run_with_host_hint") as run:
                def checked(args):
                    self.assertIsNone(cpu.trusted_choking_from_env())
                    raise RuntimeError("backend failed")
                run.side_effect = checked
                with patch.dict(os.environ, {"LAUNCHER_TRUSTED_CHOKING_JSON":"prior"}):
                    with self.assertRaises(RuntimeError):
                        cli.run(SimpleNamespace(trusted_choking_json=raw))
                    self.assertEqual(os.environ["LAUNCHER_TRUSTED_CHOKING_JSON"], "prior")

    def test_detached_controller_carries_explicit_hint_as_argv(self):
        from types import SimpleNamespace
        from launcher import cli
        with tempfile.TemporaryDirectory(dir=LOCAL_TMP_DIR) as tmp:
            hint = json.dumps({"reason":"host_oom", "observed_at":datetime.now(timezone.utc).isoformat()})
            args=SimpleNamespace(config_dir=tmp,id="proof",cwd=tmp,tmpdir=tmp,trusted_choking_json=hint,resource_config="/trusted/policy.json")
            with patch.object(cli.subprocess,"run",return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(cli.spawn_ql_controller(args,return_dict=True)["state"],"queued-or-starting")
                command=run.call_args.args[0]
                index=command.index("--trusted-choking-json")
                self.assertEqual(command[index+1],hint)
                self.assertEqual(command[command.index("--resource-config")+1],"/trusted/policy.json")


class AdmissionContextRecoveryTests(unittest.TestCase):
    def test_oom_and_full_psi_survive_busy_sampler_exception_before_baseline_advances(self):
        from unittest.mock import MagicMock
        store=MagicMock()
        store.get_oom_baseline.return_value=10
        store.get_fresh_choking.return_value=None
        with patch.object(cpu,"sample_cpu_usage",side_effect=OSError), \
             patch.object(cpu,"read_oom_counter",return_value=11), \
             patch.object(cpu,"read_cpu_psi",return_value=7), \
             patch.dict(os.environ,{"LAUNCHER_TRUSTED_CHOKING_JSON":""}):
            usage,hint=cpu.collect_admission_context(store,RESOURCE_DEFAULTS)
        self.assertIsNone(usage["busy_cores"])
        self.assertEqual(usage["oom_kills_delta"],1)
        self.assertEqual(usage["psi_full_avg10"],7)
        self.assertEqual(store.method_calls[1][0],"record_choking_event")
        store.set_oom_baseline.assert_called_once_with("/proc",11)
        self.assertIn(cpu.check_cpu_choking(usage,RESOURCE_DEFAULTS,cores=4,trusted_choking=hint)[1],("cpu_pressure","host_oom"))

    def test_new_hint_persists_over_old_hint_and_dry_run_never_writes(self):
        from unittest.mock import MagicMock
        now=datetime.now(timezone.utc)
        newer={"reason":"host_runtime_choking","observed_at":now.isoformat()}
        older={"reason":"host_oom","observed_at":datetime.fromtimestamp(now.timestamp()-295,timezone.utc).isoformat()}
        for dry in (False,True):
            store=MagicMock();store.get_oom_baseline.return_value=10;store.get_fresh_choking.return_value=older
            with self.subTest(dry=dry), patch.object(cpu,"sample_cpu_usage",return_value={**usage(),"oom_counter":10}), \
                 patch.dict(os.environ,{"LAUNCHER_TRUSTED_CHOKING_JSON":json.dumps(newer)}):
                _,hint=cpu.collect_admission_context(store,RESOURCE_DEFAULTS,dry_run=dry)
            self.assertEqual(hint,newer)
            if dry:
                store.record_choking_event.assert_not_called();store.set_oom_baseline.assert_not_called()
            else:
                store.record_choking_event.assert_called_once_with(newer["reason"],newer["observed_at"])

    def test_task_unit_context_honors_file_active_commitments_and_frozen_cpu(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from launcher import cli
        with tempfile.TemporaryDirectory(dir=LOCAL_TMP_DIR) as tmp:
            policy=Path(tmp)/"policy.json";policy.write_text(json.dumps({"memory_floor_mb":4096,"cpu_psi_full_hard_cap_pct":8}))
            store=MagicMock();store.memory_samples_p90.return_value=None
            store.freeze_cpu_start.return_value={"expected_cpu_cores":3,"expected_cpu_source":"repo"}
            store.get_active_resources.return_value=(700,25)
            store.cpu_ramping_entries.return_value=[]
            hint={"reason":"host_runtime_choking","observed_at":datetime.now(timezone.utc).isoformat()}
            args=SimpleNamespace(id="task",cwd=tmp,tmpdir=tmp,resource_config=str(policy))
            with patch.object(cpu,"collect_admission_context",return_value=(usage(),hint)), \
                 patch("launcher.resources.check_resources") as check:
                context=cli.task_unit_resource_context(store,args,{"expected_cpu_cores":1,"expected_cpu_source":"default"},1500)
            self.assertEqual(context["memory_floor_mb"],4096)
            self.assertEqual(context["active_mem_mb"],700);self.assertEqual(context["active_disk_mb"],25)
            self.assertEqual(context["expected_cpu_cores"],3)
            self.assertEqual(context["cpu_policy"]["cpu_psi_full_hard_cap_pct"],8)
            self.assertEqual(context["trusted_choking"],hint)
            check.assert_called_once_with(1500,tmp,tmp,repo_root=tmp,**context)

    def test_both_task_unit_execution_paths_forward_resource_commitments_before_effects(self):
        from launcher import task_units
        context=dict(active_mem_mb=700,active_disk_mb=25,expected_cpu_cores=3,
                     ramping_cpu_cores=2,cpu_policy=dict(RESOURCE_DEFAULTS),cpu_usage=usage(),
                     trusted_choking={"reason":"host_oom","observed_at":datetime.now(timezone.utc).isoformat()})
        for execute in (task_units.execute_transient_task_unit,task_units.spawn_transient_task_unit):
            with self.subTest(execute=execute.__name__), \
                 patch.object(task_units,"admit_task_unit",side_effect=task_units.TaskUnitAdmissionError("known choking")) as admit, \
                 patch.object(task_units.subprocess,"run") as effect:
                with self.assertRaisesRegex(task_units.TaskUnitAdmissionError,"known choking"):
                    execute(task_id="proof",command_argv=["/usr/bin/true"],memory_mb=1500,workspace="/repo",**context)
                for key,value in context.items():
                    self.assertEqual(admit.call_args.kwargs[key],value)
                effect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
