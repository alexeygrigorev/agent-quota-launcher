import shutil
from pathlib import Path

def get_mem_available():
    with open('/proc/meminfo') as f:
        for line in f:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    return 0

def check_resources(requested_memory_mb, requested_cwd, requested_tmpdir, active_mem_mb=0, active_disk_mb=0):
    if requested_memory_mb > 1500:
        raise ValueError("requested worker memory > 1500MiB")
        
    mem_avail = get_mem_available()
    req_mem = requested_memory_mb * 1024 * 1024
    act_mem = active_mem_mb * 1024 * 1024
    
    if mem_avail - act_mem - req_mem < 10 * 1024 * 1024 * 1024:
        raise ValueError("host MemAvailable < 10GiB")
        
    cwd_stat = shutil.disk_usage(requested_cwd)
    tmp_stat = shutil.disk_usage(requested_tmpdir)
    
    req_disk = active_disk_mb * 1024 * 1024 + 512 * 1024 * 1024
    
    if cwd_stat.free < 50 * 1024 * 1024 * 1024 + req_disk:
        raise ValueError("cwd filesystem free < 50GiB + reservations + spike")
        
    if tmp_stat.free < 50 * 1024 * 1024 * 1024 + req_disk:
        raise ValueError("tmpdir filesystem free < 50GiB + reservations + spike")
        
    tmp_path = Path(requested_tmpdir).resolve()
    if tmp_path.parts[:2] == ('/', 'tmp') or tmp_path == Path('/tmp'):
        raise ValueError("reject /tmp")
        
    if ".local/tmp" not in str(tmp_path):
        raise ValueError("TMPDIR under owned .local/tmp on root required")
        
    return True
