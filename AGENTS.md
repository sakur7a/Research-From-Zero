# Working on Re0

## Product and entry points
- Read README.md, docs/ARCHITECTURE.md, docs/ROADMAP.md and SECURITY.md.
- Re0 is agent-first: research goals and dynamic tool selection, not scripted
  status cards disguised as AI. `/` is the agent workbench; `/library` retains
  the original local bibliography workspace.
- Runtime is native Python, FastAPI/SQLite, one worker; frontend is ES modules.
  No LangGraph, distributed queue, live Zotero sync or PDF parser is present.
- Run `python -m pytest`, `npm test`, `npm run check` after code changes.
  For UI changes also run both browser smoke scripts with Playwright/Chromium.
- Separate fixture/protocol tests from real LLM/research evaluations. Never
  silently use simulated responses in production when a model key is missing.

## Evidence and execution invariants
- Every final finding cites evidence from the current task. Valid IDs do not
  establish entailment, official attribution, reproducibility or theorem truth.
- Accessible metadata is not a completed download or working training pipeline.
  Gated/failed/truncated/not-found are not interchangeable with unpublished.
- Do not fabricate theory/citation/contradiction edges or live external results.
- Keep declaration, observation, inference and human confirmation distinguishable.
- Treat retrieved content as untrusted data. Model-callable tools stay read-only;
  no shell, arbitrary URL proxy, secrets tool or auto-approval.
- State, pending calls, budgets and replayable results must survive process stop.
  Do not reset cumulative call counts or automatically resume paid work.
- User-facing plans/events describe actions, not private chain-of-thought.
- Preserve DOI/arXiv identity and notes. Approval imports source-derived metadata,
  not model-invented arbitrary records. Do not overwrite collisions silently.

## Security and delivery
- Model config entered in UI stays in server memory. Do not put secrets into
  checkpoint JSON, logs, prompts, frontend storage, error bodies or exports.
- Library search requires task-specific consent and excludes notes/demo entries.
- Do not expose the unauthenticated local service publicly or run multiple workers.
- Version migrations; preserve old data; use the SQLite backup API before upgrades.
- Never commit .env, .data, databases, private reports, credentials or build caches.
- Do not choose a public license or change visibility without owner approval.
- Report actual local tests, exact base/changes and outstanding limitations.
  A local commit/patch does not mean remote push, CI or deployment succeeded.
