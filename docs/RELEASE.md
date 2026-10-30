# CI, source releases and rollout (issue #80)

Three separate steps, each with its own authority:

1. **CI** (`.github/workflows/ci.yml`) runs the full suite on every push to
   `main` and on manual dispatch. It is evidence, nothing more.
2. **Source release** (`.github/workflows/release.yml`, manual) turns one
   exact CI-tested commit into an immutable GitHub release. Publication is
   gated by the `source-release` environment review.
3. **Rollout** on a host is an operator action. This kit only verifies and
   stages; it never activates, edits Hub configuration or clears a hold.

## Test contract

`ci/suite-contract.json` is the single source of truth for the suite:

| item | value |
| --- | --- |
| Python | 3.12 |
| test dependencies | `ci/requirements-test.txt`: pytest 9.1.1 and its deps, `--require-hashes` |
| agent-coordination | `alexeygrigorev/agent-coordination` at the pinned commit; `launcher.consumer_fencing` must import it (the 5 consumer-fencing tests never skip silently) |
| Agent Hub host code | `alexeygrigorev/agent-hub` `host/` at the pinned commit, passed as `AGENT_HUB_HOST_PATH` |
| discovery | `python -m pytest` from the repository root: no path, `-k`, `-m` or deselection |
| floors | at least 801 collected tests and 526 passed subtests |

`scripts/ci_full_suite.py` enforces it. It refuses to start when HOME is not
the OS account home (the account guard is exercised, never bypassed), when
live account or Hub state is visible under HOME, when a pinned checkout is at
the wrong commit or holds anything beyond its tree (modified, untracked or
ignored files, such as a `sitecustomize.py` or stale bytecode), when the
`--pydeps` target holds anything except the pinned distributions with files
matching their `RECORD` hashes (install with `--no-compile`), or when the
source tree is not exactly HEAD (modified, untracked, or ignored stray Python
files). The test process gets a
rebuilt environment (no inherited `PYTEST_ADDOPTS`, plugin autoload, provider
keys or Hub variables), an explicit git identity for fixture repositories, and
scratch under `--scratch` (`TMPDIR`, `LAUNCHER_HOST_FENCING_DIR`). It fails
on any failure or error, on a skip outside the allowlist, when discovery
shrinks below the floors, when a test comes from an untracked file, when the
suite changes the source tree, creates account/Hub state, leaves anything in
`<repo>/.local/tmp`, or grows scratch past 512 MiB. On a timeout it kills the
whole pytest process group. It writes `report.json`, `report.log` and
`report.junit.xml`.

### Host-evidence tests: two modes

Two tests prove that the **installed** pinned runtime exists on the launcher
host: `test_launch.TestAdapters::test_validate_zai_runtime_direct` (ZCode node
and CJS, zcodex) and `test_opencode_route.TestOpencodeAdapter::test_installed_runtime_is_valid`
(OpenCode). GitHub-hosted runners do not have that runtime, and placing
stand-ins there would fake the evidence. So:

* `--mode ci` (GitHub): those two tests, and only those, may skip, and only
  with the `requires_host_runtime` reason. The report lists them by name.
  **A CI run is not host validation.**
* `--mode host` (Hetzner): `LAUNCHER_REQUIRE_HOST_RUNTIME=1` and zero skips.
  This mode is required for the same commit before rollout.

The argv and wiring tests that build commands for those pinned paths use
`tests/pinned_runtime.py::pinned_runtime_present`. It reports only the exact
pinned paths as present and leaves every other path on the real filesystem,
so the tests that point a pin at a missing, unreadable or non-executable file
still reach the real validators. Production validation code is unchanged.

## Running the suite on the launcher host

```bash
SRC=/path/to/agent-quota-launcher-checkout   # clean checkout at the commit under test
DEPS=$SRC/.local/ci-deps                     # gitignored
git clone -q https://github.com/alexeygrigorev/agent-coordination "$DEPS/agent-coordination"
git -C "$DEPS/agent-coordination" checkout -q <contract agent-coordination commit>
git clone -q https://github.com/alexeygrigorev/agent-hub "$DEPS/agent-hub"
git -C "$DEPS/agent-hub" checkout -q <contract agent-hub commit>
python3 -m pip install --no-deps --require-hashes --only-binary :all: --no-compile \
    --target "$DEPS/pydeps" -r "$SRC/ci/requirements-test.txt"
systemd-run --user --scope -p CPUQuota=400% -- \
    "$SRC/scripts/host_suite_sandbox.sh" host "$SRC" "$DEPS" "$SRC/.local/suite-$(date -u +%Y%m%dT%H%M%SZ)"
```

`scripts/host_suite_sandbox.sh` uses bubblewrap. It mounts an empty tmpfs at
the account home (HOME stays the real OS path), binds the source, the pinned
dependencies and the exact pinned runtime paths read-only, makes only the
output directory and `<src>/.local/tmp` writable, and unshares the network,
PID and IPC namespaces. Live Hub/account state, quota caches and the default
host-fencing registry are not visible inside it.

## Cutting a source release

Before the first release, an operator does this once, in
Settings → Environments, with the usual repository authority:

* create the environment `source-release`, add **required reviewers**, and
  limit deployments to `main`. The publish job refuses to run when the
  environment has no required-reviewer rule;
* optionally turn on immutable releases (Settings → General → Releases).

Then:

1. Confirm that CI passed on `main` for the exact commit `C`. To re-run it:
   `gh workflow run ci.yml --ref main`.
2. Build a candidate without publishing:
   `gh workflow run release.yml --ref main -f commit=C -f tag=vX.Y.Z-rcN -f publish=false`.
   The `verify` job refuses when `C` is not on `main`, when the tag or release
   already exists, when there is no successful `ci.yml` run on `main` for
   exactly `C`, or when that run's suite report is not green for `C`'s tree
   under `C`'s own contract and requirements (any undocumented skip, failure,
   dependency drift or shrunken discovery counts). The `build` job writes the
   release files and verifies each tarball member against the commit. They are
   uploaded as the workflow artifact `source-release-<tag>`, and their
   checksums appear in the job summary.
3. Publish, with authorization: dispatch again with `-f publish=true`. A
   `source-release` reviewer approves the deployment. The job verifies the
   files again against `C`. It then reserves the tag by creating
   `refs/tags/<tag>` at exactly `C`; that call fails if the tag already exists
   or is created at the same moment, and any API error stops the job before a
   release exists. It uploads a **draft** release on that tag, downloads and
   verifies the draft, checks that the tag still points at `C`, and only then
   publishes. If a step fails after the reservation, the tag stays reserved
   with no published release, and an operator investigates before choosing a
   new tag.

Release assets:

| file | content |
| --- | --- |
| `agent-quota-launcher-<tag>.tar` | `git archive` of `C` with the prefix `agent-quota-launcher-<tag>/`. It is byte-for-byte reproducible from the commit, and its pax header names `C` |
| `MANIFEST.json` | repository, tag, commit, tree, commit date, artifact SHA-256, CI run id and URL, suite counts and skips, dependency pins, and the path, mode, size and SHA-256 of every tracked file |
| `suite-report.json` | the CI suite report for `C` |
| `SHA256SUMS` | SHA-256 of the three files above |

## Rollout kit (operator-run, no activation)

```bash
TAG=vX.Y.Z  C=<exact commit>  WORK=<new private directory>
# The trust anchor is a fresh clone from GitHub, not the release files.
git clone -q https://github.com/alexeygrigorev/agent-quota-launcher "$WORK/repo"
git -C "$WORK/repo" checkout -q "$C"
mkdir "$WORK/assets" && gh release download "$TAG" -R alexeygrigorev/agent-quota-launcher -D "$WORK/assets"
T="$WORK/repo/scripts/release_tool.py"
A=(--dir "$WORK/assets" --repo "$WORK/repo" --on-ref origin/main --commit "$C" --tag "$TAG")
python3 -I "$T" verify "${A[@]}"
# Run the host suite in --mode host on "$WORK/repo" (see above): 0 skips required.
python3 -I "$T" stage "${A[@]}" --dest "$HOME/.local/share/agent-hub/launcher-releases/$C"
python3 -I "$T" check-install "${A[@]}" --install "$HOME/.local/share/agent-hub/launcher-releases/$C"
```

`stage` writes only to a new directory, named by the full commit like the
existing `launcher-releases/<commit>` roots. After verification it writes the
commit's files from the trusted clone, never by re-reading the release files.
It writes `.release-manifest.json` from the verified manifest bytes, makes
every file and directory read-only (so Python cannot write bytecode caches
there), checks the result again, and only then renames it into place. It
refuses an existing destination. `check-install` is read-only. It proves that
an installed tree holds exactly the release's tracked files, with matching
content and executable bits, and no other file. An extra `sitecustomize.py`,
a `.pth`, or any `__pycache__` fails the check, because a poisoned cache
beside an unchanged module would run in its place and cannot be checked
against the commit. A writable root that has already run therefore fails
until its caches are removed. Use `check-install` to confirm the currently
configured root before a change and the new root after one. Every command
compares the release files against the commit's objects in `--repo`, and a
tarball, manifest and SHA256SUMS that were all rewritten consistently are
still refused.

**Activation is not part of this kit.** Pointing the Hub `launcher.root`, its
`PYTHONPATH` or any subscription at a new root, or clearing a permission
hold, needs the existing head/operator authorization. Record the
`check-install` output for the old and new roots. To roll back, point the
configuration back at the previous `launcher-releases/<commit>` root, which
staging never modifies.
