# Working on re0

## Start here
- Read README.md, docs/ARCHITECTURE.md, docs/ROADMAP.md and SECURITY.md.
- The app is a local single-user FastAPI + SQLite service with native ES modules.
- Run `python -m pytest`, `npm test`, and `npm run check` after changes.
- For UI work also run `python scripts/browser_smoke.py` with Playwright/Chromium.
- Provider tests are fixture-based. Report separately which live checks actually ran.

## Evidence invariants
- Accessible metadata is not a file download, working training implementation, or reproduction.
- A failed/404/truncated/unsupported request does not establish that an artifact is unpublished.
- User ownership/release assertions remain separate from automatic observations.
- Preserve source locator, inspected revision, check scope, limitations, and paper version snapshot.
- Rechecks append observations; do not overwrite old judgments. Explicit resource/paper deletion is destructive and confirmed.
- No automatic official attribution from similar names. README links are unconfirmed candidates.
- Fictional fixtures must remain visibly marked and cannot execute live resource checks.
- Do not fabricate theory, citation, contradiction, or result-comparison edges.

## Engineering constraints
- No remote code execution, arbitrary URL proxy, credential leakage, or automatic uploads of private notes.
- Keep database migrations versioned and reversible. Back up before destructive schema changes.
- Preserve DOI/arXiv identity and imported notes; collisions must be visible, not silent overwrites.
- Do not commit .env, .data, SQLite files, logs, downloaded PDFs, tokens, or user exports.
- Keep external network requests bounded and separately test adapter error handling.
- Never report a remote repo, CI run, deployment, or integration as successful without a verified result.
- Do not pick a public license or change repository visibility without owner approval.

## Delivery
Prefer small commits, an updated changelog, and test evidence. Record known limitations
and new user feedback in docs/ROADMAP.md or issues rather than silently expanding scope.
