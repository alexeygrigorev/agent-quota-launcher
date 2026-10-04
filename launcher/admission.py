import json
import subprocess
import time
from datetime import datetime, timezone

def fetch_quse():
    res = subprocess.run(["quse", "--json"], capture_output=True, text=True)
    if res.returncode != 0:
        raise ValueError("Failed to fetch quse")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        raise ValueError("Invalid quse JSON output")

def _parse_iso(s):
    if not s:
        return None
    try:
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        return datetime.fromisoformat(s)
    except ValueError:
        return None

def validate_quse(quse_data):
    if not isinstance(quse_data, dict):
        raise ValueError("Invalid quse structure")
        
    valid_routes = []
    rejections = {}
    
    now = datetime.now(timezone.utc)
    
    for name, route in quse_data.items():
        provider = name
        if provider == "gemini":
            provider = "antigravity"
            
        if provider not in ["grok", "antigravity", "zai", "codex"]:
            rejections[name] = "Unsupported route explicitly blocked"
            continue
            
        if provider != "antigravity":
            rejections[name] = "Temporarily blocked to force antigravity"
            continue
            
        if route.get("error"):
            rejections[name] = f"Error: {route.get('error')}"
            continue
            
        status = route.get("status")
        if status != "ok":
            rejections[name] = f"Status not ok: {status}"
            continue
            
        details = route.get("details", {})
        if details.get("limit_reached"):
            rejections[name] = "Limit reached"
            continue
            
        windows = route.get("windows", {})
        if not windows:
            rejections[name] = "Missing route evidence (no windows)"
            continue
            
        min_rem = 100.0
        min_hours = None
        
        exhausted = False
        valid_evidence = False
        stale_evidence = False
        
        for win_name, win in windows.items():
            perc = win.get("percent_remaining")
            reset_at_str = win.get("reset_at")
            
            if perc is None or not isinstance(perc, (int, float)):
                continue
                
            reset_time = _parse_iso(reset_at_str)
            if not reset_time:
                continue
                
            if reset_time < now:
                stale_evidence = True
                continue
                
            valid_evidence = True
                
            if provider == "codex" and perc <= 15:
                exhausted = True
            elif perc <= 0:
                exhausted = True
                
            if perc < min_rem:
                min_rem = perc
                
            hours = (reset_time - now).total_seconds() / 3600.0
            if hours < 0:
                hours = 0
            if min_hours is None or hours < min_hours:
                min_hours = hours
                
        if stale_evidence and not valid_evidence:
            rejections[name] = "Stale route evidence"
            continue
            
        if not valid_evidence:
            rejections[name] = "Missing valid route evidence (no numeric percent or valid reset_at)"
            continue
            
        if exhausted:
            rejections[name] = "Quota exhausted (or codex <= 15)"
            continue
            
        health = 1.0 if status == "ok" else 0.0
        task_fit = 1.0 
        promo = 1.0
        
        valid_routes.append({
            "name": name,
            "provider": provider,
            "health": health,
            "remaining_fraction": min_rem / 100.0,
            "hours_to_reset": min_hours,
            "task_fit": task_fit,
            "promo_multiplier": promo
        })
        
    return valid_routes, rejections
