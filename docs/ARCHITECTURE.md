# Architecture · v0.2.0

## Product inversion

The primary object is a **research task**, not a manually populated paper record.
A model chooses read-only tool actions, inspects their results, revises a public
plan, and submits a structured report. The literature library becomes a store for
user-approved paper results. The v0.1 static adapters are tools, not the runtime.

This is a native Python tool-calling runtime, **not LangGraph, LangChain, a fixed
search/summarize pipeline, or simulated AI output**. A single configured model can
perform the planning and synthesis roles. It does not contain a trained model.
No additional runtime dependency or frontend build service has been introduced.

## Boundaries

```text
web/agent.html + agent.js        web/index.html + app.js
        / (research)                    /library (retained)
                   \                 /
                    FastAPI /api routes
                     │              │
              agent/api.py      service.py → papers/resources/observations
                     │
              agent/runtime.py (single worker, persisted loop)
                ├─ model.py → configured Chat Completions endpoint
                ├─ tools.py → allowlisted read-only research APIs
                └─ storage.py → SQLite tasks, checkpoints, evidence, events
                     │
              explicit approval → validated paper metadata → library
```

### Modules

| Module | Responsibility |
|---|---|
| `agent/schemas.py` | Config, task, approval, tool and report input contracts |
| `agent/model.py` | In-memory BYOK config, destination validation, bounded HTTP, common tool-call protocol |
| `agent/tools.py` | Tool registry, argument validation, consent-scoped adapters, source documents |
| `literature.py` | Multi-source scholarly connectors, cross-source de-duplication, year window |
| `env_file.py` | Opt-in dotenv loader; fills only unset variables and never prints a value |
| `agent/storage.py` | Additive schema, append-only evidence/events, replayable results, atomic approved import |
| `agent/runtime.py` | Model/tool iteration, budget reservation, cancellation, checkpoint/recovery, context bounds, report validation |
| `agent/api.py` | UI-facing task/config endpoints, no credentials or internal messages in reads/exports |
| `mcp_server.py` | MCP-over-stdio retrieval surface; reuses the same tool contracts and opens no database |
| `skill_search.py` | The literature-search capability's own surface: query, merge, render, report coverage. Both entry points below call it, so there is no second copy of the logic. It renders what `resource_audit.py` already fetched and decided |
| `cli.py` | Thin console entry points (`re0 paper search`, `re0 doctor`, `re0 mcp`). Dispatches only; starts no LLM, and `doctor` probes the network only when asked |
| `resource_audit.py` | Candidate discovery and the field-level resource audit, as data rather than as print statements. Owns the two shared budgets, the audit states and the per-artifact-class coverage; every affirmative conclusion carries the source it rests on |
| `resource_matrix.py` | Three renderings of one row list (JSON, Markdown, CSV) for comparing 2–6 papers, plus the importable approval payload. Re-renders, never re-checks; escapes spreadsheet formulas and writes only http(s) links |
| `workspace.py` | The opt-in source store behind `--workspace`: content-addressed ids over a source's identity only, snapshots of tool results, and an import that previews, stays idempotent and refuses a bundle from another workspace. An audit travels beside the identity, never inside it, so re-checking a repository does not mint a second source |
| `service.py` | The library: papers, resources and an append-only observation history. Each record is labelled `observation` or `confirmation`, so a check and a human revision of it stay separately retrievable |
| `result_model.py` | The versioned result shape (`schema_version`) that every exit shares. Unknown fields ride along under `unrecognised` instead of vanishing, and a bounded body states `content_chars`/`excerpt_chars`/`truncated` so a cut body cannot look like a short one |
| `web/agent-core.js` | Pure status/tool presentation logic, independently tested |
| `web/agent.js` | Task composer, model settings, polling trace, report and evidence views |
| `web/theme.css` + `web/theme.js` | Shared Emilia light/dark palette (every colour a variable) and the persisted theme toggle used by both pages; no hardcoded colours remain in page CSS |

## Loop, not a predetermined chain

1. Resolve any omitted budget field against the workspace defaults, then save the
   goal, permissions, the **resolved** limits (so a later default change cannot
   alter a saved task), public model configuration and initial messages.
2. Reserve a model-call count **before** I/O and checkpoint it.
3. Ask the model for normal assistant/tool-call output; do not request or persist
   provider-specific hidden reasoning fields. Ordinary assistant/tool messages
   needed for subsequent protocol rounds are checkpointed internally.
4. Reassign unique per-turn call IDs. Save pending calls before executing them.
5. Validate the named tool, task consent and arguments. Reserve tool budget.
6. Execute one bounded read-only tool, atomically store its evidence plus result,
   then append a **bounded excerpt** of that result to model context and checkpoint.
   Whole source bodies never enter the conversation; `read_evidence` serves a slice
   of a stored body on request.
7. Repeat. The model can change queries, inspect discovered resources, read files,
   revise its plan, or finish; no fixed ordering is baked into this loop.
8. `finish_report` must use valid per-task evidence IDs. On success the runtime
   saves a report. The separate approval route imports source-derived paper data.

A schema-valid model answer can still be scientifically wrong. The current
validator checks identity and structure, **not entailment**, official-resource
identity, theorem validity, benchmark comparability or successful reproduction.
Report summaries and inferred findings remain model output requiring review.

## Persistence and concurrency

The original `schema_version=1` tables are unchanged. Independent
`agent_schema_version=1` tables are added in the same database:

- `agent_runs`: input, public model config, status, internal messages/pending work,
  plan/report, call counts and reported token usage;
- `agent_events`: append-only public operational event records, not private reasoning;
- `agent_evidence`: provider-derived documents with source locator, time and tool;
- `agent_tool_results`: idempotent replay by `(run_id, call_id)`;
- `agent_imports`: idempotent mapping from approved evidence to a library item;
- `agent_settings`: workspace-level defaults (task budgets, library permission).
  Contains no credentials and is deliberately independent of the in-memory model
  config, so clearing the model does not reset workspace policy.

An HTTP server process owns **one** worker. Do not run Uvicorn with multiple
workers or two application processes against the same task database. There is no
distributed scheduler, process lease or multi-user isolation in this release.
Application startup marks unfinished work `interrupted`; it does not silently
resume paid requests. User-requested resume uses the same model/base URL/token
parameter/output limit, at most three times. Calls stay cumulative; elapsed time
starts a new explicitly authorized attempt window.

A tool result and its evidence commit together before conversation advancement.
If the process stops in between, the result is replayed without another fetch.
A crash during an in-flight read may still require repeating that read; there is
no claim of exactly-once external I/O. A reserved model request might have been
billed without a returned response; its call count remains reserved.

States: `queued → running → completed | failed | cancelled | interrupted |
budget_exhausted`. Completed/cancelled/budget-exhausted runs are not resumable.
User steering after a completed run and multi-turn research conversations are not
implemented yet. Create a new scoped task instead.

## Budgets and network controls

Task defaults are 12 model calls, 20 tool calls and 360 seconds per explicit
attempt; schema upper bounds are 24, 40 and 900. In-flight calls can finish or time
out after a cancellation/time boundary. This is not a hard wall-clock or monetary
cap. Prompt/context text is capped at 150,000 serialized characters.

Context cost is managed in three steps, because a tool result is re-sent on every
later model call and its size therefore multiplies by the remaining turns:

- A tool result enters the conversation as metadata plus a bounded excerpt: 6,000
  characters shared across one result, one item allowed to claim all of it. Each
  item records `content_chars` and `elided`, so the model can see that it is reading
  a excerpt. Whole bodies stay in `agent_evidence`.
- `read_evidence` serves a slice of a stored body (200–12,000 characters, with an
  offset to continue), so a long body does not have to sit in context for the whole
  task. It reads only evidence belonging to the current task.
- Once the serialized conversation passes 110,000 characters, the excerpts of all
  but the two most recent tool results are dropped, the model is told in-band how to
  fetch them back, and a `context_compacted` event is recorded in the public trace.
  Evidence rows are never deleted or rewritten. This is a stated elision, not a
  silent loss of source context.

The 150,000-character cap remains as a last-resort explicit failure: an elided
conversation that still exceeds it fails rather than quietly shrinking further.
Compaction reduces prompt size; it is **not** a claim of lower billing, and no
currency estimate is produced. Whether these bounds match a real provider's cache
behaviour has not been measured.

Model HTTP uses a selected endpoint, HTTPS for allowlisted remote domains, or an
explicit-port loopback service. No redirects, auto retries, provider failover or
system proxy inheritance. Response limit 2 MiB; provider error bodies are not
shown. The raw configured model credential is redacted if echoed in outputs.
This is not a general secret detector for sensitive user-supplied task text.

The settings picker uses the same destination rules. `POST /api/agent/models`
validates the supplied base URL exactly like a model call, then performs one
`GET {base_url}/models` with a 20-second timeout and a 512 KiB response bound, and
returns normalized IDs. The key travels with that one request and is never stored,
returned or checkpointed. Providers commonly omit this endpoint; the route then
fails with an app-authored message and the user types a model ID. A returned list
is a statement about what the service lists, **not** about tool-calling support —
`POST /api/agent/config/test` stays the only gate. The offered presets are a subset
of the destination allowlist, which a test asserts so the two cannot drift apart.

Research tools use fixed GitHub/HF/arXiv/Crossref endpoints and the old bounded
provider client. Optional Tavily adds search snippets via one fixed POST endpoint.
Agent-generated URLs never become unrestricted HTTP destinations. Repository file
reads resolve the requested ref to a commit and return bounded text, not code
execution. GitHub token, Tavily token and model token are separately scoped.

`search_papers` queries five scholarly services (`semanticscholar`, `openalex`,
`arxiv`, `openreview`, `crossref`) with one shared client: `max_calls=12`,
`seconds=60`, `read_timeout=20`. The longer read timeout is not cosmetic — arXiv
measures over 5 s for a plain query, so the 8 s default tuned for metadata endpoints
would fail it every time. There is one retry for a transient status (429/500/502/503/504)
and none otherwise: an automatic retry of a paid or rate-limited request must stay visible.

De-duplication matches on **every** identifier a record carries (DOI, arXiv ID, and a
normalised title of at least 16 characters), not one chosen key. A single key fails the
common case where one service reports a DOI and another only a title, and the merged
entry keeps the list of services that reported it. Sources are queried in signal order,
so the first to report a work supplies its fields; a later duplicate only adds
provenance, a higher citation count, and identifiers the first source lacked.

A source that fails is reported with its own error and is **never** folded into an empty
result. If every source fails the call raises instead of returning an empty list, because
an empty list would be read as "no such work". Unknown publication years are not filtered
by the year window: "we could not tell" is not "out of range".

Library metadata tools are absent unless the task carries the library permission.
That flag now defaults from the workspace setting and the per-task consent text
names local-library material whenever it is on, so the scope of what leaves the
machine is stated at the moment the task starts. Notes and demo records are
excluded even when consented. Tools never see the model config or key.
The model has no approval, configuration-write, library-delete or shell tool.

## HTTP interface

All write endpoints preserve the original JSON, same-origin and
`X-Re0-Client: web` requirements. These guards are **not authentication**.

| Route | Purpose |
|---|---|
| `GET /api/agent/config` | Redacted config, capabilities, task defaults, endpoint presets, busy status |
| `PUT /api/agent/config` | Explicitly trusted model settings, memory only |
| `DELETE /api/agent/config` | Clear in-memory settings (not shell environment) |
| `POST /api/agent/config/test` | One potentially billed tool-call capability test |
| `POST /api/agent/models` | One allowlist-checked `GET {base_url}/models`; key not stored |
| `PUT /api/agent/defaults` | Workspace budgets and library permission for **new** tasks |
| `GET, POST /api/agent/runs` | Last 100 tasks / start a task |
| `GET /api/agent/runs/{id}` | Status, plan, report, source evidence and usage |
| `GET /api/agent/runs/{id}/events?after=...` | Incremental public event records |
| `POST /api/agent/runs/{id}/cancel` | Stop at the next safe boundary |
| `POST /api/agent/runs/{id}/resume` | Explicit recovery of interrupted/failed work |
| `POST /api/agent/runs/{id}/evidence/{eid}/import` | User-approved metadata import |
| `GET /api/agent/runs/{id}/export` | Public task report/evidence JSON, no key or checkpoint |

### Second surface: MCP over stdio

`re0/mcp_server.py` requires the handshake before any tool request, answers a requested protocol
version with one it actually implements rather than echoing an unimplemented one, and turns every
malformed request into a defined JSON-RPC error instead of an exception that would end the loop. Its
default is stateless: no directory is opened, no database is touched, and `--workspace DIR` is the
only thing that changes that.

It exposes the same read-only retrieval tools to a foreign agent, and returns
`structuredContent` beside the text summary. The summary is rendered *from* that structure, so the
two cannot describe one call differently, and it names any field it did not print rather than
looking complete. A failed source is listed before the documents, because a source that was not
searched is not an empty result
without going through HTTP. It is deliberately narrow:

- Tool descriptors are generated from the same `TOOL_TYPES` contracts used inside a
  task, so the two surfaces cannot drift apart.
- Excluded: `update_plan`/`finish_report` (task-protocol steps), `read_evidence`
  (task-scoped), `search_library` (gated on per-task user consent). `search_web`
  appears only when `TAVILY_API_KEY` is set, matching task behaviour.
- It constructs `ResearchTools` with no library, so the process opens **no database**
  and is stateless: no run, no checkpoint, no evidence ID. A caller therefore gets a
  locator and a source URL, and must not read Re0's citation guarantee into the
  result — that guarantee is scoped to Re0's own runs.
- Output is bounded (1500 characters per document) because the caller's context pays
  for it, and provider failures and validation errors are reported as app-authored
  text rather than relayed upstream bodies or tracebacks.
- MCP over stdio is newline-delimited JSON-RPC 2.0, so this adds **no runtime
  dependency**. It answers `initialize`, `ping`, `tools/list`, `tools/call`, returns
  `-32601` for anything else, ignores notifications and malformed input, and echoes
  the client's requested protocol version so a newer client still handshakes.
- Nothing may be written to stdout except protocol messages.

Original `/api/papers`, topics, resources, CSL imports, demo and export endpoints
remain. The UI polls operational events; it does **not** stream token output or
SSE in this version. Task exports and library exports are distinct; SQLite backup
is the complete recovery method.

## Continuing a conversation

A **run** is one attempt. A **conversation** is the line of attempts that share evidence and one
cumulative ledger, and each run carries `conversation_id`, `turn` and `kind` (`new`, `followup`,
`retry`). The three kinds are distinct because they authorize different things: a follow-up names a
new constraint and the evidence it reuses, while a retry repeats the parent's goal verbatim and so
has no field through which new scope could arrive.

`agent/session.py` holds the rules, and the API, the console and the web composer all call it — the
page collects an authorization, it does not decide one. Scope is validated **before** the run exists,
so a refusal leaves nothing half-created. Reuse reaches a turn by two routes only: an evidence id
from the same conversation, or a content-addressed source id from an opt-in workspace. An id from
another conversation is refused as out of scope rather than reported missing, because "not found"
sends the caller looking for a typo where the real problem is authorization.

Each turn stores an immutable `origin` snapshot — goal, model destination, permissions, allowed
tools, budget and the ledger as it stood before the turn. No checkpoint rewrites it, so a later
change to a budget or a permission cannot retroactively explain a turn that already ran. Nothing is
inherited silently: library consent is granted per turn, and a destination that differs from the
parent's needs an explicit confirmation.

The ledger is **recomputed from the runs**, not incremented. That is the point: a counter that is
added to somewhere can also be zeroed somewhere, and the guarantee this exists for is that it cannot.
A follow-up therefore cannot escape a cap by being a new run. Caps are enforced live inside the
worker as well as at admission, read from the turn's own snapshot so a cap raised by a later request
cannot widen a turn already running.

A new report is a new version beside the old one. `report_delta` matches findings by the evidence
they rest on — mapped back through reuse, since reuse mints a fresh evidence id per turn — so a
finding carried forward unchanged is not reported as one addition and one drop. The handover gives
the next turn the parent's goal and report rather than its transcript, which reuses the existing
compaction and `read_evidence` instead of building a second memory.

## Deliberately not implemented

Arbitrary website fetching/browser automation, vector memory, theorem graphs,
scheduled monitoring, Zotero live sync, remote code execution, multi-agent
specialist teams, multi-user authentication, exact dollar accounting,
model-specific reasoning protocols and a distributed task queue.
These should be added only against real evaluated workflows, not described as
hidden existing capabilities. See ROADMAP.md for the staged direction.
