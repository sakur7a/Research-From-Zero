# Changelog

## Unreleased

### Temporary guest sessions and one-step BYOK onboarding (#18)

- Add an opt-in hosted guest entry: each visitor receives a separate two-hour owner, cookie and
  in-memory key namespace. The guest pool is bounded, guests cannot enter the admin account list,
  and local mode ignores the guest flag.
- Logout and explicit delete revoke the guest immediately, stop the owner's active task at its next
  safe boundary, then clear its database rows and server-managed workspace files. A bounded sweeper
  handles expired/revoked sessions; active requests are not reported as deleted before cleanup.
- Replace separate “test” and “save” model steps with one test-and-save action. The credential is
  retained only after its tool-call protocol check succeeds. Capability text comes from the backend;
  budgets and library scope stay in advanced settings, while task-level material/fee consent remains.
- Add an anonymous landing page and make report/evidence the default result views. Chromium checks
  cover guest deletion, non-disclosure of owner IDs, one connection test, task completion and narrow
  layout. No live provider request or participant test was performed.
- Verification: 576 Python tests, 84 JS tests, `npm run check`, six Chromium smoke scripts, and
  loopback HTTP smoke passed. The non-implementer usability check remains open; container CI will
  run after push.

### Hosted demo deployment contract (#17)

- Require an explicit hosted storage contract (`persistent` or `ephemeral-demo`) and show the selected
  mode through health/session/config responses; the login page warns before use when data is temporary.
- Add one proposed Render Free Docker Blueprint. Read platform URL/hostname only when `RENDER=true`,
  honor explicit `RE0_*` overrides, use injected `PORT`, and test the Render Host allowlist offline.
- Add container build/start/health/Host validation plus a real hosted task `202 → poll → complete → export`
  and remove/recreate cold-start check to CI. Its test-only fake model is outside the image and never
  contacts a provider. The Render service and public URL are not created; the owner has not accepted
  the temporary-storage and hosting decisions.

### Hosted model credential lifecycle and endpoint consistency (#16)

- Apply the hosted public-address policy to transient model-list probes as well as saved model
  settings and inference; refuse private/loopback destinations before opening a connection.
- Model keys now have an eight-hour in-memory lease. Logout, operator revocation, account disable,
  or expiry invalidates the generation held by a running task and prevents its next model call; a
  request already in flight may still finish and incur a charge.
- Show the key lifetime and clearing behavior in model settings. No key is added to persistence,
  task snapshots, logs or exports.

### Source workspace transfer into the Web workbench (#4)

- Add bounded versioned workspace JSON export/import through `re0 workspace`; preview remains
  read-only, `--apply` is explicit, unknown fields and oversized source records become visible
  conflicts, corrupt markers are not replaced, and export replacement keeps a timestamped backup.
- Add owner-scoped Web bundle preview/import/list/export endpoints and a follow-up picker for
  imported source IDs. Hosted requests use server-managed owner directories and workspace IDs;
  caller-provided filesystem paths are accepted only for local follow-up/CLI operation.
- Imported rows carry `imported_by_user`; their claimed tool provenance is explicitly unverified.
  Reuse records that fact in the model context, and bundle import never approves a paper.
- Add fixtures for preview with zero writes, idempotent import, cross-account isolation, tampered
  fields, size bounds, and a Chromium import → select source → follow-up flow.

### Wheel and source distribution acceptance (#3)

- Build both wheel and sdist in the packaging smoke, inspect required Skill/Web files in the sdist,
  and install each artifact into a separate clean virtual environment. The smoke exercises the
  installed console command, skill preview/install into a path containing spaces, invalid
  `RE0_HOME` handling, MCP initialize/tools-list, and the wheel's real HTTP serve/write flow.
- The installed MCP check exposed and fixed `re0 mcp` passing its outer command token back to the
  MCP argument parser, which had made the stdio server exit before its handshake. `re0 mcp` now
  forwards only its own arguments.
- Add the pinned `build` frontend to the test extra; CI now builds both formats and runs the clean
  install smoke. The real host/client compatibility check still depends on owner input in #1.
- Verification on Windows/Python 3.12: wheel and sdist built; both installed outside the checkout;
  wheel serve/write, Skill install, and MCP checks passed. Remote CI has not run for these changes.

### Versioned knowledge records and source-linked relations (#10)

- Add schema v3 tables for stable Works, explicit PaperVersions, immutable source snapshots,
  versioned research templates, topic assignments and typed research relations. The v1/v2
  migration preserves existing notes and observations, and does not infer a paper version from
  title or bind an observation that lacks explicit version evidence.
- Keep paper create/edit and Zotero/agent storage writes synchronized with the new projection.
  Add owner-scoped template/version and topic-assignment APIs, source/version-checked relation
  writes, superseding revisions, filtered relation queries, and schema-v3 knowledge export.
- Add the evidence-linked relation index to the library graph view. It distinguishes saved
  assertions from category/resource projection, requires a source snapshot and locator, and keeps
  claims disabled until a full-text snapshot exists. No claim inference or full-text ingestion path
  is included yet.
- Verification: 528 Python tests, 83 Node tests, JS syntax check, five Chromium smoke scripts,
  loopback HTTP and installed-wheel serve smokes passed. The knowledge graph remains incomplete;
  see `docs/ROADMAP.md` for the remaining acceptance checks.

### From imported Skill result to a reviewable library record (#14)

- The retained library can now append a field-level human review to an imported resource audit.
  Attribution, author declarations and version matches require source evidence; the saved
  `confirmation` is shown separately from the original observation. Audit citations are validated
  as credential-free HTTP(S) URLs before import or confirmation.
- Add `scripts/restore.py` to restore a SQLite backup into a **new** validated file without
  overwriting the current database. The hosted Compose template now declares its mode and required
  identity/HTTPS settings, publishes only on host loopback, and carries the backup/restore scripts.
- Pin the container to Python `3.13.15-slim-bookworm`; add the Skill page and its JS/CSS to real HTTP
  and installed-wheel smoke coverage.
- Verification: 522 Python tests, 83 Node tests, JS syntax check, five Chromium smoke scripts,
  loopback HTTP smoke, and a clean wheel install/serve smoke passed. Docker build and real hosted
  proxy/device acceptance were not run in this environment.

### A static, evidence-bounded Skill preview

- Add `/static/skill.html`, a standalone read-only page for the documented 2026-09-22
  Microsoft/LoRA resource check. It says the check is historical, links to the checked repository
  revision and the two listed adapter files, and keeps attribution/version uncertainty visible.
- The page has no retrieval or model endpoint, accepts no API key and writes no research data. Its
  tabs and command-copy action run only in the browser. It is a UI preview, not a live demo or a
  substitute for the online competition delivery.
- Bring the skill and CLI wording into line with multi-query search and move dated venue-filter
  provider checks into `references/venue-filter.md`. Correct the README's stale dotenv lookup order.
- Verification: 522 Python tests, 83 Node tests, `npm run check`, and the agent/search/login Chromium
  smoke scripts passed. A separate 1440px/390px Chromium review of this page found no JS errors,
  no horizontal overflow, and no non-local requests.

### The door: a login page, and a real browser that read the headers (#13)

- Add `/login` (`web/login.html`, `web/login.js`, pure decisions in `web/login-core.js`). It asks
  `/api/auth/session` which mode it is in *before* it renders anything, so local mode is told there is
  nothing to log into rather than being handed a form that cannot work, and hosted mode never asks for
  a secret while it does not know who would receive it.
- `web/api.js` turns a 401 into a redirect to `/login?next=` the page the reader was on. The old
  behaviour was worse than an error page: a signed-out visitor in hosted mode saw an empty library,
  which reads exactly like a working service with nothing in it.
- The page is as uninformative as the endpoints behind it on purpose. A wrong password, an unknown
  account, a disabled account and a locked one are one sentence with one timing profile in `auth.py`,
  and the page quotes that sentence rather than interpreting it — a UI that guessed would rebuild the
  enumeration oracle the API refuses to be. A 429 says *wait*, not *you typed it wrong*; a 422 says the
  attempt never reached the password check, so it did not bring the account closer to a lock.
- The typed password is cleared from the field after every attempt and reaches no storage area the page
  can read; the session token is HttpOnly, so page script cannot see it; logout revokes it server-side
  rather than deleting a cookie. A `?next=` is honoured only for a path on this origin — `//host`,
  `/\host`, schemes, control characters and markup all fold back to the workbench, because a redirect
  parameter is the oldest way for a login page to hand a session to somebody else.
- Add `scripts/login_browser_smoke.py`: the first smoke that lets the browser be a browser. It routes
  `https://re0.test/**` into the in-process TestClient, so `page.goto` carries a real query string, the
  cookie jar is Chromium's, and the success path performs a real navigation. Nine steps, 72 requests,
  zero page errors. Deliberate 401s are expected by name; a 401 that arrives uninvited is a failure.
- **Fix: the theme bootstrap on all four pages was an inline `<script>`, which the app's own
  `script-src 'self'` CSP blocks.** Every page logged a policy violation on load and dark-mode readers
  saw the light theme first. Moved to `/static/theme-bootstrap.js` — an external file the same rule
  allows — rather than weakening the policy to make a symptom go away, with a test that forbids any
  inline `<script>` in the shells and re-reads the header that would catch it. Invisible to the three
  bridged smokes, which build a page from a string and never see a response header.
- **Fix: `hidden` did not hide anything on the new page.** Each panel's class sets an explicit
  `display`, which outranks the UA's `[hidden]{display:none}`, so all three panels rendered at once and
  the failure text landed inside `agent.css`'s off-screen `#notice` toast. Guarded with
  `[hidden]{display:none!important}` and a page-local notice box.
- `quota.py` now names an anonymous ceiling as 未登录 rather than 该账户: telling a visitor about an
  account they do not have is a wrong sentence, and the wrong sentence on a login page is where users
  start doubting their password.
- Tests: 13 new in `backend/tests/test_login_page.py` (backend 504), 17 new in
  `tests/login-core.test.js` (frontend 83). The Python side pins the *shape* the JavaScript reads —
  every key `sessionView()` looks for, and the two sentences the page quotes verbatim — so the page
  cannot drift into confidently rendering a response that no longer exists.
- Still not verified: a real TLS terminator, a reverse proxy rewriting `Host`/`Origin`, `SameSite`
  behaviour in a genuine cross-site encounter, and a second human on a second device. `https://re0.test`
  is intercepted, not served. Hosted mode remains not a deliverable.

### How much may be spent, and when to stop answering: quotas, windows, a breaker (#13)

Isolation answers *who may see what*. This answers the question a reachable service cannot leave
open — *how much may be spent* — because one careless loop is otherwise an unbounded bill and an
unbounded load on the scholarly APIs this tool shares with everybody else.

- Add `backend/re0/quota.py`. Three scopes, and each one says what it is for: **per account** a
  sliding request window (`RE0_REQUESTS_PER_MINUTE`, 120) and an hourly window over turns of paid work
  (`RE0_TASKS_PER_HOUR`, 12, shared by submit/follow-up/retry/resume); **per conversation** the
  cumulative model/tool caps that already existed in `agent/runtime.py`; **site-wide** one request
  window for the whole service (`RE0_SITE_REQUESTS_PER_MINUTE`, 600) plus the serial execution slot
  and the circuit breaker. Sliding rather than fixed-interval, because a fixed bucket refills all at
  once and lets a caller spend two of them either side of the boundary.
- A bucket is keyed on the verified identity. `X-Forwarded-For`, `X-Real-IP`, a cookie a visitor
  picks: none of them can mint a fresh allowance, and logged-out traffic shares one anonymous bucket
  rather than getting one per invented address.
- The window is consulted **before** the origin, client-header and content-type checks, so being
  cheaply refused is not a free thing to do a million times. Every refusal states the limit and when
  it resets (`Retry-After`, and a message naming whether the account or the site is at its ceiling),
  and spends nothing: a request turned away at the door creates no task and no model call.
- In `local` mode an unset knob means **no limit**, and setting one works the same as in hosted mode.
  A ceiling that only ever throttles the one person at the keyboard is noise; `re0 paper search` on a
  laptop should not fail because of a number nobody chose. `re0 doctor` prints the effective numbers
  and exits 2 on a nonsense one, because `create_app` raises on that same value.
- The circuit breaker opens after eight consecutive **destination** failures (connection, timeout,
  408/429/5xx — marked at the source via `ModelError.destination`, which is why a wrong key is not one)
  and answers new work with 503 until a connection test succeeds. Counting only the destination is the
  point: otherwise a visitor who mistypes a key eight times takes the service away from everybody,
  which is a denial of service that costs the attacker nothing.
- Opening it never interrupts a turn already running — killing one could discard a provider call that
  will still be billed, and an unknown in-flight cost stays reported as unknown rather than as zero —
  and its state is written to `agent_settings` under the reserved `local:site` owner, so restarting is
  not a way around an outage. A failed half-open probe re-arms the cooldown instead of leaving the
  gate open forever, and a hand-edited state row is ignored rather than trusted.
- `create_app` builds the limits once and hands the same object to the middleware and the runtime: two
  copies would mean two answers to "may this run start". `_admit()` gates all four paid entry points in
  the order busy → breaker → task window, so a turn that never started does not cost one of the
  caller's remaining starts. `/api/health` publishes the ceilings and the breaker state;
  `/api/agent/config` publishes each caller's own usage and never another account's.
- Defects fixed on the way: the breaker's non-reentrant lock would have self-deadlocked the first
  request that asked whether it may proceed; a failed half-open probe did not re-arm the cooldown; and
  the local-mode "slot is busy" answer did not say that the refused request spent nothing.
- Tests: `backend/tests/test_quota.py`, 26 new (backend 491). Includes the bypass attempts — four
  forged source addresses, an anonymous flood, a fixed-bucket boundary crossing, a restart during an
  outage, sixteen wrong keys — none of which gets anything through. Nothing here has been re-measured
  on a real deployment; the numbers are design values, and hosted mode is still not a deliverable
  (there is still no login page in the UI).

### Two modes, one binary: hosted needs an identity, local needs nothing (#13)

- Add `backend/re0/deployment.py`. `RE0_MODE` is `local` (the default) or `hosted`, **declared and
  never inferred**: a process cannot observe who can reach its port, so guessing "probably local"
  from a loopback bind is exactly the guess that must not be made silently. A hosted start-up refuses
  with the whole list of what is missing at once — session secret, public entry, allowed origins —
  instead of naming one variable per restart.
- `run.py` checks the deployment before uvicorn imports the app, and refuses a non-loopback
  `RE0_HOST` while the mode is local: an unauthenticated service bound to `0.0.0.0` is not a
  configuration, it is an open door. It also passes `workers=1` explicitly, since task state, the
  model vault and the execution lease are per process and a second worker would be a second,
  disagreeing copy of each.
- Add `backend/re0/auth.py` and `re0 auth`. Sessions are opaque random tokens stored as SHA-256, so
  they can be revoked, which a signed token cannot be. Passwords are PBKDF2-HMAC-SHA256, 210k rounds,
  a per-account 16-byte salt; verification runs a dummy hash when the account does not exist, so a
  wrong username does not answer faster than a wrong password; five failures lock the account for 15
  minutes, and a lock is not reported as a wrong password. **There is no registration endpoint** —
  an operator provisions accounts from the console (`re0 auth
  secret|create-user|list|disable|revoke|purge`), and a password is never a flag: it is read from a
  prompt, from `RE0_AUTH_PASSWORD`, or from stdin, because argv ends up in the shell history and the
  process list. In local mode `re0 auth` says there are no accounts and no login step, and exits
  non-zero rather than pretending to have done something.
- A documented example secret is refused, including one repeated until it is long enough: "太短"
  invites an operator to pad the value they already typed, and a padded example is still public
  knowledge.
- The session cookie is HttpOnly and SameSite=Strict, and Secure whenever the mode is hosted;
  `RE0_ALLOW_INSECURE_COOKIES` is itself a start-up refusal there, as is an http public entry and a
  loopback origin in the allowlist — every visitor has their own `localhost`, so allowing one allows
  any of them to present a page the origin check accepts.
- **Every stored row now has an owner.** `papers` and `topics` gained an `owner` column; resources
  and observations are scoped by a join through `papers` instead of carrying their own copy, so one
  record has one source of truth; `agent_runs`, `agent_conversations`, `agent_settings` and the three
  `zotero_*` tables carry the owner in the row or inside the primary key. Owner is a **required
  keyword argument** on request-facing store methods, so forgetting it is a `TypeError` rather than a
  query that quietly returns somebody else's rows.
- Cross-account access is answered with **404, not 403**. A 403 confirms the id belongs to someone,
  which turns every identifier into an enumeration oracle; a 404 says only what the caller was
  entitled to learn.
- The DOI and arXiv uniqueness indexes are per owner. The same work in two accounts is two records,
  and the collision refusal no longer tells a second reader that the first one already has it.
- Migrations: papers v1→v2, agent v2→v3, zotero v1→v2. A single-user database is copied with the
  SQLite **backup API** before anything is written, every existing row is assigned to the owner
  `local`, a schema newer than this build is refused rather than downgraded, and row counts are
  compared before an old table is dropped. `local` and its obvious variants are reserved account
  names, so no account can ever own the rows a migration just assigned.
- **The model vault is per owner and stays in memory.** A key one account entered is not readable,
  echoable or clearable by another, and the configuration a turn runs with is snapshotted at launch,
  so rotating a key mid-turn cannot redirect that turn. Task defaults and session caps are per
  account as well: one reader raising their own budget does not widen what another's tasks may spend.
- Hosted mode refuses a loopback model destination outright and requires every address the model host
  resolves to be public — a public service that accepts a private resolution is an SSRF probe with a
  model bill attached. `RE0_ALLOW_LOCAL_RESOLVER` is not honoured there, and the refusal now says so
  instead of advertising a variable that cannot help; that advice is still given, and still correct,
  on the single-user full-text path where the connection really is the reader's own.
- In hosted mode every `/api/*` route needs a session, while `/`, `/library` and `/api/health` stay
  reachable — nobody could reach a login form otherwise. A request claiming `X-Re0-Client: cli` is
  refused, a cross-origin write is refused unless the origin is on the allowlist, and `owner` is not a
  field a request body may declare.
- The execution lease is one task at a time per process. `busy` reports that the slot is held and
  whether it is yours, never whose.
- The per-process retrieval cache became per owner (32 of them, evicting the least recently used), so
  a repeated question does not re-spend one reader's budget and a cached answer cannot cross accounts.
- `re0 doctor` now reports the deployment first — which mode would run, and, when it is hosted, the
  public entry and how many origins are allowed. A declared mode that could not start makes doctor
  **exit 2** instead of printing one more informational line: doctor is what an operator checks before
  trusting the rest of the report, so it must not say "all clear" with a status of 0.
- 455 Python tests (was 428), including a two-account suite that runs two independent cookie jars
  against one app: libraries, resources, observations, deletes, topics, exports, runs, defaults and
  model keys, each invisible to the other, plus the migrations, the launcher's refusals and the
  console's.
- **Not done in this increment:** quotas, rate limiting and the site-wide circuit breaker; a login
  **web page** (hosted mode authenticates over HTTP, but there is no form in the UI yet); lease
  behaviour under concurrent load; retention and audit-redaction rules; and any real hosted
  deployment — none of this has run behind a real TLS terminator with a real second user. **Hosted
  mode is not yet a deliverable.** Local mode is unchanged in behaviour and remains the default.

### A local workbench that reads a search result instead of running one (#14)

- Add `web/search.html`, `web/search.js`, `web/search-core.js` and `web/search.css`, reachable at
  `/static/search.html` from the static mount that already existed. **No route, no endpoint and no
  second retrieval implementation were added**: the page reads the versioned file that
  `re0 paper search --json` writes and renders four views — retrieval coverage (the window and limit
  the run requested, per-source counts, failed sources shown as failures rather than as zeros), the
  candidate list with filters that only narrow the view and state how much they hid, the
  resource-audit matrix (one row per audited candidate plus a row for every paper with none, saying
  which kind of absence it is), and export: copyable BibTeX, CSV/JSON download, and the existing
  `POST /api/import/resource-audits` with a preview first and a separate confirmation second.
- **The page keeps no vocabulary of its own where the backend already has one.** Publication labels
  come from the payload — `paper_document` puts them there precisely so no UI keeps a second copy —
  and the audit/component label tables in `search-core.js` are pinned to `models.py` by a test that
  fails if either side changes alone.
- Two silent drops in `result_model.normalize` are fixed, because otherwise the page would have had
  to parse prose to learn what the terminal prints: `queries` is carried through, and the tool's own
  `coverage` block (requested window, per-(query, source) attempts, pagination, run state) is merged
  into the normalized coverage instead of being replaced by the narrower summary. `paper_document`
  also exposes `sources` and `citations` as fields; both were previously only inside the text body.
- Every URL the page puts in an `href` goes through the existing `link()` sanitizer, so a hand-edited
  result file cannot smuggle a `javascript:` URL into the matrix. A Node test and a browser smoke
  both pin it.
- `tests/fixtures/search-result.json` is generated through the real code path by
  `scripts/gen_search_fixture.py`, and a Python test regenerates and compares it, so the Node tests
  cannot quietly pass against a shape the product no longer produces. Its four papers are fictional,
  and the file's own `note` field says so.
- 29 new Node tests (`tests/search-core.test.js`, 66 in total) and a third browser smoke
  (`scripts/search_browser_smoke.py`) that drives the page end to end offline, including the
  preview-then-write round trip through the existing import endpoint; 465 Python tests pass.
- **Not done, by design:** the page does not search. There is still no HTTP surface that runs a
  query, which is what an online demo would need — that is the remaining half of #14.

### Read-only incremental Zotero sync (#11)

- Add `backend/re0/zotero.py` (connector + mapping store), `backend/re0/zotero_sync.py` (plan and
  commit) and `re0 zotero status|collections|select|preview|sync|disconnect`. A user names one
  library; the sync previews, and only `--apply` writes.
- **It never opens a local Zotero database.** Sync goes through `api.zotero.org`, now on the shared
  host allowlist, with a key that is sent only there and stored nowhere — not in the database, not in
  the sync log, not in a response, not even masked. The console reads it from `ZOTERO_API_KEY` rather
  than a flag, because a command line is visible in shell history and the process list.
- **Attachments, annotations and notes are never transferred.** The item request carries an explicit
  *inclusion* list of 34 bibliographic types, so a private note body is not fetched and discarded —
  it is never asked for. Because that makes the response narrower than the change list, the
  difference is counted as `unaccounted` and reported; a narrow request cannot pass for a complete one.
- Identity is `(library_type, library_id, item_key)` plus the remote version. DOI and arXiv only
  *associate* a remote item with a paper already here, so the same work in two libraries keeps two
  mappings over one paper, and a paper with no DOI still syncs.
- **A remote deletion is a tombstone.** The link becomes `remote_deleted`; the paper, its notes, its
  resources and their observations all stay. Zotero no longer holding a record says something about
  Zotero and nothing about work done here. An item that comes back is relinked, not duplicated.
- **The cursor moves inside the same transaction as the links.** An interrupted sync re-reads the
  same window instead of committing a cursor past items nobody wrote. An incomplete page read — a
  `Total-Results` the pages never reached — refuses the sync outright for the same reason.
- Updates overwrite bibliographic fields only. `notes`, `topics` and reading `status` are read back
  from the stored row and merged, so a remote edit cannot reset an annotation to a default.
- Linking an item to a paper that already exists (from a CSL import, say) does **not** rewrite that
  paper. Linking is not importing.
- Collisions are refused during planning with a reason, not discovered at write time: a DOI already
  held by a different paper, two remote items sharing one DOI in the same window, and two library
  records with the same title (which is ambiguous, so nothing is guessed). Each is reported, and a
  deliberate skip is recorded in the sync log with the note that the cursor still advances, so it
  will not retry itself.
- `disconnect` forgets the cursor and the collection selection and keeps every paper;
  `--remove-links` also drops the mappings, and still deletes no research record.
- Add the HTTP surface over the same service: `GET /api/zotero/status`, `GET /api/zotero/links`,
  `POST /api/zotero/collections`, `PUT /api/zotero/selection`, `POST /api/zotero/sync` and
  `POST /api/zotero/disconnect`. **Preview is the default here too** — the body carries `apply`, and
  a request without it writes nothing but the collection list. `status` and `links` need no
  credential at all, so "what is mapped and where does the cursor stand" can be answered without
  handing over a key again.
- The key crosses the wire as a `SecretStr`, which is why it cannot ride out in a repr, a log line or
  an echoed validation error. It is used for the one call and dropped: the database holds the
  mapping, the cursor and the selection, never the key. `/api/zotero/sync` is serialized on a lock,
  so a second sync of the same library gets a 409 rather than two writers racing one cursor.
- **Scope filters are no longer silently narrowed.** Selecting three collections sends all three
  (Zotero accepts them comma-separated) where the first implementation sent one; several tags are
  sent as a union and the response *says* it is a union rather than leaving the reader to guess an
  intersection. The applied scope is returned beside the plan and stored on the cursor.
- The library page's settings dialog replaces the paragraph that claimed remote sync was
  unimplemented — a statement that had become false — with a credentials form, a collection picker,
  a preview and an explicit commit. The key field is cleared as soon as the commit returns, and the
  dialog clears it again on close.
- 428 Python tests (was 393), all offline against a fixture Zotero service; the browser smoke now
  drives the dialog end to end and asserts the key reaches neither `iterdump()` nor the status JSON.
  **Not done in this increment:** write-back even as a dry-run contract, and any real-account run —
  that needs the owner's local authorization, so it stays marked `live` and unauthorized.

### Continuing a research conversation: follow-ups, changed constraints, auditable reruns (#9)

- Add `backend/re0/agent/session.py`, the shared service behind `POST /api/agent/followups`,
  `POST /api/agent/retries`, `POST /api/agent/followups/scope`, `GET /api/agent/conversations`
  and `re0 session`. A run is one attempt; a **conversation** is the line of attempts that share
  evidence and one cumulative ledger. Three turn kinds are told apart because they authorize
  different things: `new`, `followup` (a new constraint on work already done) and `retry` (the same
  goal verbatim, so it cannot carry new scope).
- **Agent schema v2, additive.** `agent_conversations` plus five columns on `agent_runs`
  (`conversation_id`, `turn`, `kind`, `origin`, `idempotency_key`). Every pre-existing run is adopted
  into its own one-turn conversation with the counters from its last checkpoint, so a migration
  cannot erase what was already spent. A database stamped with a *later* version is refused rather
  than downgraded. Run statuses, exports and the paper schema are unchanged.
- **Scope is validated before the model sees any history.** Reuse is limited to evidence ids from the
  same conversation, or content-addressed source ids from an opt-in workspace. An id from another
  conversation is refused with a message that says so — not treated as "not found", which would send
  the caller hunting a typo where the real problem is scope. Refusing leaves no half-created turn.
- **Nothing is inherited silently.** `use_library` must be granted again per turn; a model endpoint
  that differs from the parent's requires an explicit `trust_new_destination`, so old material cannot
  travel to a provider nobody agreed to. Each turn stores an immutable `origin` snapshot — goal,
  destination, permissions, allowed tools, budget, and the ledger as it stood before the turn — which
  no later checkpoint can rewrite.
- **The ledger is cumulative and recomputed, never incremented.** A follow-up cannot escape a cap by
  being a new run. Caps can be raised only by an explicit `raise_session_caps` on the request that
  needs it, never lowered that way, and the cap is enforced live inside the turn rather than only at
  admission. `unreported_calls` is carried forward because a call whose provider did not report usage
  may still have been billed.
- **A repeated submit is not a second turn.** An `idempotency_key` returns the run it already created,
  checked *before* the busy guard so a double click is answered with the existing turn rather than
  "one task at a time". Evidence seeding is idempotent too: ids derive from the run and the origin.
- **A new report is a new version.** `report_delta` reports added, changed, dropped, still-uncertain
  and resolved-from-uncertain against the previous turn, and the earlier report stays exportable
  under its own run id. Findings are matched by the evidence they rest on, mapped back through reuse
  — otherwise a finding carried forward unchanged would read as one addition and one drop.
- Reused material keeps its original retrieval time and parent turn, and anything older than 30 days
  is flagged stale. **Staleness is reported, never acted on**: no automatic paid re-fetch.
- The handover gives the new turn the parent's goal and report, not the parent's transcript, and
  deliberately omits the parent's evidence ids — they belong to the earlier run and a report citing
  one is rejected. This reuses the existing compaction and `read_evidence`; it is not a second memory.
- `re0 session list|show|delta|scope` read the task database with no model and no network; `scope`
  runs the same validation the API runs and prints what a turn would be allowed to touch. Starting a
  turn needs a key, and keys live in server memory, so `follow-up`/`retry` post to the local service.
  The write guard now accepts `X-Re0-Client: cli`, refused when the request carries a browser
  `Origin` or `Referer`.
- Web adds a collapsed composer under a stopped run: the new condition, checkboxes for the evidence
  to reuse, and the two consent gates. The logic is the shared service's, not the page's.
- 393 Python tests (was 352) and 37 JS tests (was 31); both browser smoke scripts pass in Chromium,
  the agent one now driving a real second turn.

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
