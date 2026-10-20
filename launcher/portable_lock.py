"""Portable file locking for POSIX (fcntl) and Windows (msvcrt)."""
from __future__ import annotations

import contextlib
import os
from typing import IO, Any

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


@contextlib.contextmanager
def file_lock(f: IO[Any], exclusive: bool = True):
    """Context manager for cross-platform file locking."""
    if fcntl:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(f, mode)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    elif msvcrt:
        mode = msvcrt.LK_LOCK
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            try:
                f.write(b'\0' if 'b' in getattr(f, 'mode', '') else '\0')
                f.flush()
            except Exception:
                pass
        f.seek(0)
        msvcrt.locking(f.fileno(), mode, 1)
        try:
            yield
        finally:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
    else:
        raise RuntimeError("No supported file locking mechanism (neither fcntl nor msvcrt)")
