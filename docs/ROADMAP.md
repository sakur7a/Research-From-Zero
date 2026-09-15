# Roadmap · Agent-first

## Implemented in v0.2

The unit of work is now a research task. The application includes a BYOK model
gateway, dynamic tool-calling loop, read-only research tools, persistent task
checkpoints, evidence-bound reports, a task-first UI and explicit import approval.
The original library and data remain available; they are not a substitute for AI.

## Next acceptance milestone: one real research loop

Run user-selected tool-capable models against a small, manually checked paper set
before expanding infrastructure. Track official-resource precision, unsupported
claims, source match, incomplete searches, provider failures, human correction,
request/token usage and time. Record model, endpoint protocol, prompt version,
search date and exact dataset. Do not turn protocol-fixture pass rates into
research-quality claims.

The target task is: given a narrow topic and experimental requirements, find
candidate papers, follow likely official repository/model links, compare code,
weights/data/evaluation coverage, explain missing evidence, and let the user save
the chosen papers with their provenance.

## Subsequent stages (not implemented)

| Stage | Work | Acceptance condition |
|---|---|---|
| Conversation and steering | Follow-up messages, editable plan/constraints, resume with explicit changed goals | Preserve prior sources, show changes, reauthorize added material/cost |
| Fulltext reading | DOI/arXiv to licensed/OA fulltext, parsing, section/page evidence, supplementary material | No false claim of reading the full paper; locator-level evaluation |
| Broader resource discovery | Safe webpage retrieval, more release/discussion adapters, author/resource identity verification | Measure incorrect official attribution and inaccessible/gated cases |
| Structured resource verdicts | Separate declaration, accessibility, version match, coverage and execution depth | Every field cites evidence; uncertainty and human overrides persist |
| Durable knowledge | Topic schemas, paper versions, claims/assumptions/method relations, retrieval memory | Trace each edge to a source and distinguish inference from author claims |
| Zotero connection | Read-only incremental sync first, then previewed write-back | Identity/version conflict handling; never overwrite annotations silently |
| Monitoring | Scheduled resource rechecks and user notifications | Explicit opt-in, cadence/budget, change evidence and unsubscribe |
| Product scale | Authentication, encrypted secret store, separate worker/queue, database migrations, SSE | Multi-user access controls and durable worker ownership tested first |

## Architectural decisions

FastAPI and SQLite are retained because the product change is in orchestration,
state, tool permissions, evidence and human approval, not a frontend framework
name. The runtime is intentionally modular: a future LangGraph/other engine may
replace orchestration without replacing library/provenance APIs. No dependency
has been added merely to make the project appear agentic.

Start with one good model configuration. Specialist agents, separate embedding
models or a vector index need demonstrated quality/cost benefits before adoption.
Do not start model training before evaluation identifies a need.
