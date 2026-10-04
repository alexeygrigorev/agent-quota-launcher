# Review of README.md against Agent Quota Launcher Requirements

Based on the rules defined in `AGENTS.md`, here is a review of the current `README.md` and the launcher's contract:

## Positives
*   **TMPDIR Compliance**: The `launcher run` example correctly uses `--tmpdir /home/alexey/git/agent-quota-launcher/.local/tmp`, which aligns with the rule that `/tmp` fails and `TMPDIR` must be under `.local/tmp`.
*   **Idempotency and Goals**: The `launcher submit` command correctly accepts `--id`, `--key`, and `--payload` with goals, satisfying durable task constraints.
*   **Ranking Formula**: Mentions health metrics and quota headroom, fitting the provider route requirement for health evidence.

## Missing Constraints (Action Required)
1.  **Memory & Disk Limits**: The README generically mentions "memory limits" but does not explicitly state the hard requirements:
    *   `aplexer` must use `--memory 1500M` maximum per new worker.
    *   Host `MemAvailable` must retain `>=10GiB` after reservation.
    *   Disk target and scratch mount must retain `>=50GiB` plus `512MiB` estimated spike.
2.  **Quse Refresh**: The run command description mentions it "checks quse rules", but must explicitly enforce a "fresh quse immediately before every external launch."
3.  **Codex Restrictions**: 
    *   Real Codex launches must use `scripts/launch-codex.sh`.
    *   Must deny if `<=15%` remaining or unknown.
    *   No `zcodex` promotion eligibility (adapter not verified ZCode >=3.10). Conservative cutoff is `2026-10-06T16:00:00Z`.
4.  **Worker Isolation Specs**: Missing explicit mentions of:
    *   Bounded timeouts.
    *   Genuine `whoami`.
    *   Requirement for "first tool action and incremental artifacts."

## Conclusion
The `README.md` provides a solid structural overview but needs to explicitly document the strict resource thresholds (e.g. 1500M max memory, 10GiB host memory buffer) and model-specific constraints (Codex routing rules, zcodex promo cutoff) to fully reflect the launcher's operational contract.
