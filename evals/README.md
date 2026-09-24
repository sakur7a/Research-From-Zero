# Evals

A fixed, versioned set of public tasks with labels, so a claim about this system can point at
something reproducible instead of at one lucky run.

## Three channels, never mixed

| Channel | What it runs | Needs a key | Where |
|---|---|---|---|
| `unit` | fixtures and protocol contracts: `python -m pytest`, `npm test` | no | `backend/tests/`, `tests/` |
| `connector` | the real scholarly and resource services, no model | no | `evals/runner.py` (default) |
| `live` | a real task that plans and calls tools, then writes a report | **yes** (BYOK) | `evals/runner.py --channel live` |

A result from one channel never reaches another's metric. A fixture passing is not evidence about
research quality, and a live run failing is not evidence about the connector code.

## Running it

```bash
python evals/runner.py --list                  # the task ids, channel and kind
python evals/runner.py                         # every connector task (real network, no model)
python evals/runner.py --only recall-stable-layers
python evals/runner.py --channel live          # requires RE0_LLM_BASE_URL / RE0_LLM_MODEL / key
python evals/score.py                          # writes evals/results/SUMMARY.md
python evals/score.py --channel connector      # updates connector, keeps other channel summaries
```

## What the states mean

Every step carries a state, and the difference between them is the point:

| State | Meaning |
|---|---|
| `ok` | the check ran and matched the expectation |
| `missed` | the check ran and the expected identifier was **not** in the results — a coverage gap for that query and source, never evidence of absence |
| `contradicted` | the check ran and disagreed with the expectation |
| `unknown` | the check **did not complete** (network, rate limit, missing service). This is not a negative result |
| `deferred` | the expectation is read from another step of the same task |
| `human` | a person has to judge this; the runner cannot |

Task status follows from those: `completed`, `partial` (something was missed or contradicted),
`pending_human`, `unknown`, or `blocked` when a live task has no credentials.

## Two rules the code enforces

1. **No denominator is not 100%.** `score.py` reports `null` with a reason when nothing was judged,
   because a rate over no samples has no value and `1.0` would make an unfinished evaluation look
   finished.
2. **A miss is a miss.** The first real run reported a recall case as `completed` while the expected
   arXiv id was absent from the results. That is the exact overclaim this harness exists to catch, so
   `found_identifiers` now records `missed` and the task becomes `partial`.

## What is recorded, and what is never recorded

Each run records the tool trace with timings, the source revision where one exists, failures with
their reasons, coverage (sources queried, per-source counts, failures, duplicates merged, records
dropped by the year window), wall time, and usage as reported — or `unknown` when the provider
reported none, which is never counted as zero.

**Never recorded**: key values, `Authorization` headers, the full environment, or private queries.
Records list which credential *names* were configured, never a value.

## Labels

Labels live beside the tasks in `tasks.json`. A label may only claim `official` when a human
recorded who checked, when, and over what scope — machine-filled labels are marked `machine` and
their verdict is `unknown`, because a name match is not authorship.

## Current state

`evals/results/SUMMARY.md` is the public record. Raw run records in `evals/runs/` are regenerable and
not committed. Nothing in either is a claim about research quality: it says which tasks ran, what
each step found, and what was not measured.
