"""Issue #80: the full-suite runner refuses dependencies beyond their pinned contents."""
import base64
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import ci_full_suite as cfs

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}


def sh(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=dict(os.environ, **GIT_ENV)).stdout.strip()


def record_hash(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


class VerifyCheckoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "dep"
        (self.repo / "pkg").mkdir(parents=True)
        (self.repo / "pkg" / "__init__.py").write_text("")
        (self.repo / ".gitignore").write_text("*.pyc\n")
        sh(tmp.name, "init", "-q", str(self.repo))
        sh(self.repo, "add", ".")
        sh(self.repo, "commit", "-qm", "init")
        self.commit = sh(self.repo, "rev-parse", "HEAD")

    def test_exact_pinned_tree_passes(self):
        self.assertEqual(cfs.verify_checkout(self.repo, self.commit, "dep"), self.repo.resolve())

    def test_refuses_wrong_commit(self):
        with self.assertRaisesRegex(cfs.SuiteError, "contract pins"):
            cfs.verify_checkout(self.repo, "0" * 40, "dep")

    def test_refuses_modified_untracked_or_ignored_files(self):
        cases = [("pkg/__init__.py", "x = 1\n"), ("sitecustomize.py", "import os\n"),
                 ("pkg/__init__.cpython-312.pyc", "")]
        for rel, text in cases:
            with self.subTest(path=rel):
                original = (self.repo / rel).read_text() if (self.repo / rel).exists() else None
                (self.repo / rel).write_text(text)
                with self.assertRaisesRegex(cfs.SuiteError, "not exactly the pinned tree"):
                    cfs.verify_checkout(self.repo, self.commit, "dep")
                if original is None:
                    (self.repo / rel).unlink()
                else:
                    (self.repo / rel).write_text(original)


class VerifyPydepsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.req = root / "requirements.txt"
        self.req.write_text("demo-pkg==1.0 \\\n    --hash=sha256:00\n")
        self.target = root / "target"
        files = {"demo_pkg/__init__.py": b"VALUE = 1\n", "bin/demo": b"#!/bin/sh\n"}
        info = self.target / "demo_pkg-1.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: demo_pkg\nVersion: 1.0\n")
        rows = []
        for rel, data in files.items():
            (self.target / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.target / rel).write_bytes(data)
            record_rel = "../../" + rel if rel.startswith("bin/") else rel
            rows.append(f"{record_rel},{record_hash(data)},{len(data)}")
        meta = (info / "METADATA").read_bytes()
        rows += [f"demo_pkg-1.0.dist-info/METADATA,{record_hash(meta)},{len(meta)}",
                 "demo_pkg-1.0.dist-info/RECORD,,"]
        (info / "RECORD").write_text("\n".join(rows) + "\n")

    def test_exact_install_passes(self):
        cfs.verify_pydeps(self.target, self.req)

    def test_refuses_extra_startup_file(self):
        (self.target / "sitecustomize.py").write_text("import os\n")
        with self.assertRaisesRegex(cfs.SuiteError, "not part of a pinned distribution"):
            cfs.verify_pydeps(self.target, self.req)

    def test_refuses_bytecode(self):
        cache = self.target / "demo_pkg" / "__pycache__"
        cache.mkdir()
        (cache / "__init__.cpython-312.pyc").write_bytes(b"")
        with self.assertRaisesRegex(cfs.SuiteError, "not part of a pinned distribution"):
            cfs.verify_pydeps(self.target, self.req)

    def test_refuses_modified_file(self):
        (self.target / "demo_pkg" / "__init__.py").write_text("VALUE = 2\n")
        with self.assertRaisesRegex(cfs.SuiteError, "does not match its RECORD hash"):
            cfs.verify_pydeps(self.target, self.req)

    def test_refuses_unpinned_distribution_set(self):
        self.req.write_text("demo-pkg==2.0\n")
        with self.assertRaisesRegex(cfs.SuiteError, "requirements pin"):
            cfs.verify_pydeps(self.target, self.req)


if __name__ == "__main__":
    unittest.main()
