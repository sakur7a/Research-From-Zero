# Connector evaluation snapshot — 2026-09-24

Task set version `1` (created 2026-09-22); public connector-only run, written around 12:39 Asia/Shanghai. No model credentials were configured. The six raw run records remain regenerable under the ignored `evals/runs/` directory; this file is the shareable review snapshot.

## Aggregate

- Tasks: 6 — 2 completed, 1 partial, 3 pending human review, 0 unknown.
- Steps: 7 ok, 1 deferred, 4 human, 1 missed, 0 unknown or contradicted.
- Expected-identifier recall miss: 1 (`arXiv:2605.30257` was absent from OpenAlex's top 25 for `layer decomposition`). A query miss is a coverage gap, not evidence the paper is absent.
- Official-candidate accuracy: no value; the three task-set labels remain machine-recorded `unknown`.
- Availability false-positive rate: 0/3 in this small set. This does not establish a general error rate.
- Provider-reported usage is unknown in all six records. No live-model task was run.

## Run records

| Task | Recorded result | Scope and review status |
|---|---|---|
| `reveallayer-paper-and-repo` | arXiv `2605.11818` resolved. GitHub `360CVGroup/RevealLayer` metadata was accessible at `da68ff58889a1799a1e75efd23e72bccebbf7589`; 703 file entries, with filename/README indicators for training (1), inference (2), evaluation (1), and environment (2). | No code, checkpoint or dataset was downloaded or run. Paper/repository authorship and any “official” label still need human review. [Paper](https://arxiv.org/abs/2605.11818) · [Repository](https://github.com/360CVGroup/RevealLayer) |
| `stable-layers-paper-and-repo` | arXiv `2605.30257` resolved. GitHub `Stability-AI/Stable-Layers` metadata was accessible at `b826314b34b12d7c7cce9f0de7f49a330bd8e011`; the bounded scanner saw four file entries and no indicator candidates. | The shallow inventory is not a release check. Human review remains pending for paper/repository attribution and whether the claimed release/artifacts are actually available. [Paper](https://arxiv.org/abs/2605.30257) · [Repository README](https://github.com/Stability-AI/Stable-Layers/blob/main/README.md) |
| `recall-reveallayer` | Found `arXiv:2605.11818` in 25 OpenAlex results for `occlusion aware image decomposition`. | One query/source only. |
| `recall-stable-layers` | Missed `arXiv:2605.30257` in 25 OpenAlex results for `layer decomposition`. | Recorded as a recall miss, not absence. |
| `recall-unworld-design` | Found `arXiv:2608.03971` in 25 OpenAlex results for `layer-native design`. | One query/source only. |
| `gated-or-absent-resource` | The deliberately unavailable example URL returned 404 and was recorded `indeterminate`, matching the allowed status. | A 404 does not prove non-publication or non-existence; the wording still needs human review. |

## Human review queue

The connector snapshot does not supply human judgments. A reviewer should record their identity, review date, checked sources, scope, verdict and unresolved limits for:

1. Whether the RevealLayer paper and candidate repository are author-linked; distinguish file-name indicators from working training/evaluation code, and confirm checkpoint/data availability separately.
2. Whether the Stable-Layers paper and repository are author-linked and which model artifacts are actually downloadable under the stated license. The repository README presents an inference release and a bundled adapter, while the automated run only read bounded metadata; this is a lead to verify, not a verdict.
3. Whether the deliberately unavailable resource output correctly avoids an unpublished/absent claim.

These public-source pointers are review aids, not a completed human review: [RevealLayer arXiv record](https://arxiv.org/abs/2605.11818), [RevealLayer repository](https://github.com/360CVGroup/RevealLayer), [Stable-Layers arXiv record](https://arxiv.org/abs/2605.30257), [Stable-Layers repository README](https://github.com/Stability-AI/Stable-Layers/blob/main/README.md).

This snapshot is a small source-coverage report, not a research-quality, reproducibility, author-attribution or hosted-service result. It preserves all misses and unknowns and does not close #7 or #22.
