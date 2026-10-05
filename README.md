# Agent Quota Launcher

A bounded, stdlib-only Python CLI for launching agent processes reliably: quota-aware admission, filesystem/resource gates, durable SQLite lifecycle, and honest evidence. Providers are strict-argv adapters over native `aplexer` sessions.

## Requirements
- Python >= 3.9 (uses `zoneinfo` and `Path.is_relative_to`)
- `aplexer` and `quse` installed and on `$PATH`
- No third-party pip installs

## Commands

All commands accept `--config-dir DIR` (state DB + launch lock live there); it must come **before** the subcommand.

```bash
# Initialize local DB
python3 -m launcher --config-dir .local/launcher-config init

# Submit an idempotent task (same key + same payload -> original id; conflict -> error)
python3 -m launcher --config-dir .local/launcher-config submit \
    --id task-123 \
    --key idempotency-1 \
    --payload '{"goal": "Check system stats", "owner": "head-x", "cwd": "/repo",
                "timeout": 1800,
                "model_requirements": {"providers": ["zai"], "models": ["glm-5.3-flash"]}}' \
    --paths .local/artifact.json

# Always dry-run: fresh quse admission + ranking projection, no state change, no launch
python3 -m launcher --config-dir .local/launcher-config plan --id task-123

# Admit, reserve, launch (bounded by payload timeout on the whole process group)
python3 -m launcher --config-dir .local/launcher-config run \
    --id task-123 \
    --cwd /home/alexey/git/agent-quota-launcher \
    --tmpdir /home/alexey/git/agent-quota-launcher/.local/tmp

# Status, dashboard projection, non-LLM queue watcher
python3 -m launcher --config-dir .local/launcher-config status
python3 -m launcher --config-dir .local/launcher-config report [--jsonl] [--project-id NAME]
python3 -m launcher --config-dir .local/launcher-config watch \
    --cwd /repo --tmpdir /repo/.local/tmp [--interval 10] [--once]

# Lifecycle completions are guarded and require a reviewer identity
python3 -m launcher --config-dir .local/launcher-config complete --id task-123 --reviewer head-x
python3 -m launcher --config-dir .local/launcher-config accept   --id task-123 --reviewer head-x

# Death without result evidence is closed as failed, never completed
python3 -m launcher --config-dir .local/launcher-config fail --id task-123 \
    --reviewer head-x --reason "no result artifacts produced"
```

## Lifecycle and semantics

`queued -> starting -> running -> completed-awaiting-review -> accepted`, plus `launch-uncertain`, `stalled`, `failed`, `blocked` notes. Invariants:

- **Exit 0 is not acceptance.** `accept` only moves `completed-awaiting-review -> accepted`, recorded with the reviewer identity. Queued, running, and `launch-uncertain` tasks cannot be accepted or completed. `complete` requires **confirmed** native death (an unknown aplexer status is refused) **and** result evidence: at least one owned path holding a non-empty artifact. Death without evidence is closed with `fail --reason`, never completed.
- **First action validates live whoami mutation vs start record, not key blacklists.** The child's first tool action must write its full native `aplexer whoami --json` output to the first-action artifact path. Validation matches `id`, `tag`, and `workspace` — and `parent_session` when present — against the launch start record, requires a timezone-aware timestamp bounded to the launch window (naive, stale, or future timestamps fail), and rejects all start-record supersets: byte-identical copies, reformatted copies, start records augmented with timestamp fields or arbitrary extra keys (`dummy=123`), and start records with invented worker fields while original start keys including `phase=starting` remain unchanged. A genuine child artifact must demonstrate live whoami mutation (`phase` running/working vs starting, updated/activity timestamps after creation, live process/containment state); native keys (`command`, `schema_version`, `worker_pid`, ...) are preserved, never blacklisted. Captured authenticated tool-trace verification remains out of v0.1; identity plus live mutation provides correlation against the launch record.
- **Timeouts are enforced on the process group.** `payload.timeout` (60–7200s) bounds `aplexer start` plus the first-action wait; the launch lock is only held for reserve+spawn, not the wait. Deadline expiry marks `launch-uncertain` and retains the lease until native session death is confirmed by reconciliation (`watch`).
- **Resource leases are honest.** RAM/disk reservations cover queued/starting/running/uncertain/stalled tasks and are released after confirmed process death; path leases hold through `completed-awaiting-review`. A launching task excludes its own reservation (`exclude_task_id`).

## Provider adapters (strict allowlist, no shell parsing)

| provider | argv |
| --- | --- |
| `grok` | `/home/alexey/.local/bin/grok --model grok-4.6 --effort high --permission-mode auto -p <PROMPT>` |
| `antigravity` | `env -u GEMINI_API_KEY -u GOOGLE_API_KEY agy --model gemini-3.1-pro-high --effort high --dangerously-skip-permissions -p --print-timeout 0 --output-format text <goal>` |
| `zai` | `/home/alexey/.local/bin/zcodex exec --model glm-5.3-flash --dangerously-bypass-approvals-and-sandbox -c check_for_update_on_startup=false --json <goal>` with `ZCODE_CJS=/opt/ZCode/resources/glm/zcode.cjs` |

Codex is explicitly unsupported in v0.1 (the protected `scripts/launch-codex.sh` wrapper is not configured here); `codex-cli -p` and `zai -p` never appear. Admission fails closed on: non-finite quota percentages (NaN/Inf), missing or timezone-naive `reset_at` (no fabricated urgency), stale evidence, `limit_reached`, exhausted windows, Codex windows <=15% or unknown, and missing Grok entitlement evidence (`has_grok_code_access`). One malformed route never crashes admission of the others.

## Ranking

`weight = task_fit * health * preference * headroom * (1 + 2 * urgency)` where `headroom = max(0.05, remaining)` and `urgency = remaining * clamp((24 - hours_to_reset)/24, 0, 1)`; the reset multiplier stays in **[1, 3]**, and missing/naive reset timestamps add no urgency. Grok/ZAI get a 1.2 preference only when a known window resets within 24h. Unknown `task_fit`/`health` stay unknown and weight 0 — never fabricated to 1.0. Seed, per-candidate weights, probabilities and the selection are recorded in `.local/launch-<task>.json` (mode 600). Promotion multiplier stays 1.0 (disabled) unless a verified ZCode >= 3.10 GLM-5.3-Flash subscription route exists; conservative cutoff 2026-10-06T16:00Z.

## Resource gates

Worker memory <= 1500 MiB; host MemAvailable must retain >= 10 GiB after active reservations; cwd and TMPDIR filesystems must retain >= 50 GiB + active reservations + 512 MiB spike; `/tmp` is rejected and TMPDIR must resolve under the owned `<repo>/.local/tmp` (path containment, not substring).

## Hostwide Capacity & Concurrency Governor (C2661 / C2664 / C2670)

- **Fixed shared ZAI ceiling:** Evaluates live `zcode-cli` backend processes across all host trees (including external workspaces like `/data/agents/ai-shipping-labs/`). Rejects new ZAI dispatches when live count + active reservations >= 26.
- **429 Cooldown Enforcement:** Persists 429 Retry-After cooldown timestamps; rejects dispatch while inside active backoff window.
- **Atomic Slot Reservation:** Uses `fcntl.flock` on `provider_capacity.lock` to guarantee race-free slot allocation and automatic release upon unit termination or failure.
- **Multi-engine Fallback:** When a requested provider is at capacity (e.g. ZAI at 26), the maintained CLI automatically falls back to an eligible alternative (e.g. `antigravity` / `gemini-3.1-pro-high`) with verified quota and headroom.

## Report (dashboard contract)

`report --jsonl` emits exactly 24 hourly UTC buckets tiling the half-open window `[as_of-24h, as_of)` with `Z`-suffixed labels, explicit gap labels, and canonical `project_id` on every line (`--project-id NAME`, default: `quota-launcher` per QL-DASH-CONTRACT-1). Tasks outside the window are counted in `coverage.tasks_outside_window`, not emitted. Token fields (`input_tokens`, `output_tokens`, `cached_tokens`, `cost`) stay `null` with `source: "unproven"` unless natively proven; quota deltas are a separate object; unknown stays unknown — never zero-fabricated. Account percent deltas are not token use or cost.
