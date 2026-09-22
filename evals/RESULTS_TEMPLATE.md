# Evaluation summary — channel `{{channel}}`

Template. `python evals/score.py` fills this in and writes `evals/results/SUMMARY.md`.

## What ran

- task set version: `{{task_set_version}}`
- tasks run: `{{tasks_total}}` ({{tasks_by_status}})
- blocked for want of credentials: `{{blocked}}`
- steps: {{steps}}
- recall misses (expected identifier not in the results): `{{recall_misses}}`

## Metrics

Each metric is a fraction with its denominator, or `no value` plus the reason it has none. A `null`
is not a zero and not a one.

| Metric | Value | Denominator | Note |
|---|---|---|---|
| Official-candidate accuracy | {{official_candidate_accuracy}} | judges only | claims of authorship confirmed by a person over judged samples |
| Resource availability false-positive rate | {{availability_fp}} | resource statuses produced | a status the check contradicted |
| Coverage unknown ratio | {{coverage_unknown}} | retrieval steps | steps that did not complete, keyed separately from misses |
| Evidence support rate | pending | — | needs a completed live task with a report |
| Valid source location rate | pending | — | needs findings that cite locations |
| Full-task rate | {{full_task_rate}} | tasks | completed over run |
| Human revision count | {{human_review_pending}} | — | expectations waiting on a person |
| Usage | {{usage}} | — | provider-reported, or `unknown`; never counted as zero |

## Not measured yet

{{pending}}

## Reading this

- A `missed` step is a coverage gap for one query and one source. It is not evidence that a work does
  not exist, and it is not a failure of the underlying record.
- Channels are separate. `connector` results say nothing about model quality, and `live` results say
  nothing about connector correctness.
- Usage reported as `unknown` means the provider did not report it. It is never recorded as zero, so
  a cost summary built from this file is a lower bound, not a total.
- Nothing here evaluates whether the papers are correct, whether code runs, or whether a released
  resource has been downloaded.
