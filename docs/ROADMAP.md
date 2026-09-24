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
Production acceptance and the final deployment identifier must be recorded after Vercel publishes
this exact build; a pushed commit alone does not change the site.

The reviewed application commit is `9062b73789f82dbc20e19b1c9e0bde5cfd621238`.
Its [CI run 35979859151](https://github.com/sakur7a/Research-From-Zero/actions/runs/35979859151)
passed Python 3.11/3.13, JavaScript, package installation, container, and browser checks.
Those are baseline results, not acceptance of a newly published static build.

The repository records an existing static site at
[re0-skill-demo.vercel.app](https://re0-skill-demo.vercel.app/).
That published snapshot comes from `54d529a91bdd83f0c6e7f60778c078f56a0fae66`,
deployment `dpl_69BNpxycDAojbbPvcK9uTD3a83Hp`. It is not connected to automatic
GitHub deployment: a new commit does not mean that the site has updated.

`web/skill.html` already presents a dated historical resource audit with source links,
tabs, keyboard support, and themes. `web/search.html`, `search.js`, and `search-core.js`
provide browser-side result parsing, filtering, a resource matrix, and export logic.
Some navigation and import actions still assume a backend; these are the remaining
frontend integration targets, not a reason to implement that backend.

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

During the 2026-09-24 scope review, the available Vercel connection listed no accessible
teams and refused project access for the existing site; its deploy action did not execute.
The existing project's team/project authorization must be available to update and verify
that deployment. This is not evidence that the public site is down.

This scope update does not publish a new frontend version or change backend code. It does
not authorize a paid plan or ask for model credentials. Frontend work and static tests can
proceed independently; publishing the updated site requires the existing Vercel access.

## Historical records

The previous detailed implementation/acceptance table is preserved in
[ROADMAP at 9062b737](https://github.com/sakur7a/Research-From-Zero/blob/9062b73789f82dbc20e19b1c9e0bde5cfd621238/docs/ROADMAP.md),
the original issues and comments, and `docs/TESTING.md`. Earlier full-backend deployment
requirements in README, DEPLOYMENT_ADR, and SUBMISSION_TODO describe that older scope;
they are no longer prerequisites for this frontend-only release. Reconcile their current
entry sections as part of #23 without deleting historical verification records.
