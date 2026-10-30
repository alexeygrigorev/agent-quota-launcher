#!/usr/bin/env python3
"""Immutable source-release tooling for agent-quota-launcher (stdlib only).

A release is one exact commit: an uncompressed `git archive` tarball (byte-for-
byte reproducible from the commit), MANIFEST.json (commit, tree, per-file
SHA-256 and mode, CI run, suite counts, dependency pins) and SHA256SUMS. The
release files are never trusted on their own: verify/stage/check-install need
--repo, a git repository you trust that contains the commit (for example a
fresh clone from GitHub), and compare every file to that commit's git objects.
The commands refuse anything that does not match:

  verify-report  CI suite report must be green, for exactly this commit/tree,
                 under the contract and requirements committed at that commit
  build          create the release files from a clean checkout at the commit
  verify         check SHA256SUMS, manifest and every tarball member
  stage          verify, then write the commit's files from the trusted repo
                 into a NEW read-only directory and re-check it before the
                 final rename; never activates, switches symlinks or touches
                 live configuration
  check-install  read-only: does an installed launcher tree hold exactly the
                 release's tracked files (content, executable bit) and nothing
                 else, bytecode caches included?
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

PROJECT = "agent-quota-launcher"
SHA_RE = re.compile(r"[0-9a-f]{40}")
TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?")
SKIP_REASON = "host-evidence test: pinned host runtime absent"


class ReleaseError(Exception):
    pass


def git(repo, *args, binary=False):
    out = subprocess.run(["git", "-C", str(repo), *args], check=True,
                         capture_output=True).stdout
    return out if binary else out.decode().strip()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def require(cond, message):
    if not cond:
        raise ReleaseError(message)


def check_commit(commit):
    require(bool(SHA_RE.fullmatch(commit or "")), f"commit must be a full 40-hex SHA, got {commit!r}")


def check_tag(tag):
    require(bool(TAG_RE.fullmatch(tag or "")), f"tag must look like v1.2.3 or v1.2.3-rc1, got {tag!r}")


def tarball_name(tag):
    return f"{PROJECT}-{tag}.tar"


# ---------------------------------------------------------------- verify-report

def verify_report(report, commit, repo):
    """Refuse unless the CI suite report is green for exactly this commit."""
    check_commit(commit)
    contract_bytes = git(repo, "show", f"{commit}:ci/suite-contract.json", binary=True)
    contract = json.loads(contract_bytes)
    req_bytes = git(repo, "show", f"{commit}:{contract['test_requirements']}", binary=True)
    tree = git(repo, "rev-parse", f"{commit}^{{tree}}")
    counts = report.get("counts") or {}
    require(report.get("schema_version") == 1, "unsupported suite report schema")
    require(report.get("ok") is True and not report.get("problems"),
            f"suite report is not green: {report.get('problems')}")
    require(report.get("mode") == "ci", "suite report must come from the CI full suite (--mode ci)")
    require(report.get("source_commit") == commit,
            f"suite report tested {report.get('source_commit')}, not {commit}")
    require(report.get("source_tree") == tree, "suite report tree does not match the commit tree")
    require(report.get("contract_sha256") == sha256(contract_bytes),
            "suite report ran under a different ci/suite-contract.json than the commit's")
    require(report.get("requirements_sha256") == sha256(req_bytes),
            "suite report ran with different test requirements than the commit's")
    require(report.get("dependencies") == {k: v["commit"] for k, v in contract["dependencies"].items()},
            "suite report dependency pins differ from the commit's contract")
    require(counts.get("failed") == 0, "suite report has failed tests")
    require(counts.get("collected", 0) >= contract["min_collected_tests"],
            "suite report collected fewer tests than the contract minimum")
    require(counts.get("subtests_failed") == 0, "suite report has failed subtests")
    require(counts.get("subtests_passed", 0) >= contract["min_passed_subtests"],
            "suite report passed fewer subtests than the contract minimum")
    allowed = set(contract["host_runtime_skip_allowlist"]["tests"])
    skipped = report.get("skipped") or []
    require(counts.get("skipped") == len(skipped), "suite report skip count is inconsistent")
    for item in skipped:
        require(item.get("node") in allowed and SKIP_REASON in (item.get("reason") or ""),
                f"undocumented skip in suite report: {item.get('node')}")
    require(counts.get("passed", 0) + len(skipped) == counts.get("collected"),
            "suite report counts do not add up")
    return contract


# ------------------------------------------------------------------------ build

def tracked_files(repo, commit, blobs=None):
    """Path, mode, size and SHA-256 of every file in the commit; fills ``blobs`` if given."""
    entries = []
    raw = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, binary=True)
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, obj = meta.decode().split()
        path = path.decode()
        require(kind == "blob" and mode in ("100644", "100755"),
                f"unsupported tracked entry {path} ({mode} {kind})")
        data = git(repo, "cat-file", "blob", obj, binary=True)
        entries.append({"path": path, "mode": mode, "size": len(data), "sha256": sha256(data)})
        if blobs is not None:
            blobs[path] = data
    return entries


def build(args):
    repo = Path(args.repo).resolve()
    check_commit(args.commit)
    check_tag(args.tag)
    require(git(repo, "rev-parse", "HEAD") == args.commit, "checkout HEAD is not the release commit")
    require(not git(repo, "status", "--porcelain", "--untracked-files=no"),
            "checkout has modified tracked files")
    report_bytes = Path(args.suite_report).read_bytes()
    report = json.loads(report_bytes)
    contract = verify_report(report, args.commit, repo)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    prefix = f"{PROJECT}-{args.tag}/"
    tar_bytes = git(repo, "archive", "--format=tar", f"--prefix={prefix}", args.commit, binary=True)
    name = tarball_name(args.tag)
    (out / name).write_bytes(tar_bytes)
    (out / "suite-report.json").write_bytes(report_bytes)

    manifest = {
        "schema_version": 1,
        "project": PROJECT,
        "repository": args.repository,
        "tag": args.tag,
        "commit": args.commit,
        "tree": git(repo, "rev-parse", f"{args.commit}^{{tree}}"),
        "commit_date": git(repo, "show", "-s", "--format=%cI", args.commit),
        "source_artifact": {"name": name, "format": "git-archive-tar", "prefix": prefix,
                            "size": len(tar_bytes), "sha256": sha256(tar_bytes)},
        "tested_by": {
            "ci_run_id": args.ci_run_id,
            "ci_run_url": args.ci_run_url,
            "suite_report": "suite-report.json",
            "suite_report_sha256": sha256(report_bytes),
            "summary": report.get("summary"),
            "counts": report.get("counts"),
            "skipped": report.get("skipped"),
            "python": report.get("python"),
            "pytest": report.get("pytest"),
        },
        "dependencies": contract["dependencies"],
        "files": tracked_files(repo, args.commit),
        "activation": "none: staging only; activation requires operator authorization (docs/RELEASE.md)",
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (out / "MANIFEST.json").write_bytes(manifest_bytes)
    sums = "".join(f"{file_sha256(out / n)}  {n}\n"
                   for n in sorted((name, "MANIFEST.json", "suite-report.json")))
    (out / "SHA256SUMS").write_text(sums)
    verify_dir(out, args.commit, repo, args.tag)
    print(sums, end="")
    return 0


# ----------------------------------------------------------------------- verify

def safe_members(tar, prefix):
    members = {}
    for m in tar.getmembers():
        if m.name == "pax_global_header" or m.type == tarfile.XGLTYPE:
            continue
        if m.isdir() and m.name.rstrip("/") + "/" == prefix:
            continue
        require(m.name.startswith(prefix), f"tar member outside prefix: {m.name}")
        rel = m.name[len(prefix):]
        if m.isdir():
            continue
        require(m.isfile(), f"tar member is not a regular file: {m.name}")
        require(rel and not rel.startswith("/") and ".." not in Path(rel).parts,
                f"unsafe tar member path: {m.name}")
        members[rel] = m
    return members


def verify_dir(directory, commit, repo, tag=None, on_ref=None):
    """Verify release files against the commit's objects in the trusted ``repo``."""
    return _verify(directory, commit, repo, tag, on_ref)[0]


def _verify(directory, commit, repo, tag=None, on_ref=None):
    """Return (manifest, manifest bytes). Each release file is read exactly once."""
    directory = Path(directory)
    repo = Path(repo)
    check_commit(commit)
    try:
        git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        raise ReleaseError(f"commit {commit} is not in the trusted repository {repo}")
    if on_ref:
        try:
            git(repo, "merge-base", "--is-ancestor", commit, on_ref)
        except subprocess.CalledProcessError:
            raise ReleaseError(f"commit {commit} is not on {on_ref} in {repo}")
    try:
        sums_text = (directory / "SHA256SUMS").read_text()
        manifest_bytes = (directory / "MANIFEST.json").read_bytes()
    except FileNotFoundError:
        raise ReleaseError("MANIFEST.json and SHA256SUMS are required")
    listed = {}
    for line in sums_text.splitlines():
        digest, name = line.split("  ", 1)
        require("/" not in name, f"unexpected path in SHA256SUMS: {name}")
        listed[name] = digest
    manifest = json.loads(manifest_bytes)
    artifact = manifest["source_artifact"]
    require(set(listed) == {artifact["name"], "MANIFEST.json", "suite-report.json"},
            f"SHA256SUMS lists {sorted(listed)}")
    content = {"MANIFEST.json": manifest_bytes,
               artifact["name"]: (directory / artifact["name"]).read_bytes(),
               "suite-report.json": (directory / "suite-report.json").read_bytes()}
    for name, digest in listed.items():
        require(sha256(content[name]) == digest, f"SHA-256 mismatch for {name}")
    require(manifest.get("project") == PROJECT, "manifest is for a different project")
    require(manifest.get("commit") == commit,
            f"release is for commit {manifest.get('commit')}, expected {commit}")
    if tag is not None:
        require(manifest.get("tag") == tag, f"release tag {manifest.get('tag')} != {tag}")
        require(artifact["name"] == tarball_name(tag), "artifact name does not match the tag")
    require(artifact["sha256"] == listed[artifact["name"]], "manifest artifact digest mismatch")
    require(manifest["tested_by"]["suite_report_sha256"] == listed["suite-report.json"],
            "manifest suite report digest mismatch")
    verify_report(json.loads(content["suite-report.json"]), commit, repo)

    # Trust anchor: the commit's tree and blobs in the trusted repository.
    require(manifest.get("tree") == git(repo, "rev-parse", f"{commit}^{{tree}}"),
            "manifest tree does not match the commit")
    git_files = tracked_files(repo, commit)
    require(manifest.get("files") == git_files, "manifest file list does not match the commit")
    expected = {f["path"]: f for f in git_files}
    with tarfile.open(fileobj=io.BytesIO(content[artifact["name"]]), mode="r:") as tar:
        require(tar.pax_headers.get("comment") == commit, "tarball was not archived from this commit")
        members = safe_members(tar, artifact["prefix"])
        require(set(members) == set(expected),
                f"tarball files differ from manifest: +{sorted(set(members) - set(expected))[:5]} "
                f"-{sorted(set(expected) - set(members))[:5]}")
        for rel, member in members.items():
            entry = expected[rel]
            data = tar.extractfile(member).read()
            require(sha256(data) == entry["sha256"] and len(data) == entry["size"],
                    f"content mismatch for {rel}")
            executable = bool(member.mode & stat.S_IXUSR)
            require(executable == (entry["mode"] == "100755"), f"mode mismatch for {rel}")
    return manifest, manifest_bytes


def verify(args):
    manifest = verify_dir(args.dir, args.commit, args.repo, args.tag, args.on_ref)
    print(json.dumps({"ok": True, "commit": manifest["commit"], "tag": manifest["tag"],
                      "artifact": manifest["source_artifact"]["name"],
                      "sha256": manifest["source_artifact"]["sha256"],
                      "files": len(manifest["files"])}, indent=2))
    return 0


# ------------------------------------------------------------------------ stage

def install_problems(install, entries, allowed_extra=()):
    """(mismatched, extra) for an installed tree against the commit's file entries.

    Bytecode caches count as extra: a timestamp-valid poisoned .pyc beside an
    unchanged module would run instead of it, and caches cannot be verified
    against the commit. Staged releases are read-only, so none are written.
    """
    install = Path(install)
    listed = {entry["path"] for entry in entries} | set(allowed_extra)
    extra = sorted(rel for rel in (p.relative_to(install).as_posix() for p in install.rglob("*")
                                   if not p.is_dir() or p.is_symlink() or p.name == "__pycache__")
                   if rel not in listed)
    mismatched = []
    for entry in entries:
        path = install / entry["path"]
        if path.is_symlink() or not path.is_file() or file_sha256(path) != entry["sha256"]:
            mismatched.append(entry["path"])
        elif bool(path.stat().st_mode & stat.S_IXUSR) != (entry["mode"] == "100755"):
            mismatched.append(entry["path"])
    return mismatched, extra


def stage(args):
    manifest, manifest_bytes = _verify(args.dir, args.commit, args.repo, args.tag, args.on_ref)
    dest = Path(args.dest)
    require(not dest.exists(), f"{dest} exists; releases are staged into a new directory only")
    tmp = dest.with_name(dest.name + ".partial")
    require(not tmp.exists(), f"{tmp} exists; remove a previous failed staging first")
    # Write from the trusted repository's blobs, never by re-reading the
    # release files, and use the manifest bytes that were verified.
    blobs = {}
    entries = tracked_files(args.repo, args.commit, blobs)
    require(entries == manifest["files"], "commit files changed since verification")
    tmp.mkdir(parents=True)
    for entry in entries:
        target = tmp / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[entry["path"]])
        target.chmod(0o555 if entry["mode"] == "100755" else 0o444)
    (tmp / ".release-manifest.json").write_bytes(manifest_bytes)
    (tmp / ".release-manifest.json").chmod(0o444)
    for dirpath, _dirs, _files in os.walk(tmp, topdown=False):
        os.chmod(dirpath, 0o555)
    mismatched, extra = install_problems(tmp, entries, {".release-manifest.json"})
    require(not mismatched and not extra,
            f"staged tree does not match the commit: {mismatched[:5]} {extra[:5]}; left in {tmp}")
    tmp.rename(dest)
    print(json.dumps({"ok": True, "staged": str(dest.resolve()), "commit": manifest["commit"],
                      "tag": manifest["tag"], "files": len(entries), "activated": False}, indent=2))
    return 0


def check_install(args):
    manifest = verify_dir(args.dir, args.commit, args.repo, args.tag, args.on_ref)
    mismatched, extra = install_problems(args.install, manifest["files"], {".release-manifest.json"})
    # Any extra file (a sitecustomize.py, a .pth, a stray module, bytecode
    # caches) fails the check.
    result = {"ok": not mismatched and not extra, "install": str(Path(args.install).resolve()),
              "commit": manifest["commit"], "tag": manifest["tag"], "files": len(manifest["files"]),
              "matched": len(manifest["files"]) - len(mismatched), "mismatched": mismatched[:50],
              "extra": extra[:50]}
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify-report")
    p.add_argument("--report", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--repo", default=".")

    p = sub.add_parser("build")
    p.add_argument("--repo", default=".")
    p.add_argument("--repository", required=True, help="owner/name")
    p.add_argument("--commit", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--ci-run-id", required=True)
    p.add_argument("--ci-run-url", required=True)
    p.add_argument("--suite-report", required=True)
    p.add_argument("--out", required=True)

    for name in ("verify", "stage", "check-install"):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True, help="directory with the downloaded release assets")
        p.add_argument("--commit", required=True, help="the exact commit you expect")
        p.add_argument("--repo", required=True,
                       help="trusted git repository containing the commit (e.g. a fresh GitHub clone)")
        p.add_argument("--on-ref", help="also require the commit to be an ancestor of this ref, e.g. origin/main")
        p.add_argument("--tag")
        if name == "stage":
            p.add_argument("--dest", required=True, help="new directory to extract into")
        if name == "check-install":
            p.add_argument("--install", required=True, help="installed launcher tree to compare")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "verify-report":
            verify_report(json.loads(Path(args.report).read_text()), args.commit, Path(args.repo))
            print(json.dumps({"ok": True, "commit": args.commit}))
            return 0
        return {"build": build, "verify": verify, "stage": stage,
                "check-install": check_install}[args.cmd](args)
    except (ReleaseError, subprocess.CalledProcessError, KeyError, ValueError, OSError) as exc:
        print(f"release_tool: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
