# Roadmap · Agent-first

## Product order

The current sequence is **finish the existing `re0-paper-search` skill → reuse its
retrieval/audit core in the BYOK research platform → complete Web Demo and delivery
acceptance**. This is the direction recorded in [GitHub issue #15](https://github.com/sakur7a/Research-From-Zero/issues/15).
The online competition requirement is tracked separately; a static page or local
server is not an online release.

## Code present and acceptance still open

GitHub issues remain open until their full acceptance evidence exists. A commit that
implements a feature does not prove host compatibility, real research quality, or safe
public deployment.

| Issue | Current code | Acceptance still needed |
|---|---|---|
| #3 Skill delivery | Stable CLI, package data, preview-first installer, config self-check, source references; clean wheel and sdist install smoke now checks MCP dispatch and space-containing Windows paths | Complete the smoke on supported CI hosts and record the selected real client/host path from #1; do not infer client compatibility from the stdio handshake |
| #4 Shared contract | Versioned result model, MCP structured results, bounded source snapshots, CLI JSON export/import, owner-scoped Web bundle preview/commit/export and follow-up reuse by workspace/source ID; one generated fixture now compares CLI JSON, MCP and the Web workbench for title, publication, resource audit, failures and coverage denominator | Run the selected real client/host path from #1 and verify the official client renders structured results without hiding failures or unknown fields |
| #5 Search session | Multi-query, source coverage, bounded request/page budgets, conservative deduplication and venue reporting | Real-source coverage and recall notes for the fixed sample set; do not use fixtures as recall evidence |
| #6 Resource audit | Field-level audit, matrix export, append-only observations and confirmations; the library now exposes an explicit human review action | Use #7’s manually checked examples to measure incorrect attribution, unsupported claims and revision work |
| #7 Evaluation | Versioned task set, connector runner and scorer; 2026-09-23 public-source run recorded 6 cases (1 complete, 1 partial, 3 pending human review, 1 unknown), while the no-credential live case remains `blocked` | Human-review the queued labels/expectations and run authorized BYOK/client tasks; preserve failures and provider-unknown usage, and do not infer quality from fixtures |
| #8 Full text | Bounded HTML/PDF text reading, parser and locator metadata | Compare parser quality on representative papers and record real citation-location review |
| #9 Continued session | Follow-up turns, reuse authorization, cumulative budgets, immutable run snapshots and report deltas | Validate with a real model and task after #1 authorizes the endpoint and spend limit |
| #11 Zotero | Read-only incremental sync, preview/commit, scoped collection selection and tombstones | Actual user-library acceptance remains opt-in and needs owner authorization |
| #13 Hosted mode | Login, per-account rows/configuration, quotas, breaker, local-mode startup refusal and owner-hashed on-disk source workspaces | Real TLS, reverse-proxy, second-user/device and recovery checks from #14 remain mandatory |
| #14 Web delivery | Existing agent UI, JSON search workbench, static historical skill page with a visible paper→abstract-link→fixed-repository provenance path, import into the library and append-only human review; the Skill page also has a standalone static-server path and offline Chromium smoke | Docker build, HTTPS/reverse-proxy flow, isolated second account/device, online contest link and final authorized release remain unverified |
| #10 Knowledge base | Library v3 migration creates Works, explicit PaperVersions, immutable SourceSnapshots, a versioned Layout/图层 template and topic membership projections. Source-linked relation write/read/query and schema-v3 export are available; the graph page separates these saved assertions from its category/resource projection. A workspace full-text bundle can now be previewed and manually appended to the exact arXiv/ACL version with its source locator; unverified imports require a human-confirmed claim. CSL import keeps same-title/different-ID records separate, previews DOI/arXiv cross-mappings and arXiv version mismatches, and appends a review declaration only after confirmation | Resolve recorded identifier conflicts against representative legacy libraries; validate template editing and migration/restore; exercise real, human-checked relation examples |

Issue #12 (explicit resource monitoring) remains future work. Issue #10 now has its initial
storage, API, export and manual relation-review surface, but it is not a complete knowledge graph:
manual resolution of DOI/arXiv conflicts against existing libraries and representative
human-reviewed relation examples are still open.
Imported full-text chunks retain an unverified bundle-provenance label; only a manually confirmed
claim can cite them. Both P2 packages follow the primary skill/BYOK/Web path in the plan.

## Next gates

1. Keep the deterministic suite, package smoke and browser flows green as the shared
   contract and Web integration evolve.
2. Verify the container template on a machine with Docker. It runs in `hosted` mode,
   binds its published host port to loopback, and refuses to start without the declared
   session secret and HTTPS origins. Its existence is not deployment approval.
3. Run the authorized real-model and selected-host examples in #7. Until then, live
   research quality and client compatibility remain unverified.
4. Public access requires #13/#14 security gates and the owner’s deployment/domain/cost
   decisions from #1. Competition submission material additionally depends on #2.

The dated `web/skill.html` case is a read-only view of one documented 2026-09-22
resource audit. It sends no model or retrieval requests and does not establish live
search, a public service, visitor isolation, or contest readiness. Preserve actual
LearnBuddy records; do not reconstruct or relabel prior conversations as platform use.
The rules excerpt and record format still need confirmation in #2.
