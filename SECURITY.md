# Security and privacy · v0.2

## Deployment boundary

Re0 is an **unauthenticated, local, single-user Alpha**. Bind only to loopback.
Do not expose it to the internet or untrusted LAN, even behind a bare reverse
proxy. Host checks, JSON content type, same-origin checks and `X-Re0-Client` are
CSRF/origin defenses, **not authorization**. Local software can still call the
API. Run one process/worker per SQLite file.

`X-Re0-Client` accepts `web` (the browser pages) and `cli` (the console entry
points that drive the same service, `re0 session follow-up`/`retry`). A request
claiming `cli` while carrying an `Origin` or `Referer` header is refused: a
browser always names its origin on a cross-origin POST, so a page cannot borrow
the console identity. This widens *who may write* to a loopback service that was
already unauthenticated; it does not make that service safe to expose.

## Secrets and material flow

Model settings entered in the browser are posted to the local backend and held
in process memory. The API never returns the configured key; validation errors
omit supplied values, and provider error bodies are suppressed. Keys are not
stored in task tables, normal exports, frontend storage or repository files.
Startup environment variables are supported but are not deleted by UI clear.
Memory storage is not a defense against a compromised local OS or process dump.

The selected LLM receives the user goal and tool-returned research material.
Library metadata is sent only when explicitly authorized for that task; notes,
attachments and fictional demo entries are excluded. Do not paste credentials,
confidential unpublished work or sensitive personal data into goals without
considering the selected provider's policies. Task text and evidence are stored
locally in plaintext, and exports may be sensitive. No at-rest encryption or
fine-grained retention controls are provided.

The model API credential is sent only to the user-selected allowed model
endpoint. GitHub credentials go only to `api.github.com`; Tavily credentials go
only to `api.tavily.com`. They are not available as model tools or tool arguments.
No telemetry or automatic fallback sends material to another model provider.
This is not a guarantee about the provider itself.

## Network and agent permissions

- Model endpoints require explicit trust. Remote destinations must be approved
  HTTPS hostnames; defaults and deployment extension are listed in README.
  Literal loopback endpoints need an explicit port. No URL credentials, fragments,
  query parameters, encoded paths or automatic redirects are allowed.
- HTTP clients ignore environment proxies and bound response sizes and durations.
  Allowlisting is not TLS certificate pinning or full protection against a
  compromised DNS/approved endpoint. Deployers must trust any host they add.
- Research tools use fixed provider APIs. Arbitrary URL fetching, downloading
  weights, PDF uploads, package installation, shells and source-code execution
  are not available. Repository paths are validated; file reads are pinned to a
  resolved commit and limited in size and line count.
- Searches, excerpts and README content are untrusted **data**. The system prompt
  tells the model not to follow instructions found there. More importantly, tool
  schemas and runtime allowlists prevent it from acquiring write/shell/config
  capabilities even when its judgment is influenced by prompt injection.
- The agent cannot approve its own results. Import is a separate user action and
  operates only on validated, tool-derived paper metadata. Existing DOI/arXiv
  matches and notes are not overwritten. Deleting library data remains a separate
  user-triggered operation in the retained library UI.
- Safe text rendering and link scheme validation protect the UI. Model output is
  not executed as HTML, JavaScript, Python or Markdown with embedded HTML.

These protections reduce privileges; they do **not** solve all prompt-injection
or model-misinterpretation risks. A model can still be misled into poor searches
or unsupported conclusions. Evidence-ID validation checks existence, not
semantic entailment. Human verification is mandatory for scientific claims.

## Cost, stop and persistence

Model/tool counts are reserved before requests and persist across manual resume.
Stopping is cooperative: an ongoing API request may complete and incur cost.
Timeouts apply at request/action boundaries; the task timer is not a hard global
kill switch. Provider-reported usage is informational, not a guaranteed invoice.
No automatic repeated paid retries, restart-resume or cross-provider failover.

Zotero sync holds an API key for the length of one call and stores it nowhere:
not in the database, not in the sync log, not in a response, not even masked. It
is sent only to `api.zotero.org`, which is on the same host allowlist as every
other provider, and the console reads it from the environment rather than a flag
because a command line persists in shell history and the process list. Local
Zotero database files are never opened. Attachments, annotations and notes are
never requested at all — the item call carries an inclusion list of bibliographic
types — and what that excludes is counted and reported rather than silently
absent. Sync is read-only: the connector has no write method.

Continuing a conversation does not reset any of it. Model and tool counts are
summed across every turn of a conversation and **recomputed from the runs**
rather than incremented, so a follow-up cannot escape a cap by being a new run;
the cap is enforced inside the turn as well as at admission. Raising a cap
requires an explicit field on the request that needs it and is recorded in that
turn's immutable snapshot. Spending is authorized per turn, library consent is
re-asked per turn, and a model endpoint that differs from the parent's needs an
explicit confirmation before any history is sent to it. An in-flight request may
still be billed after a cancel, and a call whose provider did not report usage is
counted separately as `unreported_calls` rather than assumed free.

Task records and approved papers share the original SQLite file. Use
`scripts/backup.py` rather than copying an active WAL database's main file alone.
Back up before upgrading. Never commit `.data`, `.env`, credentials, exported
private notes, live model responses or database files to GitHub.

## Reporting

Issue reports should include version, action, sanitized event/error text and
reproduction steps. Remove keys, request headers, private goals and documents.
Do not post a full database, environment file or unchecked task export publicly.
