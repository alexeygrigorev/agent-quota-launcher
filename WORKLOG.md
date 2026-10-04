# Agent Quota Launcher - Core Implementation Worklog

## Checkpoint 1: First-action identity
- Ran `aplexer whoami --json`.
- Wrote `.local/executor-first-action.json` matching `id`, `tag`, `workspace` and a UTC timestamp.
- Joined aplexer work with exclusive paths.

## Checkpoint 2: Package skeleton
- Created `launcher/__init__.py`, `launcher/__main__.py`, `launcher/cli.py`.
- Added required subcommands using argparse.
- Verified `python3 -m launcher --help` works properly.

## Checkpoint 3: Durable store and submit/idempotency
- Implemented `launcher/store.py` providing SQLite-backed tasks schema, path overlaps logic and idempotency constraints.
- Transactional commits using `BEGIN EXCLUSIVE` implemented.
- Unittest `tests/test_store.py` passing overlapping bounds and idempotency matching.

## Checkpoint 4: Quota, resource, and ranking tests
- Implemented `launcher/admission.py` handling `quse --json` validation, explicit model whitelisting (Grok, Antigravity v0.1 only), and Codex/Claude boundaries.
- Implemented `launcher/ranking.py` enforcing task fit, health, and reset boundaries scaling.
- Implemented `launcher/resources.py` with filesystem checks (`/proc/meminfo` parsing and `shutil.disk_usage`), ensuring `/tmp` is rejected and memory gates apply.
- Verified bounds and negative tests across resource exhaustion and admission invalidity in tests.

## Checkpoint 5: Launch wrapper and genuine task evidence
- Implemented `launcher/launch.py` to correctly launch `aplexer start` processes based on requested bounds and limits.
- Configured providers (grok, antigravity, and shell-fallback testing) and argument forwarding.
- Executed a genuine task through the CLI (`run --id genuine-3`), proving identity via `aplexer whoami --json` correctly written to a first-action artifact.
- Validated state transition to `completed-awaiting-review`.

## Checkpoint 6: Project scaffolding completion
- Created `pyproject.toml` establishing `launcher` package with stdlib requirements.
- Implemented `README.md` defining core commands, initialization paths, and ranking formula math.
- Added `examples/task-payload.json` stub showcasing goal declarations.
- Verified test suite executes natively across boundaries.

### Summary
The `agent-quota-launcher` package implements durable SQLite lifecycle management for Agent tasks, explicitly resolving paths to strictly avoid collisions. Quota ranking and host memory reservations are securely tracked prior to delegating jobs via isolated `aplexer` commands.

## Core-2 Repair (Checkpoint 6)
Repaired admission fail-closed cases. Repaired start lifecycle to correctly wait for child first-action and record `launch-uncertain` on unexpected failure. Drop shell provider as success path. `report` outputs valid buckets.

## 2026-10-04 QL-CORE-002 Implementation

*   Implemented correct fail-closed logic in `launcher/admission.py`, enforcing limit reached, stale ISO `reset_at`, numeric check, and unsupported routes.
*   Enhanced `launcher/store.py` with memory/disk tracking using native process death checking (via `aplexer status <tag>`).
*   Fixed `launcher/launch.py` JSON extraction logic from stdout, accommodating the `aplexer` bootstrap appended text.
*   Implemented a 180s blocking wait loop to validate child `first-action` payload against exactly assigned `session_id`, `tag`, and `workspace`.
*   Tested failure scenarios with negative unit tests covering limits, unsubmitted id, path overlap, etc.
*   Launched genuine Antigravity task via `sh -c agy` escaping to fix LLM headless termination, resulting in a successful AI completion of `examples/launch-contract-report.md`.
*   Added `python3 -m launcher watch` loop logic to constantly poll queue and skip tasks with blocked resource constraints/paths safely, logging owner and reason.

## 2026-10-04 QL-CORE-003 (quota-launcher-core-3, fd5d5ee9)

Narrow repair after REJECTED core-2. All eight must-fix areas implemented; 67 unit tests green (`python3 -m unittest discover -s tests`).

* Admission (`launcher/admission.py`): `math.isfinite` on window percents (NaN/Inf fail closed); per-route try/except isolation; timezone-naive or missing `reset_at` yields no evidence and no reset bonus (no guessed UTC); Codex gate evaluated then route marked explicitly unsupported in v0.1 (`scripts/launch-codex.sh` absent here); Grok requires `has_grok_code_access`; gemini->antigravity mapping; rejection reasons bounded to 300 chars (quse embeds Go panic dumps in provider errors). `fetch_quse` salvages the first complete JSON document from stdout (observed trailing Go panic after valid JSON) and fails closed on truncated output.
* Adapters (`launcher/launch.py`): strict argv allowlist - grok exact argv; antigravity via `env -u GEMINI_API_KEY -u GOOGLE_API_KEY agy ... -p --print-timeout 0` (no shell); zai exact `zcodex exec --model glm-5.3-flash ... --json` with `ZCODE_CJS`. `codex-cli -p` and `zai -p` never appear.
* First-action validator: requires exactly whoami-shaped `{id, tag, workspace, timestamp}` (values matched, ISO-parsable timestamp); rejects wrapper/engine start records containing `command`/`phase`/`parent_session`/`schema_version` (the rejected core-2 genuine-5 case), stale files, extra keys.
* Timeout: `payload.timeout` (60-7200s) bounds the whole launch; `aplexer start` runs via `Popen(start_new_session=True)` and deadline expiry kills the process group (SIGTERM then SIGKILL), marks `launch-uncertain`, retains lease until `watch` reconciles confirmed native death. First-action wait happens outside the exclusive launch lock.
* Store: guarded transitions with expected prior states + rowcount (`complete` only from `running`, `accept` only from `completed-awaiting-review`, both with reviewer identity); `launch-uncertain` cannot be completed/accepted; `get_active_resources(exclude_task_id=...)` stops double-counting the launching task's own 1500M; RAM/disk released for `completed-awaiting-review`, path leases held through review.
* Resources: TMPDIR containment via `Path.resolve().is_relative_to(<repo>/.local/tmp)` (rejects `/tmp` trees, sibling checkouts); `repo_root` is now a required argument.
* Ranking: docs/research.md formula, reset multiplier bounded [1,3] with 0.05 headroom floor; grok/zai 1.2 preference only near reset; unknown fit/health weight 0 with recorded reason; seed + per-candidate weights/probabilities persisted in the launch record. Promotion multiplier stays 1.0 (12:28Z is outside 15:00-01:00 UTC).
* `plan` is a real dry-run (fresh quse, ranking projection, no state change); `report` emits the dashboard JSONL contract with null token fields (`source: unproven`), separate quota object, Berlin hourly buckets with explicit 24h gap labels.
* Watcher rewritten (old one crashed on a wrong SELECT): reconciles `launch-uncertain`/`stalled` against native death, dispatches oldest queued task whose owned paths don't overlap live leases (excluding its own lease - a self-overlap bug the first watch pass caught and I fixed).

Genuine runs (evidence in `.local/`, not committed): `task-genuine-r3-1` launched via `run` at 12:28Z on zai ordinary route (fresh quse 92%/5h, 68%/7d; gemini route erroring; grok eligible but task_fit 0; seed 1791116912572), child session `568e4017` wrote a whoami-shaped first action at 12:28:32Z validated by the content checker. `task-projection-r3-2` dispatched by the non-LLM `watch` at 12:33Z. Both left pending head review; no self-acceptance.

## 2026-10-04 QL-CORE-003 (quota-launcher-core-3, fd5d5ee9)

Narrow repair after REJECTED core-2. All eight must-fix areas implemented; 67 unit tests green (`python3 -m unittest discover -s tests`).

* Admission (`launcher/admission.py`): `math.isfinite` on window percents (NaN/Inf fail closed); per-route try/except isolation; timezone-naive or missing `reset_at` yields no evidence and no reset bonus (no guessed UTC); Codex gate evaluated then route marked explicitly unsupported in v0.1 (`scripts/launch-codex.sh` absent here); Grok requires `has_grok_code_access`; gemini->antigravity mapping; rejection reasons bounded to 300 chars (quse embeds Go panic dumps in provider errors). `fetch_quse` salvages the first complete JSON document from stdout (observed trailing Go panic after valid JSON) and fails closed on truncated output.
* Adapters (`launcher/launch.py`): strict argv allowlist - grok exact argv; antigravity via `env -u GEMINI_API_KEY -u GOOGLE_API_KEY agy ... -p --print-timeout 0` (no shell); zai exact `zcodex exec --model glm-5.3-flash ... --json` with `ZCODE_CJS`. `codex-cli -p` and `zai -p` never appear.
* First-action validator: requires exactly whoami-shaped `{id, tag, workspace, timestamp}` (values matched, ISO-parsable timestamp); rejects wrapper/engine start records containing `command`/`phase`/`parent_session`/`schema_version` (the rejected core-2 genuine-5 case), stale files, extra keys.
* Timeout: `payload.timeout` (60-7200s) bounds the whole launch; `aplexer start` runs via `Popen(start_new_session=True)` and deadline expiry kills the process group (SIGTERM then SIGKILL), marks `launch-uncertain`, retains lease until `watch` reconciles confirmed native death. First-action wait happens outside the exclusive launch lock.
* Store: guarded transitions with expected prior states + rowcount (`complete` only from `running`, `accept` only from `completed-awaiting-review`, both with reviewer identity); `launch-uncertain` cannot be completed/accepted; `get_active_resources(exclude_task_id=...)` stops double-counting the launching task's own 1500M; RAM/disk released for `completed-awaiting-review`, path leases held through review.
* Resources: TMPDIR containment via `Path.resolve().is_relative_to(<repo>/.local/tmp)` (rejects `/tmp` trees, sibling checkouts); `repo_root` is now a required argument.
* Ranking: docs/research.md formula, reset multiplier bounded [1,3] with 0.05 headroom floor; grok/zai 1.2 preference only near reset; unknown fit/health weight 0 with recorded reason; seed + per-candidate weights/probabilities persisted in the launch record. Promotion multiplier stays 1.0 (12:28Z is outside 15:00-01:00 UTC).
* `plan` is a real dry-run (fresh quse, ranking projection, no state change); `report` emits the dashboard JSONL contract with null token fields (`source: unproven`), separate quota object, Berlin hourly buckets with explicit 24h gap labels.
* Watcher rewritten (old one crashed on a wrong SELECT): reconciles `launch-uncertain`/`stalled` against native death, dispatches oldest queued task whose owned paths don't overlap live leases (excluding its own lease - a self-overlap bug the first watch pass caught and I fixed).

Genuine runs (evidence in `.local/`, not committed): `task-genuine-r3-1` launched via `run` at 12:28Z on zai ordinary route (fresh quse 92%/5h, 68%/7d; gemini route erroring; grok eligible but task_fit 0; seed 1791116912572), child session `568e4017` wrote a whoami-shaped first action at 12:28:32Z validated by the content checker. `task-projection-r3-2` dispatched by the non-LLM `watch` at 12:33Z. Both left pending head review; no self-acceptance.

Post-delivery accuracy pass (same session, pre-review): `examples/launch-contract-report.md` re-verified against the rewritten README at b79ae70 — the original child review targeted the pre-rewrite README and its "missing/nonconforming" verdicts were stale for axes 2-3 and partly for axis 1. Rewrote with a re-verification section (axes 2-3 conforming; axis 1 largely conforming, residual: first-action artifact path carried by convention inside generic `--paths`, no dedicated submit field per SPEC.md:6) and preserved the original verbatim as an appendix. `examples/dashboard-projection-schema.md` commit citation corrected b23039a -> b79ae70 (schema content verified line-by-line against current `build_report()`/`report()`; coverage line was introduced in b79ae70). Docs-only change; no code touched. Still awaiting head review, nothing self-accepted.

Post-delivery accuracy pass (same session, pre-review): `examples/launch-contract-report.md` re-verified against the rewritten README at b79ae70 — the original child review targeted the pre-rewrite README and its "missing/nonconforming" verdicts were stale for axes 2-3 and partly for axis 1. Rewrote with a re-verification section (axes 2-3 conforming; axis 1 largely conforming, residual: first-action artifact path carried by convention inside generic `--paths`, no dedicated submit field per SPEC.md:6) and preserved the original verbatim as an appendix. `examples/dashboard-projection-schema.md` commit citation corrected b23039a -> b79ae70 (schema content verified line-by-line against current `build_report()`/`report()`; coverage line was introduced in b79ae70). Docs-only change; no code touched. Still awaiting head review, nothing self-accepted.
