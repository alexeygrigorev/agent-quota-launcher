"""Task-unit quota binding for explicit native commands, separate from CLI routing.

This validates argv, not prompt text or caller-supplied provider labels. It does
not authenticate installed binaries or quota packets: the launch owner must
provide a separately qualified containment backend before acquisition or
model launch. fetch_task_unit_quota currently fails closed. Raw caller packets have no admission authority.
Opaque scripts/interpreters are deliberately unsupported. A small utility
allowlist preserves non-model task-unit probes without accepting nested exec.
Codex execution remains held: evaluating its quota is not wrapper authority.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import pwd
from pathlib import Path
import shutil

from launcher.admission import ADAPTER_SUPPORTED_MODELS, parse_iso

GO_MODEL = "opencode-go/muse-spark-1.3-contributor"
IDENTITY_UNSETS = frozenset({
    "APLEXER_SESSION_ID", "APLEXER_TAG", "APLEXER_WORKSPACE", "CLAUDE_SESSION_ID",
})
PROVIDER_ALIASES = {"gemini": "antigravity", "go": "opencode", "opencode-go": "opencode"}
QUSE_KEYS = {"claude": "claude", "opencode": "go", "codex": "codex",
             "grok": "grok", "antigravity": "gemini", "zai": "zai"}
REQUIRED_WINDOWS = {
    "claude": (("5h", "five_hour"), ("7d", "weekly", "seven_day")),
    "opencode": (("5h", "rolling"), ("7d", "weekly"), ("monthly",)),
    "codex": (("7d", "weekly"),),
}
ABSENT_OPTIONAL = {"claude": {"monthly"}, "codex": {"5h", "monthly"}}
MAX_OBSERVATION_AGE_SEC = 300


def _now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CommandIdentity:
    provider: str | None
    model: str | None


def _installed_executable(value, name, require_absolute=False):
    """Accept the executable PATH selects, rejecting lookalike caller paths."""
    if require_absolute and not Path(value).is_absolute():
        return False
    installed = shutil.which(name, path=task_unit_environment()["PATH"]) if require_absolute else shutil.which(name)
    if not installed:
        return False
    if value == name:
        return True
    return (Path(value).is_absolute()
            and Path(value).resolve() == Path(installed).resolve())


def _native_options(args, value_options, flags, print_flags=()):
    values = {}
    prompt_seen = False
    print_seen = False
    i = 0
    while i < len(args):
        token = args[i]
        if token in print_flags:
            if print_seen:
                raise ValueError("duplicate print mode")
            print_seen = True
        elif token in flags:
            if token in values:
                raise ValueError("duplicate native flag")
            values[token] = True
        elif token in value_options:
            if token in values or i + 1 >= len(args) or args[i + 1].startswith("-"):
                raise ValueError("missing/duplicate native option value")
            i += 1
            values[token] = args[i]
        elif not token.startswith("-") and i == len(args) - 1 and not prompt_seen:
            # Never inspect prompt text for provider or model identity.
            prompt_seen = True
        else:
            raise ValueError("unsupported native option or opaque command")
        i += 1
    if print_flags and not print_seen:
        raise ValueError("native command must be noninteractive print mode")
    if "--model" not in values:
        raise ValueError("explicit native model is required")
    return values


def identify_command(command_argv, provider=None, model=None):
    """Recognize only explicit installed native commands and maintained adapters.

    env may ONLY unset the named session identities. Arbitrary assignments,
    shells, timeout wrappers, Python/Node scripts and indirect launchers fail.
    Maintained adapter argv must match its template exactly, plus one prompt.
    """
    if (not isinstance(command_argv, (list, tuple)) or not command_argv
            or not all(isinstance(x, str) and x and "\0" not in x for x in command_argv)):
        raise ValueError("invalid command argv")
    argv = list(command_argv)
    if argv[0] == "/usr/bin/env":
        i = 1
        while i + 1 < len(argv) and argv[i] == "-u" and argv[i + 1] in IDENTITY_UNSETS:
            i += 2
        if i > 1:
            argv = argv[i:]
    if not argv:
        raise ValueError("missing command after env")
    if _installed_executable(argv[0], "claude", True):
        options = _native_options(
            argv[1:], {"--model", "--effort", "--permission-mode", "--output-format",
                       "--allowedTools", "--disallowedTools", "--tools", "--add-dir"},
            {"--verbose", "--no-session-persistence", "--dangerously-skip-permissions"},
            {"-p", "--print"},
        )
        actual_model = options["--model"]
        if actual_model not in {"sonnet", "opus"}:
            raise ValueError("unsupported Claude model identity")
        actual = CommandIdentity("claude", actual_model)
    elif _installed_executable(argv[0], "opencode", True):
        if len(argv) < 2 or argv[1] != "run":
            raise ValueError("OpenCode requires explicit run mode")
        options = _native_options(argv[2:], {"--model", "--format", "--dir", "--title"},
                                  {"--standalone", "--auto"})
        if (options["--model"] != GO_MODEL
                or options.get("--standalone") is not True or options.get("--auto") is not True):
            raise ValueError("unsupported OpenCode provider/model or run mode")
        actual = CommandIdentity("opencode", GO_MODEL)
    elif _installed_executable(argv[0], "codex", True):
        # No positive execution capability is granted, even with healthy quota.
        actual = CommandIdentity("codex", None)
    elif _utility_command(argv):
        actual = CommandIdentity(None, None)
    else:
        from launcher.launch import ADAPTERS
        actual = None
        for route, adapter in ADAPTERS.items():
            template = list(adapter["argv"])
            if len(argv) != len(template) + 1 or "--model" not in template:
                continue
            pos = template.index("--model") + 1
            candidate_model = argv[pos]
            template[pos] = candidate_model
            if argv[:-1] == template and candidate_model in ADAPTER_SUPPORTED_MODELS[route]:
                actual = CommandIdentity(route, candidate_model)
                break
        if actual is None:
            raise ValueError("unsupported opaque command or adapter/model identity")
    if provider is not None:
        if not isinstance(provider, str) or PROVIDER_ALIASES.get(provider, provider) != actual.provider:
            raise ValueError("declared provider does not match actual command provider")
    if model is not None and model != actual.model:
        raise ValueError("declared model does not match actual command model")
    return actual


# These would select a different credential/configuration location or backend.
# Model units explicitly unset them, matching the environment of acquisition.
ACCOUNT_ENV_KEYS = frozenset({
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
    "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR",
    "OPENCODE_GO_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE", "CLAUDE_CODE_OAUTH_CLIENT_ID",
    "ANTHROPIC_CUSTOM_HEADERS",
})


# Installed quse source inventory: claude_quota, grok_quota, gemini_quota,
# opencode_go_quota and _opencode_auth. Quota cache location/lifetime and provider
# binary/proxy/reset selectors are source identity, not harmless inherited env.
QUOTA_SOURCE_ENV_KEYS = frozenset({
    "QUSE_CACHE_DIR", "QUSE_CLAUDE_CACHE_TTL", "GROK_HOME", "GROK_CLI_CHAT_PROXY_BASE_URL",
    "GROK_RESET_COOKIE", "GROK_RESET_COOKIE_FILE", "GROK_RESET_USER_AGENT", "GROK_RESET_CURL_FILE",
    "GROK_RESET_BASE_URL", "AGY_BIN", "ANTIGRAVITY_BIN", "GEMINI_BIN",
})
QUOTA_SOURCE_PREFIXES = ("QUSE_", "GROK_", "AGY_", "ANTIGRAVITY_", "GEMINI_")
DEFAULT_UNSET_ENV_KEYS = ACCOUNT_ENV_KEYS | QUOTA_SOURCE_ENV_KEYS
QUOTA_SOURCE_POLICY = tuple((key, None) for key in sorted(DEFAULT_UNSET_ENV_KEYS))


def _is_source_selector(key):
    if not isinstance(key, str):
        raise ValueError("invalid environment selector name")
    return key in DEFAULT_UNSET_ENV_KEYS or key.startswith(QUOTA_SOURCE_PREFIXES)


def task_unit_environment():
    """One fixed HOME/USER/PATH for acquisition and model-unit execution."""
    account = pwd.getpwuid(os.getuid())
    home = str(Path(account.pw_dir).resolve())
    path_entries = [str(Path(home) / ".local/bin"),
                    str(Path(home) / ".nvm/versions/node/v24.13.1/bin"),
                    "/usr/local/bin", "/usr/bin", "/bin"]
    return {"HOME": home, "USER": account.pw_name,
            "PATH": ":".join(p for p in path_entries if os.path.isdir(p))}


def validate_command_environment(identity, extra_env=None):
    """Reject account drift before quota acquisition and again before execution.

    HOME must be the actual OS user's home, never an arbitrary inherited label.
    Non-default account/config/backend selectors are unsupported. No secrets are
    read, serialized or displayed. Saved account changes remain owner-verified.
    """
    if extra_env is not None and not isinstance(extra_env, dict):
        raise ValueError("invalid task environment")
    overrides = extra_env or {}
    env = task_unit_environment()
    inherited_home = os.environ.get("HOME", env["HOME"])
    if (not inherited_home or not Path(inherited_home).is_absolute()
            or str(Path(inherited_home).resolve()) != env["HOME"]):
        raise ValueError("unverified inherited HOME/account context")
    if any(_is_source_selector(key) or key in {"HOME", "USER", "PATH"} for key in overrides):
        raise ValueError("account/backend execution environment override unsupported")
    if any(_is_source_selector(key) for key in os.environ):
        raise ValueError("inherited account/backend configuration override unsupported")
    return env


@dataclass(frozen=True, init=False)
class QuotaAcquisition:
    """Legacy immutable receipt shape; acquisition currently unavailable.

    No public constructor/from-packet/stamping API exists. This is process-local
    provenance, not an out-of-process authentication boundary against malicious
    Python reflection, patched subprocesses or same-user binaries. It grants no
    Root namespace authority and never contains credentials/environment values.
    """
    packet_json: str
    command_argv: tuple
    identity: CommandIdentity
    started_at: datetime
    completed_at: datetime
    duration_sec: float
    account_environment: tuple
    quse_executable: str
    model_executable: str
    quota_source_environment: tuple

    def __init__(self, *args, **kwargs):
        raise TypeError("quota acquisition must come from fetch_task_unit_quota")

    @property
    def packet_sha256(self):
        return hashlib.sha256(self.packet_json.encode()).hexdigest()

    @property
    def argv_sha256(self):
        return hashlib.sha256(json.dumps(self.command_argv).encode()).hexdigest()


def _model_executable(argv):
    """Select the structural executable already accepted by identify_command."""
    i = 0
    if argv[i] == "/usr/bin/env":
        i += 1
        while i + 1 < len(argv) and argv[i] == "-u": i += 2
        while i < len(argv) and "=" in argv[i]: i += 1
    return str(Path(argv[i]).resolve())


QUOTA_LIFECYCLE_HOLD = "quota-fetch-held-no-qualified-atomic-cohort"
QUOTA_LIFECYCLE_PREREQUISITE = (
    "independently qualified kernel-enforced private process cohort with "
    "non-escapable membership and bounded whole-cohort termination/empty proof"
)


class QuotaLifecycleUnavailable(ValueError):
    """No production backend currently supplies the required lifecycle proof."""


def require_quota_lifecycle():
    """Always deny until a separately reviewed backend supplies the prerequisite.

    A directory scan, repeated scan, child exit or EOF is not atomic cohort
    evidence. No receipt, provider label, caller boolean or platform-feature
    detection grants this authority. This source installs no containment backend.
    """
    raise QuotaLifecycleUnavailable(
        f"{QUOTA_LIFECYCLE_HOLD}: {QUOTA_LIFECYCLE_PREREQUISITE}"
    )


def _collect_bounded_output(argv, *, env, cwd, timeout_sec):
    """Compatibility tombstone: never starts, scans, signals or reaps a process.

    The previous streaming/session collector is retired following S2. Its
    byte/deadline controls did not establish atomic cohort ownership/quiescence.
    """
    require_quota_lifecycle()


def fetch_task_unit_quota(command_argv, provider=None, model=None, extra_env=None, *, timeout_sec=120):
    """Validate the requested context, then refuse acquisition before subprocess.

    This API currently has no positive path. A future qualified backend must
    acquire genuine provider data and prove the whole fetch cohort terminated
    before issuing an account/argv/time-bound receipt. Supplied packets cannot
    be stamped or passed through as locally acquired evidence.
    """
    if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)) or not 0 < timeout_sec <= 120:
        raise ValueError("quota fetch timeout must be finite and <=120s")
    identity = identify_command(command_argv, provider, model)
    if identity.provider is None:
        raise ValueError("provider acquisition requires an actual model command")
    validate_command_environment(identity, extra_env)
    require_quota_lifecycle()


def validate_acquisition(receipt, identity, *, now=None, command_argv=None, extra_env=None):
    """Fail closed on missing acquisition, staleness, argv/account drift."""
    now = now or _now()
    if type(receipt) is not QuotaAcquisition:
        raise ValueError("genuine bounded quota acquisition receipt required")
    if receipt.identity != identity:
        raise ValueError("quota acquisition provider/model mismatch")
    if command_argv is not None and tuple(command_argv) != receipt.command_argv:
        raise ValueError("quota acquisition executable/argv mismatch")
    if _model_executable(receipt.command_argv) != receipt.model_executable:
        raise ValueError("quota acquisition executable target changed")
    if identify_command(receipt.command_argv) != identity:
        raise ValueError("quota acquisition executable identity changed")
    current = validate_command_environment(identity, extra_env)
    if receipt.quota_source_environment != QUOTA_SOURCE_POLICY:
        raise ValueError("quota acquisition source/default policy changed")
    if tuple(sorted(current.items())) != receipt.account_environment:
        raise ValueError("quota acquisition account/environment changed")
    for stamp in (receipt.started_at, receipt.completed_at, now):
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise ValueError("unknown quota acquisition timezone")
    if (not 0 <= (now - receipt.completed_at).total_seconds() <= MAX_OBSERVATION_AGE_SEC
            or not 0 <= (receipt.completed_at - receipt.started_at).total_seconds() <= 120
            or isinstance(receipt.duration_sec, bool) or not isinstance(receipt.duration_sec, (int, float))
            or not math.isfinite(receipt.duration_sec) or not 0 <= receipt.duration_sec <= 120):
        raise ValueError("unknown or stale quota acquisition")
    return json.loads(receipt.packet_json)


def _utility_command(argv):
    # These utilities cannot dispatch another executable through an option.
    # Do not extend to env/sh/python/node/find/xargs or arbitrary named scripts.
    name = Path(argv[0]).name
    if name in {"echo", "true", "false"}:
        return _installed_executable(argv[0], name)
    if name == "sleep" and len(argv) == 2:
        try:
            seconds = float(argv[1])
            return _installed_executable(argv[0], name) and math.isfinite(seconds) and seconds >= 0
        except ValueError:
            return False
    return False


def validate_provider_quota(quse_data, identity, *, now=None, check_capacity=False, config_dir=None,
                            command_argv=None, extra_env=None):
    """Data-only check of ALL applicable provider windows; return identity.

    This is not launch authority and does not prove process-cohort cleanup.
    Every model launch separately requires the unavailable lifecycle backend.

    quse canonical null slots represent absent plan windows only where provider
    metadata permits. Other unknown/missing/stale windows fail closed. All
    extra, data-named windows are checked as well. Explicit errors are checked
    before any absent-window handling, so present:false cannot hide an error.
    """
    now = now or _now()
    quse_data = validate_acquisition(quse_data, identity, now=now,
                                     command_argv=command_argv, extra_env=extra_env)
    if not isinstance(quse_data, dict) or identity.provider not in QUSE_KEYS:
        raise ValueError("missing actual provider quota evidence")
    key = QUSE_KEYS[identity.provider]
    route = quse_data.get(key)
    if not isinstance(route, dict):
        raise ValueError("missing actual provider route")
    _healthy_record(route, "route")
    if route.get("status") != "ok":
        raise ValueError("actual provider status is not ok")
    details = route.get("details")
    if not isinstance(details, dict):
        raise ValueError("malformed actual provider details")
    _healthy_record(details, "details")
    if details.get("limit_reached") is not False:
        raise ValueError("unknown actual provider limit status")
    for field in ("observed_at",):
        if field in details:
            observed = parse_iso(details[field])
            if observed is None or not 0 <= (now - observed).total_seconds() <= MAX_OBSERVATION_AGE_SEC:
                raise ValueError("unknown or stale actual provider observation")
    if identity.provider == "grok" and details.get("has_grok_code_access") is not True:
        raise ValueError("missing Grok entitlement")
    windows = route.get("windows")
    if not isinstance(windows, dict) or not windows:
        raise ValueError("missing actual provider windows")
    available = set()
    for name, win in windows.items():
        if not isinstance(name, str) or not name or not isinstance(win, dict):
            raise ValueError("malformed actual provider window")
        _healthy_record(win, "window")
        if "status" in win and win["status"] != "ok":
            raise ValueError("window status is not ok")
        if "present" in win and not isinstance(win["present"], bool):
            raise ValueError("invalid window present flag")
        absent = win.get("percent_remaining") is None and win.get("reset_at") is None
        if (name in ABSENT_OPTIONAL.get(identity.provider, set()) and absent
                and win.get("present") is not True):
            continue
        if win.get("present") is False:
            raise ValueError("required/applicable window absent")
        percent = win.get("percent_remaining")
        if (isinstance(percent, bool) or not isinstance(percent, (int, float))
                or not math.isfinite(percent) or not 0 <= percent <= 100):
            raise ValueError("unknown or invalid actual provider window reading")
        reset = parse_iso(win.get("reset_at"))
        if reset is None or reset <= now:
            raise ValueError("unknown timezone/reset or stale actual provider window")
        cutoff = {"codex": 15, "grok": 5}.get(identity.provider, 0)
        if percent <= cutoff:
            raise ValueError(f"actual provider window <= {cutoff}% remaining")
        available.add(name)
    if not available or any(not available.intersection(group)
                            for group in REQUIRED_WINDOWS.get(identity.provider, ())):
        raise ValueError("missing required actual provider window")
    if identity.provider == "codex":
        raise ValueError("Codex execution unsupported: qualified protected wrapper unavailable")
    if check_capacity:
        from launcher.capacity import check_provider_capacity
        allowed, reason, _ = check_provider_capacity(identity.provider, config_dir=config_dir)
        if not allowed:
            raise ValueError(f"actual provider capacity rejected: {reason}")
    return identity


def _healthy_record(record, label):
    for flag in ("error", "stale", "limit_reached", "infinite"):
        if record.get(flag):
            raise ValueError(f"actual provider {label} {flag}")
