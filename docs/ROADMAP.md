# Roadmap · Frontend-only Vercel delivery

## Current scope · 2026-09-24

The project owner has narrowed this release to **a directly usable frontend Demo
on Vercel, with no further backend implementation or deployment**.

This supersedes the earlier requirement that a public URL must wait for a complete
hosted Agent, live BYOK evaluation, account isolation, or database recovery.
The final deliverable is one stable Vercel HTTPS URL, not another backend plan or a
source archive the visitor has to run.

- [Current scope and audit — issue #15](https://github.com/sakur7a/Research-From-Zero/issues/15)
- [Single remaining release work package — issue #23](https://github.com/sakur7a/Research-From-Zero/issues/23)

## Current baseline

Frontend consolidation implementation: `scripts/build_static_demo.py` now produces the allowlisted
`dist/static-demo/` output with a dated LoRA historical excerpt, immediate local browser workflow,
optional JSON import, and no backend navigation or import action. The sample explicitly records that
it is curated from the public historical README/Skill record, not the original complete search JSON.
The local static-server browser acceptance is run by `scripts/static_demo_smoke.py`.
The stable [public HTTPS alias](https://re0-skill-demo.vercel.app/) now points to deployment
`dpl_HLVV2ixC5EEBKrVWde47y88vEzAr` (READY, production). Public `build-info.json` records source
commit `e5850b80ff886dee060c45a71588735eea1a6bda` and UTC build time
`2026-09-24T14:46:29.901316+00:00`. This update presents candidates as cards with clickable
metadata previews and concise dialogs; the previews are not PDF page images. The built-directory
and anonymous HTTPS browser checks passed sample loading, card/detail interactions, filtering,
matrix/source expansion, copy/download, malformed/oversized/hostile JSON,
clipboard/download failure, mobile navigation, keyboard use and refresh. No page errors, missing
assets or unexpected external/API requests were observed. `/api/health` and unknown pages returned
404. Publishing remains manual; a Git push alone does not change the site.

The reviewed application commit is `9062b73789f82dbc20e19b1c9e0bde5cfd621238`.
Its [CI run 35979859151](https://github.com/sakur7a/Research-From-Zero/actions/runs/35979859151)
passed Python 3.11/3.13, JavaScript, package installation, container, and browser checks.
Those are baseline results, not acceptance of a newly published static build.

The earlier release recorded a static site at
[re0-skill-demo.vercel.app](https://re0-skill-demo.vercel.app/).
That prior snapshot came from `54d529a91bdd83f0c6e7f60778c078f56a0fae66`,
deployment `dpl_69BNpxycDAojbbPvcK9uTD3a83Hp`. The current deployment is recorded above.
The project is not connected to automatic GitHub deployment.

`web/skill.html` already presents a dated historical resource audit with source links,
tabs, keyboard support, and themes. `web/search.html`, `search.js`, and `search-core.js`
provide browser-side result parsing, filtering, a resource matrix, and export logic.
The static build hides backend navigation and import actions while the local service keeps them.

## Only three release steps

### 1. Make the existing frontend self-contained

Offer an immediate, clearly labelled sample experience. No account, API Key, terminal,
installed skill, or user-provided JSON file is required to get started. Importing the
visitor's own result file remains optional and happens locally in the browser.

Reuse existing views for candidates, filters, resource matrices, evidence, and downloads.
Hide or replace backend-dependent login, server-library save, model settings, and task
controls. Do not fake successful API responses or live Agent progress. Keep source dates
and verification limits clear without repeating long developer instructions everywhere.

### 2. Make static publication repeatable

Use one explicit static output directory and an allowlisted build. Publish no API or
Edge/Serverless function, database, backend source, secrets, or private research files.
Configure Vercel as a static project; preserve useful existing links and test root/subpage
refreshes. Add a public source-commit/build identifier and a documented update path.
Do not select another host, add a database service, or change frontend frameworks merely
to publish this version.

### 3. Verify and hand over the public URL

Run browser tests against the built static directory, then against the actual anonymous
HTTPS deployment. Check navigation, mobile/keyboard interactions, sample loading, filters,
source expansion, copies/downloads, invalid JSON, missing assets, and unexpected requests.
There must be no calls to absent `/api/*`, localhost, model services, or proxy services.

Record the actual deployment ID, commit, stable alias, and test result before closing #23.
A short-lived authentication-share link is not the final public address.

## Product boundaries

This release is an interactive frontend prototype, not a hosted live research Agent.
Historical records remain labelled historical; imported data is not independently verified
by the page. Do not add a nonfunctional Key input or claim cloud persistence. User BYOK
remains part of the longer-term platform, but is not a gate for this frontend release.

Existing backend, skill, MCP, audit, full-text, follow-up, knowledge, and Zotero code stays
in the repository. Do not delete it or mark its unverified acceptance complete. Further
backend work, monitoring, model-quality evaluation, and multi-user hosting are deferred.
Existing #1–#22 discussions preserve that future scope; only frontend-relevant fixes are
in the current release path. Competition-specific materials and rules remain in #2 and
are not automatically satisfied by changing this project's release scope.

## Publication access and truthful status

The earlier connected-app check listed no accessible Vercel teams. The existing project's
Vercel CLI credentials were subsequently verified; the static build was deployed to that
project and accepted anonymously at the stable alias. No model credentials, paid plan or
backend host were required for this frontend release.

## Historical records

The previous detailed implementation/acceptance table is preserved in
[ROADMAP at 9062b737](https://github.com/sakur7a/Research-From-Zero/blob/9062b73789f82dbc20e19b1c9e0bde5cfd621238/docs/ROADMAP.md),
the original issues and comments, and `docs/TESTING.md`. Earlier full-backend deployment
requirements in README, DEPLOYMENT_ADR, and SUBMISSION_TODO describe that older scope;
they are no longer prerequisites for this frontend-only release. Current entry sections
have been updated while historical verification records remain intact.
