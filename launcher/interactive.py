"""Interactive adapter construction; admission remains in launcher.launch."""
import json


def child_config_path(value,workspace):
    if value is None:return None
    import os
    from pathlib import Path
    if not isinstance(value,str):raise ValueError('child_config_path_untrusted')
    path=Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(Path(workspace).resolve()):
        raise ValueError('child_config_path_untrusted')
    info=path.stat()
    if info.st_size>16384 or (os.name!='nt' and (info.st_uid!=os.getuid() or info.st_mode&0o077)):
        raise ValueError('child_config_not_private')
    return str(path.resolve())


def objective_hash(value):
    import hashlib
    if not isinstance(value,str) or len(value.encode())>512*1024:
        raise ValueError('native_goal_objective_invalid')
    return hashlib.sha256(value.replace('\r\n','\n').replace('\r','\n').strip().encode()).hexdigest()


def headless_effort_argv(provider, argv, effort):
    if provider in {'codex','zai'}:
        value='xhigh' if effort=='max' else effort
        if value not in {'low','medium','high','xhigh'}:raise ValueError('headless_effort_unsupported')
        flags=['-c','model_reasoning_effort='+json.dumps(value)]
    elif provider=='claude' and effort in {'low','medium','high','max'}:
        flags=['--effort',effort]
    else:raise ValueError('headless_effort_unsupported')
    return [*argv[:-1],*flags,argv[-1]]


def command_effort(command):
    if not isinstance(command,list) or not all(isinstance(v,str) for v in command):return None
    values=[command[i+1] for i,v in enumerate(command[:-1]) if v=='--effort']
    for arg in command:
        if arg.startswith('model_reasoning_effort='):
            try:values.append(json.loads(arg.split('=',1)[1]))
            except ValueError:return None
    return values[0] if values and all(isinstance(v,str) and v==values[0] for v in values) else None


def goal_prompt(prompt, workspace, task_id, first_action_path):
    if len(('/goal '+prompt).encode('utf-16-le'))//2<=4000:return prompt
    import hashlib
    import os
    from pathlib import Path
    root=Path(workspace).resolve()
    directory=root/'.local'/'goal-briefs'
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not directory.resolve().is_relative_to(root):raise ValueError('goal_brief_path_untrusted')
    path=directory/(hashlib.sha256(task_id.encode()).hexdigest()+'.md')
    data=prompt.encode()
    if len(data)>512*1024:raise ValueError('goal_brief_too_large')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    result=f'First tool action: run aplexer whoami --json and write its complete JSON to {first_action_path}. Then read and fulfill the complete objective in {path}.'
    if len(('/goal '+result).encode('utf-16-le'))//2>4000:raise ValueError('goal_brief_pointer_too_large')
    return result


def interactive_argv(provider, argv, prompt, *, model, effort=None, with_goal=False):
    if not isinstance(model, str) or not model.strip():
        raise ValueError('interactive_explicit_model_required')
    if provider not in {'codex', 'zai', 'claude', 'opencode'}:
        raise ValueError('interactive_provider_unsupported')
    if with_goal and provider not in {'codex', 'zai'}:
        raise ValueError('native_goal_provider_unsupported')
    if not argv or argv[-1] != prompt:
        raise ValueError('interactive_adapter_shape_unverified')
    result = list(argv[:-1])
    if provider in {'codex', 'zai'}:
        if 'exec' not in result or '--json' not in result:
            raise ValueError('interactive_adapter_shape_unverified')
        result.remove('exec')
        result.remove('--json')
        if effort is not None:
            mapped = 'xhigh' if effort == 'max' else effort
            if mapped not in {'low', 'medium', 'high', 'xhigh'}:
                raise ValueError('interactive_effort_unsupported')
            result += ['-c', 'model_reasoning_effort=' + json.dumps(mapped)]
        if not with_goal:
            result.append(prompt)
    elif provider == 'claude':
        if '-p' not in result:
            raise ValueError('interactive_adapter_shape_unverified')
        result.remove('-p')
        if effort is not None:
            if effort not in {'low', 'medium', 'high', 'max'}:
                raise ValueError('interactive_effort_unsupported')
            result += ['--effort', effort]
        result.append(prompt)
    else:
        if effort is not None:
            raise ValueError('interactive_effort_unsupported')
        if 'run' not in result or '--auto' not in result:
            raise ValueError('interactive_adapter_shape_unverified')
        result[result.index('run')] = 'mini'
        result.remove('--auto')
        result += ['--prompt', prompt]
    observed = [result[i+1] for i, arg in enumerate(result[:-1]) if arg in {'--model', '-m'}]
    if not observed or set(observed) != {model}:
        raise ValueError('interactive_model_unverified')
    return result


def goal_store(row, *, proc_root=None):
    """Read one exact native rollout's goal row; never newest-by-workspace."""
    import datetime
    import sqlite3
    import time
    from pathlib import Path
    candidates = set()
    history = row.get('history_path')
    if isinstance(history,str) and Path(history).is_absolute() and Path(history).parent.name==row['id']:
        try:
            with (Path(history).parent/'transcript.json').open('rb') as stream:
                bound=json.loads(stream.read(65537))
            path=Path(bound['path'])
            if path.is_absolute():candidates.add(path)
        except (OSError,ValueError,KeyError,TypeError):pass
    pid=row.get('workload_pid')
    if type(pid) is int and pid>0:
        try:
            import itertools
            for fd in itertools.islice((Path(proc_root or '/proc')/str(pid)/'fd').iterdir(),128):
                try:
                    path=fd.resolve(strict=True)
                    if path.name.startswith('rollout-') and path.suffix=='.jsonl':candidates.add(path)
                except OSError:pass
        except OSError:pass
    matches=[]
    for path in candidates:
        try:
            with path.open('rb') as stream:meta=json.loads(stream.readline(65537))
            payload=meta['payload']
            timestamp=datetime.datetime.fromisoformat(payload['timestamp'].replace('Z','+00:00')).timestamp()*1000
            if (meta.get('type')=='session_meta' and payload.get('cwd')==row['workspace']
                    and abs(timestamp-row['created_at_ms'])<=10000 and isinstance(payload.get('id'),str)
                    and len(path.parents)>4 and path.parents[3].name=='sessions'):
                matches.append((path,payload['id']))
        except (OSError,ValueError,KeyError,TypeError):pass
    if len(matches)!=1:return None
    path,thread=matches[0]
    stores=[(int(p.stem[6:]),p) for p in path.parents[4].glob('goals_*.sqlite') if p.stem[6:].isdigit()]
    if not stores:return None
    try:
        from contextlib import closing
        with closing(sqlite3.connect(max(stores)[1].as_uri()+'?mode=ro',uri=True,timeout=.05)) as db:
            deadline=time.monotonic()+.05
            db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            found=db.execute('SELECT status,objective FROM thread_goals WHERE thread_id=? AND length(CAST(objective AS BLOB))<=?',(thread,512*1024)).fetchone()
        if found and found[0] in {'active','complete','paused','blocked','stalled','usage_limited','budget_limited'}:
            return {'status':found[0],'thread_id':thread,'objective_hash':objective_hash(found[1])}
    except sqlite3.Error:pass
    return None


def register_native_goal(*, native_id, tag, workspace, engine, created_at_ms, prompt,
                         timeout, runner=None, read_goal=goal_store, sleep=None, monotonic=None):
    """One guarded slash-command effect plus authoritative exact-thread proof."""
    import subprocess
    import time
    runner=runner or subprocess.run
    sleep=sleep or time.sleep
    monotonic=monotonic or time.monotonic
    if engine not in {'codex','zcodex'} or not isinstance(prompt,str) or not prompt.strip() or timeout<=0:
        raise ValueError('native_goal_provider_unsupported')
    deadline=monotonic()+min(timeout,60)
    expected_hash=objective_hash(prompt)
    def matching(proof):
        if proof and proof.get('objective_hash')!=expected_hash:
            raise RuntimeError('native_goal_objective_conflict')
        return proof
    def call(args,limit=5):
        remaining=deadline-monotonic()
        if remaining<=0:raise RuntimeError('native_goal_registration_unverified')
        result=runner(['aplexer',*args],text=True,capture_output=True,timeout=min(limit,remaining),check=True,cwd=workspace)
        if len(result.stdout)>262144:raise RuntimeError('native_response_too_large')
        return json.loads(result.stdout)
    row=call(['status',native_id,'--json'])
    if (row.get('id')!=native_id or row.get('tag')!=tag or row.get('workspace')!=workspace
            or row.get('engine')!=engine or row.get('created_at_ms')!=created_at_ms or row.get('phase')!='running'):
        raise RuntimeError('native_goal_identity_unverified')
    proof=matching(read_goal(row))
    if proof:return proof
    receipt=call(['send',native_id,'/goal '+prompt,'--enter','--json','--wait-ack','0',
                  '--resends','0','--ready-timeout',str(min(25,max(1,int(deadline-monotonic())-1)))],30)
    delivery=receipt.get('delivery',{})
    if (receipt.get('id')!=native_id or delivery.get('engine')!=engine
            or any(delivery.get(k) is not True for k in ['ready','landed','submitted'])
            or delivery.get('status') not in {'submitted','delivered_working'}
            or type(delivery.get('sends')) is not int or delivery['sends']!=1
            or type(delivery.get('enter_presses')) is not int or not 1<=delivery['enter_presses']<=3):
        raise RuntimeError('native_goal_delivery_unverified')
    while monotonic()<deadline:
        proof=matching(read_goal(row))
        if proof:return proof
        sleep(min(.5,max(0,deadline-monotonic())))
    raise RuntimeError('native_goal_registration_unverified')
