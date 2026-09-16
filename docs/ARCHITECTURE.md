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
| `agent/storage.py` | Additive schema, append-only evidence/events, replayable results, atomic approved import |
| `agent/runtime.py` | Model/tool iteration, budget reservation, cancellation, checkpoint/recovery, report validation |
| `agent/api.py` | UI-facing task/config endpoints, no credentials or internal messages in reads/exports |
| `web/agent-core.js` | Pure status/tool presentation logic, independently tested |
| `web/agent.js` | Task composer, model settings, polling trace, report and evidence views |

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
   then append the result to model context and checkpoint.
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
cap. Prompt/context text is capped at 150,000 serialized characters; there is no
automatic context compaction yet. Hitting the cap preserves evidence and fails
explicitly rather than silently dropping source context.

Model HTTP uses a selected endpoint, HTTPS for allowlisted remote domains, or an
explicit-port loopback service. No redirects, auto retries, provider failover or
system proxy inheritance. Response limit 2 MiB; provider error bodies are not
shown. The raw configured model credential is redacted if echoed in outputs.
This is not a general secret detector for sensitive user-supplied task text.

Research tools use fixed GitHub/HF/arXiv/Crossref endpoints and the old bounded
provider client. Optional Tavily adds search snippets via one fixed POST endpoint.
Agent-generated URLs never become unrestricted HTTP destinations. Repository file
reads resolve the requested ref to a commit and return bounded text, not code
execution. GitHub token, Tavily token and model token are separately scoped.

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
| `GET /api/agent/config` | Redacted config, capabilities, task defaults, busy status |
| `PUT /api/agent/config` | Explicitly trusted model settings, memory only |
| `DELETE /api/agent/config` | Clear in-memory settings (not shell environment) |
| `POST /api/agent/config/test` | One potentially billed tool-call capability test |
| `PUT /api/agent/defaults` | Workspace budgets and library permission for **new** tasks |
| `GET, POST /api/agent/runs` | Last 100 tasks / start a task |
| `GET /api/agent/runs/{id}` | Status, plan, report, source evidence and usage |
| `GET /api/agent/runs/{id}/events?after=...` | Incremental public event records |
| `POST /api/agent/runs/{id}/cancel` | Stop at the next safe boundary |
| `POST /api/agent/runs/{id}/resume` | Explicit recovery of interrupted/failed work |
| `POST /api/agent/runs/{id}/evidence/{eid}/import` | User-approved metadata import |
| `GET /api/agent/runs/{id}/export` | Public task report/evidence JSON, no key or checkpoint |

Original `/api/papers`, topics, resources, CSL imports, demo and export endpoints
remain. The UI polls operational events; it does **not** stream token output or
SSE in this version. Task exports and library exports are distinct; SQLite backup
is the complete recovery method.

## Deliberately not implemented

PDF/fulltext ingestion, arbitrary website fetching/browser automation, vector
memory, theorem graphs, scheduled monitoring, Zotero live sync, remote code
execution, multi-agent specialist teams, multi-user authentication, exact dollar
accounting, model-specific reasoning protocols and a distributed task queue.
These should be added only against real evaluated workflows, not described as
hidden existing capabilities. See ROADMAP.md for the staged direction.
