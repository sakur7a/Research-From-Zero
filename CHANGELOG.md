# Changelog

## Unreleased — task budgets and permissions move into settings

- Move the task budget limits and the library-metadata permission out of the
  new-task form and into the settings dialog. The composer now shows a one-line
  summary and a link back to settings, so the start of a task has one fewer
  collapsed block to read.
- Add workspace defaults for new tasks, stored in the same SQLite database
  (`agent_settings`) so they survive a restart. New route: `PUT /api/agent/defaults`;
  new keys in `GET /api/agent/config`.
- An omitted budget field now resolves to the stored default instead of a
  hardcoded constant. An explicit per-task value still wins, and a task keeps its
  own saved limits when the workspace default changes later.
- The per-task consent text names local-library bibliographic material whenever
  that permission is enabled, so the scope of what leaves the machine stays
  explicit at the moment of starting a task.
- Clearing the in-memory model configuration no longer resets workspace defaults;
  `DELETE /api/agent/config` remains scoped to the model credential.
- Tests: 96 Python and 24 JavaScript tests plus JavaScript syntax checks.

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
