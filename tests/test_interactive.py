import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, closing
from unittest.mock import patch
from launcher.interactive import interactive_argv,register_native_goal,goal_store,goal_prompt,objective_hash,child_config_path


class InteractiveTests(unittest.TestCase):
    def test_exact_provider_tui_argv_preserves_admission_wrapper_and_model(self):
        codex=interactive_argv('codex',['protected-wrapper','exec','--model','configured','--json','objective'],'objective',model='configured',effort='max',with_goal=True)
        self.assertEqual(codex,['protected-wrapper','--model','configured','-c','model_reasoning_effort="xhigh"'])
        go=interactive_argv('opencode',['opencode','run','--standalone','--auto','--model','configured','objective'],'objective',model='configured')
        self.assertEqual(go,['opencode','mini','--standalone','--model','configured','--prompt','objective'])
        claude=interactive_argv('claude',['managed-python','-c','trusted-script','account','--model','configured','-p','objective'],'objective',model='configured',effort='medium')
        self.assertEqual(claude[:4],['managed-python','-c','trusted-script','account'])
        self.assertNotIn('-p',claude)
        self.assertEqual(claude[-3:],['--effort','medium','objective'])

    def test_unknown_effort_goal_provider_and_model_never_fallback(self):
        argv=['opencode','run','--auto','--model','configured','objective']
        for options in ({'effort':'max'},{'with_goal':True},{'model':'other'}):
            with self.subTest(options=options),self.assertRaises(ValueError):
                interactive_argv('opencode',argv,'objective',**{'model':'configured',**options})

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        rollout=root/'codex/sessions/2026/11/04/rollout-exact.jsonl';rollout.parent.mkdir(parents=True)
        self.at=1793790000000
        rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':'exact-thread','cwd':str(root),'timestamp':datetime.datetime.fromtimestamp(self.at/1000,datetime.timezone.utc).isoformat()}})+'\n')
        native=root/'native/native-id';native.mkdir(parents=True)
        (native/'transcript.json').write_text(json.dumps({'path':str(rollout)}))
        self.row={'id':'native-id','tag':'task-test','workspace':str(root),'engine':'codex','created_at_ms':self.at,'phase':'running','history_path':str(native/'history.jsonl')}
        self.db=root/'codex/goals_1.sqlite'
        with closing(sqlite3.connect(self.db)) as db, db:db.execute('CREATE TABLE thread_goals(thread_id TEXT PRIMARY KEY,status TEXT,objective TEXT)')
        self.calls=[]
        self.delivery={'id':'native-id','delivery':{'engine':'codex','ready':True,'landed':True,'submitted':True,'status':'submitted','sends':1,'enter_presses':1}}
        self.native_effect=True

    def runner(self,argv,**kwargs):
        self.calls.append(argv)
        if argv[1]=='status':value=self.row
        else:
            if self.native_effect:
                with closing(sqlite3.connect(self.db)) as db, db:db.execute('INSERT INTO thread_goals VALUES(?,?,?)',('exact-thread','active',argv[3].removeprefix('/goal ')))
            value=self.delivery
        return subprocess.CompletedProcess(argv,0,json.dumps(value),'')

    def register(self,**kwargs):
        return register_native_goal(native_id='native-id',tag='task-test',workspace=self.temp.name,engine='codex',created_at_ms=self.at,prompt='objective',timeout=1,runner=self.runner,**kwargs)

    def test_guarded_one_effect_requires_actual_bound_goal_sqlite_row(self):
        self.assertIsNone(goal_store(self.row))
        proof=self.register()
        self.assertEqual(proof,{'status':'active','thread_id':'exact-thread','objective_hash':objective_hash('objective')})
        self.assertEqual(sum(v[1]=='send' for v in self.calls),1)
        self.assertEqual(self.register(),proof)
        self.assertEqual(sum(v[1]=='send' for v in self.calls),1)

    def test_wrong_native_tuple_refuses_before_any_tui_effect(self):
        for key,value in [('id','foreign-id'),('tag','foreign-tag'),('workspace','/foreign'),('engine','zcodex'),('created_at_ms',self.at+1),('phase','exited')]:
            original=self.row.copy();self.row[key]=value
            with self.subTest(key=key),self.assertRaisesRegex(RuntimeError,'identity'):
                self.register()
            self.row=original
        self.assertFalse(any(v[1]=='send' for v in self.calls))

    def test_uncertain_delivery_never_retries_even_when_effect_happened(self):
        self.delivery['delivery']['landed']=False
        with self.assertRaisesRegex(RuntimeError,'delivery'):self.register()
        self.assertEqual(sum(v[1]=='send' for v in self.calls),1)

    def test_foreign_thread_row_is_not_goal_evidence(self):
        with closing(sqlite3.connect(self.db)) as db, db:db.execute('INSERT INTO thread_goals VALUES(?,?,?)',('foreign-thread','active','objective'))
        self.assertIsNone(goal_store(self.row))

    def test_existing_wrong_active_or_complete_goal_is_never_overwritten(self):
        for status in ['active','complete']:
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute('DELETE FROM thread_goals')
                db.execute('INSERT INTO thread_goals VALUES(?,?,?)',('exact-thread',status,'different objective'))
            with self.subTest(status=status),self.assertRaisesRegex(RuntimeError,'objective_conflict'):
                self.register()
        self.assertFalse(any(v[1]=='send' for v in self.calls))
        self.row['created_at_ms']+=20000
        self.assertIsNone(goal_store(self.row))

    def test_large_goal_preserves_full_private_brief_and_first_action_order(self):
        objective='full objective '+('😀'*4000)
        command=goal_prompt(objective,self.temp.name,'task-id','/private/first-action.json')
        self.assertLessEqual(len(('/goal '+command).encode('utf-16-le'))//2,4000)
        brief=next((Path(self.temp.name)/'.local/goal-briefs').glob('*.md'))
        self.assertEqual(brief.read_text(encoding="utf8"),objective)
        self.assertLess(command.index('First tool action'),command.index('Then read'))
        with self.assertRaises(FileExistsError):goal_prompt(objective,self.temp.name,'task-id','/private/first-action.json')

    def test_short_one_line_goal_keeps_fast_path(self):
        self.assertEqual(goal_prompt('short objective',self.temp.name,'unused','unused'),'short objective')
        self.assertFalse((Path(self.temp.name)/'.local/goal-briefs').exists())

    def test_short_multiline_goal_uses_exact_private_brief_and_single_line_hash(self):
        import hashlib
        import stat
        for index,objective in enumerate(['first line\nsecond line','first line\r\nsecond line','first line\rsecond line']):
            with self.subTest(objective=repr(objective)):
                command=goal_prompt(objective,self.temp.name,'multiline-'+str(index),'/private/first-action.json')
                self.assertNotIn('\n',command);self.assertNotIn('\r',command)
                self.assertLessEqual(len(('/goal '+command).encode('utf-16-le'))//2,4000)
                directory=Path(self.temp.name)/'.local/goal-briefs'
                brief=directory/(hashlib.sha256(('multiline-'+str(index)).encode()).hexdigest()+'.md')
                self.assertEqual(brief.read_bytes(),objective.encode())
                self.assertIn(hashlib.sha256(objective.encode()).hexdigest(),command)
                self.assertIn(str(brief),command)
                if os.name!='nt':
                    self.assertEqual(stat.S_IMODE(brief.stat().st_mode),0o600)
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)
                # The registered native objective is the one-line pointer;
                # its embedded raw-byte digest binds the full original brief.
                self.assertEqual(objective_hash(command),hashlib.sha256(command.strip().encode()).hexdigest())
                with closing(sqlite3.connect(self.db)) as db, db:db.execute('DELETE FROM thread_goals')
                proof=register_native_goal(native_id='native-id',tag='task-test',workspace=self.temp.name,engine='codex',created_at_ms=self.at,prompt=command,timeout=1,runner=self.runner)
                self.assertEqual(proof['objective_hash'],objective_hash(command))
                self.assertEqual(goal_store(self.row)['objective_hash'],objective_hash(command))
                with self.assertRaises(FileExistsError):
                    goal_prompt(objective,self.temp.name,'multiline-'+str(index),'/private/first-action.json')

    def test_multiline_pointer_paths_are_refused_before_brief_write(self):
        for workspace,first in [(self.temp.name,'bad\nfirst'),(self.temp.name,'bad\rfirst'),(self.temp.name+'\nworkspace','first')]:
            with self.subTest(workspace=workspace,first=first),self.assertRaisesRegex(ValueError,'pointer_not_single_line'):
                goal_prompt('short\nobjective',workspace,'path-test',first)
        self.assertFalse((Path(self.temp.name)/'.local/goal-briefs').exists())

    def test_direct_multiline_registration_refuses_before_tui_effect(self):
        for prompt in ['first\nsecond','first\r\nsecond','first\rsecond']:
            with self.subTest(prompt=repr(prompt)),self.assertRaisesRegex(ValueError,'single_line_required'):
                register_native_goal(native_id='native-id',tag='task-test',workspace=self.temp.name,engine='codex',created_at_ms=self.at,prompt=prompt,timeout=1,runner=self.runner)
        self.assertEqual(self.calls,[])


class InteractiveLifecycleTests(unittest.TestCase):
    def execute(self,with_goal=False,goal_error=None,context=False):
        from launcher.store import Store
        from launcher.launch import do_run
        import time
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            root=Path(directory);(root/'.local').mkdir()
            store=Store(str(root/'store.sqlite'))
            payload={'owner':'test','cwd':directory,'goal':'harmless objective','timeout':60,'mode':'interactive','with_goal':with_goal,'effort':'max'}
            if context:
                path=root/'private-context.json';path.write_text(json.dumps({'token':'scoped-secret-token','server':'http://127.0.0.1:1'}));path.chmod(0o600)
                payload['child_config_file']=str(path)
                payload['runtime_deadline']=int(time.time()*1000)+59000
            store.submit_task('interactive-test','test-key',payload,[])
            fetch=stack.enter_context(patch('launcher.launch.fetch_quse'))
            validate=stack.enter_context(patch('launcher.launch.validate_quse',return_value=([{'provider':'codex'}],[])))
            stack.enter_context(patch('launcher.launch.select_candidate',return_value=({'provider':'codex','model':'configured-model'},{})))
            resources=stack.enter_context(patch('launcher.launch.check_resources'))
            stack.enter_context(patch('launcher.launch.build_adapter_argv',side_effect=lambda provider,goal,model:['protected-wrapper','exec','--model',model,'--json',goal]))
            popen=stack.enter_context(patch('launcher.launch.subprocess.Popen'))
            first=stack.enter_context(patch('launcher.launch.validate_first_action',return_value=True))
            registration=stack.enter_context(patch('launcher.interactive.register_native_goal',return_value={'status':'active','thread_id':'test-thread'},side_effect=goal_error))
            popen.return_value.returncode=0
            def started(**kwargs):
                command=popen.call_args.args[0]
                return json.dumps({'id':'11111111-1111-4111-8111-111111111111','tag':'task-interactive-test','workspace':directory,'engine':'codex','phase':'running','created_at_ms':int(time.time()*1000),'deadline_ms':payload.get('runtime_deadline'),'worker_pid':1234,'command':command[command.index('--')+1:]}),''
            popen.return_value.communicate.side_effect=started
            rc=do_run(str(root/'store.sqlite'),'interactive-test',directory,None,str(root/'launch.lock'))
            state=store.get_task('interactive-test')['state']
            record=json.loads((root/'.local/launch-interactive-test.json').read_text(encoding='utf8'))
            argv=popen.call_args.args[0]
            self.assertTrue(fetch.called);self.assertTrue(validate.called);self.assertTrue(resources.called)
            return rc,state,record,argv,first.call_count,registration.call_count

    def test_actual_dispatch_transforms_tui_after_admission_and_requires_first_action(self):
        rc,state,record,argv,first,registered=self.execute()
        self.assertEqual((rc,state,first,registered),(0,'running',1,0))
        self.assertNotIn('exec',argv[argv.index('--')+1:])
        self.assertTrue(record['interactive_verified'])
        self.assertTrue(record['model_verified'])

    def test_goal_registration_is_guarded_and_uncertain_effect_retains_lease(self):
        rc,state,record,argv,first,registered=self.execute(True,RuntimeError('ambiguous effect'))
        self.assertEqual((rc,state,first,registered),(1,'launch-uncertain',0,1))
        self.assertTrue(record['goal_registration_intent'])
        self.assertEqual(record['reason'],'native_goal_registration_unverified')

    def test_actual_dispatch_exposes_only_nonsecret_private_config_path(self):
        rc,state,record,argv,first,registered=self.execute(context=True)
        self.assertEqual((rc,state,first),(0,'running',1))
        self.assertTrue(any(value.startswith('HUB2_CONFIG=') for value in argv))
        self.assertNotIn('scoped-secret-token',str(argv)+json.dumps(record))

    def test_context_path_refuses_external_symlink_or_nonprivate_file(self):
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryDirectory() as outside:
            path=Path(outside)/'private.json';path.write_text('{}');path.chmod(0o600)
            with self.assertRaises(ValueError):child_config_path(str(path),directory)
            path=Path(directory)/'private.json';path.write_text('{}');path.chmod(0o600)
            self.assertEqual(child_config_path(str(path),directory),str(path.resolve()))
            if os.name!='nt':
                path.chmod(0o644)
                with self.assertRaises(ValueError):child_config_path(str(path),directory)
