# Dashboard projection schema: `python3 -m launcher report --jsonl`

Producer: `launcher/cli.py` — `build_report()` (projection) and `report()` (JSONL
emission). Consumer: the existing private metrics dashboard. This document is the
consumer contract for the `--jsonl` stream. Status: verified line-by-line
against `build_report()`/`report()` as of 2026-10-04 (head review round R1:
UTC `[as_of-24h, as_of)` window and `project_id`; round R2: window tiling
includes the partial first clock hour, offset-aware `created_at` conversion,
`created_at_invalid` counter).

Design invariants, binding for the dashboard (from SPEC.md §report and the
`build_report` docstring):

- Unknown stays unknown. Nothing in this projection is ever zero-fabricated.
- Token usage is reported only from native proven metadata; otherwise null with
  `source: "unproven"`.
- Quota accounting is a separate object and is never mixed into token usage or
  cost.
- Coverage gaps are explicit; historical hours without tasks are gaps, not
  simulated activity. Tasks outside the window are counted, not emitted.

## Stream shape

One JSON object per line (UTF-8, newline-delimited, no array wrapper). Lines are
of exactly two kinds and must be distinguished by key presence. Every line
carries `project_id` (string; `--project-id` at the producer, defaulting to the
producer's current directory name) so a dashboard can attribute a stream to a
project:

1. **Bucket lines**: `{"project_id": <pid>, "bucket": <label>, "tasks": [<task>, ...]}`
   — one per hourly bucket that has at least one task, emitted in ascending
   bucket-label order (the producer sorts labels lexicographically; with fixed
   `Z` labels that order is chronological).
2. **Coverage line**: `{"project_id": <pid>, "coverage": {...}}` — exactly one,
   always the last line of the stream.

A store with tasks but none in the window still emits bucket lines (for older
in-window hours — none, since outside-window tasks are not emitted) plus the
coverage line. A completely empty store emits zero bucket lines and only the
coverage line; consumers must tolerate that. The JSONL stream does **not**
include the `timezone`, `generated_at`, and top-level `buckets` map that the
non-JSONL (`json.dumps(..., indent=2)`) form has — a consumer that needs the
generation timestamp must use the non-JSONL form or stamp its own receipt time.

## Bucket line: `bucket`

String. Hourly bucket label in UTC, format `%Y-%m-%dT%H:00:00Z`, e.g.
`2026-10-04T13:00:00Z`. A task is grouped by the UTC hour of its recorded
`created_at` (interpreted as UTC). Because labels carry a fixed `Z` offset,
lexicographic label order is chronological — no DST caveat.

## Bucket line: `tasks[]`

Non-empty array. One entry per task whose recorded `created_at` (interpreted as
UTC) falls in that UTC hour **and** inside the projection window
`[as_of-24h, as_of)`. Entry fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Task id as submitted. |
| `state` | string | Lifecycle state as recorded (`queued`, `reserved`, `starting`, `running`, `completed-awaiting-review`, `accepted`, `failed`, `blocked`, `launch-uncertain`, `stalled`). |
| `reviewer` | string \| null | Reviewing owner; **null** until a review action happened — render as “not yet”, not as an empty reviewer. |
| `reason` | string \| null | Rejection/failure reason when recorded; **null** means “no reason recorded”, never “no problem”. |
| `usage` | object | See [Usage object](#usage-object). |
| `quota` | object | See [Quota object](#quota-object). |

Attribution caveats: a task whose `created_at` carries a UTC offset is
**converted** to its true UTC instant (never relabelled by its naive wall
clock); a task with a missing or malformed `created_at` is **invalid** —
counted in `coverage.created_at_invalid`, never emitted into a fabricated
hour and never counted as outside-window. The dashboard cannot distinguish a
dense in-window bucket from clock skew in the source store; treat a
suspiciously dense current-hour bucket accordingly.

## Usage object

Per task, always present, currently shaped exactly like this:

```json
"usage": {
  "input_tokens": null,
  "output_tokens": null,
  "cached_tokens": null,
  "cost": null,
  "source": "unproven"
}
```

- `input_tokens`, `output_tokens`, `cached_tokens`, `cost`: numbers **only**
  when natively proven (recorded from native usage metadata). Until such
  metadata exists in the store they are `null`. `source` is `"unproven"`.
- The dashboard must render `null` as unknown/“—”, **never as 0** and never
  interpolate (no per-model averages, no percent-delta-derived estimates).
- When native usage metadata is later recorded, `source` is the extension point
  for the provenance value; treat any non-`"unproven"` source as the flag that
  the numeric fields may be trusted, and render `null` fields as unknown even
  then.

## Quota object

Separate object, sibling of `usage`, never merged with it:

```json
"quota": {
  "percent_delta": null,
  "snapshots": null,
  "note": "account percent delta is not token use or cost"
}
```

- `percent_delta`: account quota percentage change around the task, when
  snapshots exist; `null` until then.
- `snapshots`: quota snapshot references/series; `null` until recorded.
- The `note` is normative for the dashboard: quota percentage movement is
  accounting headroom, **not** token consumption and **not** cost. Do not sum,
  average, or chart it on the same axis as `usage`, and do not use it to
  backfill `cost`.

## Coverage line

Exactly one, always last:

```json
{"project_id": "<pid>", "coverage": {
  "window_hours": 24,
  "window_start": "<as_of-24h, UTC Z>",
  "window_end": "<as_of, UTC Z>",
  "window_half_open": "[window_start, window_end)",
  "first_bucket": "<label> | null",
  "last_bucket": "<label> | null",
  "gaps_within_window": ["<label>", ...],
  "tasks_outside_window": <count>,
  "created_at_invalid": <count>,
  "note": "historical hours without tasks are gaps, not simulated activity; tasks outside the window and tasks with malformed timestamps are counted, not emitted or attributed"
}}
```

- `window_hours`: fixed `24`. The window is the half-open UTC interval
  `[window_start, window_end)` with `window_start = as_of - 24h`. It is tiled
  by clock hours **including the partial first hour**: the first tile is the
  clock hour *containing* `window_start` (not skipped by floor-then-+1h), and
  the last tile is the last clock hour starting before `window_end`. When
  `as_of` is not on an hour boundary this yields **25 tiles** (both edge tiles
  partial), 24 when it is; `len(gaps_within_window)` + non-empty buckets
  always equals the tile count.
- `first_bucket` / `last_bucket`: minimum/maximum bucket label present in the
  stream, or `null` when there are no buckets at all (empty store) — render
  null as “no data”.
- `gaps_within_window`: explicit list of hourly labels (same
  `YYYY-MM-DDTHH:00:00Z` UTC format) for each window tile that has no bucket.
  These are **explicit gaps**: render as missing/no-data cells, never as zero
  activity. The first (and possibly last) gap label may be a *partial* hour
  whose coverage started/ended mid-hour at the window bound.
- `tasks_outside_window`: count of known tasks whose true UTC instant falls
  outside the window (or whose hour tile is not part of the tiling). They are
  **not emitted** in the stream — the count is the only trace, so a dashboard
  can surface “N older tasks not shown”. Render the count, never the tasks
  themselves.
- `created_at_invalid`: count of tasks whose `created_at` is missing or
  unparsable. Unknown/invalid is its own bucket of ignorance: these tasks are
  neither emitted nor counted as outside-window — render the count as an
  explicit “invalid timestamps” badge, never as 0 tasks and never as gaps.

## Unknown-vs-zero rendering policy (summary)

| Field | Unknown value | Dashboard must render |
| --- | --- | --- |
| `tasks[].usage.{input_tokens,output_tokens,cached_tokens,cost}` | `null` | “unknown” / “—”, never `0`, never estimated |
| `tasks[].usage.source` | `"unproven"` | provenance badge “unproven”; only non-`unproven` sources may claim trust |
| `tasks[].quota.percent_delta`, `tasks[].quota.snapshots` | `null` | “unknown”; never blended into usage/cost |
| `tasks[].reviewer`, `tasks[].reason` | `null` | “—” (not applicable yet / not recorded), not an error state by itself |
| `coverage.first_bucket`, `coverage.last_bucket` | `null` | “no data” |
| `coverage.gaps_within_window[]` | label present | explicit gap marker, not zero; edge labels may be partial hours |
| `coverage.tasks_outside_window` | integer | count badge; the tasks themselves are absent from the stream by contract |
| `coverage.created_at_invalid` | integer | explicit “invalid timestamps” badge; never merged into task counts |

## Real example lines (read-only capture)

Produced by actually running `python3 -m launcher report --jsonl` on
2026-10-04 against the live default store
(`~/.config/agent-quota-launcher/state.db`), no state modified. The stream
holds one bucket line (five tasks in the 11:00Z hour) and the coverage line
(24 of the 25 window tiles explicitly named as gaps; both edge tiles are
partial hours, and the tile containing the tasks is 11:00Z).

Bucket line (first stream line, tasks abbreviated to the first two for
fidelity — the full line carries all five):

```json
{"project_id": "agent-quota-launcher", "bucket": "2026-10-04T11:00:00Z", "tasks": [{"id": "genuine-1", "state": "failed", "reviewer": null, "reason": null, "usage": {"input_tokens": null, "output_tokens": null, "cached_tokens": null, "cost": null, "source": "unproven"}, "quota": {"percent_delta": null, "snapshots": null, "note": "account percent delta is not token use or cost"}}, {"id": "genuine-2", "state": "failed", "reviewer": null, "reason": null, "usage": {"input_tokens": null, "output_tokens": null, "cached_tokens": null, "cost": null, "source": "unproven"}, "quota": {"percent_delta": null, "snapshots": null, "note": "account percent delta is not token use or cost"}}, "... genuine-3 failed, genuine-4 stalled, genuine-5 stalled, same shape ..."]}
```

Coverage line (last stream line of the same run; the 24 gap labels are shown
in full for fidelity):

```json
{"project_id": "agent-quota-launcher", "coverage": {"window_hours": 24, "window_start": "2026-10-03T13:18:33Z", "window_end": "2026-10-04T13:18:33Z", "window_half_open": "[window_start, window_end)", "first_bucket": "2026-10-04T11:00:00Z", "last_bucket": "2026-10-04T11:00:00Z", "gaps_within_window": ["2026-10-03T13:00:00Z", "2026-10-03T14:00:00Z", "2026-10-03T15:00:00Z", "2026-10-03T16:00:00Z", "2026-10-03T17:00:00Z", "2026-10-03T18:00:00Z", "2026-10-03T19:00:00Z", "2026-10-03T20:00:00Z", "2026-10-03T21:00:00Z", "2026-10-03T22:00:00Z", "2026-10-03T23:00:00Z", "2026-10-04T00:00:00Z", "2026-10-04T01:00:00Z", "2026-10-04T02:00:00Z", "2026-10-04T03:00:00Z", "2026-10-04T04:00:00Z", "2026-10-04T05:00:00Z", "2026-10-04T06:00:00Z", "2026-10-04T07:00:00Z", "2026-10-04T08:00:00Z", "2026-10-04T09:00:00Z", "2026-10-04T10:00:00Z", "2026-10-04T12:00:00Z", "2026-10-04T13:00:00Z"], "tasks_outside_window": 0, "created_at_invalid": 0, "note": "historical hours without tasks are gaps, not simulated activity; tasks outside the window and tasks with malformed timestamps are counted, not emitted or attributed"}}
```

Note how the example exercises every contract rule at once: five tasks in one
bucket with `state` `failed`/`stalled`, all-null usage with `source:
"unproven"`, all-null quota, a half-open UTC window with explicit bounds,
partial edge tiles (first tile `2026-10-03T13:00:00Z` starts
mid-hour at `window_start`), and 24 of 25 window tiles explicitly named as
gaps.
