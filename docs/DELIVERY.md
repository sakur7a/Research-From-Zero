# v0.2 publication and upgrade

This commit publishes the previously delivered agent-first refactor. It builds
on the existing repository history; no force push or database replacement is
part of the upgrade. The original ZIP and patch were produced before publication
and therefore describe their original local-only delivery status.

## Baseline

- repository: `sakur7a/Research-From-Zero`
- parent baseline: `7ab5cedc67eb86f1aa3a3b900e67634abb52a881`
- baseline tree: `6b5c0fdfeffee6c3b2319e47f62320a554f36137`

Application code is the same as the previously delivered v0.2 ZIP. Publication
updates only documentation about delivery. No user databases, API keys, private
reports, `.env` files or generated build caches are included.

## Upgrade an existing checkout

Back up the database using the old application before replacing code:

```bash
python scripts/backup.py --output backups/re0-before-agent.sqlite3
```

Stop the old service and ensure the Git worktree is clean, then:

```bash
git status --short
git switch main
git pull --ff-only origin main
python -m pip install -r requirements.txt
python run.py
```

Do not discard local edits or force a merge when Git reports divergence. Preserve
those changes on a separate branch and reconcile them first. Do not replace
`.data` or `.env`. Backups contain private research data and must not be committed.

The homepage is now the agent workbench; `/library` retains the literature UI.
Configure and test a tool-calling model before starting an AI task. The model
connection test makes a real request and may incur charges.

## Fresh checkout or source archive

```bash
git clone https://github.com/sakur7a/Research-From-Zero.git re0
cd re0
```

Follow README for the Python environment and startup. A separate checkout or
unzipped directory uses an empty database by default. To reuse an old library,
back it up, stop the old service and set `RE0_DB` to the original database's
absolute path. Never run both versions against one database concurrently.

## Original patch

The original patch applies to the baseline above. It is an alternative to pulling
the new source, not another installation step. Do not apply it again after this
commit. For a clean baseline checkout, `git apply --check` must pass before
applying it; otherwise merge the changes instead of forcing an overwrite.

## Verification

Before publication, 92 Python tests, 20 JavaScript tests and JavaScript syntax
checks passed again locally. Browser and local HTTP validation from the initial
delivery are recorded in `docs/TESTING.md`. Model/provider responses in those
protocol tests are fixtures, not live research-quality evaluations.

Remote CI status is recorded in GitHub Actions for this commit. A source push,
local test pass, or old CI run is not proof of this commit's remote CI outcome.
