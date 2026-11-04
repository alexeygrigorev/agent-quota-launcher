"""Private central provider homes; controller admission never enters native metadata."""
import hashlib
import base64
import datetime
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

PATHS = {'codex': '.codex/auth.json', 'claude': '.claude/.credentials.json',
         'go': '.local/share/opencode-hub/device-auth.json', 'zai': '.config/goz/config.json'}
SDK_VERSIONS = {'codex': '0.166.0', 'claude': '2.1.289', 'go': '2.0.32'}


def sdk_version(provider):
    binary = shutil.which('opencode' if provider == 'go' else provider)
    if not binary:
        raise ValueError('provider_sdk_unavailable')
    result = subprocess.run([binary, '--version'], text=True, capture_output=True, timeout=5, check=False)
    observed = re.search(r'(?<![0-9.])' + re.escape(SDK_VERSIONS[provider]) + r'(?![0-9.])', result.stdout)
    if result.returncode or len(result.stdout) > 4096 or not observed:
        raise ValueError('provider_sdk_refresh_policy_unverified')
    return SDK_VERSIONS[provider]


def make_private(path):
    if os.name == 'nt':
        user = subprocess.check_output(['whoami'], text=True).strip()
        permission = '(OI)(CI)F' if Path(path).is_dir() else 'F'
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', user + ':' + permission],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    else:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)


def private_read(path, limit=131072):
    path = Path(path)
    if os.name == 'nt':
        from launcher.win_trust import read_security, current_user_sid, TRUSTED_OWNER_SIDS
        owner, aces = read_security(path)
        allowed = {current_user_sid(), *TRUSTED_OWNER_SIDS}
        if owner not in allowed or aces is None or any(kind not in {1, 6, 10, 12} and not flags & 8 and mask & 0x80000001 and sid not in allowed for sid, kind, flags, mask in aces):
            raise ValueError('provider_file_not_private')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('provider_file_invalid')
        if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('provider_file_not_private')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('provider_file_too_large')
    return data


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def access_only(provider, value):
    """Preserve actual expiry; ordinary SDKs never receive a rotatable token.

    Installed Codex 0.166 refreshes at last_refresh + eight days. Claude
    2.1.289 and Go 2.0.32 refresh within five minutes of access expiry.
    """
    value = json.loads(json.dumps(value))
    proactive = None
    if provider == 'codex':
        token = value['tokens']['access_token']
        claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
        expires = claims['exp'] * 1000
        refreshed = datetime.datetime.fromisoformat(value['last_refresh'].replace('Z', '+00:00'))
        if refreshed.tzinfo is None:
            raise ValueError('provider_refresh_time_unknown')
        if refreshed.timestamp() * 1000 > time.time() * 1000 + 5000:
            raise ValueError('provider_refresh_time_invalid')
        proactive = refreshed.timestamp() * 1000 + 8 * 86400000
    elif provider == 'claude':
        expires = value['claudeAiOauth']['expiresAt']
    else:
        active = [row for row in value if row.get('active') is True]
        if len(active) != 1:
            raise ValueError('provider_active_identity_ambiguous')
        expires = active[0]['value']['expires']
    if type(expires) not in (int, float) or not 0 < expires < 10**14:
        raise ValueError('provider_access_expiry_unknown')
    if proactive is None:
        proactive = expires - 300000
    def strip(item):
        if isinstance(item, list):
            return [strip(entry) for entry in item]
        if isinstance(item, dict):
            return {key: ('' if key in {'refresh', 'refresh_token', 'refreshToken', 'idpRefreshToken'} else strip(entry)) for key, entry in item.items()}
        return item
    stripped = strip(value)
    if provider == 'codex':
        stripped['tokens']['refresh_token'] = ''
    elif provider == 'go':
        for row in stripped:
            row['value']['refresh'] = ''
    return stripped, expires, proactive


def require_lifetime(data, timeout):
    if data['provider'] == 'zai':
        return
    # Sixty seconds is the explicit controller clock/dispatch safety margin,
    # in addition to the SDK's independently recorded proactive cutoff.
    if type(timeout) not in (int, float) or not 1 <= timeout <= 7200 or min(data['access_expires_at'], data['proactive_refresh_at']) <= time.time() * 1000 + (timeout + 60) * 1000:
        raise ValueError('provider_access_lifetime_insufficient')


def token_for(provider, value):
    if provider == 'codex':
        token = value['tokens']['access_token']
    elif provider == 'claude':
        token = value['claudeAiOauth']['accessToken']
    elif provider == 'go':
        active = [row for row in value if row.get('active') is True]
        if len(active) != 1:
            raise ValueError('provider_active_identity_ambiguous')
        token = active[0]['value']['access']
    else:
        token = value['zai_token']
        if value.get('zai_base_url', 'https://api.z.ai/api/anthropic') != 'https://api.z.ai/api/anthropic':
            raise ValueError('provider_origin_untrusted')
    if not isinstance(token, str) or not token or len(token) > 16000 or token.startswith('enc:') or any(c.isspace() for c in token):
        raise ValueError('provider_token_invalid')
    return token


def zcode_files(token, model):
    # SDK schema identifiers are not Hub account labels. The native model comes
    # from the configured policy, never a fallback/default model.
    provider = 'zai-coding-plan'
    base = 'https://api.z.ai/api/anthropic'
    native = model
    config = {'schemaVersion': 1, 'config': {
        'providerConfigRules': {'providerRules': [{'providerId': provider, 'providerName': 'Z.AI Coding Plan',
            'config': {'group': 'standard-personal', 'access': {'type': 'api-key', 'apiKey': token},
                       'api': {'type': 'anthropic-messages', 'baseUrl': base},
                       'personalModelIds': [native], 'modelOrder': [native]}}]},
        'modelConfigRules': {'providerModelRules': [{'modelId': native, 'providerId': provider,
            'config': {'properties': {'contextWindow': 200000}}}], 'manualProviderModelRules': []},
        'defaultModelSelection': {'providerId': provider, 'modelId': native}}}
    cli = {'model': 'zai/' + model, 'provider': {provider: {'name': 'Z.AI Coding Plan', 'kind': 'anthropic',
        'source': 'custom', 'options': {'apiKey': token, 'baseURL': base, 'apiKeyRequired': True},
        'models': {native: {'limit': {'context': 200000}, 'modalities': {'input': ['text'], 'output': ['text']}}}},
        'zai': {'models': {model: {'name': model}}}}}
    harness = ('approval_policy="never"\nsandbox_mode="danger-full-access"\ncheck_for_update_on_startup=false\n'
               'model=' + json.dumps(model) + '\nmodel_provider="zcode"\n'
               '[model_providers.zcode]\nname="zcode"\nbase_url="https://api.z.ai/api/anthropic"\nwire_api="zcode"\n')
    return {'.zcode/v2/provider_config.json': json.dumps(config), '.zcode/cli/config.json': json.dumps(cli),
            '.zcodex/config.toml': harness}


def selected_token(provider, home, model):
    source = PATHS[provider]
    if provider == 'zai':
        harness=tomllib.loads(private_read(home / '.zcodex/config.toml').decode('utf8'))
        route=harness.get('model_providers',{}).get('zcode',{})
        if harness.get('model_provider')!='zcode' or harness.get('model')!=model or route.get('wire_api')!='zcode' or route.get('base_url')!='https://api.z.ai/api/anthropic':
            raise ValueError('provider_harness_route_mismatch')
        source = '.zcode/v2/provider_config.json'
        data = json.loads(private_read(home / source))['config']
        selection = data['defaultModelSelection']
        rules = data['providerConfigRules']['providerRules']
        matches = [row for row in rules if row['providerId'] == selection['providerId']]
        if len(matches) != 1 or selection['modelId'].lower() != model.lower():
            raise ValueError('provider_sdk_selection_mismatch')
        rule = matches[0]['config']
        if rule['api']['baseUrl'] != 'https://api.z.ai/api/anthropic':
            raise ValueError('provider_sdk_origin_mismatch')
        token = rule['access']['apiKey']
    elif provider == 'go':
        source = '.local/share/opencode/opencode.db'
        exported = go_sdk(home, ['auth', 'export', 'opencode'])
        if exported != access_only(provider, exported)[0]:
            raise ValueError('provider_refresh_material_forbidden')
        token = token_for(provider, exported)
    else:
        token = token_for(provider, json.loads(private_read(home / source)))
    return source, fingerprint(token)


def materialize(root, account, provider, generation, files, model, timeout_seconds=7200):
    if provider not in PATHS or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', account):
        raise ValueError('provider_identity_invalid')
    if provider == 'zai' and account != 'zai-central':
        raise ValueError('zai_central_required')
    if type(generation) is not int or generation < 1 or set(files) != {PATHS[provider]}:
        raise ValueError('provider_generation_or_schema_invalid')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,120}', model):
        raise ValueError('provider_model_required')
    text = files[PATHS[provider]]
    if not isinstance(text, str) or not 0 < len(text) <= 16000:
        raise ValueError('provider_bundle_invalid')
    token = token_for(provider, json.loads(text))
    bounds = {}
    if provider != 'zai':
        native, expires, proactive = access_only(provider, json.loads(text))
        bounds = {'access_expires_at': expires, 'proactive_refresh_at': proactive, 'access_only': True, 'sdk_version': sdk_version(provider)}
        require_lifetime({'provider': provider, **bounds}, timeout_seconds)
        files = {PATHS[provider]: json.dumps(native)}
        text = files[PATHS[provider]]
    digest = fingerprint(token)
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    make_private(root)
    destination = root / (account + '-' + str(generation) + '-' + fingerprint(model)[:12])
    if destination.exists():
        result = verify(destination / 'manifest.json')
        if result['credential_hash'] != digest or result['model'] != model:
            raise ValueError('provider_generation_conflict')
        require_lifetime(result, timeout_seconds)
        return str(destination / 'manifest.json')
    staging = Path(tempfile.mkdtemp(prefix='.provider-', dir=root))
    make_private(staging)
    try:
        content = dict(files)
        if provider == 'zai':
            content.update(zcode_files(token, model))
        for relative, value in content.items():
            path = staging / 'home' / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for directory in [path.parent, *path.parent.parents]:
                if directory == staging:
                    break
                make_private(directory)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf8', newline='') as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            make_private(path)
        if provider == 'go':
            go_sdk(staging / 'home', ['auth', 'import'], text)
        source, actual = selected_token(provider, staging / 'home', model)
        if actual != digest:
            raise ValueError('provider_sdk_credential_mismatch')
        metadata = {'version': 1, 'account_id': account, 'provider': provider, 'generation': generation,
                    'model': model, 'source': source, 'credential_hash': digest,
                    'files': {name: fingerprint(value) for name, value in content.items()}, **bounds}
        fd = os.open(staging / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(metadata, stream)
            stream.flush()
            os.fsync(stream.fileno())
        make_private(staging / 'manifest.json')
        if os.name != 'nt':
            for directory in sorted([p for p in staging.rglob('*') if p.is_dir()], reverse=True) + [staging]:
                fd = os.open(directory, os.O_RDONLY)
                os.fsync(fd)
                os.close(fd)
        os.rename(staging, destination)
        if os.name != 'nt':
            fd = os.open(root, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return str(destination / 'manifest.json')


def verify(manifest):
    path = Path(manifest)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError('provider_manifest_untrusted')
    data = json.loads(private_read(path))
    provider = data.get('provider')
    if data.get('version') != 1 or provider not in PATHS or type(data.get('generation')) is not int or data['generation'] < 1:
        raise ValueError('provider_manifest_invalid')
    if provider == 'zai' and data['account_id'] != 'zai-central':
        raise ValueError('zai_central_required')
    expected = {PATHS[provider]} | ({'.zcode/v2/provider_config.json', '.zcode/cli/config.json', '.zcodex/config.toml'} if provider == 'zai' else set())
    if set(data['files']) != expected:
        raise ValueError('provider_manifest_schema_invalid')
    home = path.parent / 'home'
    if home.is_symlink():
        raise ValueError('provider_home_untrusted')
    for relative, digest in data['files'].items():
        target = home / relative
        if not target.resolve().is_relative_to(home.resolve()) or fingerprint(private_read(target).decode('utf8')) != digest:
            raise ValueError('provider_file_changed')
    source, digest = selected_token(provider, home, data['model'])
    if source != data['source'] or digest != data['credential_hash']:
        raise ValueError('provider_sdk_credential_mismatch')
    if provider != 'zai':
        native = json.loads(private_read(home / PATHS[provider]))
        stripped, expires, proactive = access_only(provider, native)
        if native != stripped or data.get('access_only') is not True or expires != data.get('access_expires_at') or proactive != data.get('proactive_refresh_at'):
            raise ValueError('provider_refresh_material_forbidden')
        if data.get('sdk_version') != sdk_version(provider):
            raise ValueError('provider_sdk_refresh_policy_unverified')
    return data


def admission(manifest, controller):
    data = verify(manifest)
    context = json.loads(private_read(controller))
    from urllib.parse import urlsplit
    origin = urlsplit(context['server'])
    if origin.username or origin.password or origin.query or origin.fragment or origin.path not in {'', '/'} or not (origin.scheme == 'https' or origin.scheme == 'http' and origin.hostname in {'127.0.0.1', 'localhost', '::1'}):
        raise ValueError('controller_origin_untrusted')
    binding = context['binding']
    if binding['account_id'] != data['account_id']:
        raise ValueError('provider_account_mismatch')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            raise ValueError('controller_redirect_refused')
    body = json.dumps({'operation_id': binding['operation_id'], 'epoch': binding['epoch'], 'provider_check': True}).encode()
    request = urllib.request.Request(context['server'].rstrip('/') + '/api/sessions/' + binding['session_id'] + '/admission',
        data=body, headers={'Authorization': 'Bearer ' + context['token'], 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            raw = response.read(16385)
        result = json.loads(raw)
    except Exception:
        raise ValueError('central_provider_admission_unavailable') from None
    if len(raw) > 16384 or any(result.get(key) != value for key, value in binding.items()):
        raise ValueError('central_provider_binding_mismatch')
    quota = result.get('quota', {})
    now = time.time() * 1000
    if (result.get('launch_allowed') is not True or result.get('provider') != data['provider']
            or result.get('credential_generation') != data['generation'] or result.get('model') != data['model']
            or quota.get('source') != 'provider:' + data['provider'] + ':generation:' + str(data['generation'])
            or not now - 300000 <= quota.get('captured_at', 0) <= now + 5000
            or not now < quota.get('valid_until', 0) <= quota.get('captured_at', 0) + 300000
            or type(quota.get('remaining')) not in (int, float) or not 0 < quota['remaining'] <= 100 or not math.isfinite(quota['remaining'])):
        raise ValueError('central_provider_evidence_unverified')
    deadline = result.get('runtime_deadline')
    if type(deadline) is not int or not now < deadline <= now + 7200000:
        raise ValueError('central_runtime_deadline_unverified')
    data['runtime_deadline'] = deadline
    if data['provider'] == 'codex':
        from launcher.admission import CODEX_MIN_REMAINING
        if quota['remaining'] <= CODEX_MIN_REMAINING:
            raise ValueError('central_provider_quota_floor')
    return {'provider': 'opencode' if data['provider'] == 'go' else data['provider'], 'model': data['model']}, data


def home_environment(home, provider):
    env = {'HOME': str(home), 'USERPROFILE': str(home), 'XDG_CONFIG_HOME': str(home / '.config'),
           'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_CACHE_HOME': str(home / '.cache'),
           'XDG_STATE_HOME': str(home / '.local/state'), 'APPDATA': str(home / 'AppData/Roaming'),
           'LOCALAPPDATA': str(home / 'AppData/Local'), 'CLAUDE_CONFIG_DIR': str(home / '.claude'),
           'CODEX_HOME': str(home / ('.zcodex' if provider == 'zai' else '.codex'))}
    if provider == 'zai':
        env['ZCODE_HOME'] = str(home / '.zcode')
    if provider == 'go':
        env['OPENCODE_DB'] = str(home / '.local/share/opencode/opencode.db')
    return env


def clean_provider_environment(source=None):
    prefixes = ('HUB', 'AGENT_HUB_', 'OPENCODE_', 'ANTHROPIC_', 'OPENAI_', 'CODEX_', 'CLAUDE_', 'ZAI_', 'GO_API_')
    return {key: value for key, value in (os.environ if source is None else source).items() if not key.startswith(prefixes)}


def go_sdk(home, arguments, text=None):
    env = clean_provider_environment()
    env.update(home_environment(home, 'go'))
    binary = shutil.which('opencode')
    if not binary:
        raise ValueError('provider_sdk_unavailable')
    result = subprocess.run([binary, *arguments, '--standalone'], input=text, text=True, capture_output=True,
                            env=env, timeout=20, check=False)
    if result.returncode or len(result.stdout) > 131072:
        raise ValueError('provider_sdk_auth_unverified')
    database = home / '.local/share/opencode/opencode.db'
    if database.exists():
        if database.is_symlink() or not database.resolve().is_relative_to(home.resolve()):
            raise ValueError('provider_sdk_store_untrusted')
        make_private(database)
    return json.loads(result.stdout) if text is None else None


def native_environment(manifest):
    data = verify(manifest)
    return home_environment(Path(manifest).parent / 'home', data['provider'])


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        print(json.dumps({'manifest': materialize(**request)}))
    except Exception:
        print('provider_materialization_refused', file=sys.stderr)
        sys.exit(1)
