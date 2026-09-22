# Credentials

Reference material for the `re0-paper-search` skill. The entry point is [SKILL.md](../SKILL.md); this file holds the detail that does not need to be read
to run a first search.

Everything comes from the environment, and nothing is hardcoded. The script loads the
first file it finds, filling only variables that are **not already set** — a real
environment variable always wins:

| Order | Location |
|---|---|
| 1 | `$RE0_ENV_FILE` (explicit override) |
| 2 | `./.env` |
| 3 | `~/.codex/skills/.env` |
| 4 | `~/.re0/.env` |

Recognised names, each sent only to the service that owns it:

| Variable | Used for | Needed? |
|---|---|---|
| `OPENALEX_API_KEY` | OpenAlex | optional |
| `OPENALEX_MAILTO` | OpenAlex polite pool; identifies the caller | recommended |
| `SEMANTIC_SCHOLAR_API_KEY` (alias `SEMANTICSCHOLAR_API_KEY`) | Semantic Scholar | recommended — unauthenticated requests are rate limited hard |
| `OPENREVIEW_TOKEN` | OpenReview, for anything beyond public reads | optional |

The script prints how many variables the file supplied, **never a value**. No key is
written to the result file, the cache, or any log. When a source fails, the failure line names the
variable that would fix it if that variable is unset — a 429 from Semantic Scholar is usually a
missing key rather than a broken service.

### Setting `GITHUB_TOKEN`

It is a plain environment variable — never a literal in code and never a committed file:

| Context | How it is supplied |
|---|---|
| One shell session | `export GITHUB_TOKEN=ghp_…` (bash) or `$env:GITHUB_TOKEN="ghp_…"` (PowerShell) |
| Persisted for this skill | put `GITHUB_TOKEN=ghp_…` in the file `RE0_ENV_FILE` points at. The loader fills it like any other variable, never prints a value, and a real environment variable always wins |
| Online deployment | it must be a **server-side environment variable**. There is no secret store yet, so it must not be committed, exported in a task record, or baked into an image |

Create one at GitHub → *Settings → Developer settings → Personal access tokens*. A **classic token
with no scopes** is enough: the repositories are public and Re0 sends this token only to
`api.github.com`. Keep it in a `.gitignore`d file (`.env` already is).

| Endpoint | Anonymous | With a token |
|---|---|---|
| search (`--find-artifacts`) | **10 requests/minute** — measured from the response header | 30/minute (documented, not measured here) |
| core (`--verify`) | 60/hour (documented) | 5000/hour (documented) |

OpenReview is queried publicly and needs no account. Credentialed access uses a bearer
token rather than the username/password pair in some existing setups: posting a password
from inside a retrieval path would add a credential-handling surface for no gain while
public term search already answers the query. Set `OPENREVIEW_TOKEN` if you want the
wider read scope.
