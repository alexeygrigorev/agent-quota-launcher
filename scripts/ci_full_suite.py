#!/usr/bin/env python3
"""Run the full, unfiltered launcher test suite under the ci/suite-contract.json contract.

Used by .github/workflows/ci.yml and for the local launcher-host run. It never
weakens a guard: HOME must be the real OS account home (the account guard is
exercised, not bypassed), the pinned agent-coordination and Agent Hub checkouts
must be clean git checkouts at the contract commits, live account/Hub state
must not be visible under HOME, the source tree must be exactly HEAD, and the
test environment is rebuilt from an allowlist (no inherited PYTEST_ADDOPTS,
plugins, provider keys or Hub state). The run fails on any failure, error,
unexpected skip, shrunken discovery, source-tree mutation, live-state write,
leaked scratch or scratch overrun. It writes a JSON report that the release workflow verifies.

    python3 scripts/ci_full_suite.py --mode ci|host --scratch DIR \\
        --coordination DIR --agent-hub DIR --pydeps DIR --report FILE
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONTRACT = REPO / "ci" / "suite-contract.json"
SKIP_REASON = "host-evidence test: pinned host runtime absent"
ENV_PASSTHROUGH = ("LANG", "LC_ALL", "TZ", "SYSTEMROOT")
# Live account/Hub state that must not be visible to the suite at all.
LIVE_STATE = (".config/agent-hub", ".config/agent-quota-launcher", ".config/quse",
              ".cache/quse", ".local/share/agent-hub", ".local/share/agent-quota-launcher",
              ".local/state/aplexer")


class SuiteError(Exception):
    pass


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def verify_checkout(path, commit, label):
    path = Path(path).resolve()
    try:
        head = git(path, "rev-parse", "HEAD")
        # Anything beyond the pinned tree (untracked or ignored, e.g. a
        # sitecustomize.py or stale bytecode) lands on PYTHONPATH: refuse it.
        dirty = git(path, "status", "--porcelain", "--untracked-files=all", "--ignored=matching")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SuiteError(f"{label}: {path} is not a git checkout ({exc})")
    if head != commit:
        raise SuiteError(f"{label}: checkout is at {head}, contract pins {commit}")
    if dirty:
        raise SuiteError(f"{label}: checkout is not exactly the pinned tree:\n{dirty[:1000]}")
    return path


def pinned_requirements(path):
    pins = {}
    for line in Path(path).read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", line)
        if m:
            pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pins


def verify_pydeps(pydeps, requirements):
    """--target install must hold exactly the pinned distributions, byte-for-byte per RECORD."""
    pins = pinned_requirements(requirements)
    found, expected = {}, {}
    for info in pydeps.glob("*.dist-info"):
        meta = (info / "METADATA").read_text(errors="replace")
        name = re.search(r"^Name: (.+)$", meta, re.M).group(1).strip()
        version = re.search(r"^Version: (.+)$", meta, re.M).group(1).strip()
        found[re.sub(r"[-_.]+", "-", name).lower()] = version
        for row in (info / "RECORD").read_text().splitlines():
            rel, digest = row.rsplit(",", 2)[:2]
            if rel.startswith("../../bin/"):  # pip --target moves scripts to <target>/bin
                rel = "bin/" + rel[len("../../bin/"):]
            expected[rel] = digest
    if found != pins:
        raise SuiteError(f"pydeps holds {found}, requirements pin {pins}")
    for path in sorted(p for p in pydeps.rglob("*") if p.is_file() or p.is_symlink()):
        rel = path.relative_to(pydeps).as_posix()
        if rel not in expected:
            raise SuiteError(f"pydeps: {rel} is not part of a pinned distribution "
                             "(install with --no-compile into an empty --target)")
        digest = expected[rel]
        if not digest:
            if not rel.endswith(".dist-info/RECORD"):
                raise SuiteError(f"pydeps: {rel} has no RECORD hash")
            continue
        algo, _, b64 = digest.partition("=")
        actual = base64.urlsafe_b64encode(hashlib.new(algo, path.read_bytes()).digest()).rstrip(b"=")
        if path.is_symlink() or actual.decode() != b64:
            raise SuiteError(f"pydeps: {rel} does not match its RECORD hash")


def source_state():
    """HEAD, tree and anything besides the exact commit that could be imported or collected."""
    head = git(REPO, "rev-parse", "HEAD")
    tree = git(REPO, "rev-parse", "HEAD^{tree}")
    status = git(REPO, "status", "--porcelain", "--untracked-files=all", "--ignored=matching")
    stray = []
    for line in status.splitlines():
        code, path = line[:2], line[3:]
        if code != "!!":
            stray.append(line)  # modified, staged or untracked
            continue
        if path.startswith(".local/") or path.endswith("__pycache__/"):
            continue
        target = REPO / path
        if target.is_dir():
            stray += [f"!! {p.relative_to(REPO)}" for p in target.rglob("*")
                      if p.suffix in (".py", ".pth") and "__pycache__" not in p.parts]
        elif target.suffix in (".py", ".pth"):
            stray.append(line)
    return head, tree, "\n".join(stray)


def tracked_module(classname, tracked):
    parts = (classname or "").split(".")
    return any("/".join(parts[:i]) + ".py" in tracked for i in range(len(parts), 0, -1))


def repo_scratch_entries():
    # Several suites deliberately place scratch under the gitignored
    # <repo>/.local/tmp (root-disk scratch policy); they must clean it up.
    root = REPO / ".local" / "tmp"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def dir_bytes(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def build_env(home, account, scratch, coordination, hub_host, pydeps, mode):
    env = {k: os.environ[k] for k in ENV_PASSTHROUGH if k in os.environ}
    env.update({
        "HOME": home,
        "USER": account,
        "LOGNAME": account,
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "TMPDIR": str(scratch / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": os.pathsep.join([str(REPO), str(coordination), str(pydeps)]),
        "LAUNCHER_HOST_FENCING_DIR": str(scratch / "host_fencing"),
        "AGENT_HUB_HOST_PATH": str(hub_host),
        # Test fixtures create throwaway repos; give them an explicit identity
        # and keep the account's own git config out of the run.
        "GIT_CONFIG_GLOBAL": str(scratch / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    env.setdefault("LANG", "C.UTF-8")
    if mode == "host":
        env["LAUNCHER_REQUIRE_HOST_RUNTIME"] = "1"
    return env


def parse_junit(path):
    root = ET.parse(path).getroot()
    cases = []
    for case in root.iter("testcase"):
        node = f"{case.get('classname')}::{case.get('name')}"
        outcome, message = "passed", ""
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[child.tag]
                message = (child.get("message") or "")[:300]
                break
        cases.append({"node": node, "outcome": outcome, "message": message})
    return cases


def summary_counts(text):
    line = ""
    for candidate in text.strip().splitlines()[-5:]:
        if re.search(r"\bin [0-9.]+s\b", candidate):
            line = candidate.strip("= ")
    counts = {key: int(num) for num, key in re.findall(
        r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?|subtests passed|subtests failed|subtests skipped)",
        line)}
    return line, counts


def run(args):
    contract = json.loads(CONTRACT.read_text())
    deps = contract["dependencies"]
    account = pwd.getpwuid(os.getuid())
    home = os.environ.get("HOME", "")
    if not home or str(Path(home).resolve()) != str(Path(account.pw_dir).resolve()):
        raise SuiteError(
            f"HOME must be the OS account home {account.pw_dir!r} (got {home!r}); "
            "the launcher account guard is exercised, not bypassed. Use a fresh home "
            "mounted at that path for an isolated local run (docs/RELEASE.md).")

    coordination = verify_checkout(args.coordination, deps["agent-coordination"]["commit"],
                                   "agent-coordination")
    hub_root = verify_checkout(args.agent_hub, deps["agent-hub"]["commit"], "agent-hub")
    hub_host = hub_root / deps["agent-hub"]["host_subdir"]
    if not (hub_host / "agent_hub_host").is_dir():
        raise SuiteError(f"agent-hub: {hub_host}/agent_hub_host missing")
    pydeps = Path(args.pydeps).resolve()
    verify_pydeps(pydeps, REPO / contract["test_requirements"])

    scratch = Path(args.scratch).resolve()
    if scratch.exists() and any(scratch.iterdir()):
        raise SuiteError(f"scratch {scratch} must be new or empty")
    for sub in ("tmp", "host_fencing"):
        (scratch / sub).mkdir(parents=True, exist_ok=True)
    (scratch / "gitconfig").write_text(
        "[user]\n\tname = launcher-ci\n\temail = launcher-ci@invalid\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")

    head, tree, status = source_state()
    if status:
        raise SuiteError("source checkout is not exactly HEAD (modified, untracked or ignored "
                         "Python files); test an exact commit:\n" + status[:2000])
    visible = [rel for rel in LIVE_STATE if (Path(home) / rel).exists()]
    if visible:
        raise SuiteError("live account/Hub state is visible under HOME (" + ", ".join(visible)
                         + "); run in a fresh home (scripts/host_suite_sandbox.sh)")
    repo_scratch_before = repo_scratch_entries()
    env = build_env(home, account.pw_name, scratch, coordination, hub_host, pydeps, args.mode)

    probe = subprocess.run(
        [sys.executable, "-c",
         "import launcher.consumer_fencing as c, pytest, sys; "
         "assert c.HAVE_COORD_FENCING, 'agent-coordination fencing unavailable'; "
         "print(pytest.__version__, sys.version.split()[0])"],
        cwd=REPO, env=env, capture_output=True, text=True)
    if probe.returncode != 0:
        raise SuiteError("dependency preflight failed: " + (probe.stderr or probe.stdout)[-800:])
    pytest_version, python_version = probe.stdout.split()

    junit = scratch / "junit.xml"
    # Unfiltered root discovery: no path, -k, -m or deselection arguments.
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfEs",
           f"--junitxml={junit}", "-o", "junit_family=xunit2"]
    started = time.time()
    proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=contract["timeout_seconds"])
    except subprocess.TimeoutExpired:
        raise SuiteError(f"suite exceeded {contract['timeout_seconds']}s")
    finally:
        # Reap anything the suite left in its process group, on every path.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if proc.poll() is None:
            proc.wait()
    duration = round(time.time() - started, 1)
    output = stdout + stderr
    (Path(args.report).with_suffix(".log")).write_text(output)

    problems = []
    if not junit.exists():
        raise SuiteError("pytest produced no junit report:\n" + output[-2000:])
    shutil.copyfile(junit, Path(args.report).with_suffix(".junit.xml"))
    cases = parse_junit(junit)
    line, counts = summary_counts(stdout)
    failed = [c for c in cases if c["outcome"] in ("failed", "error")]
    skipped = [c for c in cases if c["outcome"] == "skipped"]
    allowed = set(contract["host_runtime_skip_allowlist"]["tests"]) if args.mode == "ci" else set()
    bad_skips = [c for c in skipped
                 if c["node"] not in allowed or SKIP_REASON not in c["message"]]

    if proc.returncode != 0:
        problems.append(f"pytest exited {proc.returncode}")
    if failed:
        problems.append(f"{len(failed)} failed/errored tests")
    tracked = set(git(REPO, "ls-files").splitlines())
    foreign = sorted({c["node"].split("::")[0] for c in cases
                      if not tracked_module(c["node"].split("::")[0], tracked)})
    if foreign:
        problems.append("collected tests outside tracked files: " + ", ".join(foreign[:10]))
    if bad_skips:
        problems.append("skips outside the documented allowlist: "
                        + ", ".join(c["node"] for c in bad_skips))
    if len(cases) < contract["min_collected_tests"]:
        problems.append(f"collected {len(cases)} < contract minimum {contract['min_collected_tests']}")
    if counts.get("subtests failed"):
        problems.append(f"{counts['subtests failed']} subtests failed")
    if counts.get("subtests passed", 0) < contract["min_passed_subtests"]:
        problems.append(f"subtests passed {counts.get('subtests passed', 0)} "
                        f"< contract minimum {contract['min_passed_subtests']}")
    if counts.get("deselected"):
        problems.append("tests were deselected")
    after = source_state()
    if after != (head, tree, ""):
        problems.append("the suite modified the tracked source tree")
    leaked_state = [rel for rel in LIVE_STATE if (Path(home) / rel).exists()]
    if leaked_state:
        problems.append("the suite created account/Hub state under HOME: " + ", ".join(leaked_state))
    leaked = sorted(set(repo_scratch_entries()) - set(repo_scratch_before))
    if leaked:
        problems.append(f"the suite left {len(leaked)} entries in <repo>/.local/tmp")
    scratch_bytes = dir_bytes(scratch)
    if scratch_bytes > contract["scratch_limit_mib"] * 1024 * 1024:
        problems.append(f"scratch grew to {scratch_bytes} bytes (> {contract['scratch_limit_mib']} MiB)")

    report = {
        "schema_version": 1,
        "mode": args.mode,
        "ok": not problems,
        "problems": problems,
        "source_commit": head,
        "source_tree": tree,
        "contract_sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        "requirements_sha256": hashlib.sha256(
            (REPO / contract["test_requirements"]).read_bytes()).hexdigest(),
        "dependencies": {name: dep["commit"] for name, dep in deps.items()},
        "python": python_version,
        "pytest": pytest_version,
        "command": " ".join(["python"] + cmd[1:]),
        "summary": line,
        "counts": {
            "collected": len(cases),
            "passed": sum(c["outcome"] == "passed" for c in cases),
            "failed": len(failed),
            "skipped": len(skipped),
            "subtests_passed": counts.get("subtests passed", 0),
            "subtests_failed": counts.get("subtests failed", 0),
        },
        "skipped": [{"node": c["node"], "reason": c["message"]} for c in skipped],
        "failed": [c["node"] for c in failed][:50],
        "duration_seconds": duration,
        "scratch_bytes": scratch_bytes,
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("ok", "source_commit", "summary", "counts", "problems")},
                     indent=2))
    return 0 if report["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("ci", "host"), required=True,
                    help="ci: GitHub-hosted runner, documented host-evidence skips allowed; "
                         "host: launcher host, zero skips and the pinned runtime required")
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--coordination", required=True)
    ap.add_argument("--agent-hub", required=True)
    ap.add_argument("--pydeps", required=True, help="directory with ci/requirements-test.txt installed")
    ap.add_argument("--report", required=True)
    args = ap.parse_args(argv)
    try:
        return run(args)
    except SuiteError as exc:
        print(f"ci_full_suite: {exc}", file=sys.stderr)
        Path(args.report).write_text(json.dumps({"schema_version": 1, "ok": False,
                                                 "problems": [str(exc)]}, indent=2) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
