# Changelog

## Unreleased

### Full-text reading with paragraph and page locators (#8)

- Add `backend/re0/safe_fetch.py` and `backend/re0/fulltext.py`, the tool `fetch_paper_text`, the CLI
  `re0 paper text`, and an optional `fulltext` extra (`pypdf==6.1.1`, BSD-3-Clause, pure Python).
- **There is no `url` argument.** The tool takes an arXiv or ACL Anthology *identifier* and builds the
  address itself against a fixed allowlist (`arxiv.org`, `export.arxiv.org`, `aclanthology.org`), so
  text inside a paper cannot aim the reader anywhere. A DOI is refused outright: resolving one leads
  to a publisher that may be paywalled, and this routes around no paywall.
- Every redirect hop is re-validated — scheme, port, no embedded credentials, allowlist, and a
  resolved-address check on *all* records a name returns, not the first. Loopback, private,
  link-local (where cloud metadata lives), multicast and reserved addresses are refused, as are
  redirect loops, more than five hops, a body over 8 MiB counted *after* decompression, and a
  content type that is not the one asked for. No credential or cookie is ever attached.
- Locators are derived from the bytes read — a section-and-paragraph ordinal for HTML, a page number
  for PDF — so re-reading the same version returns the same locators and a citation stays checkable.
  The response carries the version, fetch time, parser name, `content_sha256` and `parse_quality`.
- Only one bounded slice is returned; the rest are listed by locator and stored as chunks in the
  workspace when the caller named one. A model is never handed the whole paper.
- References, acknowledgements and appendices are marked `back_matter`, so a link in a bibliography is
  not attributed to the paper being read. No OCR, ever: a scanned PDF is `scan_only` with the page
  count, a corrupt one is `unsupported_format`, and a missing backend is `parser_missing` with the
  install command and its licence.
- **Two defects found only by reading real documents.** (1) A single `<input>` in arXiv's page
  furniture — a void element, so it has no end tag — left a counter-based reader permanently "inside
  a skipped element", and 532 kB came back as one 79-character block. Void elements are now never
  pushed, chrome is parsed and dropped block by block rather than hard-skipped, and a
  section/article/h1 inside chrome breaks out of it and says so. (2) pypdf's `extraction_mode="layout"`
  returned **1 character** for a page where plain mode returned 4568, so each page now keeps
  whichever mode extracted more and the mix is reported. A safety net remains: a document over 20 kB
  that yields under 2% of its length is reported as `under_extracted` and the state becomes
  `partial`, because "the parser found nothing" must never read as "the paper says nothing".
- Retrieval is untrusted-content aware: script bodies are skipped, inline instructions carry no
  authority, and the payload says so.
- One environment finding worth recording: on a network whose DNS answers through a local
  interceptor, `arxiv.org` resolved to `198.18.1.3` and `fdfe:dcba:9876::f9`, both non-public, and
  the guard refused — correctly, and uselessly. `RE0_ALLOW_LOCAL_RESOLVER=1` opts in explicitly and
  the addresses connected to are then recorded in the result.

### The skill ships as package data, and installs with a preview (#3)

- Add `backend/re0/skill_package.py` and `re0 skill show | package | install`. The skill directory is
  now declared as data in `pyproject.toml`, so a wheel carries it and `locate_skill()` finds it under
  `<prefix>/share/re0/skills` — reporting *how* it was found, because "installed from the wheel" and
  "picked up from a checkout next door" are different facts when a host is debugging a stale skill.
- **An install never overwrites silently.** Every file is classified `new` / `identical` /
  `conflict` before anything is written; `--dry-run` prints that and writes nothing; a conflict exits
  **3** and leaves the existing file untouched; `--force` renames the old file aside as
  `.re0-backup-<UTC timestamp>` instead of deleting it. Re-running is idempotent. `MANIFEST.json`
  carries per-file sha256 and a tree hash, and the manifest order is sorted by posix path so it does
  not depend on the build machine's case-sensitivity rules.
- Refusals are refusals: installing onto the skill itself or into a directory inside it, a target
  that is an existing file, and a `package --output` that already holds a non-empty copy all fail
  with a reason rather than proceeding.
- `SKILL.md` is split per the Agent Skills convention: 501 lines down to 229, with the credentials,
  publication-status and open-source-status deep dives moved **verbatim** into `references/`. Nothing
  was summarised away, and the doc-surface tests now read the whole set and fail if the entry point
  stops linking to a reference — a rule that moved into a file nobody opens is a rule that was
  deleted.
- Verified in a clean venv from the built wheel, run from a non-repo working directory: `skill show`
  resolves the installed data directory, `install --dry-run` writes nothing, a real install writes
  both files plus the manifest, a second install writes zero, an edited `SKILL.md` exits 3 with the
  edit intact, `--force` keeps it as a backup, and the installed wrapper still runs.

### Bounded pagination, one shared request budget, and a venue filter that says which it is (#5)

- Add `backend/re0/scheduling.py`: a `Governor` shared by every client in a call, so a rate limit met
  by the third request is honoured by the fourth and one ceiling bounds the whole session. It reads
  the provider's own `Retry-After` (seconds *or* an HTTP date) and GitHub's `x-ratelimit-reset`,
  clamps both, and falls back to a bounded backoff when neither parses — never to an immediate retry.
  Retries are capped at three attempts, so they cannot nest. `requests_used`, per-provider counts,
  seconds deferred and cache statistics are reported in `coverage.scheduling`.
- `paginate()` owns the stopping rules and gives every ending its own name: `complete`, `empty_page`,
  `page_budget`, `request_budget`, `cancelled`, `cursor_repeated`, `cursor_rejected`, `rate_limited`,
  `provider_failed`, `not_supported`. Only `complete` means the source ran out of pages; everything
  else sets `truncated` and keeps the cursor. A page that fails after earlier pages keeps them.
- **Only OpenAlex and Semantic Scholar document their cursors, so only they page.** arXiv, Crossref
  and OpenReview are read once and their coverage row says `not_supported` rather than looking
  complete. Paging a source whose cursor semantics are undocumented would produce a result set
  nobody can reproduce.
- `--max-pages` (default 1, max 10), `--max-requests` (default 40, shared across the whole call) and
  `--refresh`. `limit` is the *per-page* ceiling, so a paged run's recall ceiling is `limit × max_pages`.
- **Two bugs found by writing this down.** A page used to cost two budget slots because the paginator
  and the client each reserved from the same counter — with `--max-requests 1` nothing could be
  fetched at all. And a source that failed lost its coverage row, so "we asked and it refused" was
  indistinguishable from "we never asked". Both have regression tests.
- Responses are cached per session with an explicit TTL, a `stored_at` timestamp and a forced-refresh
  path. **The credential scope is part of the key**, so an anonymous answer is never served to a
  credentialed call or vice versa; the scope names which credentials are configured and never their
  values. The cache lives on the tools instance rather than the module, because a global one would
  carry answers across users.
- `--venue NAME` is now a real filter where one can be verified. OpenAlex resolves the name to stable
  source IDs via `/sources?filter=display_name.search:` and filters on
  `primary_location.source.id`, and **the resolved source names are always printed** — a conference
  family is split into per-edition records, so a strict filter on "CVPR" has been observed to cover
  only the 2022 edition. Elsewhere the name stays a query hint and is labelled `hint`. An HTML body
  where an API response was expected is now named as an interstitial instead of "could not parse".

### A resource audit with states that describe the check, not the resource (#6)

- Add `backend/re0/resource_audit.py` and `ResourceAudit` in `models.py`. Discovery, verification and
  printing used to be one function, and two of the three were lost on the way out because a print
  statement is not a record: a name search whose endpoints failed was reported to the terminal and
  never attached to the document, and a `--verify` result never entered `--json` at all. Both are now
  fields on the document, carried by the shared result contract into the CLI JSON, the MCP
  `structuredContent`, a workspace snapshot and the matrix. `print_document` renders and fetches
  nothing.
- **The audit vocabulary describes what the check established, never whether the authors released
  something**: `not_checked`, `candidate_located`, `metadata_readable`, `access_required`,
  `partially_available`, `access_failed`, `not_found_in_scope`, `unsupported`. The provider's own
  answer is kept beside it in `provider_status` — `HTTP 404`, `HTTP 429`, `gated`,
  `empty_repository` — so a dead link and a rate limit cannot collapse into one sentence. That
  needed a new field on `Observation`: it was discarding the HTTP code and keeping only
  `indeterminate`.
- **Eight artifact classes are recorded separately** (training/inference/evaluation code, checkpoint,
  dataset, split, preprocessing, environment), each as `present`, `absent_in_scope`,
  `not_applicable`, `unknown`, `requires_access` or `check_failed`. `not_applicable` and `unknown`
  are kept apart because "this work needs no checkpoint" is a finding and "this check could not tell"
  is a gap. A `present` or an `absent_in_scope` **must carry a source** — the model refuses one that
  does not — so every affirmative conclusion can be opened, pinned to a commit.
- `artifact_search` now distinguishes `searched`, `partial`, `failed`, `skipped` and `not-run`, with
  the per-endpoint attempts, the failure list and the reason nothing ran
  (`disabled` or `budget`) in `artifact_search_detail`. Before, `searched` was written the moment the
  search started, so "GitHub answered, both Hub endpoints were rate-limited" came back looking like a
  completed search that found nothing.
- Add `--resource-matrix PREFIX`, writing `.json`, `.md` and `.csv` from the rows already in the
  result — it re-renders and never re-checks. Each row carries a `blockers` column restating why it is
  not a drop-in baseline, a paper with no candidate still gets a row (otherwise "searched and found
  nothing" and "the search failed" look identical in the one artifact a reader compares from), cells
  a spreadsheet would run as a formula are escaped, and only http(s) links are written.
- Fix two bugs a real run caught that no fixture did. A check that ran and failed reaches no depth,
  and keying the "was this verified" question off `verification_depth` reported an attempted
  verification as one nobody made — the row said `access_failed` while the summary counted it
  unverified and the terminal printed 未核验. And a paper whose title has no project name was spending
  a name-search slot, so `--find-artifacts 3` searched fewer than three papers and blamed the budget
  for a search that was never possible. Both are now keyed off the right field and pinned.
- An **empty repository is a finding, not an access failure**. GitHub answers that there are no
  commits, which is different from not answering, and folding the two together reported a repository
  that plainly exists as one nobody could reach. It is now `not_found_in_scope` with
  `provider_status: empty_repository`.
- Incompleteness is now two kinds. A **truncated listing** downgrades every absence to `unknown`,
  because the provider said the listing was incomplete; a README that 404s does not, because it says
  nothing about which files the repository holds. Treating both as incompleteness downgraded every
  repository without a README.
- `inspect_resource` returns the same audit row, so the in-task model and an MCP client read one
  vocabulary instead of deriving states from a prose summary. Its tool description carries the rules,
  because that description is the only surface either of them reads.
- Library records are now labelled: `record_kind` is `observation` for a check (performed here or
  imported) and `confirmation` for a human revision, appended beside the observation rather than over
  it. `POST /api/resources/{id}/confirmations` refuses a settled attribution or version judgement with
  no source, and refuses one about a different resource. Rows written before records were labelled
  read back as the observations they always were. A model's inference is **not** a third kind of
  resource record: it lives in the task report's findings, where `assessment` already separates
  observed, inference and uncertain.
- `POST /api/import/resource-audits` links approved papers and their audits into the library,
  previewing unless `dry_run: false`. It is idempotent by identifier, by resource URL and by audit
  fingerprint, never updates a paper that already exists (an existing record carries the reader's
  notes), and refuses to create a paper it cannot attach a resource to. The matrix JSON carries the
  importable payload in its `approval` block, so approving does not mean transcribing.
- **A `confirmed: true` in a tool result is not an approval.** The tool surface is read-only and no
  argument reaches a write path, which is now a test rather than an intention.

### Several queries in one budgeted call, and a coverage model (#5)

- `search_papers` accepts `queries` (up to 5) and a `sources` subset beside the existing single
  `query` / `source`, which keep working unchanged. One call merges once, so a work found by several
  queries becomes one record instead of two, and each record keeps **which queries found it** rather
  than only the last — provenance that hand-merging JSON files cannot preserve.
- Every `(query, source)` pair is recorded as an attempt, and the result carries a `coverage` block
  with `requested`, `attempts`, `succeeded`, `failed`, `hits` (records, unique, duplicates merged,
  records dropped by the year window, records with an unknown year) and a `state` of `ok`, `partial`,
  `all_failed` or `zero_hits`. Zero hits, a partial run and a total failure are now distinguishable;
  previously a failed source showed `=0` in the per-source line, which reads as "no hits".
- Add a conservative merge guard: two records that share a title but disagree on real identifiers
  (different non-arXiv DOIs and different years) are **kept apart**, because merging them would
  invent a work. A preprint and its published version still merge, and the second DOI is kept on the
  record as `other_dois` rather than being overwritten.
- The skill gains `--queries "a|b|c"` and multi-source `--sources`, and its heading now prints the
  coverage state, so the CLI cannot describe a call differently from the JSON and MCP exits. The
  tool description was updated with the contract.

### An opt-in source workspace, and a protocol that validates (#4)

- Add `backend/re0/workspace.py` and `re0 mcp --workspace DIR`. The default surface stays stateless
  and opens nothing; with a directory named, each document a tool returned is stored as a snapshot
  with a stable content-addressed id, and the id travels in `structuredContent`. Only payloads
  marked as coming from a tool are recorded — model text is refused, because a workspace holding it
  would look like evidence that was never gathered. An import previews by default, is idempotent,
  never approves a paper into the library, and refuses a bundle from a different workspace rather
  than merging two that are not the same.
- Fix an identity bug the first real probe caught: the recording timestamp was inside the hashed
  payload, so the same source recorded a second later minted a second id and the directory filled
  with near-duplicates. The id now covers a source's identity only, and re-recording keeps the first
  record's metadata.
- Harden the stdio protocol. A tool request before `initialize` is refused (-32002) instead of being
  answered under assumptions the client never agreed to; a requested protocol version this server
  does not implement is answered with one it does, rather than echoed; and a request that is not an
  object, a `params` that is not an object, and a `tools/call` without a name all get defined errors
  instead of raising out of the loop.

### A versioned evaluation set, and a harness that caught its own overclaim (#7)

- Add `evals/`: a versioned public task set with labels (`tasks.json`), a runner for the
  **connector** channel (real services, no model), a scorer, a result template and its own tests.
  The three channels — `unit`, `connector`, `live` — are never averaged together, so a fixture
  result cannot reach a live metric.
- Two rules are in code rather than in prose. **No denominator is not 100%**: with nothing judged
  official the accuracy is reported as `no value` plus its reason. And **a miss is a miss**: the
  first real run reported a recall case as `completed` while the expected arXiv id was absent from
  the 25 results returned. That is the overclaim this harness exists to prevent, so
  `found_identifiers` now records `missed` and the task becomes `partial`. The step also states that
  a miss is a coverage gap for that query and source, never evidence of absence.
- A live task without model configuration is recorded `blocked`, never `passed`, and the summary
  counts it as such. Provider usage that was not reported stays `unknown` and is never counted as
  zero.
- CI proves the distribution builds (`pip wheel .`) and keeps the deterministic channels; the live
  channel is a separate `workflow_dispatch`-only workflow, so a push or a fork cannot spend tokens.

### One versioned result, two machine-readable exits (#4)

- Add `backend/re0/result_model.py`: the result shape every exit shares, carrying `schema_version`
  and a `coverage` block (sources queried, per-source counts, failures, duplicates merged, records
  dropped by the year window). Fields a version does not describe ride along under `unrecognised`
  instead of being discarded, and a bounded body states `content_chars`, `excerpt_chars` and
  `truncated`, so a cut body cannot be mistaken for a short one. This surfaced two real coverage
  fields, `duplicates_merged` and `dropped_out_of_range`, that had been travelling as unknowns.
- The MCP surface now returns `structuredContent` beside its text summary, and renders the summary
  *from* that structure. Before, `render()` printed only the first 1500 characters of each body plus
  a 1200-character tail of everything else — so `artifact_candidates` and `artifact_search`, which
  live on the document, were never printed at all, and a failure list could be cut mid-JSON. The
  summary now names any field it did not print, and lists a failed source before the documents
  because a source that was not searched is not an empty result.
- `--json` writes the same versioned structure, so the two machine-readable exits cannot describe
  one call differently. Documents keep `paper`/`publication`/`artifact_candidates`; the body moves
  under `documents[].body.excerpt` with its true length recorded.

### One search implementation, two entry points (#3)

- Add `backend/re0/cli.py` with `re0 paper search`, `re0 doctor` and `re0 mcp`, and move the
  literature-search capability into the package as `re0.skill_search`. The skill's
  `paper_search.py` is now a thin wrapper that locates the package and delegates, so the skill and
  the console script call one implementation instead of two copies of it.
- `doctor` separates what runs with **no model key** (`paper search`, `mcp`) from what needs BYOK,
  and runs no network probe unless `--probe-network` is passed, so a routine check cannot spend
  money or trip a rate limit. It prints credential **names and set/unset**, never a value.
- Credential precedence is explicit now: `RE0_ENV_FILE` is honoured exactly and reports when it
  yields nothing instead of silently falling back, and **another product's `.env` is no longer read
  implicitly** — reading whichever account that client held is a credential mix-up, not a
  convenience. Pass `RE0_ENV_FILE` or set `RE0_ENV_INCLUDE_AGENT_DIRS=1`, and the default reports
  the file it left alone.
- Fix three documentation drifts the parser contradicting: `--find-artifacts` is 10 by default
  (prose said 5), `--verify` allows 0-8 (README said 0-5), and `--sources` takes `all` or exactly
  one source rather than a comma-separated subset. A test now holds the parser and the prose
  together.

### Venue-aware search, and a model probe that never prints a key

- Add `--venue NAME` to the skill. Conference-only papers (CVPR, NeurIPS, ACL…) were already
  covered — Crossref, Semantic Scholar and OpenAlex index proceedings and the venue is reported —
  so the route is a **query hint that prepends the venue**, not an API-side filter. The three
  filter routes were checked and none is usable yet: DBLP answers a non-browser client with a
  `Making sure you're not a bot!` challenge page instead of JSON; OpenAlex rejects
  `primary_location.source.display_name.search` with HTTP 400 *"is not a valid field"*; and
  Semantic Scholar's documented `venue=` parameter could not be verified while rate-limited.
  A source filter still matters for "what did CVPR 2024 accept" and is left for when one of those
  can be verified end to end.
- Add `scripts/model_probe.py`: tests the model endpoint held in the environment before the agent
  uses it. It reports only whether each variable is set — never a value, not even a masked prefix —
  and prints the endpoint URL, which is safe because `validate_endpoint` refuses a URL carrying
  credentials or a query string. It reuses `ModelVault` and `ChatModel.test()`, so a pass means the
  real agent can use the same endpoint.
- Raise the search page from 8 to 25 per source (skill default 20). Recall is bounded by this,
  so eight made any survey a matter of luck; conversation cost is bounded separately by the excerpt
  budget. The trade-off is stated rather than hidden: a larger page means a noisier head, because
  these sources rank by their own relevance and nothing here re-ranks.
- Keep the **longest** abstract when merging rather than the first, so a code link that only one
  service's abstract carries is not dropped in favour of a shorter one.
- Reword the publication states to report evidence instead of asserting a fact:
  `有会议或期刊版本` / `投稿或评审中` / `仅见预印本版本` / `来源未给出发表信息`. A preprint and its
  published version are normally two separate records, so a run that reached only the preprint
  record must not report the absence of a publication. The skill adds a caveat when any result is
  `仅见预印本版本`, naming `SEMANTIC_SCHOLAR_API_KEY` as the largest correction.
- Always print the artifact line, including `摘要中未提及 code/dataset 链接` when there is none, and
  count the candidates left unchecked: an absent module should read as a finding, not an omission.
- Print the credential that would fix a failing source when it is unset, so a 429 from Semantic
  Scholar is not mistaken for a broken service.
- Search GitHub and the Hugging Face Hub by each paper's project name (`--find-artifacts N`,
  default 5, 0 disables). Most papers carry no link in any metadata field yet do have released code
  or data, so an empty artifact line was a failure rather than a finding: `RevealLayer: Disentangling
  Hidden and Visible Layers…` carries no link in its abstract or body, and this surfaces its
  repository, its model and its 100K dataset. Every hit is a name match, not proof of authorship,
  and carries a marker — `描述与论文标题相符` when the repository description repeats the paper
  title, `仅名称匹配` otherwise. Three outcomes stay apart: found, searched and empty, and
  **search incomplete** (one retry per endpoint), because a transient network failure must never
  read as "there is no repository".
- Correct the documented GitHub limits. The **search** endpoint allows 10 requests/minute anonymously
  (read off the response header) while 60/hour is the **core** endpoint; `GITHUB_TOKEN` raises both,
  and the skill now says where that variable goes for one shell session, for `RE0_ENV_FILE`, and for
  an online deployment, and that it must never be committed.
- Make the retrieval rules reach every consumer that reads them. They were only in the CLI help,
  so the in-task model and MCP clients — which read the tool description and nothing else — never
  saw them, and that description had quietly gone stale on the merge rule, still advertising the
  single-key `DOI > arXiv ID > title` precedence that the code no longer uses. The description now
  carries the recall ceiling, the query-formulation rule, the all-identifier merge, and where to look
  for released code; `SKILL.md` gains an actionable *Surveying a topic* procedure instead of rules
  buried under output-reading bullets, and a test pins all three surfaces together so they cannot
  drift apart again.
- Carry open-source candidates into `--json` as `artifact_candidates` (with `artifact_search`
  recording whether the name search ran), and write that file after the search instead of before it.
  The candidates were printed for a human and never attached to the document, so a report built from
  the JSON saw nothing at all — indistinguishable from a tool that never had the feature.
- Raise `--find-artifacts` to cover 10 papers by default and print the coverage: how many papers were
  searched and how many candidates came back, out of how many records. In a large survey most
  records have no candidate *because nobody searched them*, and silence read as "no code released".
- Add *Reporting from these results* to `SKILL.md` and a **Documentation and reporting discipline**
  section to `AGENTS.md`: a report keeps only what was judged relevant and states how many records
  were set aside, never appends a "for completeness" section of rejected matches, and carries
  through what the tools reported. A dropped column makes a working feature look absent.
- Document what each open-source layer can and cannot yield. The distinction matters because only
  `--verify` shows that a public repository may carry **no licence**, that it may hold **code but no
  weights**, or that it is a **fork with vendored source** — three things that change the answer and
  none of which a name match can reveal. The section also names what no layer settles and therefore
  stays with a person: official attribution, whether the code runs, whether the weights download, and
  gated access, which is `access_failed` rather than closed source.
- Tests: 51 Python cases added.

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
- Add `skills/re0-paper-search/`: a self-contained skill that runs the multi-source search
  from the command line, prints per-source hit counts, lists failures on stderr, and sinks
  survey/review papers to the bottom as `[survey]` without removing them. It deliberately
  has no "model knowledge" source, and no semantic relevance filter that could hide rows.
- Add opt-in credential loading: `RE0_ENV_FILE` (also honoured by `run.py`) loads a dotenv
  file, filling only variables that are not already set. Values are never printed, logged
  or written to a task record; Re0 still reads no file unless asked.
- Print links best-first — arXiv, then DOI, then the source record page — and recover the
  arXiv ID from an arXiv DOI (`10.48550/arXiv.<id>`) when no service supplied it directly.
  That is the link most readers want, and several services report only the DOI.
- Surface code/data URLs that the abstract itself advertises, as
  `artifact candidate (from the abstract, unverified)`. Extraction uses the existing
  `artifact_urls` helper and deliberately does not search GitHub by title: a repository merely
  *named* like the paper does not establish official authorship, so "they have a repo" would be
  a guess presented as a finding.
- Add opt-in `--verify N` (0–5, default 0) to the skill: Re0's bounded resource check runs on
  the first N artifact candidates, which is what a link alone cannot answer. A link that
  resolves to an **empty** repository shows up as `metadata_accessible` with a file count near
  zero and no candidate files; a link that **404s** reports `indeterminate` with
  "可能不存在、已移动或无访问权限" and never "不存在". The cap is global rather than per paper, one
  check costs about four GitHub requests against an anonymous limit of roughly 60/hour, and an
  unchecked link still prints as a candidate instead of appearing to have failed. A subagent was
  rejected here: it returns prose, while this returns a status comparable across papers.
- Report where a paper stands — `已收录于会议或期刊` / `投稿或评审中` / `仅预印本` / `无可用信息` —
  next to the raw venue string and the service that claimed it. An unstated venue is `无可用信息`,
  never `仅预印本`: a service carrying no venue has said nothing, and "just a preprint" would be an
  invented conclusion. A work that is both an arXiv preprint and published reports the stronger
  claim **plus** a note that a preprint also exists, instead of one hiding the other.
- Report up to three affiliations from Semantic Scholar `authors.affiliations` and OpenAlex's
  parsed institutions, de-duplicated in first-seen order. `各来源均未提供` is common and means the
  services did not say, **not** that the authors are unaffiliated — preprint coverage depends
  mainly on Semantic Scholar, which rate-limits without a key.
- Tests: 35 Python cases added.

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
