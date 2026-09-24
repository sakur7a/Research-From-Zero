# ADR: Hosted Re0 Web Demo

- **Status:** the static Skill demo is live on Vercel; host/storage for the full backend remains undecided.
- **Decision:** use Vercel for the read-only Skill demo. Keep the Render Free Docker template as an unelected candidate for the full FastAPI service.
- **Scope:** the Vercel deployment is a static UI snapshot. It does not deploy the API, database, task worker or model configuration.

## Vercel static deployment · 2026-09-24

The frontend-only release is built with `python scripts/build_static_demo.py` into
`dist/static-demo/` and deployed from **that directory** to the existing `re0-skill-demo` project
(Framework: Other). The build copies an explicit asset allowlist; `build-info.json` gives the source
commit and UTC build time. It includes no API route, serverless function, SQLite file or model key.
The root page opens an interactive, dated historical case; `/search.html` is the same local browser
workbench, and `/skill.html` retains the detailed static audit. User-provided JSON stays in the
browser. The previous deployment below is historical and remains the live version until the new
deployment is confirmed READY and checked anonymously.

- Public URL: [https://re0-skill-demo.vercel.app/](https://re0-skill-demo.vercel.app/).
- Source snapshot: clean repository commit `54d529a91bdd83f0c6e7f60778c078f56a0fae66`; a temporary 23-file, 197.1 KB package contains the Skill page, its CSS/JS, and the static search viewer assets. No `.data`, SQLite file, `.env`, backend source or API key was uploaded.
- Vercel project: `re0-skill-demo`; deployment `dpl_69BNpxycDAojbbPvcK9uTD3a83Hp`. The CLI reported `target=production` and assigned the stable alias because this was the project's first deployment, even though the command did not pass `--prod`. The stable URL returned HTTP 200 anonymously; the public Chromium check passed tabs, keyboard navigation, clipboard copy and theme switching with zero page errors or failed assets.
- This only proves that the static page is reachable. It does not provide a real Agent/BYOK backend. Actions that require the API, such as saving an imported audit into the research library, are unavailable on this static site. It does not satisfy issues #17, #22 or #23.

## Full-backend hosting analysis

Re0 is a native Python/FastAPI process with SQLite, static ES modules, one worker and a durable task
loop. A Docker web service can run that existing shape with one process. A static host would need a
separate API service and external database, so it cannot replace the full backend. The Render template
remains an unselected candidate; no Render service or API deployment has been created.

Vercel can run FastAPI, but it packages the app as a Function. Its Hobby duration ceiling is
300 seconds while Re0's default per-task window is 360 seconds; after a `202` response, this app keeps
work in a process thread and its sessions/quotas/tasks depend on that process and local SQLite. Vercel
does not promise durable/shared local-file storage or shared instance memory, so a `vercel.json` alone
would not satisfy this runtime. A full Vercel deployment would require a different execution and
shared-storage design; the static Skill deployment does not supply that design.

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
These limits describe the Render candidate, not the current Vercel static page. Render Free services
sleep after 15 minutes without inbound traffic; waking typically takes about a
minute. Their filesystem can be cleared on restart, deploy or spin-down, and Free web services cannot
attach persistent disks. Therefore accounts, SQLite research records and workspace source files under
`/app/.data` can disappear. In that mode, the hosted login page makes this visible. This is only suitable for an
owner-approved demo that does not hold the only copy of research material.

Render documents 750 free instance hours per workspace each month. Outbound bandwidth beyond any
included allowance can be chargeable. The free plan's limits and the bandwidth/cost policy must be
rechecked before using that candidate. No paid model call or API-key test was run; Vercel plan limits
and cost caps have not been reviewed.

The generated `onrender.com` hostname is sufficient; no purchased domain is assumed. The template
does not send periodic pings to prevent sleep. A free service can therefore have a cold first request
and should not be described as always-on.

`RE0_STORAGE_MODE=persistent` is an operator declaration, not an automatic mount check. A persistent
deployment is valid only after a real persistent volume covers both SQLite and workspace files and a
backup/restore path is verified. The Render Free template intentionally does not claim persistence.

## Full-backend deployment acceptance

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
