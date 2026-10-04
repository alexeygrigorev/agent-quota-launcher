# ZAI route review — launcher admission/launch/lifecycle (bounded independent review, corrected)

Reviewer: `quota-launcher-zai-review` (`0896a5ef-b4d1-4fd8-acd0-fc4458a72d65`), 2026-10-04 ~11:45Z.
Scope: read-only review of `launcher/`, two offline no-provider repros, sha256 diff vs `.local/rejected-core-20261004/report.json`. I own only this file and `.local/zai-review-first-action.json`.

**Corrections vs my earlier native message to head (`01a106b2`):** (a) the `test_provider: "shell"` bash escape hatch existed only in the rejected snapshot (`rejected-core-20261004/launcher/launch.py:24-25,44`) — core2's R2 edit removed it; it is not an open defect. (b) First-action validation now exists (`launch.py:110-126`); the open question is that it proves identity only, not a genuine agent tool action (finding 9). Line numbers below were re-verified by a full re-read of the current tree after R2 edits.

## Ranked findings

**1. NaN/Infinity window admits a route at fabricated 100% headroom — CONFIRMED by repro.**
`admission.py:81` `isinstance(perc,(int,float))` passes NaN (Python `json.loads` parses bare `NaN`); `:94-97` `NaN <= 15` / `NaN <= 0` are False → not exhausted; `:99` `NaN < min_rem` False → `min_rem` stays initial `100.0` (`:70`); route admitted at `remaining_fraction=1.0` (`:136`). Repro output: `NaN route admitted: [('zai', 1.0, ...)]`. Fix: `math.isfinite(perc)` (and `json.loads(..., parse_constant=...)` reject).

**2. One naive `reset_at` crashes admission for ALL routes — CONFIRMED by repro.**
`_parse_iso` (`admission.py:15-23`) returns naive datetimes for offset-less strings; `:88` `reset_time < now` (aware) raises uncaught `TypeError` inside the `:77` loop, killing validation of every provider. Repro: grok `"reset_at": "2026-10-04T14:00:00"` → `TypeError: can't compare offset-naive and offset-aware datetimes`. Fix: attach UTC when naive; wrap per-route window parsing in try/except → rejection reason.

**3. No timeout is enforced; the global launch lock is held across launch + a 60s wait.**
`payload.timeout` is validated (`launch.py:46`) then never used. agy runs with `--print-timeout 0` (unbounded, `:78`); `subprocess.run` has no `timeout=` (`:98`); grok has no bound (`:80`). `cli.py:35` holds `launch.lock` exclusive across all of it, including the up-to-60s first-action poll (`:111-117`) and per-active-task `aplexer status` subprocesses (`store.py:146`) — one hang serializes all launches indefinitely. Fix: bounded child argv timeout + `subprocess.run(timeout=)`; on expiry mark `launch-uncertain`, never `failed`.

**4. `complete`/`accept` are unconditional UPDATEs.**
`cli.py:49-50,57-58` set state with no `AND state=…` guard, no rowcount check, and record no reviewer identity. `accept` on a queued/uncertain task moves it out of every active set (`store.py:69,142` exclude only `accepted`/`failed`), silently releasing path and resource leases for work that never ran. Fix: guarded transitions + rowcount + reviewer identity in the record.

**5. zai/codex pass admission but have no launch adapter — stranded `queued`.**
`admission.py:42` admits them; `launch.py:81-82` raises `ValueError("Unsupported provider")` after the quse fetch (`:49`) and resource check (`:61`). Task stays `queued` forever. Fix: restrict `valid_routes` to adapter-backed providers, or route codex via the required `scripts/launch-codex.sh`.

**6. Launching task's own 1500M is double-counted in the memory gate.**
At `launch.py:60` the task is still `queued`; `get_active_resources` (`store.py:134-163`, no `exclude_task_id` param — unlike `get_active_paths` `:64`) counts its reservation row (`:159-161`), then `resources.py:19` subtracts active + requested 1500M again → 3 GiB charged. Fail-closed false rejections near the 10 GiB floor. Fix: add and pass `exclude_task_id`.

**7. Ranking deviates from the documented formula.**
`ranking.py:13-15` bonus `1 + min(5, remaining/max(hours,0.01))` spans [1,6]; `docs/research.md` specifies `(1 + 2*urgency)` bounded [1,3] with a 0.05 headroom floor (absent here). Seed is coarse `int(time.time())` (`launch.py:54`); seed/weights/selection are never persisted. Fix: implement the documented formula; record provenance in the launch record.

**8. TMPDIR containment is a substring match.**
`resources.py:37` `".local/tmp" not in str(tmp_path)` accepts `/opt/.local/tmp` or any sibling repo. Fix: resolved-path `is_relative_to` this workspace's owned root.

**9. First-action check proves file identity, not genuine agent action.**
`validate_first_action` (`launch.py:12-25`) checks existence, mtime > launch start, and id/tag/workspace equality — content is never validated, so a launcher-supplied wrapper script that writes `.local/first-action-<id>.json` itself satisfies it. Design doc explicitly says wrapper-written files are insufficient. Fix: require evidence only the agent could produce (e.g., native session log/whoami correlation).

**10. Known first-draft items still open:** fabricated `health=1.0`/`task_fit=1.0` with deliberation comments left in production code (`admission.py:120-130` verbatim); no watcher/reaper so `launch-uncertain` leases leak (`launch.py:129,133`; nothing transitions out); `plan` is a stub (`cli.py:28-30`).

**Uncertainty:** grok argv ordering (`launch.py:80`, positional goal after flags) unverified — nothing was launched. R2 is mid-flight; line numbers are as of the ~11:45Z read.

## Native ZAI route evidence (safe, non-credential)

ZCode desktop **3.14.0** confirmed in its own startup log; `/opt/ZCode/resources/glm/zcode.cjs` present (14.8 MB, mode 755). This harness reports model `zai/glm-5.3-flash`, matching the native CLI config in `docs/research.md` — but this session is aplexer engine `shell`, not a zcodex child: configured-route corroboration only. No promotion/zero-consumption claim; conservative cutoff 2026-10-06T16:00Z; ordinary paid allowance framing.

## Verification method

Two offline no-network Python repros against `launcher.admission` (outputs above), sha256 comparison against the rejected-checkpoint manifest, and a full re-read of all six `launcher/*.py` modules at correction time. No provider contacted; no files outside my ownership modified.
