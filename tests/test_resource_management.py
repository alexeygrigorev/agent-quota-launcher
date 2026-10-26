"""Placement policy tests (Hub intake 4fcd3139): live MemAvailable admission.

Covers: configurable floor (default 2048), start-ramp estimate from measured
p90 (fallback 512), no double reservation of residents, CPU load5 guard at
1.5x cores, unchanged cgroup caps, Hub payload knobs, explicit-path config
file with safe defaults, and resource-status ramp/estimate reporting.
Unknown measurements fail closed. Tests never stop sessions.
"""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher.resource_policy import (
    RESOURCE_DEFAULTS,
    cpu_guard_status,
    check_cpu_guard,
    load_resource_policy,
    percentile_of,
    read_cpu_count,
    read_load5,
    resolve_memory_estimate_mb,
)
from launcher.resources import check_resources
from launcher.store import Store

GiB = 1024 * 1024 * 1024
MiB = 1024 * 1024
LOCAL_TMP_DIR = Path(__file__).resolve().parent.parent / ".local" / "tmp"


class DiskUsage:
    def __init__(self, free):
        self.free = free


def calm_cpu(test):
    """Deterministic calm CPU for admission tests (live load is covered
    by the dedicated guard tests)."""
    p1 = patch("launcher.resource_policy.read_load5", return_value=1.0)
    p2 = patch("launcher.resource_policy.read_cpu_count", return_value=12)
    p1.start()
    p2.start()
    test.addCleanup(p1.stop)
    test.addCleanup(p2.stop)


class PolicyDefaultsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(load_resource_policy(), RESOURCE_DEFAULTS)
        self.assertEqual(RESOURCE_DEFAULTS["memory_floor_mb"], 2048)
        self.assertEqual(RESOURCE_DEFAULTS["ramp_reserve_default_mb"], 512)
        self.assertEqual(RESOURCE_DEFAULTS["estimate_percentile"], 90)
        self.assertEqual(RESOURCE_DEFAULTS["estimate_multiplier"], 1.25)
        self.assertEqual(RESOURCE_DEFAULTS["cpu_load_factor"], 1.5)
        self.assertEqual(RESOURCE_DEFAULTS["ramp_seconds"], 180)

    def test_payload_overlay(self):
        policy = load_resource_policy(
            {"resource_management": {"memory_floor_mb": 1024,
                                     "ramp_seconds": 60,
                                     "cpu_load_factor": 2.0}})
        self.assertEqual(policy["memory_floor_mb"], 1024)
        self.assertEqual(policy["ramp_seconds"], 60)
        self.assertEqual(policy["cpu_load_factor"], 2.0)
        self.assertEqual(policy["ramp_reserve_default_mb"], 512)

    def test_payload_must_be_object(self):
        with self.assertRaisesRegex(ValueError, "must be an object"):
            load_resource_policy({"resource_management": [1]})
        for bad in ("x", 1.5, True):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "invalid"):
                load_resource_policy({"resource_management": {"memory_floor_mb": bad}})

    def test_out_of_range_fails_closed(self):
        # Bounds track the Hub contract: floor 128..1048576, ramp
        # 64..65536, percentile 50..100, multiplier 1..4, cpu 0.5..4,
        # ramp_seconds 30..900.
        for knob, bad in (("memory_floor_mb", 64), ("memory_floor_mb", 2 ** 30),
                          ("ramp_reserve_default_mb", 32),
                          ("ramp_reserve_default_mb", 1 << 20),
                          ("estimate_percentile", 49),
                          ("estimate_percentile", 101),
                          ("estimate_multiplier", 0.99),
                          ("estimate_multiplier", 4.5),
                          ("cpu_load_factor", 0.4),
                          ("cpu_load_factor", 4.5),
                          ("ramp_seconds", 10),
                          ("ramp_seconds", 99999)):
            with self.subTest(knob=knob, bad=bad), self.assertRaisesRegex(ValueError, "resource_management"):
                load_resource_policy({"resource_management": {knob: bad}})

    def test_hub_bounds_edges_accepted(self):
        policy = load_resource_policy({"resource_management": {
            "memory_floor_mb": 128, "ramp_reserve_default_mb": 65536,
            "estimate_percentile": 100, "estimate_multiplier": 4,
            "cpu_load_factor": 0.5, "ramp_seconds": 30}})
        self.assertEqual(policy["estimate_percentile"], 100)
        self.assertEqual(policy["cpu_load_factor"], 0.5)
        policy = load_resource_policy({"resource_management": {
            "memory_floor_mb": 1048576, "ramp_reserve_default_mb": 64,
            "estimate_percentile": 50, "estimate_multiplier": 1,
            "cpu_load_factor": 4, "ramp_seconds": 900}})
        self.assertEqual(policy["memory_floor_mb"], 1048576)
        self.assertEqual(policy["estimate_multiplier"], 1)

    def test_full_hub_config_object_interop(self):
        # The Hub passes its whole config/resource-management.json,
        # including watchdog keys the launcher must safely ignore.
        hub_config = {"memory_floor_mb": 2048,
                      "ramp_reserve_default_mb": 512,
                      "estimate_percentile": 90,
                      "estimate_multiplier": 1.25,
                      "ramp_seconds": 180,
                      "cpu_load_factor": 1.5,
                      "watchdog_interval_s": 300,
                      "idle_minutes": 60,
                      "rss_flag_gb": 4}
        policy = load_resource_policy({"resource_management": hub_config})
        self.assertEqual(policy["memory_floor_mb"], 2048)
        self.assertEqual(policy["estimate_multiplier"], 1.25)
        self.assertEqual(set(policy), set(RESOURCE_DEFAULTS))

    def test_unknown_keys_ignored(self):
        policy = load_resource_policy({"resource_management": {"future_knob": 1}})
        self.assertEqual(policy, RESOURCE_DEFAULTS)

    def test_explicit_file_and_env(self):
        with tempfile.TemporaryDirectory(dir=str(LOCAL_TMP_DIR)) as d:
            cfg = Path(d) / "resource-management.json"
            cfg.write_text(json.dumps({"memory_floor_mb": 3072,
                                       "estimate_percentile": 95}))
            policy = load_resource_policy({}, explicit_path=str(cfg))
            self.assertEqual(policy["memory_floor_mb"], 3072)
            self.assertEqual(policy["estimate_percentile"], 95)
            # Payload still wins over the file.
            policy2 = load_resource_policy(
                {"resource_management": {"memory_floor_mb": 1024}},
                explicit_path=str(cfg))
            self.assertEqual(policy2["memory_floor_mb"], 1024)
            with patch.dict(os.environ, {"LAUNCHER_RESOURCE_CONFIG": str(cfg)}):
                self.assertEqual(load_resource_policy({})["memory_floor_mb"], 3072)

    def test_explicit_file_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=str(LOCAL_TMP_DIR)) as d:
            missing = str(Path(d) / "nope.json")
            with self.assertRaisesRegex(ValueError, "unreadable"):
                load_resource_policy({}, explicit_path=missing)
            bad = Path(d) / "bad.json"
            bad.write_text("{not json")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                load_resource_policy({}, explicit_path=str(bad))
            arr = Path(d) / "arr.json"
            arr.write_text("[1,2]")
            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                load_resource_policy({}, explicit_path=str(arr))
            wrong = Path(d) / "wrong.json"
            wrong.write_text(json.dumps({"memory_floor_mb": -5}))
            with self.assertRaisesRegex(ValueError, "resource_management"):
                load_resource_policy({}, explicit_path=str(wrong))

    def test_no_implicit_cwd_config(self):
        # A config file in cwd must NOT be picked up without an explicit path.
        with tempfile.TemporaryDirectory(dir=str(LOCAL_TMP_DIR)) as d:
            (Path(d) / "resource-management.json").write_text(
                json.dumps({"memory_floor_mb": 111}))
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(load_resource_policy({})["memory_floor_mb"], 2048)
            finally:
                os.chdir(cwd)


class PercentileEstimateTests(unittest.TestCase):
    def test_percentile_math(self):
        self.assertEqual(percentile_of([100, 200, 300, 400, 500,
                                       600, 700, 800, 900, 1000], 90), 900)
        self.assertEqual(percentile_of([100, 200, 300, 400, 500,
                                       600, 700, 800, 900, 1000], 100), 1000)
        self.assertEqual(percentile_of([400], 90), 400)
        self.assertEqual(percentile_of([100, 900], 50), 100)
        self.assertIsNone(percentile_of([], 90))
        self.assertIsNone(percentile_of([0, -3, "x", None], 90))

    def test_resolve_chain(self):
        # Explicit Hub p90 wins; measured p90 is scaled by the multiplier
        # (ceil(max(64, p90 * mult))); the ramp default is the fallback.
        self.assertEqual(
            resolve_memory_estimate_mb({"memory_estimate_mb": 800}, 900, None), 800)
        self.assertEqual(
            resolve_memory_estimate_mb({}, 900, RESOURCE_DEFAULTS), 1125)
        self.assertEqual(
            resolve_memory_estimate_mb({}, None, RESOURCE_DEFAULTS), 512)
        self.assertEqual(
            resolve_memory_estimate_mb({"memory_estimate_mb": 0}, None,
                                       RESOURCE_DEFAULTS), 512)
        policy = dict(RESOURCE_DEFAULTS, ramp_reserve_default_mb=256)
        self.assertEqual(resolve_memory_estimate_mb({}, None, policy), 256)
        # Multiplier honored; tiny samples floor at 64.
        policy = dict(RESOURCE_DEFAULTS, estimate_multiplier=2.0)
        self.assertEqual(resolve_memory_estimate_mb({}, 900, policy), 1800)
        self.assertEqual(resolve_memory_estimate_mb({}, 10, policy), 64)

    def test_resolve_caps_to_worker_ceiling(self):
        # A historic oversized p90 must shrink to the cgroup ceiling,
        # never block placement with a ValueError.
        self.assertEqual(
            resolve_memory_estimate_mb({}, 2000, RESOURCE_DEFAULTS,
                                       cap_mb=1500), 1500)
        self.assertEqual(
            resolve_memory_estimate_mb({"memory_estimate_mb": 2000}, 100,
                                       RESOURCE_DEFAULTS, cap_mb=1500), 1500)
        self.assertEqual(
            resolve_memory_estimate_mb({}, 2000, RESOURCE_DEFAULTS,
                                       cap_mb=4096), 2500)


class CpuGuardTests(unittest.TestCase):
    def test_allows_calm_host(self):
        self.assertTrue(check_cpu_guard(load5=9.0, cpu_count=12, factor=1.5))

    def test_blocks_overloaded_host_with_slug(self):
        with self.assertRaisesRegex(ValueError, "host_cpu_load"):
            check_cpu_guard(load5=19.0, cpu_count=12, factor=1.5)

    def test_boundary_is_allowed(self):
        self.assertTrue(check_cpu_guard(load5=18.0, cpu_count=12, factor=1.5))

    def test_factor_is_configurable(self):
        self.assertTrue(check_cpu_guard(load5=19.0, cpu_count=12, factor=2.0))
        with self.assertRaisesRegex(ValueError, "host_cpu_load"):
            check_cpu_guard(load5=25.0, cpu_count=12, factor=2.0)

    def test_unknown_measurements_fail_closed(self):
        # None means "auto-detect": unknown is when the readers find nothing.
        with patch("launcher.resource_policy.read_load5", return_value=None), \
             patch("launcher.resource_policy.read_cpu_count", return_value=None):
            with self.assertRaisesRegex(ValueError, "unknown"):
                check_cpu_guard()
        with self.assertRaisesRegex(ValueError, "unknown"):
            check_cpu_guard(load5=float("nan"), cpu_count=12, factor=1.5)
        with self.assertRaisesRegex(ValueError, "invalid"):
            check_cpu_guard(load5=1.0, cpu_count=12, factor=0)

    def test_status_never_raises(self):
        ok = cpu_guard_status(load5=1.0, cpu_count=4, factor=1.5)
        self.assertTrue(ok["allowed"])
        self.assertIsNone(ok["reason"])
        blocked = cpu_guard_status(load5=99.0, cpu_count=4, factor=1.5)
        self.assertFalse(blocked["allowed"])
        self.assertIn("host_cpu_load", blocked["reason"])

    def test_live_readers(self):
        self.assertIsInstance(read_load5(), float)
        self.assertIsInstance(read_cpu_count(), int)
        self.assertIsNone(read_load5("/nonexistent-proc-loadavg-xyz"))


class MemoryAdmissionTests(unittest.TestCase):
    def setUp(self):
        import shutil
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.repo = tempfile.mkdtemp(dir=LOCAL_TMP_DIR)
        self.owned = Path(self.repo) / ".local" / "tmp" / "r1"
        self.owned.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        calm_cpu(self)

    def check(self, **kw):
        base = dict(requested_memory_mb=1500, requested_cwd=self.repo,
                    requested_tmpdir=str(self.owned), repo_root=self.repo)
        base.update(kw)
        return check_resources(**base)

    def test_estimate_charged_not_cap(self):
        # 3 GiB free: a 1500 cap request fails, but a 512 estimate fits
        # above the 2048 floor (3072 - 512 = 2560 >= 2048).
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=3 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            with self.assertRaisesRegex(ValueError, "launcher_memory_reserve"):
                self.check()
            self.assertTrue(self.check(memory_estimate_mb=512))

    def test_floor_is_configurable(self):
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=3 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            with self.assertRaisesRegex(ValueError, "launcher_memory_reserve"):
                self.check(memory_floor_mb=2048)
            self.assertTrue(self.check(memory_floor_mb=512))

    def test_ramp_reserve_counts(self):
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=5 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            self.assertTrue(self.check(memory_estimate_mb=512, active_mem_mb=0))
            with self.assertRaisesRegex(ValueError, "launcher_memory_reserve"):
                self.check(memory_estimate_mb=512, active_mem_mb=3000)

    def test_oom_floor_blocks_with_zero_ramp(self):
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=1 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            with self.assertRaisesRegex(ValueError, "launcher_memory_reserve"):
                self.check(memory_estimate_mb=64, active_mem_mb=0)

    def test_cpu_guard_blocks_and_slug(self):
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=30 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            with self.assertRaisesRegex(ValueError, "host_cpu_load"):
                self.check(_load5=99.0, _cpu_count=12)
            self.assertTrue(self.check(_load5=1.0, _cpu_count=12,
                                       cpu_load_factor=2.0))

    def test_unknown_cpu_fails_closed(self):
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=30 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)), \
             patch("launcher.resource_policy.read_load5", return_value=None), \
             patch("launcher.resource_policy.read_cpu_count", return_value=None):
            with self.assertRaisesRegex(ValueError, "unknown"):
                self.check()

    def test_cpu_can_be_disabled_for_unit_isolation(self):
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=30 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            self.assertTrue(self.check(check_cpu=False,
                                       _load5=999.0, _cpu_count=12))

    def test_human_override_skips_memory_not_cpu(self):
        import launcher.resources as res
        with patch.object(res, "get_mem_available", return_value=1 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            with patch.dict(os.environ, {"HUMAN_RAM_OVERRIDE": "1"}):
                self.assertTrue(self.check(_load5=1.0, _cpu_count=12))
                with self.assertRaisesRegex(ValueError, "host_cpu_load"):
                    self.check(_load5=99.0, _cpu_count=12)

    def test_invalid_estimate_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "invalid memory_estimate_mb"):
            self.check(memory_estimate_mb=99999)

    def test_caps_unchanged(self):
        from launcher.resources import native_memory_request_for
        self.assertEqual(native_memory_request_for("codex", True), 4096)
        self.assertEqual(native_memory_request_for("zai", False), 1500)
        with self.assertRaisesRegex(ValueError, "1500MiB"):
            self.check(requested_memory_mb=1600)


class StoreRampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict(os.environ,
                             {"LAUNCHER_HOST_FENCING_DIR": str(self.root / "fencing")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(str(self.root / "state.db"))

    def submit(self, task_id="t1", payload_extra=None):
        payload = {"goal": "g", "owner": "o", "cwd": str(self.root),
                   "timeout": 60}
        if payload_extra:
            payload.update(payload_extra)
        self.store.submit_task(task_id, f"key-{task_id}", payload, [])
        return payload

    def test_measured_p90_and_breakdown_bases(self):
        self.submit("t1", {"memory_estimate_mb": 700})
        self.submit("t2")
        with self.store.get_conn() as conn:
            now_bucket = 1000
            conn.execute("INSERT INTO memory_samples VALUES (?,?,?,?,?)",
                         ("t2", "n2", 7, now_bucket, 300))
            conn.execute("INSERT INTO memory_samples VALUES (?,?,?,?,?)",
                         ("t2", "n2", 7, now_bucket - 1, 900))
        self.assertEqual(self.store.memory_samples_p90(90), 900)
        rows = self.store.resource_breakdown()
        by_id = {r["task_id"]: r for r in rows}
        # Queued tasks reserve nothing before starting (disk leases stay).
        self.assertEqual(by_id["t1"]["reserve_mb"], 0)
        self.assertEqual(by_id["t1"]["basis"], "queued-estimate")
        self.assertEqual(by_id["t2"]["reserve_mb"], 0)
        mem, disk = self.store.get_active_resources()
        self.assertEqual((mem, disk), (0, 1024))
        # Once starting, the ramp holds the estimate: per-task Hub p90
        # for t1, measured p90 x multiplier for t2.
        self.store.transition_task("t1", "starting", ("queued",))
        self.store.transition_task("t2", "starting", ("queued",))
        rows = self.store.resource_breakdown()
        by_id = {r["task_id"]: r for r in rows}
        self.assertEqual(by_id["t1"]["reserve_mb"], 700)
        self.assertEqual(by_id["t1"]["basis"], "start-ramp")
        self.assertEqual(by_id["t2"]["reserve_mb"], 1125)
        self.assertEqual(by_id["t2"]["basis"], "start-ramp")

    def test_empty_samples_falls_back_to_ramp_default(self):
        self.assertIsNone(self.store.memory_samples_p90())
        self.submit("t1")
        mem, _ = self.store.get_active_resources(ramp_reserve_mb=256)
        self.assertEqual(mem, 0)  # queued holds nothing before starting
        self.store.transition_task("t1", "starting", ("queued",))
        mem, _ = self.store.get_active_resources(ramp_reserve_mb=256)
        self.assertEqual(mem, 256)

    def test_ramp_seconds_ages_out_start_ramp(self):
        self.submit("t1")
        self.store.transition_task("t1", "starting", ("queued",))
        young = self.store.resource_breakdown(ramp_seconds=3600)
        self.assertEqual(young[0]["basis"], "start-ramp")
        self.assertEqual(young[0]["reserve_mb"], 512)
        with patch.object(Store, "_task_age_seconds", return_value=9999):
            old = self.store.resource_breakdown(ramp_seconds=60)
            self.assertEqual(old[0]["basis"], "full-unknown")
            self.assertEqual(old[0]["reserve_mb"], 1500)

    def test_dead_tasks_settle_without_stopping_live_ones(self):
        self.submit("t1")
        self.store.transition_task("t1", "running", ("queued",))
        self.store.record_native_binding(
            "t1", native_id="11111111-2222-4333-8444-555555555555",
            tag="task-t1", workspace=str(self.root), engine="codex",
            incarnation=1000)
        with patch("launcher.store.probe_native_lifecycle",
                   return_value=("dead", "confirmed")):
            changes = []
            mem, _ = self.store.get_active_resources(reconciliation=changes)
            self.assertEqual(mem, 0)
            self.assertEqual(len(changes), 1)
            self.assertEqual(self.store.get_task("t1")["state"], "failed")

    def test_exclude_task_id_for_launcher(self):
        self.submit("t1")
        self.submit("t2")
        self.store.transition_task("t1", "starting", ("queued",))
        self.store.transition_task("t2", "starting", ("queued",))
        mem, _ = self.store.get_active_resources(exclude_task_id="t1")
        self.assertEqual(mem, 512)

    def test_concurrent_reads_are_consistent(self):
        for i in range(5):
            self.submit(f"t{i}")
        results = []
        errors = []

        def read():
            try:
                for _ in range(20):
                    results.append(self.store.get_active_resources())
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=read) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertTrue(results)
        # Queued tasks reserve no RAM; disk leases (512 each) are unchanged.
        self.assertTrue(all(r == (0, 5 * 512) for r in results))


class ResidentSettleTests(unittest.TestCase):
    """RM-RAMP-7: additional per-session reserve only while in start ramp."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict(os.environ,
                             {"LAUNCHER_HOST_FENCING_DIR": str(self.root / "fencing")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(str(self.root / "state.db"))
        self.binding = dict(
            native_id="11111111-2222-4333-8444-555555555555",
            tag="task-t1", workspace=str(self.root), engine="codex",
            incarnation=1000)

    def submit_running(self, task_id="t1"):
        self.store.submit_task(task_id, f"key-{task_id}",
                               {"goal": "g", "owner": "o",
                                "cwd": str(self.root), "timeout": 60}, [])
        self.store.record_native_binding(task_id, **self.binding)
        self.store.transition_task(task_id, "running", ("queued",))

    def live(self, credit, age):
        return (patch("launcher.store.probe_native_lifecycle",
                      return_value=("alive", {})),
                patch("launcher.store.resident_credit_mb",
                      return_value=credit),
                patch.object(Store, "_task_age_seconds",
                             return_value=age))

    def test_old_measured_residents_hold_zero_additional_ram(self):
        self.submit_running()
        p1, p2, p3 = self.live(800, 9999)
        with p1, p2, p3:
            rows = self.store.resource_breakdown()
            self.assertEqual(rows[0]["reserve_mb"], 0)
            self.assertEqual(rows[0]["basis"], "resident-settled")
            mem, _ = self.store.get_active_resources()
            self.assertEqual(mem, 0)
        # Samples were still recorded for the measured p90 estimate.
        self.assertEqual(self.store.memory_samples_p90(), 800)

    def test_young_credit_reduces_ramp(self):
        self.submit_running()
        p1, p2, p3 = self.live(200, 10)
        with p1, p2, p3:
            rows = self.store.resource_breakdown()
            # 512 default estimate minus 200 proven usage.
            self.assertEqual(rows[0]["reserve_mb"], 312)
            self.assertEqual(rows[0]["basis"], "start-ramp")

    def test_young_credit_above_estimate_floors_at_zero(self):
        self.submit_running()
        p1, p2, p3 = self.live(800, 10)
        with p1, p2, p3:
            rows = self.store.resource_breakdown()
            self.assertEqual(rows[0]["reserve_mb"], 0)
            self.assertEqual(rows[0]["basis"], "start-ramp")

    def test_unknown_age_with_live_measurement_uses_ramp_credit(self):
        self.submit_running()
        p1, p2, p3 = self.live(200, None)
        with p1, p2, p3:
            rows = self.store.resource_breakdown()
            self.assertEqual(rows[0]["reserve_mb"], 312)
            self.assertEqual(rows[0]["basis"], "start-ramp")


class ResourceStatusTests(unittest.TestCase):
    def setUp(self):
        import shutil
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cfg = Path(self.dir) / "cfg"
        self.cfg.mkdir()
        from launcher.store import Store as _Store
        _Store(str(self.cfg / "state.db"))
        calm_cpu(self)

    def status(self, **kw):
        import json as _json
        from launcher.cli import resource_status

        class Args:
            config_dir = str(self.cfg)
            dry_run = True
            resource_config = None

        for k, v in kw.items():
            setattr(Args, k, v)
        with patch("launcher.resources.get_mem_available",
                   return_value=30 * GiB), \
             patch("builtins.print") as pr:
            self.assertEqual(resource_status(Args()), 0)
            return _json.loads(pr.call_args.args[0])

    def test_reports_ramp_and_estimates(self):
        doc = self.status()
        for key in ("memory_floor_mb", "ramp_reserve_mb", "measured_p90_mb",
                    "estimate_new_mb", "estimate_percentile",
                    "estimate_multiplier",
                    "ramp_reserve_default_mb", "ramp_seconds", "cpu",
                    "breakdown", "launch_allowed", "reason"):
            self.assertIn(key, doc, key)
        self.assertEqual(doc["memory_floor_mb"], 2048)
        self.assertEqual(doc["estimate_multiplier"], 1.25)
        self.assertEqual(doc["estimate_new_mb"], 512)
        self.assertEqual(doc["estimate_new_codex_managed_mb"], 512)
        self.assertTrue(doc["launch_allowed"])
        self.assertIsNone(doc["reason"])
        self.assertTrue(doc["cpu"]["allowed"])

    def test_tight_host_reports_launcher_memory_reserve(self):
        import json as _json
        from launcher.cli import resource_status

        class Args:
            config_dir = str(self.cfg)
            dry_run = True
            resource_config = None

        with patch("launcher.resources.get_mem_available",
                   return_value=1 * GiB), \
             patch("builtins.print") as pr:
            self.assertEqual(resource_status(Args()), 0)
            doc = _json.loads(pr.call_args.args[0])
        self.assertFalse(doc["launch_allowed"])
        self.assertEqual(doc["reason"], "launcher_memory_reserve")
        self.assertFalse(doc["launch_allowed_codex_managed"])
        self.assertEqual(doc["reason_codex_managed"], "launcher_memory_reserve")

    def test_cpu_overload_reports_host_cpu_load(self):
        import json as _json
        from launcher.cli import resource_status

        class Args:
            config_dir = str(self.cfg)
            dry_run = True
            resource_config = None

        with patch("launcher.resources.get_mem_available",
                   return_value=30 * GiB), \
             patch("launcher.resource_policy.read_load5", return_value=99.0), \
             patch("builtins.print") as pr:
            self.assertEqual(resource_status(Args()), 0)
            doc = _json.loads(pr.call_args.args[0])
        self.assertFalse(doc["launch_allowed"])
        self.assertEqual(doc["reason"], "host_cpu_load")

    def test_explicit_config_file_flows_through(self):
        cfg = Path(self.dir) / "rm.json"
        cfg.write_text(json.dumps({"memory_floor_mb": 100 * 1024}))
        doc = self.status(resource_config=str(cfg))
        self.assertEqual(doc["memory_floor_mb"], 100 * 1024)
        self.assertFalse(doc["launch_allowed"])
        self.assertEqual(doc["reason"], "launcher_memory_reserve")

    def test_never_transitions_on_dry_run(self):
        from launcher.store import Store as _Store
        store = _Store(str(self.cfg / "state.db"))
        store.submit_task("t1", "k1",
                          {"goal": "g", "owner": "o",
                           "cwd": str(self.dir), "timeout": 60}, [])
        before = store.get_task("t1")["state"]
        self.status()
        self.assertEqual(store.get_task("t1")["state"], before)


class LaunchWiringTests(unittest.TestCase):
    def setUp(self):
        import shutil
        LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=str(LOCAL_TMP_DIR))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (Path(self.dir) / ".local").mkdir(exist_ok=True)
        from launcher.store import Store as _Store
        self.db = str(Path(self.dir) / "store.db")
        _Store(self.db)
        self.lock = str(Path(self.dir) / "launch.lock")
        calm_cpu(self)

    def test_do_run_records_policy_and_estimate(self):
        import json as _json
        from launcher.launch import do_run
        from launcher.store import Store as _Store
        store = _Store(self.db)
        payload = {"owner": "me", "cwd": self.dir, "timeout": 60,
                   "goal": "g x",
                   "resource_management": {"memory_floor_mb": 1024},
                   "memory_estimate_mb": 700}
        store.submit_task("t-pol", "ik-pol", payload, [])
        fake_quse = {"go": {"status": "ok", "windows": {
            "monthly": {"percent_remaining": 80.0,
                        "reset_at": "2026-11-01T00:00:00+00:00"}}}}
        seen = {}

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                return "{}", ""

        with patch("launcher.launch.fetch_quse", return_value=fake_quse), \
             patch("launcher.launch.validate_quse",
                   return_value=([{"name": "go", "provider": "opencode"}],
                                 {})), \
             patch("launcher.launch.select_candidate",
                   return_value=({"provider": "opencode",
                                  "model": "opencode-go/muse-spark-1.3-contributor"},
                                 {})), \
             patch("launcher.launch.subprocess.Popen") as popen, \
             patch("launcher.launch.validate_first_action", return_value=False), \
             patch("launcher.launch.native_status",
                   return_value=("dead", "gone")), \
             patch("launcher.resources.get_mem_available",
                   return_value=30 * GiB), \
             patch("shutil.disk_usage", return_value=DiskUsage(100 * GiB)):
            popen.return_value = FakeProc()
            rc = do_run(self.db, "t-pol", self.dir, None, self.lock)
            self.assertEqual(rc, 1)  # model mismatch on empty start doc
        record = _json.loads(
            (Path(self.dir) / ".local" / "launch-t-pol.json").read_text())
        self.assertEqual(record["resource_policy"]["memory_floor_mb"], 1024)
        self.assertEqual(record["memory_estimate_mb"], 700)
        self.assertEqual(record["resource_policy"]["estimate_multiplier"], 1.25)
        self.assertEqual(record["resource_policy"]["ramp_seconds"], 180)


if __name__ == "__main__":
    unittest.main()
