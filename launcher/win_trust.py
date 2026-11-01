"""Windows owner and DACL trust checks for AGENT_HUB_HOST_PATH (Hub intake 86254a30 AC5-8).

POSIX trusts the resolved host directory and its agent_hub_host package when
they are owned by the current user or root and are not group/world-writable.
Windows has no uid or mode bits, so the same bar is expressed with security
identifiers:

* owner: the current user, or SYSTEM / BUILTIN\\Administrators (the Windows
  counterparts of root);
* write: no access-allowed ACE that applies to the object itself may grant a
  write-class right to anyone else. A NULL DACL (everyone full access) and
  unknown allow-type ACEs fail closed. Deny ACEs never widen trust and are
  ignored; inherit-only ACEs do not apply to the object itself.

The evaluation is a pure function (testable on every platform). Only
read_security() touches Win32, and it fails closed on any API error.
"""
import ctypes as C
import os

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"
TRUSTED_OWNER_SIDS = (SYSTEM_SID, ADMINISTRATORS_SID)

ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
SYSTEM_AUDIT_ACE_TYPE = 2
SYSTEM_ALARM_ACE_TYPE = 3
DENY_TYPES = (ACCESS_DENIED_ACE_TYPE, 6, 10, 12)  # plain, object, callback, callback-object
NON_ACCESS_TYPES = (SYSTEM_AUDIT_ACE_TYPE, SYSTEM_ALARM_ACE_TYPE)
INHERIT_ONLY_ACE = 0x08

# Rights that let a principal change what code lives in the directory or who
# controls it: add file/subdirectory, append, write EA/attributes, delete
# child, delete, change DACL/owner, and the generic write/all bits.
WRITE_MASK = (0x0002 | 0x0004 | 0x0010 | 0x0040 | 0x0100 | 0x00010000
              | 0x00040000 | 0x00080000 | 0x10000000 | 0x40000000)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


class WindowsTrustUnavailable(OSError):
    """The Windows security descriptor could not be read; trust is unproven."""


def untrusted_reason(owner_sid, user_sid, aces):
    """Return why an object is untrusted, or None when it meets the bar.

    owner_sid / user_sid: string SIDs. aces: None for a NULL DACL, else an
    iterable of (sid_string, ace_type, ace_flags, access_mask).
    """
    if not owner_sid or not user_sid:
        return "owner or current user SID unavailable"
    if owner_sid != user_sid and owner_sid not in TRUSTED_OWNER_SIDS:
        return f"owned by {owner_sid}, not the current user"
    if aces is None:
        return "NULL DACL grants everyone full access"
    trusted_writers = {user_sid, owner_sid, OWNER_RIGHTS_SID, *TRUSTED_OWNER_SIDS}
    for sid, ace_type, flags, mask in aces:
        if flags & INHERIT_ONLY_ACE:
            continue
        if ace_type in DENY_TYPES or ace_type in NON_ACCESS_TYPES:
            continue
        if ace_type != ACCESS_ALLOWED_ACE_TYPE:
            if mask & WRITE_MASK:
                return f"unsupported allow ACE type {ace_type} grants write access"
            continue
        if mask & WRITE_MASK and sid not in trusted_writers:
            return f"writable by {sid}"
    return None


class _ACL(C.Structure):
    _fields_ = [("revision", C.c_ubyte), ("sbz1", C.c_ubyte), ("size", C.c_ushort),
                ("count", C.c_ushort), ("sbz2", C.c_ushort)]


class _ACE(C.Structure):
    _fields_ = [("type", C.c_ubyte), ("flags", C.c_ubyte), ("size", C.c_ushort),
                ("mask", C.c_uint32), ("sid_start", C.c_uint32)]


def _api():
    adv = C.WinDLL("advapi32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    P = C.c_void_p
    adv.GetNamedSecurityInfoW.argtypes = [C.c_wchar_p, C.c_uint32, C.c_uint32, C.POINTER(P), C.POINTER(P),
                                          C.POINTER(P), C.POINTER(P), C.POINTER(P)]
    adv.GetNamedSecurityInfoW.restype = C.c_uint32
    adv.GetAce.argtypes = [P, C.c_uint32, C.POINTER(P)]
    adv.GetAce.restype = C.c_int
    adv.ConvertSidToStringSidW.argtypes = [P, C.POINTER(C.c_wchar_p)]
    adv.ConvertSidToStringSidW.restype = C.c_int
    adv.OpenProcessToken.argtypes = [P, C.c_uint32, C.POINTER(P)]
    adv.OpenProcessToken.restype = C.c_int
    adv.GetTokenInformation.argtypes = [P, C.c_uint32, P, C.c_uint32, C.POINTER(C.c_uint32)]
    adv.GetTokenInformation.restype = C.c_int
    kernel.GetCurrentProcess.restype = P
    kernel.CloseHandle.argtypes = [P]
    kernel.LocalFree.argtypes = [P]
    kernel.LocalFree.restype = P
    kernel.GetFileAttributesW.argtypes = [C.c_wchar_p]
    kernel.GetFileAttributesW.restype = C.c_uint32
    return adv, kernel


def _sid_text(adv, kernel, sid):
    text = C.c_wchar_p()
    if not adv.ConvertSidToStringSidW(sid, C.byref(text)):
        raise WindowsTrustUnavailable(C.get_last_error(), "ConvertSidToStringSidW failed")
    try:
        return text.value
    finally:
        kernel.LocalFree(C.cast(text, C.c_void_p))


def current_user_sid():
    adv, kernel = _api()
    token = C.c_void_p()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, C.byref(token)):  # TOKEN_QUERY
        raise WindowsTrustUnavailable(C.get_last_error(), "OpenProcessToken failed")
    try:
        size = C.c_uint32()
        adv.GetTokenInformation(token, 1, None, 0, C.byref(size))  # TokenUser
        if not 0 < size.value <= 65536:
            raise WindowsTrustUnavailable(0, "TokenUser size unavailable")
        buf = C.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buf, size, C.byref(size)):
            raise WindowsTrustUnavailable(C.get_last_error(), "GetTokenInformation failed")
        sid = C.cast(buf, C.POINTER(C.c_void_p))[0]
        return _sid_text(adv, kernel, sid)
    finally:
        kernel.CloseHandle(token)


def is_reparse_point(path):
    _, kernel = _api()
    attrs = kernel.GetFileAttributesW(str(path))
    if attrs == INVALID_FILE_ATTRIBUTES:
        raise WindowsTrustUnavailable(C.get_last_error(), "GetFileAttributesW failed")
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def read_security(path):
    """(owner_sid, aces|None) for a file-system object; raises on any failure."""
    adv, kernel = _api()
    owner, dacl, sd = C.c_void_p(), C.c_void_p(), C.c_void_p()
    # SE_FILE_OBJECT=1; OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    err = adv.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4, C.byref(owner), None, C.byref(dacl), None, C.byref(sd))
    if err:
        raise WindowsTrustUnavailable(err, "GetNamedSecurityInfoW failed")
    try:
        if not owner:
            raise WindowsTrustUnavailable(0, "object has no owner")
        owner_sid = _sid_text(adv, kernel, owner)
        if not dacl:
            return owner_sid, None
        acl = C.cast(dacl, C.POINTER(_ACL)).contents
        aces = []
        for index in range(acl.count):
            ptr = C.c_void_p()
            if not adv.GetAce(dacl, index, C.byref(ptr)):
                raise WindowsTrustUnavailable(C.get_last_error(), "GetAce failed")
            ace = C.cast(ptr, C.POINTER(_ACE)).contents
            if ace.type in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                sid = _sid_text(adv, kernel, C.c_void_p(ptr.value + _ACE.sid_start.offset))
            else:
                sid = None  # object/callback ACE layouts differ; evaluated by type and mask only
            aces.append((sid, ace.type, ace.flags, ace.mask))
        return owner_sid, aces
    finally:
        if sd:
            kernel.LocalFree(sd)


def check_trusted_directory(path, label):
    """Raise ValueError-compatible reason text via return; None when trusted."""
    try:
        if is_reparse_point(path):
            return f"{label} still resolves to a reparse point (unresolvable link target): {path}"
        owner_sid, aces = read_security(path)
        reason = untrusted_reason(owner_sid, current_user_sid(), aces)
    except (OSError, ValueError) as e:
        return f"{label} security descriptor unavailable ({type(e).__name__}): {path}"
    if reason:
        return f"{label} is untrusted: {reason}: {path}"
    return None


def supported():
    return os.name == "nt"
