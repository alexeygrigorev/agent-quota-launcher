# Dashboard projection schema: `python3 -m launcher report --jsonl`

Producer: `launcher/cli.py` — `build_report()` (projection) and `report()` (JSONL
emission). Consumer: the existing private metrics dashboard. This document is the
consumer contract for the `--jsonl` stream. Status: matches implementation at
commit `b23039a` (2026-10-04).

Design invariants, binding for the dashboard (from SPEC.md §report and the
`build_report` docstring):

- Unknown stays unknown. Nothing in this projection is ever zero-fabricated.
- Token usage is reported only from native proven metadata; otherwise null with
  `source: "unproven"`.
- Quota accounting is a separate object and is never mixed into token usage or
  cost.
- Coverage gaps are explicit; historical hours without tasks are gaps, not
  simulated activity.

## Stream shape

One JSON object per line (UTF-8, newline-delimited, no array wrapper). Lines are
of exactly two kinds and must be distinguished by key presence:

1. **Bucket lines**: `{"bucket": <label>, "tasks": [<task>, ...]}` — one per
   hourly bucket that has at least one task, emitted in ascending bucket-label
   order (the producer sorts labels lexicographically).
2. **Coverage line**: `{"coverage": {...}}` — exactly one, always the last line
   of the stream.

A store with tasks but no tasks in the last 24h still emits bucket lines (for
older hours) plus the coverage line. A completely empty store emits zero bucket
lines and only the coverage line; consumers must tolerate that. The JSONL stream
does **not** include the `timezone` and `generated_at` top-level fields that the
non-JSONL (`json.dumps(..., indent=2)`) form has — a consumer that needs the
generation timestamp must use the non-JSONL form or stamp its own receipt time.

## Bucket line: `bucket`

String. Hourly bucket label in Europe/Berlin wall time, format
`%Y-%m-%dT%H:00:00%z`, e.g. `2026-10-04T13:00:00+0200`. The UTC offset is part
of the label (`+0200` in summer, `+0100` in winter), so a task is grouped by its
Berlin local hour. Edge case: in the repeated hour of a fall-back DST
transition, the two wall-clock-identical hours are distinct labels
(`...T02:00:00+0200` and `...T02:00:00+0100`) and their lexicographic order is
not chronological; sort by the offset-aware instant, not by raw label, if that
matters to the dashboard.

## Bucket line: `tasks[]`

Non-empty array. One entry per task whose recorded `created_at` (interpreted as
UTC) falls into that Berlin hour. Entry fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Task id as submitted. |
| `state` | string | Lifecycle state as recorded (`queued`, `reserved`, `starting`, `running`, `completed-awaiting-review`, `accepted`, `failed`, `blocked`, `launch-uncertain`, `stalled`). |
| `reviewer` | string \| null | Reviewing owner; **null** until a review action happened — render as “not yet”, not as an empty reviewer. |
| `reason` | string \| null | Rejection/failure reason when recorded; **null** means “no reason recorded”, never “no problem”. |
| `usage` | object | See [Usage object](#usage-object). |
| `quota` | object | See [Quota object](#quota-object). |

Attribution caveat: a task with a missing or unparseable `created_at` is
attributed to the *generation* hour (`now` at report time), not dropped. The
dashboard cannot distinguish such a task from a task genuinely created in that
hour; treat a suspiciously dense current-hour bucket accordingly.

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
{"coverage": {
  "window_hours": 24,
  "first_bucket": "<label> | null",
  "last_bucket": "<label> | null",
  "gaps_within_last_24h": ["<label>", ...],
  "note": "historical hours without tasks are gaps, not simulated activity"
}}
```

- `window_hours`: fixed `24`.
- `first_bucket` / `last_bucket`: minimum/maximum bucket label present in the
  stream, or `null` when there are no buckets at all (empty store) — render
  null as “no data”.
- `gaps_within_last_24h`: explicit list of hourly labels (same
  `YYYY-MM-DDTHH:00:00±HHMM` Berlin format) for each of the 24 hours ending at
  the current Berlin hour (`hour_start - 23h … hour_start`) that has no bucket.
  These are **explicit gaps**: render as missing/no-data cells, never as zero
  activity. Hours older than the 24h window are simply absent from the stream
  and are *not* listed as gaps — absence outside the window means “not covered
  by this projection”, absence inside the window means “covered and empty”.

## Unknown-vs-zero rendering policy (summary)

| Field | Unknown value | Dashboard must render |
| --- | --- | --- |
| `tasks[].usage.{input_tokens,output_tokens,cached_tokens,cost}` | `null` | “unknown” / “—”, never `0`, never estimated |
| `tasks[].usage.source` | `"unproven"` | provenance badge “unproven”; only non-`unproven` sources may claim trust |
| `tasks[].quota.percent_delta`, `tasks[].quota.snapshots` | `null` | “unknown”; never blended into usage/cost |
| `tasks[].reviewer`, `tasks[].reason` | `null` | “—” (not applicable yet / not recorded), not an error state by itself |
| `coverage.first_bucket`, `coverage.last_bucket` | `null` | “no data” |
| `coverage.gaps_within_last_24h[]` | label present | explicit gap marker, not zero |

## Real example line (read-only capture)

Produced by actually running `python3 -m launcher report --jsonl` on
2026-10-04 against the live default store (`~/.config/agent-quota-launcher/state.db`),
no state modified. Bucket line (first stream line):

```json
{"bucket": "2026-10-04T13:00:00+0200", "tasks": [{"id": "genuine-1", "state": "failed", "reviewer": null, "reason": null, "usage": {"input_tokens": null, "output_tokens": null, "cached_tokens": null, "cost": null, "source": "unproven"}, "quota": {"percent_delta": null, "snapshots": null, "note": "account percent delta is not token use or cost"}}, {"id": "genuine-2", "state": "failed", "reviewer": null, "reason": null, "usage": {"input_tokens": null, "output_tokens": null, "cached_tokens": null, "cost": null, "source": "unproven"}, "quota": {"percent_delta": null, "snapshots": null, "note": "account percent delta is not token use or cost"}}, {"id": "genuine-3", "state": "failed", "reviewer": null, "reason": null, "usage": {"input_tokens": null, "output_tokens": null, "cached_tokens": null, "cost": null, "source": "unproven"}, "quota": {"percent_delta": null, "snapshots": null, "note": "account percent delta is not token use or cost"}}, {"id": "genuine-4", "state": "stalled", "reviewer": null, "reason": null, "usage": {"input_tokens": null, "output_tokens": null, "cached_tokens": null, "cost": null, "source": "unproven"}, "quota": {"percent_delta": null, "snapshots": null, "note": "account percent delta is not token use or cost"}}, {"id": "genuine-5", "state": "stalled", "reviewer": null, "reason": null, "usage": {"input_tokens": null, "output_tokens": null, "cached_tokens": null, "cost": null, "source": "unproven"}, "quota": {"percent_delta": null, "snapshots": null, "note": "account percent delta is not token use or cost"}}]}
```

Coverage line (last stream line of the same run; the 23 gap labels are shown in
full for fidelity):

```json
{"coverage": {"window_hours": 24, "first_bucket": "2026-10-04T13:00:00+0200", "last_bucket": "2026-10-04T13:00:00+0200", "gaps_within_last_24h": ["2026-10-03T15:00:00+0200", "2026-10-03T16:00:00+0200", "2026-10-03T17:00:00+0200", "2026-10-03T18:00:00+0200", "2026-10-03T19:00:00+0200", "2026-10-03T20:00:00+0200", "2026-10-03T21:00:00+0200", "2026-10-03T22:00:00+0200", "2026-10-03T23:00:00+0200", "2026-10-04T00:00:00+0200", "2026-10-04T01:00:00+0200", "2026-10-04T02:00:00+0200", "2026-10-04T03:00:00+0200", "2026-10-04T04:00:00+0200", "2026-10-04T05:00:00+0200", "2026-10-04T06:00:00+0200", "2026-10-04T07:00:00+0200", "2026-10-04T08:00:00+0200", "2026-10-04T09:00:00+0200", "2026-10-04T10:00:00+0200", "2026-10-04T11:00:00+0200", "2026-10-04T12:00:00+0200", "2026-10-04T14:00:00+0200"], "note": "historical hours without tasks are gaps, not simulated activity"}}
```

Note how the example exercises every contract rule at once: five tasks in one
bucket with `state` `failed`/`stalled`, all-null usage with `source:
"unproven"`, all-null quota, and 23 of 24 window hours explicitly named as
gaps (the current wall-clock hour `14:00` is a gap too because it has no tasks
yet).
