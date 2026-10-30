"""Issue #80: the immutable source-release tool refuses mismatched or untested sources."""
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import release_tool as rt

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}
SKIP = ("tests.test_launch.TestAdapters::test_validate_zai_runtime_direct")


def sh(repo, *args):
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


class ReleaseToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        (self.repo / "ci").mkdir(parents=True)
        (self.repo / "launcher").mkdir()
        self.contract = {
            "test_requirements": "ci/requirements-test.txt",
            "dependencies": {"agent-coordination": {"commit": "a" * 40},
                             "agent-hub": {"commit": "b" * 40}},
            "min_collected_tests": 3, "min_passed_subtests": 1,
            "host_runtime_skip_allowlist": {"tests": [SKIP]},
        }
        (self.repo / "ci" / "suite-contract.json").write_text(json.dumps(self.contract))
        (self.repo / "ci" / "requirements-test.txt").write_text("pytest==9.1.1\n")
        (self.repo / "launcher" / "__init__.py").write_text("")
        run = self.repo / "run.sh"
        run.write_text("#!/bin/sh\n")
        run.chmod(0o755)
        sh(root, "init", "-q", "-b", "main", str(self.repo))
        sh(self.repo, "add", ".")
        sh(self.repo, "commit", "-qm", "init")
        self.commit = sh(self.repo, "rev-parse", "HEAD")
        self.root = root

    def report(self, **overrides):
        contract_bytes = (self.repo / "ci" / "suite-contract.json").read_bytes()
        req_bytes = (self.repo / "ci" / "requirements-test.txt").read_bytes()
        report = {
            "schema_version": 1, "mode": "ci", "ok": True, "problems": [],
            "source_commit": self.commit,
            "source_tree": sh(self.repo, "rev-parse", "HEAD^{tree}"),
            "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "requirements_sha256": hashlib.sha256(req_bytes).hexdigest(),
            "dependencies": {"agent-coordination": "a" * 40, "agent-hub": "b" * 40},
            "counts": {"collected": 3, "passed": 2, "failed": 0, "skipped": 1,
                       "subtests_passed": 1, "subtests_failed": 0},
            "skipped": [{"node": SKIP, "reason": rt.SKIP_REASON + " (x)"}],
            "summary": "2 passed, 1 skipped, 1 subtests passed",
        }
        report.update(overrides)
        path = self.root / f"report-{len(list(self.root.iterdir()))}.json"
        path.write_text(json.dumps(report))
        return path

    def build(self, report, out="dist", tag="v0.1.0-rc1"):
        return rt.main(["build", "--repo", str(self.repo), "--repository", "o/r",
                        "--commit", self.commit, "--tag", tag, "--ci-run-id", "1",
                        "--ci-run-url", "https://ci.invalid/1", "--suite-report", str(report),
                        "--out", str(self.root / out)])

    def test_build_verify_and_stage_exact_commit(self):
        self.assertEqual(self.build(self.report()), 0)
        dist = self.root / "dist"
        manifest = json.loads((dist / "MANIFEST.json").read_text())
        self.assertEqual(manifest["commit"], self.commit)
        self.assertEqual({f["path"] for f in manifest["files"]},
                         {"ci/suite-contract.json", "ci/requirements-test.txt",
                          "launcher/__init__.py", "run.sh"})
        self.assertEqual(rt.main(["verify", "--dir", str(dist), "--commit", self.commit,
                                  "--repo", str(self.repo), "--tag", "v0.1.0-rc1"]), 0)
        dest = self.root / "stage" / "rel"
        self.assertEqual(rt.main(["stage", "--dir", str(dist), "--commit", self.commit,
                                  "--repo", str(self.repo), "--dest", str(dest)]), 0)
        self.addCleanup(lambda: [os.chmod(d, 0o755) for d, _, _ in os.walk(dest)])
        self.assertTrue(os.access(dest / "run.sh", os.X_OK))
        self.assertFalse(os.access(dest / "launcher" / "__init__.py", os.W_OK))
        self.assertEqual(self.check_install(dist, dest), 0)
        self.assertEqual(self.check_install(dist, self.root), 1)
        # Staging never overwrites.
        self.assertEqual(rt.main(["stage", "--dir", str(dist), "--commit", self.commit,
                                  "--repo", str(self.repo), "--dest", str(dest)]), 1)

    def check_install(self, dist, install):
        return rt.main(["check-install", "--dir", str(dist), "--commit", self.commit,
                        "--repo", str(self.repo), "--install", str(install)])

    def test_check_install_refuses_extra_startup_code(self):
        self.assertEqual(self.build(self.report()), 0)
        dist = self.root / "dist"
        install = self.root / "install"
        sh(self.root, "clone", "-q", str(self.repo), str(install))
        sh(install, "checkout", "-q", self.commit)
        (install / ".git").rename(self.root / "install-git")
        self.assertEqual(self.check_install(dist, install), 0)
        cache = install / "launcher" / "__pycache__"
        for extra in ("sitecustomize.py", "launcher/extra.pth",
                      "launcher/__pycache__/__init__.cpython-312.pyc"):
            with self.subTest(extra=extra):
                cache.mkdir(exist_ok=True)
                (install / extra).write_text("import os\n")
                self.assertEqual(self.check_install(dist, install), 1)
                (install / extra).unlink()
                cache.rmdir()
        # Even an empty cache directory is unverifiable state.
        cache.mkdir()
        self.assertEqual(self.check_install(dist, install), 1)

    def test_stage_writes_trusted_blobs_after_verification(self):
        self.assertEqual(self.build(self.report()), 0)
        dist = self.root / "dist"
        tar_path = dist / "agent-quota-launcher-v0.1.0-rc1.tar"
        real_verify = rt._verify

        def verify_then_swap(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            # The release files change after verification succeeded.
            tar_path.write_bytes(b"not a tarball")
            (dist / "MANIFEST.json").write_text("{}")
            return result

        dest = self.root / "staged"
        with patch.object(rt, "_verify", side_effect=verify_then_swap):
            self.assertEqual(rt.main(["stage", "--dir", str(dist), "--commit", self.commit,
                                      "--repo", str(self.repo), "--dest", str(dest)]), 0)
        self.addCleanup(lambda: [os.chmod(d, 0o755) for d, _, _ in os.walk(dest)])
        self.assertEqual((dest / "launcher" / "__init__.py").read_bytes(), b"")
        self.assertEqual(json.loads((dest / ".release-manifest.json").read_text())["commit"],
                         self.commit)
        self.assertEqual(oct(dest.stat().st_mode & 0o777), oct(0o555))

    def test_verify_requires_commit_in_trusted_repo(self):
        self.assertEqual(self.build(self.report()), 0)
        other = self.root / "other"
        sh(self.root, "init", "-q", str(other))
        with self.assertRaisesRegex(rt.ReleaseError, "not in the trusted repository"):
            rt.verify_dir(self.root / "dist", self.commit, other)
        with self.assertRaisesRegex(rt.ReleaseError, "not on refs/heads/nope"):
            rt.verify_dir(self.root / "dist", self.commit, self.repo, on_ref="refs/heads/nope")

    def test_archive_is_reproducible(self):
        self.assertEqual(self.build(self.report(), out="a"), 0)
        self.assertEqual(self.build(self.report(), out="b"), 0)
        self.assertEqual((self.root / "a" / "SHA256SUMS").read_text(),
                         (self.root / "b" / "SHA256SUMS").read_text())

    def test_refuses_red_mismatched_or_unvetted_reports(self):
        bad = [
            {"ok": False, "problems": ["1 failed"]},
            {"mode": "host"},
            {"source_commit": "c" * 40},
            {"source_tree": "d" * 40},
            {"contract_sha256": "0" * 64},
            {"requirements_sha256": "0" * 64},
            {"dependencies": {"agent-coordination": "f" * 40, "agent-hub": "b" * 40}},
            {"counts": {"collected": 2, "passed": 1, "failed": 0, "skipped": 1,
                        "subtests_passed": 1, "subtests_failed": 0}},
            {"counts": {"collected": 3, "passed": 3, "failed": 0, "skipped": 0,
                        "subtests_passed": 0, "subtests_failed": 0}, "skipped": []},
            {"skipped": [{"node": "tests.test_x::test_y", "reason": rt.SKIP_REASON}]},
            {"skipped": [{"node": SKIP, "reason": "skipped for convenience"}]},
        ]
        for i, override in enumerate(bad):
            with self.subTest(override=override):
                self.assertEqual(self.build(self.report(**override), out=f"bad{i}"), 1)
                self.assertFalse((self.root / f"bad{i}" / "SHA256SUMS").exists())

    def test_refuses_dirty_checkout(self):
        (self.repo / "launcher" / "__init__.py").write_text("x = 1\n")
        self.assertEqual(self.build(self.report()), 1)

    def test_verify_refuses_wrong_commit_and_tampering(self):
        self.assertEqual(self.build(self.report()), 0)
        dist = self.root / "dist"
        self.assertEqual(rt.main(["verify", "--dir", str(dist), "--commit", "e" * 40,
                                  "--repo", str(self.repo)]), 1)
        self.assertEqual(rt.main(["verify", "--dir", str(dist), "--commit", self.commit,
                                  "--repo", str(self.repo), "--tag", "v9.9.9"]), 1)
        tar_path = dist / "agent-quota-launcher-v0.1.0-rc1.tar"
        # Re-pack with one modified file and matching SHA256SUMS: manifest catches it.
        with tarfile.open(tar_path) as src:
            members = [(m, src.extractfile(m).read() if m.isfile() else None)
                       for m in src.getmembers()]
            pax = src.pax_headers
        tampered = self.root / "tampered.tar"
        with tarfile.open(tampered, "w", format=tarfile.PAX_FORMAT, pax_headers=pax) as dst:
            for m, data in members:
                if m.name.endswith("launcher/__init__.py"):
                    data = b"import os\n"
                    m.size = len(data)
                dst.addfile(m, None if data is None else io.BytesIO(data))
        tar_path.write_bytes(tampered.read_bytes())
        # Re-sign everything the release itself carries: artifact digest, the
        # per-file manifest entry and SHA256SUMS. Only the trusted git objects
        # can catch it now.
        manifest_path = dist / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source_artifact"]["sha256"] = rt.file_sha256(tar_path)
        manifest["source_artifact"]["size"] = tar_path.stat().st_size
        resigned = json.loads(json.dumps(manifest))
        for entry in resigned["files"]:
            if entry["path"] == "launcher/__init__.py":
                entry.update(size=len(b"import os\n"), sha256=hashlib.sha256(b"import os\n").hexdigest())

        def sign(m):
            manifest_path.write_text(json.dumps(m))
            (dist / "SHA256SUMS").write_text("".join(
                f"{rt.file_sha256(dist / n)}  {n}\n"
                for n in sorted((tar_path.name, "MANIFEST.json", "suite-report.json"))))

        sign(resigned)
        with self.assertRaisesRegex(rt.ReleaseError, "manifest file list does not match the commit"):
            rt.verify_dir(dist, self.commit, self.repo)
        sign(manifest)
        with self.assertRaisesRegex(rt.ReleaseError, "content mismatch for launcher/__init__.py"):
            rt.verify_dir(dist, self.commit, self.repo)

if __name__ == "__main__":
    unittest.main()
