# Changelog

## Unreleased

### Multi-source literature search, and a skill for it

- `search_papers` grows from two sources to five — Semantic Scholar, OpenAlex, arXiv,
  OpenReview and Crossref — with an optional `start_year`/`end_year` window. `source='all'`
  is now the default; a single source name still works for a targeted recheck.
- Results are de-duplicated **across** sources. Matching uses every identifier a record
  carries (DOI, arXiv ID, and a normalised title of at least 16 characters) rather than one
  chosen key, because a single key misses the common case where one service reports a DOI
  and another only a title. The merged entry keeps which services reported it, the highest
  citation count, and identifiers only a later source had. A short title is not used for
  matching at all — "Survey" is too easy to collide on.
- **A failing source is reported, not swallowed.** Rate limits, timeouts and bad tokens are
  listed per source, and if *every* source fails the call raises instead of returning an
  empty list, because an empty list reads as "no such work". Unknown publication years are
  never filtered out by the year window. One transient retry (429/5xx) is attempted and
  nothing else: an automatic retry of a rate-limited request must stay visible.
- Raise the per-request read timeout for retrieval to 20 s. arXiv measures over 5 s for a
  plain query, so the 8 s default tuned for GitHub/HF metadata failed it every time.
- Add `skills/paper-search/`: a self-contained skill that runs the multi-source search
  from the command line, prints per-source hit counts, lists failures on stderr, and sinks
  survey/review papers to the bottom as `[survey]` without removing them. It deliberately
  has no "model knowledge" source, and no semantic relevance filter that could hide rows.
- Add opt-in credential loading: `RE0_ENV_FILE` (also honoured by `run.py`) loads a dotenv
  file, filling only variables that are not already set. Values are never printed, logged
  or written to a task record; Re0 still reads no file unless asked.
- Tests: 27 Python cases added.

### MCP retrieval surface

- Add `re0/mcp_server.py`: an MCP-over-stdio server that exposes the read-only
  retrieval tools, so another agent (Claude Code, Codex, dsh, …) can use them without
  Re0's UI. Start it with `python -m re0.mcp_server`.
- Tool descriptors come from the same `TOOL_TYPES` contracts the in-task model sees,
  so the two surfaces cannot drift. `update_plan`, `finish_report` and the
  task-scoped `read_evidence` are not exposed, nor is the consent-gated
  `search_library`; `search_web` appears only when `TAVILY_API_KEY` is configured.
- The surface is stateless and constructs no library, so the process opens **no
  database**: it creates no run, no checkpoint and no evidence ID. A caller therefore
  receives a locator and a source URL and does **not** inherit Re0's "every finding
  cites this task's evidence" guarantee. Output is bounded per document (1500
  characters) because the caller's context pays for it.
- MCP over stdio is newline-delimited JSON-RPC 2.0, so this adds **no runtime
  dependency**; Re0 still ships four packages. The server answers `initialize`,
  `ping`, `tools/list` and `tools/call`, returns `-32601` otherwise, ignores
  notifications and malformed lines, and echoes the client's protocol version so a
  newer client still handshakes.
- Tests: 7 Python cases added.

### Context cost of the agent loop

- A tool result no longer pastes whole source bodies into the conversation. It
  carries metadata plus a bounded excerpt (6000 characters shared per result; a
  single item may claim all of it), with `content_chars` and `elided` recording
  what was left out. The full body stays in `agent_evidence`. A tool result is
  re-sent on every later model call, so this was the dominant context cost of a
  long task and it multiplied by the number of remaining turns.
- Add `read_evidence(evidence_id, offset, chars)` so the model can pull a stored
  body back, or continue past an excerpt, instead of the body having to stay in
  context for the whole task. It serves only this task's evidence and is bounded
  (200–12000 characters per call, 0–100000 offset).
- Add in-band compaction. Once the serialized conversation passes 110000
  characters, the excerpts of all but the two most recent tool results are dropped,
  the model is told in-band how to fetch them back, and a `context_compacted` event
  is written to the public trace. Evidence rows are never deleted, so this is a
  stated elision rather than a silent loss of source context; the 150000-character
  cap in the model gateway remains as a last-resort explicit failure.
- Widen `wait_done` in the backend tests from 10 s to 30 s. Measured here, a trivial
  GET costs 51–193 ms, so a fixture task with several rounds needs more than 10 s of
  wall time; the old deadline made a few tests flaky rather than wrong.
- Tests: 4 Python and 1 JavaScript cases added.

### Fixed

- Notices raised while the settings dialog was open were invisible. The toast lives
  in `<body>`, but a modal `<dialog>` and its `::backdrop { backdrop-filter: blur() }`
  occupy the top layer and painted over it, so the text looked blurred and
  unreachable. The toast is now moved into the open dialog to stay in the top layer,
  and returned to `<body>` when the dialog closes. This affected every notice raised
  from the dialog — including a wrong API key returning 422 — not only the model
  picker's result.

### Model setup from a provider preset

- Replace the free-text provider field with a preset list served by the backend
  (`endpoint_presets` in `GET /api/agent/config`), so a user picks a platform and
  pastes only an API key. Presets: OpenAI, DeepSeek, DashScope (both regions),
  Moonshot, Zhipu GLM, SiliconFlow, Volcengine Ark, OpenRouter and local Ollama.
- Add `POST /api/agent/models`: one bounded, allowlist-checked
  `GET {base_url}/models` that returns candidate model IDs for the picker. The key
  is used for that single request only and is never stored, returned, or written
  into a task record. A provider that does not implement the endpoint fails with a
  clear message and the user types a model ID instead; nothing is guessed.
- Model IDs are offered through a `<datalist>` and can still be typed by hand.
- **The returned list is not a compatibility claim.** It reports which models the
  service lists, not which support tool calling. The existing "test tool call"
  step remains the only gate before a research task can run.
- Add four hosts to the destination allowlist: `open.bigmodel.cn`,
  `api.moonshot.cn`, `api.siliconflow.cn`, `ark.cn-beijing.volces.com`. This is a
  network destination permit, not a tested-compatibility list, and a drift test
  asserts every offered preset host stays inside it.
- Tests: 5 Python and 1 JavaScript cases added.

### Task budgets and permissions move into settings

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

- Rework the frontend UI into an Emilia (Re:Zero) dual-theme palette with a
  light mode (bg `#F7F5FA`, primary `#995FB4`, accent `#28A878`, ink `#332448`)
  and a dark mode (bg `#1A1628`, primary `#A274C2`, accent `#32B886`,
  ink `#E6E8F2`). A toggle in the top bar / sidebar switches themes, the choice
  persists in `localStorage`, and light is the default. New `web/theme.css`
  holds every colour as a variable and `web/theme.js` wires the toggle;
  `styles.css` and `agent.css` no longer contain hardcoded colour values.
  Favicon and `meta theme-color` follow the theme.
- The two browser smoke scripts now inline `theme.css` and load `theme.js`
  (previously they only inlined the page CSS), write their JSON report before
  closing the browser, and use `ignore_cleanup_errors` temp dirs so a slow
  child-process handle release on Windows cannot turn a passing run into a
  cleanup error.
- Tests: 96 Python and 24 JavaScript tests, JS syntax checks, both browser
  smokes (8 and 10 groups) and the HTTP smoke all pass after the change.

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
