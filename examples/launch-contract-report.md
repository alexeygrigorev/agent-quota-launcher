# Launch Contract Review: README.md vs SPEC.md

- Reviewer session: task-task-genuine-r3-1 (id 568e4017-5b38-4f4a-bf5e-1eaeffef3c73)
- Date: 2026-10-04T12:28:32Z
- Inputs: `README.md`, `SPEC.md` (workspace root, this checkout)
- Method: static read-only documentation comparison; implementation was not executed. Line references are to the files as read at this commit. This replaces the earlier AGENTS.md-based draft committed in b23039a.

> **Re-verification 2026-10-04 (quota-launcher-core-3, fd5d5ee9).** The original
> review below targeted `README.md` as it stood when the child session ran
> (pre-`b79ae70`). The README was rewritten later the same day in commit
> `b79ae70` and now documents most of what the original review found missing.
> All three axes were re-checked line-by-line against the current checkout; the
> original text is preserved verbatim as the appendix. After head review round
> R1 (Principal C1450: a key blacklist rejects genuine native whoami, which
> carries `command`/`phase`/`parent_session`/`schema_version`), the first-action
> check was replaced by identity matching against the launch start record.
> Current status summary (as of R1):
>
> | Axis | Original | Current (R1) |
> | --- | --- | --- |
> | 1. Identity / first action | missing / nonconforming | largely conforming; one residual |
> | 2. Accept vs exit 0 | missing / nonconforming | conforming (strengthened in R1) |
> | 3. Resource gates | partially conforming | conforming |
>
> Known limitation of this document: it is a documentation review; it does not
> certify any launch. The genuine r3 runs it accompanied were validated under
> the superseded blacklist contract and accepted by head manual verification,
> not by this report.

## 1. Identity and first action — current status: largely conforming

`README.md` now states the first-action contract as identity matching: the
child's first tool action must write its full native `aplexer whoami --json`
output to the first-action artifact path; validation matches `id`, `tag`, and
`workspace` — plus `parent_session` when present — against the launch start
record, rejects any copy of that record (byte-identical or reformatted), and
requires a sane time field. The rich whoami is preserved as-is; no key is
blacklisted. This covers SPEC.md:12 (whoami tag/workspace match; native first
action distinct from the wrapper started marker) and both rejection causes
observed so far: core-2's `phase: starting` wrapper record (a start-record
copy) and forged identities.

Residual (unchanged by R1, documentation nit): SPEC.md:6 requires each task to
carry a **first-action artifact path** and an **expected result artifact path**.
The CLI has no dedicated arguments for either (`submit` takes generic
`--paths`, `launcher/cli.py` submit parser); the contract is met by convention
— the first-action artifact must live inside the owned `--paths` and is
content-validated at launch — and the submit example passes
`--paths .local/artifact.json` without saying that this is the first-action
artifact.

## 2. Accept versus exit 0 — current status: conforming (strengthened in R1)

- `README.md` documents exact runnable `complete` / `accept` / `fail` examples,
  all requiring `--reviewer` (SPEC.md:4 "names may vary but document exact
  runnable examples").
- `README.md` enumerates the full lifecycle including `launch-uncertain`.
- `README.md` states "**Exit 0 is not acceptance.**" and that `accept` only
  moves `completed-awaiting-review -> accepted` with the reviewer identity;
  queued, running, and `launch-uncertain` tasks cannot be accepted or
  completed (SPEC.md:14). R1 (head review) added two guards the original SPEC
  reading under-specified: `complete` refuses until native death is
  **confirmed** (an unknown aplexer status is not evidence) and refuses without
  result evidence (at least one owned path holding a non-empty artifact);
  death without evidence is closed with the guarded `fail` subcommand instead.
- `plan` is documented as always dry-run, no state change, no launch,
  matching SPEC.md:4.

## 3. Resource gates — current status: conforming

`README.md` carries every audited number: worker memory <= 1500 MiB; host
MemAvailable >= 10 GiB after active reservations; cwd/TMPDIR >= 50 GiB + active
reservations + 512 MiB spike; `/tmp` rejected and TMPDIR resolved under the
owned `<repo>/.local/tmp` by path containment (SPEC.md:12, SPEC.md:16). The
`run` example passes `--tmpdir .../.local/tmp`, which resolves under the owned
root. Ranking is also contract-grade: reset multiplier bounded to [1, 3],
unknown `task_fit` / `health` weight 0 and never fabricated to 1.0, seed +
weights recorded, and the promotion multiplier 1.0 / cutoff 2026-10-06T16:00Z
stated (SPEC.md:10).

## Verdict (re-verified, R1)

README at the R1 head is a contract document, not just a quickstart: it covers
the guarded acceptance transition and the exit-0 caveat (axis 2, now with
confirmed-death and result-evidence gates on `complete`), all numeric resource
gates plus the `/tmp` rejection (axis 3), and the identity-matched
first-action contract that replaced the superseded key blacklist (axis 1).
One residual gap: the first-action artifact path is carried by convention
inside generic `--paths` with no dedicated field and no explicit labeling in
the submit example — a documentation nit, not a correctness defect.

---

## Appendix: original review (2026-10-04T12:28:32Z, pre-`b79ae70` README)

### 1. Identity and first action

SPEC requires:

- Task submission must carry a first-action artifact path (SPEC.md:6).
- The starting CLI returns session identity, and inside the wrapper `aplexer whoami --json` must match the expected tag/workspace (SPEC.md:12).
- The native agent itself must perform its first tool action creating an incremental artifact, distinct from the wrapper started marker (SPEC.md:12).

Record classification applied: a genuine agent first action is whoami-shaped JSON — identity keys (`id`, `tag`, `workspace`) plus a `timestamp`, written by the native agent as its first tool action. Records containing `command` / `phase` / `parent_session` / `schema_version` keys are wrapper/engine start records, not agent first actions. (This session's own `aplexer whoami --json` output carries `schema_version`, `command`, `parent_session` and `phase` — by that rule it is a wrapper start record, which is exactly why the distinction must be documented rather than left implicit.)

README status: **missing / nonconforming.**

- The submit example (README.md:16-22) records only `--id/--key/--payload/--paths/--store`; no first-action artifact path appears anywhere in README (contrast SPEC.md:6).
- No README section documents the whoami tag/workspace verification, expected-tag checking, or the wrapper-start vs native-first-action distinction (README.md:10-48; contrast SPEC.md:12).
- The nearest text is "It securely wraps `aplexer` for system gating" (README.md:47), which states no identity contract at all.

### 2. Accept versus exit 0

SPEC requires:

- Full durable lifecycle: queued / reserved / starting / running / completed-awaiting-review / accepted / failed / blocked / launch-uncertain (SPEC.md:14).
- Head reviews artifacts; "exit zero != accepted"; outcome and acceptance owner are recorded (SPEC.md:14).
- The subcommand set includes `plan` and `complete/accept`, with exact runnable examples documented (SPEC.md:4).

README status: **missing / nonconforming.**

- README documents only `init`, `submit`, `run`, `status`, `report` (README.md:10-39). `plan` and `complete/accept` from SPEC.md:4 are absent, so the guarded transition completed-awaiting-review → accepted is not documented anywhere in README.
- Nothing in README states that a zero exit code or a live process does not imply acceptance (contrast SPEC.md:14 "exit zero != accepted"); README.md:24-32 presents `run`/admission as the last documented lifecycle step before passive `status`/`report`.
- No reviewer identity / acceptance-owner concept appears in README (contrast SPEC.md:14 "acceptance owner").

### 3. Resource gates

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

### Verdict (original)

README is a working quickstart, not a contract document. It is consistent with SPEC on the ranking formula shape (README.md:43 vs SPEC.md:10) and on the `.local/tmp` example (README.md:30), but it omits or understates every audited contract element: identity/first-action verification (axis 1), the guarded acceptance transition and the exit-0 caveat (axis 2), and all numeric resource gates plus the `/tmp` rejection (axis 3). A reader relying on README alone would not learn that exit 0 is not acceptance, that submit needs a first-action artifact, or that any numeric resource floor exists.
