# Security and privacy · v0.2

## Deployment boundary

Re0 runs in one of two modes, and the mode is **declared, never inferred**: a
process cannot see who can reach its port, so it cannot work out on its own
whether it is public.

**`RE0_MODE=local` (the default) is an unauthenticated, single-user Alpha.**
Every row belongs to the owner `local`; there are no accounts and no login step.
Bind it to loopback. `run.py` refuses a non-loopback `RE0_HOST` in this mode
instead of starting an open door, and passes one worker explicitly. Do not expose
it to the internet or an untrusted LAN, even behind a bare reverse proxy. Host
checks, JSON content type, same-origin checks and `X-Re0-Client` are CSRF/origin
defenses, **not authorization**. Local software can still call the API. Run one
process/worker per SQLite file.

**`RE0_MODE=hosted` requires an identity and refuses to start without one:**
`RE0_SESSION_SECRET` (at least 32 characters; `python -m re0 auth secret` prints
one, once), `RE0_PUBLIC_ENTRY` (the https URL TLS terminates at) and
`RE0_ALLOWED_ORIGINS`. Start-up names every missing item at once and never falls
back to local mode silently. An http entry, `RE0_ALLOW_INSECURE_COOKIES` and a
documented example secret — including one padded until it is long enough — are
each a start-up refusal. Accounts are provisioned by an operator from the console
(`python -m re0 auth create-user`); **there is no registration endpoint**, and a
password is never a command-line flag. Every `/api/*` route then needs a session
cookie, while `/`, `/library` and `/api/health` stay reachable so a login form is
reachable at all. Cross-account access is answered with **404 rather than 403**,
because a 403 confirms that an id belongs to somebody and turns every identifier
into an enumeration oracle. Loopback model destinations are refused, and a model
host whose resolution is not entirely public is refused with no opt-out:
`RE0_ALLOW_LOCAL_RESOLVER` is a single-user setting and hosted mode says so
instead of pointing at it.

**Hosted mode is not yet a deliverable.** There is no login page in the UI (the
session endpoints exist; the browser pages do not use them yet), no quotas or rate
limiting, no site-wide circuit breaker, no retention or audit-redaction rules, and
none of it has run behind a real TLS terminator with a real second user. What is
in place — identity, per-account isolation, per-account key scope and the start-up
refusals above — is tested; what is missing is written here so it cannot be
mistaken for finished.

`X-Re0-Client` accepts `web` (the browser pages) and `cli` (the console entry
points that drive the same service, `re0 session follow-up`/`retry`). A request
claiming `cli` while carrying an `Origin` or `Referer` header is refused: a
browser always names its origin on a cross-origin POST, so a page cannot borrow
the console identity. In hosted mode a `cli` claim is refused outright. This
widens *who may write* to a loopback service that was already unauthenticated; it
does not make that service safe to expose.

## Secrets and material flow

Model settings entered in the browser are posted to the local backend and held
in process memory. The API never returns the configured key; validation errors
omit supplied values, and provider error bodies are suppressed. Keys are not
stored in task tables, normal exports, frontend storage or repository files.
Startup environment variables are supported but are not deleted by UI clear.
Memory storage is not a defense against a compromised local OS or process dump.

The vault holding those settings is **keyed by owner**, so one account's key is
not readable, echoable or clearable by another, and the configuration a turn runs
with is snapshotted when the turn launches: rotating a key afterwards cannot
redirect a turn already in flight. Passwords are PBKDF2-HMAC-SHA256 at 210k rounds
with a per-account salt; session tokens are opaque random values stored as
SHA-256 so they can be revoked, which a signed token cannot be. Verifying an
unknown account runs a dummy hash, so a wrong username does not answer faster than
a wrong password, and five failures lock an account for 15 minutes. Sessions live
in a cookie that is HttpOnly and SameSite=Strict, and Secure in hosted mode.

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

Ceilings sit in front of that, at three scopes: per account (`RE0_REQUESTS_PER_MINUTE`
API calls and `RE0_TASKS_PER_HOUR` turns of paid work), site-wide
(`RE0_SITE_REQUESTS_PER_MINUTE` and the serial execution slot), and per conversation
(the cumulative model/tool caps, enforced inside every turn). In `hosted` mode the
numbers are on by default; in `local` mode an unset knob means *no limit*, because a
ceiling that only ever throttles the one person at the keyboard is noise — setting it
explicitly works either way. A bucket is keyed on the verified identity, never on
`X-Forwarded-For` or any other value a caller controls, so a forged source address
cannot mint a fresh allowance; logged-out traffic shares one bucket rather than one
per invented address. Every refusal states the limit and when it resets, and spends
nothing: a request turned away at the door creates no task and no model call, and
counting the arrival is not the same as counting the spend.

The circuit breaker opens after `RE0` sees eight consecutive *destination* failures
(connection, timeout, 408/429/5xx) and refuses new work with 503 until a connection
test succeeds. It counts only failures of the model service, because one account's
wrong key or empty balance must not take the service away from everybody else — a
mistyped credential can be retried forever without opening anything. The breaker
protects the process and not the truth: it never interrupts a turn already running,
since killing one could discard a provider call that will still be billed, and an
unknown in-flight cost stays reported as unknown rather than as zero. Its state is
written to the database, so restarting is not a way around an outage; a hand-edited
state row is ignored rather than trusted. The limits and the breaker's current state
are published in `/api/health` and in `re0 doctor`, and each caller sees only their
own remaining budget.

Zotero sync holds an API key for the length of one call and stores it nowhere:
not in the database, not in the sync log, not in a response, not even masked. It
is sent only to `api.zotero.org`, which is on the same host allowlist as every
other provider, and the console reads it from the environment rather than a flag
because a command line persists in shell history and the process list. Local
Zotero database files are never opened. Attachments, annotations and notes are
never requested at all — the item call carries an inclusion list of bibliographic
types — and what that excludes is counted and reported rather than silently
absent. Sync is read-only: the connector has no write method.

Over HTTP the key arrives as a `SecretStr`, so it cannot ride out in a repr, a
log line or an echoed validation error — the app-wide handler already replaces
every validation message, and this body carries a secret too. Two endpoints need
no credential at all (`status`, `links`), so checking what is mapped does not
require re-sending a key. Preview is the default: a sync body without `apply`
writes nothing, and one sync at a time is enforced by a lock rather than by
trusting the caller not to click twice. In the browser the field is cleared as
soon as the commit returns and again when the dialog closes; the key is never
put in frontend storage. Filters are reported, not narrowed silently: a scope
that covered less than the reader asked for would look like a complete sync.

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

Upgrades that add the `owner` column (papers v1→v2, agent v2→v3, zotero v1→v2)
copy the file with the SQLite backup API before writing anything, assign every
existing row to the owner `local`, and compare row counts before dropping an old
table. A database written by a newer build is refused rather than downgraded, and
nothing is migrated in place without that copy beside it. `local` is a reserved
account name, so no account can later claim the rows a migration assigned.

## Reporting

Issue reports should include version, action, sanitized event/error text and
reproduction steps. Remove keys, request headers, private goals and documents.
Do not post a full database, environment file or unchecked task export publicly.
