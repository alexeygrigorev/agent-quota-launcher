> **Deprecated: superseded by [Agent Hub](https://github.com/alexeygrigorev/agent-hub).**
> Use Agent Hub for fleet start/spawn commands and account-aware placement. This repository remains the maintained execution/admission backend during migration; deprecation of its standalone orchestration entry points does not authorize bypassing quota or resource gates. See the [migration guide](https://github.com/alexeygrigorev/agent-hub/blob/main/docs/GENERIC-HUB.md).

# Agent Quota Launcher

A bounded, stdlib-only Python CLI for launching agent processes reliably: quota-aware admission, filesystem/resource gates, durable SQLite lifecycle, and honest evidence. Providers are strict-argv adapters over native `aplexer` sessions.

## Requirements
- Python >= 3.9 (uses `zoneinfo` and `Path.is_relative_to`)
- `aplexer` and `quse` installed and on `$PATH`
- No third-party pip installs

## Tests, CI and releases

The full suite is plain unfiltered `python -m pytest` under the contract in `ci/suite-contract.json`: hash-pinned pytest, pinned agent-coordination and Agent Hub host checkouts, and a real OS-account HOME. `scripts/ci_full_suite.py` enforces the contract in GitHub Actions (`.github/workflows/ci.yml`) and on the launcher host (`scripts/host_suite_sandbox.sh host ...`, zero skips). In CI, the two tests that prove the installed host runtime are the only tests that may skip, and they are reported by name. Source releases of one exact CI-tested commit are cut manually with `.github/workflows/release.yml` and verified or staged with `scripts/release_tool.py`. Nothing here activates a release. See [docs/RELEASE.md](docs/RELEASE.md).

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

# Reconcile resource leases without starting or stopping any native session.
# Back up state.db with SQLite backup before the first repair; preview first.
python3 -m launcher --config-dir .local/launcher-config resource-status --dry-run
python3 -m launcher --config-dir .local/launcher-config resource-status

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

**GLM routing rule (2026-10-14):** Z.ai / GLM work routes only to `zai` (zcodex). OpenCode must never run a GLM model; it is pinned to Muse Spark 1.3 (`opencode-go/muse-spark-1.3-contributor`). `build_adapter_argv` refuses a GLM model on any other route.

Codex is explicitly unsupported in v0.1 (the protected `scripts/launch-codex.sh` wrapper is not configured here); `codex-cli -p` and `zai -p` never appear. Admission fails closed on: non-finite quota percentages (NaN/Inf), missing or timezone-naive `reset_at` (no fabricated urgency), stale evidence, `limit_reached`, exhausted windows, Codex windows <=15% or unknown, and missing Grok entitlement evidence (`has_grok_code_access`). One malformed route never crashes admission of the others.

## Ranking

`weight = task_fit * health * preference * headroom * (1 + 2 * urgency)` where `headroom = max(0.05, remaining)` and `urgency = remaining * clamp((24 - hours_to_reset)/24, 0, 1)`; the reset multiplier stays in **[1, 3]**, and missing/naive reset timestamps add no urgency. Grok/ZAI get a 1.2 preference only when a known window resets within 24h. Unknown `task_fit`/`health` stay unknown and weight 0 — never fabricated to 1.0. Seed, per-candidate weights, probabilities and the selection are recorded in `.local/launch-<task>.json` (mode 600). Promotion multiplier stays 1.0 (disabled) unless a verified ZCode >= 3.10 GLM-5.3-Flash subscription route exists; conservative cutoff 2026-10-06T16:00Z.

## Resource gates

Placement is gated on live data: `MemAvailable - ramp_reserve(inflight starts) - estimate(new) >= floor`. Resident workers are already counted inside MemAvailable and are never reserved again; only inflight starts hold a ramp reserve (measured p90 × 1.25, fallback 512 MiB). Queued tasks hold no RAM before starting. CPU admission is choking-only (USER t1511u V2): heavy busy% is fine (90%+ admits); only fresh PSI CPU FULL / PSI memory FULL, fresh positively classified host-wide OOM evidence, or fresh trusted host runtime evidence queues with `cpu_pressure` / `memory_pressure` / `host_oom` / `host_runtime_choking`. Load average, busy%, predicted load, and unknown telemetry never block. Worker MemoryMax caps (1500 MiB, 4096 MiB managed Codex) are per-session safety caps, not reservations. cwd and TMPDIR filesystems must retain >= 50 GiB + active reservations + 512 MiB spike; `/tmp` is rejected and TMPDIR must resolve under the owned `<repo>/.local/tmp` (path containment, not substring).

## Hostwide Capacity & Concurrency Governor (C2661 / C2664 / C2670)

- **Fixed shared ZAI ceiling:** Evaluates live `zcode-cli` backend processes across all host trees (including external workspaces like `/data/agents/ai-shipping-labs/`). Rejects new ZAI dispatches when live count + active reservations >= 26.
- **429 Cooldown Enforcement:** Persists 429 Retry-After cooldown timestamps; rejects dispatch while inside active backoff window.
- **Atomic Slot Reservation:** Uses `fcntl.flock` on `provider_capacity.lock` to guarantee race-free slot allocation and automatic release upon unit termination or failure.
- **Multi-engine Fallback:** When a requested provider is at capacity (e.g. ZAI at 26), the maintained CLI automatically falls back to an eligible alternative (e.g. `antigravity` / `gemini-3.1-pro-high`) with verified quota and headroom.

## Report (dashboard contract)

`report --jsonl` emits exactly 24 hourly UTC buckets tiling the half-open window `[as_of-24h, as_of)` with `Z`-suffixed labels, explicit gap labels, and canonical `project_id` on every line (`--project-id NAME`, default: `quota-launcher` per QL-DASH-CONTRACT-1). Tasks outside the window are counted in `coverage.tasks_outside_window`, not emitted. Token fields (`input_tokens`, `output_tokens`, `cached_tokens`, `cost`) stay `null` with `source: "unproven"` unless natively proven; quota deltas are a separate object; unknown stays unknown — never zero-fabricated. Account percent deltas are not token use or cost.

### Live memory admission

The 1500 MiB cgroup limit is a hard worker cap, not a reservation charged
again for every running worker. Exact native/cgroup identity and anonymous
resident measurement establish live usage already reflected in MemAvailable.
A live resident past the start-ramp window holds no additional RAM; a young
one holds the placement estimate minus proven current usage (`max(0,
estimate - current)`, mirroring the Hub headroom credit). Samples are per
native incarnation, one maximum per 30 seconds, retained for 15 minutes,
and still recorded for the measured p90 even when the reserve is zero. A
new worker is charged the placement estimate: payload `memory_estimate_mb` (the Hub p90 by project/engine/kind),
else the measured p90 over resident samples scaled by `estimate_multiplier`
(`ceil(max(64, p90 × multiplier))`, default ×1.25), else the 512 MiB ramp
default — capped to the worker cgroup ceiling so a historic oversized
sample shrinks the estimate instead of blocking placement. Still-starting
workers hold that ramp estimate; queued tasks hold nothing; stale starts
and other unknown measurements hold the full allowance (fail closed).
Admission requires the estimate plus the
inflight ramp reserve to fit above the floor (default 2048 MiB).
`resource-status --dry-run` neither settles tasks nor writes memory samples,
CPU samples, OOM baselines, or choking events (fully readonly).

Knobs arrive from the Hub payload as `resource_management`
(`memory_floor_mb`, `ramp_reserve_default_mb`, `estimate_percentile`,
`estimate_multiplier`, `ramp_seconds` default 180, `cpu_load_factor`
deprecated default 3.0 UNUSED, `cpu_target_utilization` default 1.0
advisory, `cpu_default_cores` default 1.0, `cpu_estimate_multiplier`
default 1.25, legacy `cpu_psi_hard_cap_pct` default 50 unused,
`cpu_psi_full_hard_cap_pct` default 5, `memory_psi_full_hard_cap_pct`
default 1, `choking_cooldown_seconds` default 300)
with an optional per-task
`memory_estimate_mb` plus authoritative per-task `expected_cpu_cores` /
`expected_cpu_source` (`repo_kind`/`repo`/`global`/`default`, frozen per
task under the launch lock; missing payload falls back to the conservative
`cpu_default_cores`). Bounds track the Hub contract
(`src/resource-management.ts`): floor 128..1048576, ramp default
64..65536, percentile 50..100, multiplier 1..4, ramp 30..900 s, load factor
0.5..4.0 (validated, unused), target 0.1..1, default cores 0.05..128,
cpu multiplier 1..4, SOME cap 1..100 (unused), FULL caps 0.1..100,
cooldown 30..3600; out-of-range values fail closed. Irrelevant Hub watchdog keys
(`watchdog_interval_s`, `idle_minutes`, `rss_flag_gb`) are ignored. A JSON
file with the same keys is honored only via an
explicit path: payload `resource_config_path`, `--resource-config`, or
`LAUNCHER_RESOURCE_CONFIG`; the repo `config/resource-management.json` is
never auto-loaded. Missing knobs fall back to safe defaults; invalid values
fail closed. `resource-status` reports the floor, ramp reserve, measured
p90, active estimate, per-task breakdown, CPU choking verdict (numbers,
telemetry, trusted hint, advisory `predicted_over_target`), and the
`launcher_memory_reserve` / `cpu_pressure` / `memory_pressure` / `host_oom` /
`host_runtime_choking` verdicts. `resource-status --dry-run` is fully
readonly: no task transitions, no CPU sample/OOM/choking writes.

Hosts pass validated runtime evidence through `run --trusted-choking-json`
as a separate bounded argument, independent of task payloads. This argument
survives detached systemd controller boundaries. It accepts only the fixed
reason/timestamp object; malformed, stale or unknown evidence is advisory.
The environment hint remains supported for direct operator invocation.

### Managed native Codex memory

Hub headless tasks (`owner: hub-fleet`, `profile: model-task`) use the host's explicit `memory_mb` as the cap for every provider (Hub sends 1500 MiB), raised only by the 8192 MiB Cargo compile floor. The Hub `memory_estimate_mb` is a placement hint, never a cap: admission charges it clipped to the cap. Only when the host requests no `memory_mb` is the cap the larger of the estimate and the 4096 MiB cold-start floor. Managed Codex on the native aplexer backend defaults to a 4096 MiB workload cap; other providers and task-unit/FileBus backends retain their existing 1500 MiB ceiling. Submission reserves the known route allowance, and launch rebinds the reservation to the exact selected route under launch.lock before checking host headroom and spawning. The placement floor (default 2048 MiB) is charged the measured estimate, not the full allowance. The native startup timeout bounds the start handshake and first action, not the lifetime of a running job. resource-status reports the estimate-based gate plus the fail-closed 4096 MiB managed-Codex headroom.

The aggregate `/proc/vmstat` OOM counter includes per-session memcg kills and is diagnostic only. It never creates a launcher host refusal or durable cooldown. Kernel-classified host-wide OOM reaches the launcher through the fresh validated `--trusted-choking-json` channel; session OOMs remain session failures. Without classified host evidence, OOM scope is unknown and other independent PSI/memory guards still apply. Existing cooldowns expire normally; upgrading does not clear evidence.
