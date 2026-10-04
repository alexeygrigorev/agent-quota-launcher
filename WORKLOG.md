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
