#!/usr/bin/env python3
"""Use an isolated instance of the existing Agent Branches local prototype.

No provider code is copied into this project. The operator supplies a pinned,
compiled upstream checkout. Secrets stay in .local, never command arguments.
"""
import argparse, datetime, fcntl, json, os, pathlib, secrets, shutil
import socket, subprocess, sys, time, urllib.request, urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
CONFIG = LOCAL / 'platform.json'

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as h:
        json.dump(value, h, indent=2)
    os.replace(tmp, path)

def run(argv, **kw):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60, **kw)
    if p.returncode:
        raise RuntimeError(f'{argv[0]} {argv[1]} failed (exit {p.returncode}); private diagnostics retained')
    return p.stdout.strip()

def api(base, token, path, body=None):
    assert base.startswith('http://127.0.0.1:')
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

def auth_env(url, token):
    # A URL-scoped environment config avoids credentials in argv/Git config.
    env = os.environ.copy()
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '1',
                'GIT_CONFIG_KEY_0': 'http.' + url + '.extraHeader',
                'GIT_CONFIG_VALUE_0': 'Authorization: Bearer ' + token})
    return env

def provision(args):
    if CONFIG.exists():
        print('already provisioned; use status')
        return
    source = pathlib.Path(args.prototype).resolve()
    assert (source / '.build/node/src/local/main.js').is_file()
    assert (source / 'local-artifacts/sidecar.mjs').is_file()
    for port in [args.port, args.port + 1]:
        with socket.socket() as s:
            s.bind(('127.0.0.1', port))
    if shutil.disk_usage(ROOT).free < 50 * 1024**3 + 512 * 1024**2:
        raise RuntimeError('root disk reserve not met')
    mem = int(next(x.split()[1] for x in pathlib.Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:'))) * 1024
    if mem - 1024**3 < 10 * 1024**3:
        raise RuntimeError('host memory reserve not met')
    cfg = {'sidecar': f'http://127.0.0.1:{args.port}', 'coordinator': f'http://127.0.0.1:{args.port + 1}',
           'sidecar_token': secrets.token_urlsafe(32), 'admin_token': secrets.token_urlsafe(32),
           'runner_token': secrets.token_urlsafe(32), 'prototype': str(source), 'source_commit': args.source_commit,
           'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    save(CONFIG, cfg)
    for mode in ['sidecar', 'coordinator']:
        cmd = ['aplexer', 'start', '--workspace', str(ROOT), '--cwd', str(ROOT), '--tag', 'quota-platform-' + mode,
               '--memory', '512M', '--pids', '50', '--json', '--', sys.executable, str(pathlib.Path(__file__).resolve()), 'serve', mode]
        data = json.loads(run(cmd))
        save(LOCAL / ('platform-' + mode + '-session.json'), {k:data.get(k) for k in ['id','tag','workspace','limits','workload_pid']})
    for _ in range(30):
        try:
            api(cfg['sidecar'],cfg['sidecar_token'],'/api/repos')
            api(cfg['coordinator'],cfg['admin_token'],'/status')
            break
        except (urllib.error.URLError, ConnectionError): time.sleep(.2)
    setup = api(cfg['coordinator'],cfg['admin_token'],'/setup',{})
    save(LOCAL / 'platform-setup.json',setup)
    print(json.dumps({'source_commit':cfg['source_commit'],'sidecar':cfg['sidecar'],'coordinator':cfg['coordinator'],'setup': 'complete'}))

def serve(mode):
    cfg = json.loads(CONFIG.read_text()); source = pathlib.Path(cfg['prototype'])
    env = os.environ.copy()
    if mode == 'sidecar':
        env.update(SIDECAR_ROOT=str(LOCAL/'platform-store'), SIDECAR_HOST='127.0.0.1',
          SIDECAR_PORT=cfg['sidecar'].rsplit(':',1)[1], SIDECAR_TOKEN=cfg['sidecar_token'],
          SIDECAR_NOTIFY_URL=cfg['coordinator']+'/events/push')
        entry = source/'local-artifacts/sidecar.mjs'
    else:
        env.update(LOCAL_ARTIFACTS_URL=cfg['sidecar'], LOCAL_ARTIFACTS_TOKEN=cfg['sidecar_token'],
          ADMIN_TOKEN=cfg['admin_token'], RUNNER_TOKEN=cfg['runner_token'], HOST='127.0.0.1',
          PORT=cfg['coordinator'].rsplit(':',1)[1], COORDINATOR_STATE_FILE=str(LOCAL/'platform-coordinator.json'))
        entry=source/'.build/node/src/local/main.js'
    os.umask(0o077)
    log=open(LOCAL/('platform-'+mode+'.log'),'a')
    os.dup2(log.fileno(),1);os.dup2(log.fileno(),2)
    os.execvpe('node',['node',str(entry)],env)

def checkpoint():
    cfg=json.loads(CONFIG.read_text())
    setup=json.loads((LOCAL/'platform-setup.json').read_text())
    # API spelling is checked here rather than silently guessing at a changed wire.
    canonical=setup['canonical']; name=canonical['name']; url=canonical['remote']
    tok=api(cfg['sidecar'],cfg['sidecar_token'],f'/api/repos/{name}/tokens',{'scope':'write','ttlSeconds':3600})['plaintext']
    env=auth_env(url,tok)
    lock=open(LOCAL/'git.lock','a');fcntl.flock(lock,fcntl.LOCK_EX)
    remotes=run(['git','remote'],cwd=ROOT).splitlines()
    if 'platform' not in remotes:run(['git','remote','add','platform',url],cwd=ROOT)
    if run(['git','remote','get-url','platform'],cwd=ROOT)!=url:raise RuntimeError('unexpected platform remote')
    run(['git','fetch','platform','main'],cwd=ROOT,env=env)
    # Initial isolated platform seed is an empty tree; merge its recoverable history.
    # Later non-fast-forward platform changes require explicit owner reconciliation.
    p=subprocess.run(['git','merge-base','HEAD','FETCH_HEAD'],cwd=ROOT,capture_output=True,text=True)
    if p.returncode:
        tree=run(['git','ls-tree','-r','FETCH_HEAD'],cwd=ROOT)
        if tree:raise RuntimeError('refuse unrelated nonempty canonical history')
        run(['git','merge','--allow-unrelated-histories','--no-edit','FETCH_HEAD'],cwd=ROOT)
    run(['git','push','platform','HEAD:main'],cwd=ROOT,env=env)
    sha=run(['git','rev-parse','HEAD'],cwd=ROOT)
    actual=api(cfg['sidecar'],cfg['sidecar_token'],f'/api/repos/{name}/head?ref=main')['sha']
    if actual!=sha:raise RuntimeError('canonical SHA mismatch')
    save(LOCAL/'platform-checkpoint.json',{'sha':sha,'canonical':name,'remote':url,'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
    print(json.dumps({'sha':sha,'canonical':name,'remote':url,'verified':True}))

def task(args):
    cfg=json.loads(CONFIG.read_text()); marker=LOCAL/('platform-task-'+args.id+'.json')
    if not args.id.replace('-','').isalnum():raise ValueError('simple task id required')
    if marker.exists():
        obj=json.loads(marker.read_text())
        if obj.get('intent')!=args.intent:raise RuntimeError('task idempotency conflict')
    else:
        # A pending marker survives ambiguous HTTP completion; no blind repeat.
        pending=marker.with_suffix('.pending')
        if pending.exists():raise RuntimeError('ambiguous task creation: reconcile /status before retry')
        save(pending,{'intent':args.intent,'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
        sha=run(['git','rev-parse','HEAD'],cwd=ROOT)
        obj=api(cfg['coordinator'],cfg['admin_token'],'/tasks',{'agent':args.id,'intent':args.intent,'base_sha':sha})
        save(marker,obj);pending.unlink()
    print(json.dumps({k:v for k,v in obj.items() if k!='token'}))

def status():
    cfg=json.loads(CONFIG.read_text());data=api(cfg['coordinator'],cfg['admin_token'],'/status')
    # State excludes task credential plaintext per the upstream API contract.
    save(LOCAL/'platform-status.json',data)
    print(json.dumps({'canonical':data.get('canonical'),'heads':data.get('heads'),'agents':data.get('agents'),'source_commit':cfg['source_commit']},indent=2))

def main():
    os.umask(0o077);LOCAL.mkdir(mode=0o700,exist_ok=True)
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd',required=True)
    s=sub.add_parser('provision');s.add_argument('--prototype',required=True);s.add_argument('--source-commit',required=True);s.add_argument('--port',type=int,default=8848)
    s=sub.add_parser('serve');s.add_argument('mode',choices=['sidecar','coordinator'])
    sub.add_parser('checkpoint');sub.add_parser('status')
    s=sub.add_parser('task');s.add_argument('id');s.add_argument('intent')
    args=p.parse_args()
    if args.cmd=='provision':provision(args)
    elif args.cmd=='serve':serve(args.mode)
    elif args.cmd=='checkpoint':checkpoint()
    elif args.cmd=='task':task(args)
    else:status()

if __name__=='__main__':main()
