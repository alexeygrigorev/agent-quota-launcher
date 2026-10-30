#!/usr/bin/env bash
# Run scripts/ci_full_suite.py on the launcher host in an isolated sandbox.
#
# The account guard requires HOME to be the OS account home, so the sandbox
# mounts a fresh empty tmpfs AT that path instead of faking HOME. Source,
# pinned dependencies and the pinned host runtime are bound read-only; only
# OUT is writable; the network is unshared. Nothing touches the live account
# home, Hub state, quota caches or the default host-fencing registry.
#
#   scripts/host_suite_sandbox.sh MODE SRC DEPS OUT
#     MODE  host (zero skips, pinned runtime bound and required) | ci
#     SRC   git checkout (or worktree) at the exact commit under test
#     DEPS  dir holding agent-coordination/, agent-hub/ (contract commits)
#           and pydeps/ (pip install --target ... -r ci/requirements-test.txt)
#     OUT   new or empty writable dir for scratch, report and log
set -euo pipefail

if [ "$#" -ne 4 ]; then
    sed -n '2,15p' "$0" >&2
    exit 64
fi
mode=$1
src=$(realpath "$2")
deps=$(realpath "$3")
mkdir -p "$4"
out=$(realpath "$4")
if [ -n "$(ls -A "$out")" ]; then
    echo "OUT $out must be new or empty" >&2
    exit 64
fi
account_home=$(getent passwd "$(id -un)" | cut -d: -f6)

args=(--ro-bind / / --dev /dev --proc /proc --tmpfs /tmp
      --tmpfs "$account_home"
      --ro-bind "$src" "$src" --ro-bind "$deps" "$deps" --bind "$out" "$out"
      --unshare-net --unshare-pid --unshare-ipc --die-with-parent --new-session)

# Suites deliberately use the gitignored <src>/.local/tmp as root-disk
# scratch; back it with a writable directory inside OUT.
mkdir -p "$out/repo-tmp" "$src/.local/tmp"
args+=(--bind "$out/repo-tmp" "$src/.local/tmp")

# A worktree's .git file points into the main repository's git dir.
common=$(git -C "$src" rev-parse --path-format=absolute --git-common-dir)
gitdir=$(git -C "$src" rev-parse --path-format=absolute --git-dir)
for d in "$common" "$gitdir"; do
    case "$d" in "$src"/*) ;; *) args+=(--ro-bind "$d" "$d") ;; esac
done

if [ "$mode" = host ]; then
    # Bind the exact pinned runtime paths launcher.launch validates (read-only).
    while IFS= read -r p; do
        [ -e "$p" ] || { echo "pinned host runtime missing: $p" >&2; exit 65; }
        case "$p" in
            "$account_home"/.nvm/versions/node/*)
                root=${p%%/bin/*}
                args+=(--ro-bind "$root" "$root") ;;
            "$account_home"/*)
                args+=(--ro-bind "$(realpath "$p")" "$p") ;;
        esac
    done < <(PYTHONPATH="$src" python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from tests.pinned_runtime import PINNED_RUNTIME_PATHS as P; print("\n".join(P))' "$src")
fi

exec bwrap "${args[@]}" --chdir "$src" --clearenv \
    --setenv HOME "$account_home" --setenv USER "$(id -un)" \
    --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv LANG C.UTF-8 \
    python3 -I "$src/scripts/ci_full_suite.py" --mode "$mode" \
        --scratch "$out/scratch" --coordination "$deps/agent-coordination" \
        --agent-hub "$deps/agent-hub" --pydeps "$deps/pydeps" \
        --report "$out/report.json"
