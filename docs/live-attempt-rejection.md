# Product launch attempts awaiting repair

Observed 2026-10-04T11:47:25Z. These records are preserved negative evidence, not accepted executions.

| Attempt | Recorded native child | Observation |
| --- | --- | --- |
| core2 `genuine-1` | `d3228c14-0322-4c90-b366-2d249125c36c` | No matching active session on status query; local task marked failed; no verified child first action/result. |
| core2 `genuine-2` | `be5f82a7-864b-4843-a036-a22ea4e56c7e` | Task marked failed. `.local/first-action-genuine-2.json` names parent core2 session `76619348-0102-4c04-a03b-3a14d8891d27`, tag `quota-launcher-core-2`, instead of recorded child. Reject this as child first-action evidence. |
| core2 `genuine-3` | `6fe6b357-064c-4df2-b9ff-1b4409911a7e` | Task starting at this observation, but status query found no matching session. Outcome unresolved; do not blindly retry or infer success. |

The mismatched identity could result from incorrect inherited binding, parent execution, or another cause. The artifact alone cannot distinguish them. Inspect actual native tool event provenance and process containment; do not rewrite identity fields or override APLEXER variables to make it pass. No matching active session does not by itself prove a particular exit or empty containment. Reconcile durable native session records before releasing uncertain process reservations.

An independent offline admission probe froze source SHA256 `6f2f74a186495725da2c86d23888914ab8f4d8f846dc79be43be350118e6bb44` at 11:46:56Z in `.local/admission-probe-20261004T114656Z/`. One of six checks passed: the valid finite future-window control. Five failed: NaN, infinity and boolean values admitted; known zero quota with missing reset ignored when another window was valid; naive reset timestamp raised `TypeError`. No provider was called by these probes.

The core writer was still active during this observation. Rerun these checks against its final checkpoint. The native head owns acceptance and writer handoff; source is preserved and no second writer is introduced.
