"""Explicit test seams for the pinned host runtime (issue #80).

The launcher pins absolute host runtime paths (ZCode node/CJS, zcodex,
OpenCode). Two kinds of tests touch them:

* Argv/wiring tests assert the exact adapter shape built for those pinned
  paths and never execute them. ``pinned_runtime_present`` answers the
  presence/permission checks for exactly the pinned paths and defers every
  other path to the real filesystem, so a fail-closed test that repoints a pin
  at a missing or unreadable file still reaches the real validator. It proves
  nothing about the installed runtime.
* Host-evidence tests prove the installed runtime itself. They are marked
  ``requires_host_runtime``: they run unchanged whenever the pinned runtime
  exists, and fail (never skip) when ``LAUNCHER_REQUIRE_HOST_RUNTIME=1``.
  Only on a host without that runtime (GitHub-hosted CI) do they skip, and
  ``scripts/ci_full_suite.py`` refuses any skip outside the documented
  allowlist in ``ci/suite-contract.json``.
"""
import functools
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from launcher.launch import OPENCODE_BIN, ZCODE_CJS, ZCODE_NODE, ZCODEX_BIN

PINNED_RUNTIME_PATHS = (ZCODE_NODE, ZCODE_CJS, ZCODEX_BIN, OPENCODE_BIN)
REQUIRE_HOST_RUNTIME_ENV = "LAUNCHER_REQUIRE_HOST_RUNTIME"


def _is_pinned(path):
    try:
        return os.fspath(path) in PINNED_RUNTIME_PATHS
    except TypeError:
        return False


@contextmanager
def pinned_runtime_present():
    real_exists, real_isfile, real_access = os.path.exists, os.path.isfile, os.access

    def exists(path):
        return True if _is_pinned(path) else real_exists(path)

    def isfile(path):
        return True if _is_pinned(path) else real_isfile(path)

    def access(path, mode, *args, **kwargs):
        if _is_pinned(path):
            return True
        return real_access(path, mode, *args, **kwargs)

    with patch("os.path.exists", side_effect=exists), \
         patch("os.path.isfile", side_effect=isfile), \
         patch("os.access", side_effect=access):
        yield


def requires_host_runtime(*paths):
    """Mark a host-evidence test that needs the installed runtime at ``paths``."""
    paths = paths or PINNED_RUNTIME_PATHS

    def decorate(test):
        @functools.wraps(test)
        def wrapper(*args, **kwargs):
            missing = [p for p in paths if not os.path.isfile(p)]
            if missing and os.environ.get(REQUIRE_HOST_RUNTIME_ENV) != "1":
                raise unittest.SkipTest(
                    "host-evidence test: pinned host runtime absent (" + ", ".join(missing)
                    + "); runs on the launcher host, see ci/suite-contract.json")
            return test(*args, **kwargs)

        return wrapper

    return decorate
