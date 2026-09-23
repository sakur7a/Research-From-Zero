# ADR: one hosted Web Demo path

- **Status:** proposed; owner acceptance and deployment are pending.
- **Decision:** prepare one Render Free Docker web service from `render.yaml`.
- **Scope:** a reviewable hosted demo configuration; this does not create a Render service or URL.

## Why this path

Re0 is a native Python/FastAPI process with SQLite, static ES modules, one worker and a durable task
loop. A Docker web service can run that existing shape with one process. A static host would need a
separate API service and external database, so it is not a smaller demo path. The repo keeps only the
Render template to avoid maintaining multiple unverified hosting recipes.

Vercel is a real FastAPI option, but it packages the app as a Function. Its Hobby duration ceiling is
300 seconds while Re0's default per-task window is 360 seconds; after a `202` response, this app keeps
work in a process thread and its sessions/quotas/tasks depend on that process and local SQLite. Vercel
does not promise durable/shared local-file storage or shared instance memory, so a `vercel.json` alone
would not satisfy this runtime. Selecting Vercel would require a different execution and shared-storage
design, which the master issue tracks only if it becomes a hard competition requirement.

The container listens on `0.0.0.0` and uses the platform-provided `PORT` when present. Render's
`RENDER=true`, `RENDER_EXTERNAL_URL` and `RENDER_EXTERNAL_HOSTNAME` provide defaults for the public
HTTPS entry, same-origin allowlist and trusted Host. Explicit `RE0_PUBLIC_ENTRY`,
`RE0_ALLOWED_ORIGINS` and `RE0_ALLOWED_HOSTS` take precedence. The template asks the operator for a
fresh `RE0_SESSION_SECRET`; no secret is committed.

Submitting a task returns HTTP 202 and the one worker thread continues in that same process. There is
no queue outside the container: a process stop cannot promise task continuation. With `ephemeral-demo`,
SQLite and workspace files are on the instance filesystem; if they disappear, the account/session
table and saved task state disappear with them. A previously active model request may still finish
and incur a charge. Model keys remain in process memory for at most eight hours and are cleared on
logout or process stop; they are never in the task export.

## Data and runtime limits

The template sets `RE0_STORAGE_MODE=ephemeral-demo`, uses one free instance, and does not add a disk.
Render Free services sleep after 15 minutes without inbound traffic; waking typically takes about a
minute. Their filesystem can be cleared on restart, deploy or spin-down, and Free web services cannot
attach persistent disks. Therefore accounts, SQLite research records and workspace source files under
`/app/.data` can disappear. The public login page makes this visible. This is only suitable for an
owner-approved demo that does not hold the only copy of research material.

Render documents 750 free instance hours per workspace each month. Outbound bandwidth beyond any
included allowance can be chargeable. The free plan's limits and the bandwidth/cost policy must be
rechecked before deployment. No paid model call, API-key test or hosting account operation is part of
this repository change.

The generated `onrender.com` hostname is sufficient; no purchased domain is assumed. The template
does not send periodic pings to prevent sleep. A free service can therefore have a cold first request
and should not be described as always-on.

`RE0_STORAGE_MODE=persistent` is an operator declaration, not an automatic mount check. A persistent
deployment is valid only after a real persistent volume covers both SQLite and workspace files and a
backup/restore path is verified. The Render Free template intentionally does not claim persistence.

## Deploy acceptance

1. The owner accepts the host, storage-loss behavior, account, competition URL requirement and any
   bandwidth/cost exposure.
2. Set a new secret in the Render Blueprint prompt; do not copy an example or commit it.
3. Create the service from the blueprint and wait for `/api/health` to pass. The username/password
   path is completed by issue #18's self-service onboarding; this template does not depend on an
   interactive platform shell.
4. Complete issue #18 guest/BYOK work, then verify a real HTTPS login/guest session, a second isolated
   browser, restart behavior, and the complete agent → resource review → export flow.
5. Record the actual URL, current deployment commit, restoration limitations and remaining failures
   before marking issue #23 complete.

Render's current limits are documented in [Free instances](https://render.com/docs/free),
[web services](https://render.com/docs/web-services),
[environment variables](https://render.com/docs/environment-variables),
[health checks](https://render.com/docs/health-checks), and the
[Blueprint specification](https://render.com/docs/blueprint-spec).
The alternative reviewed above is described by Vercel's
[FastAPI deployment guide](https://vercel.com/docs/frameworks/backend/fastapi),
[Function limits](https://vercel.com/docs/functions/limitations), and
[SQLite storage guidance](https://vercel.com/kb/guide/is-sqlite-supported-in-vercel).
