# Launch Contract Review: README.md vs SPEC.md

- Reviewer session: task-task-genuine-r3-1 (id 568e4017-5b38-4f4a-bf5e-1eaeffef3c73)
- Date: 2026-10-04T12:28:32Z
- Inputs: `README.md`, `SPEC.md` (workspace root, this checkout)
- Method: static read-only documentation comparison; implementation was not executed. Line references are to the files as read at this commit. This replaces the earlier AGENTS.md-based draft committed in b23039a.

## 1. Identity and first action

SPEC requires:

- Task submission must carry a first-action artifact path (SPEC.md:6).
- The starting CLI returns session identity, and inside the wrapper `aplexer whoami --json` must match the expected tag/workspace (SPEC.md:12).
- The native agent itself must perform its first tool action creating an incremental artifact, distinct from the wrapper started marker (SPEC.md:12).

Record classification applied: a genuine agent first action is whoami-shaped JSON — identity keys (`id`, `tag`, `workspace`) plus a `timestamp`, written by the native agent as its first tool action. Records containing `command` / `phase` / `parent_session` / `schema_version` keys are wrapper/engine start records, not agent first actions. (This session's own `aplexer whoami --json` output carries `schema_version`, `command`, `parent_session` and `phase` — by that rule it is a wrapper start record, which is exactly why the distinction must be documented rather than left implicit.)

README status: **missing / nonconforming.**

- The submit example (README.md:16-22) records only `--id/--key/--payload/--paths/--store`; no first-action artifact path appears anywhere in README (contrast SPEC.md:6).
- No README section documents the whoami tag/workspace verification, expected-tag checking, or the wrapper-start vs native-first-action distinction (README.md:10-48; contrast SPEC.md:12).
- The nearest text is "It securely wraps `aplexer` for system gating" (README.md:47), which states no identity contract at all.

## 2. Accept versus exit 0

SPEC requires:

- Full durable lifecycle: queued / reserved / starting / running / completed-awaiting-review / accepted / failed / blocked / launch-uncertain (SPEC.md:14).
- Head reviews artifacts; "exit zero != accepted"; outcome and acceptance owner are recorded (SPEC.md:14).
- The subcommand set includes `plan` and `complete/accept`, with exact runnable examples documented (SPEC.md:4).

README status: **missing / nonconforming.**

- README documents only `init`, `submit`, `run`, `status`, `report` (README.md:10-39). `plan` and `complete/accept` from SPEC.md:4 are absent, so the guarded transition completed-awaiting-review → accepted is not documented anywhere in README.
- Nothing in README states that a zero exit code or a live process does not imply acceptance (contrast SPEC.md:14 "exit zero != accepted"); README.md:24-32 presents `run`/admission as the last documented lifecycle step before passive `status`/`report`.
- No reviewer identity / acceptance-owner concept appears in README (contrast SPEC.md:14 "acceptance owner").

## 3. Resource gates

SPEC requires:

- Requested worker memory ≤ 1500MiB (SPEC.md:12).
- Host MemAvailable minus live reservations minus requested budget ≥ 10GiB (SPEC.md:12).
- Target cwd and TMPDIR filesystems ≥ 50GiB + active disk reservations + 512MiB spike budget (SPEC.md:12); `/tmp` is rejected as TMPDIR (SPEC.md:16).
- TMPDIR must resolve under the owned root `.local/tmp` (SPEC.md:12).

README status: **partially conforming; all numbers missing.**

- README.md:24-26 says `run` "checks quse rules, memory limits, and target filesystem boundaries" — but no 1500MiB worker cap, no 10GiB MemAvailable floor, and no 50GiB + 512MiB disk spike budget appears anywhere in README (contrast SPEC.md:12).
- The `/tmp` rejection rule (SPEC.md:16) is not mentioned in README.
- Conforming point: the `run` example passes `--tmpdir /home/alexey/git/agent-quota-launcher/.local/tmp` (README.md:30), which resolves under the owned root `.local/tmp` as SPEC.md:12 requires.
- README.md:46-47 (Design) reiterates the flock lock and `aplexer` wrapping but no numeric gate; "memory limits" appears only in the `run` comment (README.md:25), unquantified.

## Verdict

README is a working quickstart, not a contract document. It is consistent with SPEC on the ranking formula shape (README.md:43 vs SPEC.md:10) and on the `.local/tmp` example (README.md:30), but it omits or understates every audited contract element: identity/first-action verification (axis 1), the guarded acceptance transition and the exit-0 caveat (axis 2), and all numeric resource gates plus the `/tmp` rejection (axis 3). A reader relying on README alone would not learn that exit 0 is not acceptance, that submit needs a first-action artifact, or that any numeric resource floor exists.
