import json
import base64
import datetime
import os
from pathlib import Path
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from launcher.provider_home import materialize, verify, admission, native_environment, make_private, go_sdk, access_only, require_lifetime


class ProviderHomes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.files = {'.config/goz/config.json': json.dumps({'zai_token': 'synthetic-central-key'})}

    def home(self, **kwargs):
        return Path(materialize(self.root, 'zai-central', 'zai', 1, kwargs.get('files', self.files), 'configured-model'))

    def test_atomic_generation_sdk_selection_and_replay(self):
        manifest = self.home()
        self.assertEqual(self.home(), manifest)
        info = verify(manifest)
        self.assertEqual(info['source'], '.zcode/v2/provider_config.json')
        self.assertEqual(info['account_id'], 'zai-central')
        native = native_environment(manifest)
        self.assertEqual(native['ZCODE_HOME'], str(manifest.parent / 'home/.zcode'))
        self.assertNotIn('synthetic-central-key', json.dumps(info) + json.dumps(native))
        harness=tomllib.loads((manifest.parent/'home/.zcodex/config.toml').read_text())
        self.assertEqual((harness['model'],harness['model_provider'],harness['model_providers']['zcode']['wire_api']),
                         ('configured-model','zcode','zcode'))
        if os.name != 'nt':
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            self.home(files={'.config/goz/config.json': json.dumps({'zai_token': 'different-key'})})

    def test_retired_accounts_encryption_origin_and_unknown_files_refused(self):
        for files in [{'.config/goz/config.json': json.dumps({'zai_token': 'enc:not-a-key'})},
                      {'.config/goz/config.json': json.dumps({'zai_token': 'token', 'zai_base_url': 'https://foreign.invalid'})},
                      {'../escape': '{}'}]:
            with self.subTest(files=list(files)), self.assertRaises(ValueError):
                self.home(files=files)
        with self.assertRaises(ValueError):
            materialize(self.root, 'shared-zai', 'zai', 1, self.files, 'configured-model')

    def test_actual_sdk_rule_tampering_and_external_symlink_refused(self):
        manifest = self.home()
        target = manifest.parent / 'home/.zcode/v2/provider_config.json'
        value = json.loads(target.read_text())
        value['config']['providerConfigRules']['providerRules'][0]['config']['access']['apiKey'] = 'wrong-selected-key'
        target.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'changed'):
            verify(manifest)
        if os.name != 'nt':
            target.unlink()
            outside = self.root / 'outside.json'
            outside.write_text('{}')
            target.symlink_to(outside)
            with self.assertRaises(ValueError):
                verify(manifest)

    def test_codex_claude_and_go_native_auth_import_export(self):
        jwt = 'e30.' + base64.urlsafe_b64encode(json.dumps({'exp': time.time() + 86400}).encode()).decode().rstrip('=') + '.synthetic'
        cases = [('codex', '.codex/auth.json', {'tokens': {'access_token': jwt, 'refresh_token': 'codex-refresh'}, 'last_refresh': datetime.datetime.now(datetime.timezone.utc).isoformat()}),
                 ('claude', '.claude/.credentials.json', {'claudeAiOauth': {'accessToken': 'claude-access', 'refreshToken': 'claude-refresh', 'expiresAt': (time.time() + 86400) * 1000}}),
                 ('go', '.local/share/opencode-hub/device-auth.json', [{'active': True, 'value': {'access': 'go-access', 'refresh': 'go-refresh', 'expires': (time.time() + 86400) * 1000}}])]
        for provider, source, value in cases:
            with self.subTest(provider=provider), patch('launcher.provider_home.sdk_version', return_value='synthetic-tested-sdk'), patch('launcher.provider_home.go_sdk', return_value=access_only(provider, value)[0]) as sdk:
                path = materialize(self.root, provider + '-central', provider, 2, {source: json.dumps(value)}, 'configured-model')
                self.assertEqual(verify(path)['provider'], provider)
                self.assertNotIn(provider + '-refresh', (Path(path).parent / 'home' / source).read_text())
                self.assertEqual(native_environment(path)['HOME'], str(Path(path).parent / 'home'))
                self.assertEqual(sdk.called, provider == 'go')
                if provider == 'go':
                    self.assertTrue(any(call.args[1] == ['auth', 'import'] for call in sdk.call_args_list))
                    self.assertTrue(any(call.args[1] == ['auth', 'export', 'opencode'] for call in sdk.call_args_list))

    def test_access_only_actual_expiry_and_proactive_cutoff_are_independent(self):
        now = time.time() * 1000
        value = {'claudeAiOauth': {'accessToken': 'access', 'refreshToken': 'rotatable-secret', 'expiresAt': now + 600000}}
        stripped, expiry, proactive = access_only('claude', value)
        self.assertEqual(stripped['claudeAiOauth']['refreshToken'], '')
        self.assertEqual(expiry, value['claudeAiOauth']['expiresAt'])
        self.assertEqual(proactive, expiry - 300000)
        with self.assertRaisesRegex(ValueError, 'lifetime'):
            require_lifetime({'provider': 'claude', 'access_expires_at': expiry, 'proactive_refresh_at': proactive}, 600)
        require_lifetime({'provider': 'claude', 'access_expires_at': expiry, 'proactive_refresh_at': proactive}, 60)
        require_lifetime({'provider': 'claude', 'access_expires_at': expiry, 'proactive_refresh_at': proactive}, 60.0)
        with self.assertRaises(ValueError):
            require_lifetime({'provider': 'claude', 'access_expires_at': expiry, 'proactive_refresh_at': proactive}, True)
        with self.assertRaises(ValueError):
            access_only('claude', {'claudeAiOauth': {'accessToken': 'access', 'expiresAt': None}})
        jwt = 'e30.' + base64.urlsafe_b64encode(json.dumps({'exp': time.time() + 86400}).encode()).decode().rstrip('=') + '.synthetic'
        old = {'tokens': {'access_token': jwt}, 'last_refresh': (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=9)).isoformat()}
        stripped, expiry, proactive = access_only('codex', old)
        self.assertGreater(expiry, now + 3600000)
        self.assertEqual(stripped['last_refresh'], old['last_refresh'])
        with self.assertRaisesRegex(ValueError, 'lifetime'):
            require_lifetime({'provider': 'codex', 'access_expires_at': expiry, 'proactive_refresh_at': proactive}, 60)

    def test_go_auth_forces_private_standalone_sdk_not_ambient_service(self):
        forbidden = {'OPENCODE_SERVER': 'https://foreign.invalid', 'HUB2_TOKEN': 'synthetic-host-secret',
                     'ANTHROPIC_API_KEY':'foreign','OPENAI_API_KEY':'foreign','CODEX_API_KEY':'foreign',
                     'CLAUDE_CODE_OAUTH_TOKEN':'foreign','OPENCODE_GO_API_KEY':'foreign','ZAI_API_KEY':'foreign',
                     'CODEX_HOME':'/foreign','ANTHROPIC_BASE_URL':'https://foreign.invalid'}
        with patch.dict(os.environ, forbidden), \
             patch('launcher.provider_home.shutil.which', return_value='installed-opencode'), \
             patch('launcher.provider_home.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '[]'
            self.assertEqual(go_sdk(self.root, ['auth', 'export', 'opencode']), [])
            self.assertEqual(run.call_args.args[0], ['installed-opencode', 'auth', 'export', 'opencode', '--standalone'])
            env = run.call_args.kwargs['env']
            self.assertNotIn('OPENCODE_SERVER', env)
            for key,value in forbidden.items():
                self.assertNotEqual(env.get(key),value)
            self.assertEqual(env['OPENCODE_DB'], str(self.root / '.local/share/opencode/opencode.db'))

    def test_actual_authenticated_http_gate_revalidates_every_attempt(self):
        manifest = self.home()
        binding = {'account_id': 'zai-central', 'session_id': 'session-1', 'operation_id': 'op-1', 'epoch': 4, 'machine_id': 'machine-1'}
        now = int(time.time() * 1000)
        response = {**binding, 'launch_allowed': True, 'provider': 'zai', 'credential_generation': 1,
                    'model': 'configured-model', 'quota': {'source': 'provider:zai:generation:1',
                    'remaining': 3, 'captured_at': now, 'valid_until': now + 300000}}
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, self.headers['Authorization'], body))
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        context = self.root / 'controller.json'
        context.write_text(json.dumps({'server': 'http://127.0.0.1:' + str(server.server_port), 'token': 'synthetic-host-token', 'binding': binding}))
        make_private(context)
        self.assertEqual(admission(manifest, context)[0], {'provider': 'zai', 'model': 'configured-model'})
        admission(manifest, context)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ('/api/sessions/session-1/admission', 'Bearer synthetic-host-token',
                                   {'operation_id': 'op-1', 'epoch': 4, 'provider_check': True}))
        for key, value in [('credential_generation', 2), ('machine_id', 'foreign'), ('model', 'foreign')]:
            previous = response[key]
            response[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                admission(manifest, context)
            response[key] = previous
        for remaining in (True,False,float('nan'),float('inf'),float('-inf')):
            response['quota']['remaining']=remaining
            with self.subTest(remaining=remaining),self.assertRaises(ValueError):admission(manifest,context)
        response['quota']['remaining']=3
        response['quota']['valid_until'] = now - 1
        with self.assertRaises(ValueError):
            admission(manifest, context)

    def test_actual_do_run_uses_central_parent_gate_and_drops_controller_before_spawn(self):
        from launcher.store import Store
        from launcher.launch import do_run
        manifest = self.home()
        root = self.root / 'workspace'
        (root / '.local').mkdir(parents=True)
        store = Store(str(root / 'tasks.sqlite'))
        payload = {'owner': 'test', 'cwd': str(root), 'goal': 'harmless synthetic objective', 'timeout': 60,
                   'mode': 'interactive', 'account_label': 'zai-central', 'account_profile': 'hub-zai-central',
                   'provider_home': str(manifest), 'model_requirements': {'models': ['configured-model'], 'providers': ['zai']}}
        store.submit_task('central-test', 'central-key', payload, [])
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'HUB2_LAUNCH_CONTEXT': '/private/controller.json', 'HUB2_TOKEN': 'synthetic-host-secret','CODEX_API_KEY':'synthetic-paid-key'}))
            gate = stack.enter_context(patch('launcher.provider_home.admission', return_value=({'provider': 'zai', 'model': 'configured-model'}, verify(manifest))))
            quse = stack.enter_context(patch('launcher.launch.fetch_quse', side_effect=AssertionError('legacy quota used')))
            legacy = stack.enter_context(patch('launcher.launch.build_managed_profile_argv', side_effect=AssertionError('legacy package used')))
            stack.enter_context(patch('launcher.launch.check_resources'))
            stack.enter_context(patch('launcher.launch.build_adapter_argv', side_effect=lambda provider, goal, model: ['native-sdk', 'exec', '--model', model, '--json', goal]))
            stack.enter_context(patch('launcher.launch.validate_first_action', return_value=True))
            popen = stack.enter_context(patch('launcher.launch.subprocess.Popen'))
            popen.return_value.returncode = 0
            def started(**kwargs):
                argv = popen.call_args.args[0]
                return json.dumps({'id': '11111111-1111-4111-8111-111111111111', 'tag': 'task-central-test',
                    'workspace': str(root), 'engine': 'codex', 'phase': 'running', 'created_at_ms': int(time.time() * 1000),
                    'worker_pid': 1234, 'command': argv[argv.index('--') + 1:]}), ''
            popen.return_value.communicate.side_effect = started
            self.assertEqual(do_run(str(root / 'tasks.sqlite'), 'central-test', str(root), None, str(root / 'lock')), 0)
            self.assertEqual(gate.call_count,2)
            gate.assert_called_with(str(manifest), '/private/controller.json')
            self.assertFalse(quse.called)
            self.assertFalse(legacy.called)
            argv = popen.call_args.args[0]
            env = popen.call_args.kwargs['env']
            self.assertIn('hub-zai-central', argv)
            self.assertIn('HOME=' + str(manifest.parent / 'home'), argv)
            self.assertNotIn('HUB2_LAUNCH_CONTEXT', env)
            self.assertNotIn('HUB2_TOKEN', env)
            self.assertNotIn('CODEX_API_KEY', env)
            self.assertNotIn('synthetic-host-secret', json.dumps(argv))


    def test_actual_remote_gate_after_resources_refuses_changed_generation_before_spawn(self):
        from launcher.store import Store
        from launcher.launch import do_run
        manifest=self.home()
        root=self.root/'late-gate';(root/'.local').mkdir(parents=True)
        now=int(time.time()*1000)
        binding={'account_id':'zai-central','session_id':'s1','operation_id':'op1','epoch':1,'machine_id':'m1'}
        response={**binding,'launch_allowed':True,'provider':'zai','credential_generation':1,'model':'configured-model',
                  'quota':{'source':'provider:zai:generation:1','remaining':3,'captured_at':now,'valid_until':now+300000}}
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                calls.append(self.path);raw=json.dumps(response).encode()
                self.send_response(200);self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        context=self.root/'late-controller.json'
        context.write_text(json.dumps({'server':'http://127.0.0.1:'+str(server.server_port),'token':'synthetic-host-token','binding':binding}))
        make_private(context)
        store=Store(str(root/'tasks.sqlite'))
        store.submit_task('late-gate','late-key',{'owner':'test','cwd':str(root),'goal':'synthetic objective','timeout':60,
            'mode':'interactive','account_label':'zai-central','account_profile':'hub-zai-central','provider_home':str(manifest),
            'model_requirements':{'models':['configured-model'],'providers':['zai']}},[])
        with patch.dict(os.environ,{'HUB2_LAUNCH_CONTEXT':str(context)}), \
             patch('launcher.launch.fetch_quse',side_effect=AssertionError('legacy quota')), \
             patch('launcher.launch.build_adapter_argv',side_effect=lambda provider,goal,model:['synthetic-sdk','exec','--model',model,'--json',goal]), \
             patch('launcher.launch.check_resources',side_effect=lambda *a,**k:response.update(credential_generation=2)), \
             patch('launcher.launch.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'central_provider_evidence_unverified'):
                do_run(str(root/'tasks.sqlite'),'late-gate',str(root),None,str(root/'lock'))
            self.assertFalse(spawn.called)
        self.assertEqual(calls,['/api/sessions/s1/admission']*2)

    def test_central_codex_dispatch_preserves_all_admission_without_unmanaged_wrapper(self):
        from launcher.store import Store
        from launcher.launch import do_run
        root=self.root/'central-codex';(root/'.local').mkdir(parents=True)
        store=Store(str(root/'store.sqlite'))
        store.submit_task('central-codex','central-key',{'owner':'test','cwd':str(root),'goal':'synthetic first action',
            'timeout':60,'provider_home':'/private/synthetic-manifest.json','account_label':'codex-central',
            'model_requirements':{'providers':['codex'],'models':['configured-model']}},[])
        with ExitStack() as stack:
            stack.enter_context(patch('launcher.provider_home.admission',return_value=({'provider':'codex','model':'configured-model'},
                {'provider':'codex','account_id':'codex-central','generation':1,'access_expires_at':(time.time()+86400)*1000,'proactive_refresh_at':(time.time()+86400)*1000})))
            stack.enter_context(patch('launcher.provider_home.native_environment',return_value={'HOME':'/private/sdk-home'}))
            stack.enter_context(patch('launcher.launch.shutil.which',return_value='/installed/codex'))
            legacy=stack.enter_context(patch('launcher.launch.build_adapter_argv',side_effect=AssertionError('unmanaged adapter selected')))
            quse=stack.enter_context(patch('launcher.launch.fetch_quse',side_effect=AssertionError('legacy quota selected')))
            resources=stack.enter_context(patch('launcher.launch.check_resources'))
            first=stack.enter_context(patch('launcher.launch.validate_first_action',return_value=True))
            popen=stack.enter_context(patch('launcher.launch.subprocess.Popen'))
            popen.return_value.returncode=0
            def started(**kwargs):
                argv=popen.call_args.args[0]
                return json.dumps({'id':'11111111-1111-4111-8111-111111111111','tag':'task-central-codex','workspace':str(root),
                    'engine':'codex','phase':'running','created_at_ms':int(time.time()*1000),'worker_pid':1234,'command':argv[argv.index('--')+1:]}),''
            popen.return_value.communicate.side_effect=started
            self.assertEqual(do_run(str(root/'store.sqlite'),'central-codex',str(root),None,str(root/'lock')),0)
            self.assertFalse(legacy.called);self.assertFalse(quse.called)
            self.assertTrue(resources.called);self.assertTrue(first.called)
            argv=popen.call_args.args[0]
            self.assertEqual(argv[argv.index('--')+1],'/installed/codex')
            self.assertIn('--model',argv)


if __name__ == '__main__':
    unittest.main()
