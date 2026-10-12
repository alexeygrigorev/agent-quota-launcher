"""Linux worker PID/process-control isolation; fails before payload execution."""
import os

UNSHARE = "/usr/bin/unshare"
_BOOTSTRAP = """import os,sys
expected=sys.argv[1]
if os.getpid()!=1 or os.readlink('/proc/self/ns/pid')==expected:
    raise SystemExit('PID isolation verification failed')
if not os.path.isfile('/proc/1/stat'):
    raise SystemExit('Private proc verification failed')
os.execvp(sys.argv[2],sys.argv[2:])
"""


def wrap_command(argv, python):
    if not argv or not all(isinstance(a,str) and a for a in argv):
        raise ValueError('Nonempty argument vector required')
    if not os.path.isfile(UNSHARE) or not os.access(UNSHARE,os.X_OK):
        raise RuntimeError('PID isolation unavailable; no unsandboxed fallback')
    return [UNSHARE,'--user','--map-root-user','--pid','--fork',
            '--mount-proc','--kill-child=KILL','--',python,'-c',_BOOTSTRAP,
            os.readlink('/proc/self/ns/pid')]+list(argv)


def manager_properties(uid=None):
    uid=os.getuid() if uid is None else uid
    if isinstance(uid,bool) or not isinstance(uid,int) or uid<0:
        raise ValueError('Native numeric UID required')
    paths=[f'-/run/user/{uid}/bus', f'-/run/user/{uid}/systemd',
           f'-/run/user/{uid}/aplexer','-/run/dbus/system_bus_socket',
           '-/run/systemd/private']
    return ['-p','InaccessiblePaths='+' '.join(paths),
            '-p','NoNewPrivileges=yes','-p','ProtectControlGroups=yes']
