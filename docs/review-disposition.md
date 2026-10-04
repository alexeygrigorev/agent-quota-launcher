# Independent review disposition

Observed 2026-10-04T11:42Z. The ZAI reviewer completed a real provider turn with native thread `01a106ae-087f-7532-94e7-767000c70021`; its actual first tool was `aplexer whoami --json` and matched aplexer session `0896a5ef-b4d1-4fd8-acd0-fc4458a72d65`. It wrote `docs/zai-live-route-review.md` and delivered native messages to the head. Its self-labelled `~11:45Z` timestamp is not a verified clock reading and must not replace collector timestamps.

Accepted: finite numeric quota validation; per-route rejection for malformed timestamps; bounded process and submission timeouts; guarded state transitions with reviewer evidence; excluding unimplemented adapters before selection; correcting own-reservation counting; resolved scratch ownership; genuine agent first tool correlation; explicit missing queue/reconciliation and plan behavior. These are implementation acceptance requirements, not merely advisory concerns.

Corrections and qualifications:

- Reject offset-less reset timestamps with an explicit reason. Do not silently attach UTC as proposed in finding 2: the source timezone is unknown.
- The research weight expression is a candidate design, not an exact normative equation. A different bounded formula can be accepted if documented, tested against depletion/task fit, and recorded with actual selection inputs. Fabricated headroom or health is never acceptable.
- The report was written while the owner changed source. Its shell override finding was retracted correctly. The unsupported-adapter finding briefly became invented `zai -p`/`codex-cli -p` branches; that version was separately blocked in native message `01a106b7-094b-70c2-a359-e3040875d920`. Recheck final code; no line-number claim is timeless.
- Aplexer's engine metadata `shell` identifies the outer wrapper; it does not show that no zcodex child existed. The recorded launch script executed `/home/alexey/.local/bin/zcodex exec --model glm-5.3-flash` with the official `ZCODE_CJS` path, and native JSONL contains an actual model turn and tool executions. This establishes an actual ZAI review task outside the product launcher, not promotion entitlement or product launch acceptance. A transient descendant Node process was not observed independently.
- “No provider contacted” in the reviewer means its two offline repro scripts launched no additional provider. The review itself consumed a real model turn. Reported native usage is input 216026, cached input 0, output 2154; those are harness-reported counters, not inferred quota debits or money. Provider semantics may differ.
- Agent identity JSON is necessary but not sufficient evidence of useful progress. Verify the native command event and a meaningful scoped result, then review separately from process exit.

The reviewer released its declared file scope. Core remains owned by `quota-launcher-core-2` until the native head records checkpoint/release or confirmed exit; no second core writer is authorized concurrently. This review is accepted as an independent challenge, while the implementation remains unaccepted pending fixes and a real task/continuation cycle.
