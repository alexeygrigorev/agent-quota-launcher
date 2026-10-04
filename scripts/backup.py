#!/usr/bin/env python3
"""Private GitHub main mirror with actual independent checkout verification."""
import argparse,datetime,fcntl,json,pathlib,subprocess,tempfile,shutil,os
ROOT=pathlib.Path(__file__).resolve().parents[1]
def call(args,**kw):
    p=subprocess.run(args,capture_output=True,text=True,timeout=120,**kw)
    if p.returncode:raise RuntimeError(f'{args[:2]} failed (exit {p.returncode})')
    return p.stdout.strip()
def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('--repo',default='alexeygrigorev/agent-quota-launcher');a=p.parse_args()
    local=ROOT/'.local';local.mkdir(exist_ok=True,mode=0o700)
    if shutil.disk_usage(local).free < 50*1024**3 + 512*1024**2:
        raise RuntimeError('backup restore requires 50GiB free plus 512MiB spike')
    lock=open(local/'git.lock','a');fcntl.flock(lock,fcntl.LOCK_EX)
    if call(['git','branch','--show-current'],cwd=ROOT)!='main':raise RuntimeError('backup only main')
    sha=call(['git','rev-parse','HEAD'],cwd=ROOT)
    r=subprocess.run(['gh','repo','view',a.repo,'--json','isPrivate,url'],capture_output=True,text=True,timeout=30)
    if r.returncode:
        call(['gh','repo','create',a.repo,'--private','--description','Quota-aware agent task admission and verified native execution'])
        r=subprocess.run(['gh','repo','view',a.repo,'--json','isPrivate,url'],capture_output=True,text=True,timeout=30,check=True)
    info=json.loads(r.stdout)
    if info['isPrivate'] is not True:raise RuntimeError('backup repository is not private')
    url='https://github.com/'+a.repo+'.git'
    call(['git','push',url,'HEAD:refs/heads/main'],cwd=ROOT)
    remote_sha=call(['git','ls-remote',url,'refs/heads/main']).split()[0]
    if remote_sha!=sha:raise RuntimeError('mirror ref mismatch')
    dest=pathlib.Path(tempfile.mkdtemp(prefix='restore-',dir=local))
    call(['git','clone','--quiet','--single-branch','--branch','main',url,str(dest)])
    restored=call(['git','rev-parse','HEAD'],cwd=dest)
    if restored!=sha:raise RuntimeError('restore mismatch')
    call(['git','fsck','--no-reflogs'],cwd=dest)
    result={'repo':info['url'],'private':True,'main':sha,'restored_sha':restored,'restore_path':str(dest),
            'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'fsck':'passed'}
    (local/'backup-evidence.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
if __name__=='__main__':main()
