# Changelog

## 0.2.0 — 2026-09-15 — Agent-first refactor

- Make `/` a research task workbench; move the retained library to `/library`.
- Add memory-only BYOK model settings and a real tool-call capability check.
- Add a native dynamic model/tool loop, persistent checkpoints and public events.
- Add scholarly/GitHub/Hub search, bounded commit-pinned text reads, release
  discussion search, consented library lookup and optional Tavily search snippets.
- Require current-task evidence IDs for structured reports; distinguish model
  interpretation from verified evidence. No semantic correctness guarantee.
- Add cooperative cancellation, manual recovery with cumulative budgets, and
  user-approved, idempotent import of source-derived paper metadata.
- Preserve v0.1 tables and data; add independently versioned task/evidence tables.
- Add fixture-based gateway/runtime/API tests and desktop/mobile browser flows.
- No new runtime dependency. No live model quality evaluation or Docker build.
- Publish the previously delivered v0.2 source on top of v0.1 without rewriting history.
- Keep real-model evaluation separate from local protocol tests and remote CI.

## 0.1.0 — 2026-09-15

Initial local FastAPI/SQLite research workspace: paper/topic management, bounded
GitHub/Hugging Face static observations, metadata/CSL import, comparison and
saved-relation views, export/backup, Chinese UI and offline regression tests.
No LLM was present in v0.1.
