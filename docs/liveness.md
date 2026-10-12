# Agent liveness supervision

Issue [62](https://github.com/alexeygrigorev/agent-quota-launcher/issues/62) requires agents to report progress and recover from hangs. The first deliverable observes one existing head in shadow mode. Its only target operations are native status and rendered-screen capture; it records observations and proposed escalation without sending input, signals, launch commands or restart requests.

## Evidence and identity

An observer heartbeat proves the observer completed a cycle. It does not prove the agent made progress. Each observation binds to a full native session UUID; a tag is a discovery aid, never a recovery identity. An unexpected UUID, malformed response, failed probe or expired probe deadline means observation is unavailable, not that the agent died.

Keep these clocks separate:

| Signal | Source | Meaning |
| --- | --- | --- |
| Observer heartbeat | UTC observation timestamp | Observer completed a sampling cycle |
| Last terminal activity | Native `last_activity_ms` | PTY output occurred; repainting can produce it |
| Last tool output | Authenticated harness event | A tool produced output; unavailable in the initial adapter |
| Current tool start and command | Authenticated harness event | Tool duration and applicable timeout; unavailable in the initial adapter |
| Useful progress | Task artifact and checkpoint evidence | Work advanced toward the assigned outcome |

Unavailable values stay null with an explicit source/coverage reason. Do not substitute terminal activity for tool output, infer a tool command from arbitrary screen text, or treat a busy label as task completion. Silence alone is a suspected stall requiring diagnosis.

The screen classifier recognizes working, idle, menu, quota-held, goal-stalled and content-filter states; ambiguity stays unknown. These are observations, not authorization. Drafts, menus, idle and quota-held states remain protected. A displayed failure card can be historical: screen evidence needs freshness and harness correlation before live recovery.

## One-head shadow wrapper

The proposed entry point is `python3 -m launcher.liveness --session UUID --output-dir .local/liveness/pilot --duration 300`. `--once` performs one cycle. The executor supplies the exact supported options and tests before integration.

Every native probe has a deadline. A failed or hung observer probe produces an unavailable observation. Ending that read-only probe must never signal the observed workload. The observer lifetime is bounded; stopping it leaves the head running. A per-output-directory lock prevents concurrent writers. The snapshot is replaced atomically and the event log rotates under a fixed size bound. Private directories and files use restrictive permissions. Raw screens, transcripts, environment variables and command contents are not persisted.

The initial pilot targets this liveness head's UUID, observes it without intervention, and records several cycles. Simulations cover silent panes, hung probes, goal-stalled and content-filter cards, quota/menu/idle states, malformed identity, process-limit warnings and login-session placement. Simulations establish classification and safe behavior; they do not prove live recovery from PID exhaustion or SIGHUP.

## Escalation contract

Live enforcement requires an explicit acknowledgement from codex-principal for the reviewed revision, target, thresholds and allowed actions. Shadow annotations grant no such authority. Every actual step must be logged with the target generation, evidence, selected action, owner and result.

1. Diagnose the missing heartbeat or tool timeout using fresh native identity, task checkpoint and harness events. Protect useful work, drafts and unknown states.
2. Terminate an identified hung tool child only after proving its ownership and process identity. A model or session PID is not a tool-child identity. Confirm exit before releasing reservations.
3. Use supported Esc and a resume nudge only under the approved custody and prompt-safety policy. Do not inject into unrelated busy panes.
4. Gracefully restart the same conversation after acknowledged checkpoint/exit or proved vacancy, retaining task claims, pending reviews and history. Fence competing writers.
5. Hard termination and recovery follow the principal-owned [head recovery procedure](https://github.com/alexeygrigorev/cloudflare-agent-git/blob/main/recovery/head.md). Fresh quota, resource, provider and custody gates apply to the replacement.

Per-tool budgets must come from the task/tool class: deterministic checks, model requests, builds and long tests need different deadlines. Exceeding a budget triggers diagnosis; unrelated periodic output must not reset the start time of a hung call. Clock duration checks use monotonic time; stored timestamps use UTC.

## Launch placement and integration

All heads, executors, reviewers, root jobs and mailbox workloads eventually need coverage. Observe workload and PTY-worker cgroups and login-session ancestry separately. A surviving model with a dead PTY worker is not a healthy interactive head. Launch through the maintained manager-backed route outside SSH login lifetime; verify placement and actual first action rather than trusting a launch receipt.

Each worker must have independent containment, memory no greater than 1500MiB, a bounded lifetime and a role-appropriate PID budget. The current task backend's 100-task constant is a launch-hygiene follow-up: raising it needs a scoped implementation and different-model review, plus an exhaustion/SIGHUP fixture. Do not change productive live cgroups during this pilot.

Reuse experiment-supervision's existing event and due-scan service and principal-ping infrastructure. This pilot is a bounded diagnostic process, not an additional persistent scheduler or recovery writer. Later integration needs an acknowledged owner and schema contract with those services.

Until agent-hub-head agrees a health API, the private local snapshot is the transport. A proposed v1 payload contains schema version, observer identity, target UUID/generation, observed UTC time, source coverage, agent state, activity timestamp, nullable tool telemetry, reasons and shadow-only action proposals. No unauthenticated listener is needed. The hub must distinguish stale observer data from a fresh observation of a stalled target. API agreement, remote transport and retention are separate acceptance gates.

## Acceptance and rollout

The first deliverable needs a pinned design and wrapper, meaningful simulated negative cases, evidence of a bounded live shadow run on one head, and approval from a different-model reviewer of the exact files. Only then integrate with explicit paths and serialized Git commits. Keep issue 62 open: this deliverable does not establish fleet coverage or working recovery.

Next deliverables are authenticated tool-event ingestion, agreed hub reporting, launch placement/PID hygiene and fenced disposable recovery tests. Live escalation remains disabled until the principal acknowledges its concrete reviewed scope. Each phase records a terminal receipt, retained evidence, a next owner/action and a continuation trigger.
