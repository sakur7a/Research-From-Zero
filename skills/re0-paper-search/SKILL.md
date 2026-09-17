---
name: re0-paper-search
description: Find literature across arXiv, OpenAlex, Semantic Scholar, OpenReview and Crossref at once, merge the duplicates each source reports separately, report acceptance status and affiliations, and list which sources failed instead of reading a failure as "no such paper". Optionally verify that a paper's own code/data links resolve and are not empty. Use it for literature discovery, prior-art checks and topic surveys, and whenever a claim needs a source that can be checked.
---

# Re0 paper search

One query, five scholarly sources, one de-duplicated list. The name is deliberately distinct from
a plain `paper_search` skill: if both are installed, this one is the one that reports failed
sources rather than silently returning fewer results.

## Installing it outside the repository

The script finds the `re0` package in this order, so a copy can live anywhere:

1. `RE0_HOME=/path/to/re0` — an explicit checkout, checked strictly and reported if wrong;
2. the repository the skill still sits in (`<repo>/skills/<skill>/scripts/`);
3. the ambient environment, i.e. an installed `re0-research` (`pip install -e <repo>`).

`~/.codex/skills/<skill>/` is **another application's** directory; install there only if that
agent is the one you want to use, and never let this skill write to it.

```bash
# A copy works as long as re0 is either installed or RE0_HOME points at a checkout.
cp -r skills/re0-paper-search ~/.learnbuddy/skills/
RE0_HOME=/path/to/re0 python ~/.learnbuddy/skills/re0-paper-search/scripts/paper_search.py \
    --query "your topic"
```

A copy is a snapshot: re-copy after changing the repository, or run it from the repository.

```bash
python scripts/paper_search.py --query "KV cache compression for long-context LLMs" \
    --start-year 2024 --end-year 2026 --json /tmp/papers.json

```

## What it does

1. Queries every source that its credentials or public access allow, in signal order:
   **Semantic Scholar → OpenAlex → arXiv → OpenReview → Crossref**.
2. Merges duplicates **across** sources — the same paper is routinely listed by several
   of them with different metadata, and a list that shows it four times is a list the
   user cannot judge.
3. Prints a per-source hit line, so "quiet" and "broken" stay distinguishable.
4. Sinks survey/review papers to the bottom of the list, tagged `[survey]`, without
   removing them.

## Conference papers

Papers published only at a conference (CVPR, NeurIPS, ACL…) are **already covered**: Crossref,
Semantic Scholar and OpenAlex all index proceedings, and the venue is reported. A live query for
`CVPR diffusion model watermarking` returned the CVPR paper labelled
`已收录于会议或期刊 · 2024 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)`.

`--venue NAME` prepends a venue name to the query:

```bash
python scripts/paper_search.py --query "diffusion watermarking" --venue CVPR --start-year 2024
```

**That is a query hint, not an API-side venue filter.** The reason is worth recording so nobody
retries these blindly:

| Route | Status |
|---|---|
| DBLP — the obvious conference index | **Unusable.** It answers a non-browser client with a `<title>Making sure you're not a bot!</title>` challenge page instead of JSON. |
| OpenAlex source-name filter | **Rejected by the API.** `primary_location.source.display_name.search` returns HTTP 400 — *"is not a valid field"*. Filtering by source needs a source ID from a separate lookup. |
| Semantic Scholar `venue=` | Documented, but **unverified**: every attempt was rate-limited without an API key, so it is not used. |

An API-side venue filter would be genuinely better for "what did CVPR 2024 accept", because a
keyword query only surfaces papers whose *text* matches. It is not shipped until one of those
routes can be verified end to end.

**More sources would not fix this.** The five sources already hold hundreds of millions of records
including conference proceedings; the remaining gap is query shape, not source count. A keyword
search cannot ask "everything this venue accepted", and a sixth index would not change that.

## Credentials

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

## Reading the output

- **`per-source hits: … · N unique (M duplicates merged)`** — if a source shows `=0` and
  also appears under "source failures", that source was **not searched**.
- **A failure is not a negative result.** A rate limit or an outage must be reported as
  such. Never write "no such work exists" because a source was unreachable.
- **An empty list is not evidence of absence.** Widen the query, change the year window,
  or recheck one source alone before concluding anything.
- `--max-papers` defaults to 20; the tool caps it at 25. **This is the recall ceiling** — a paper
  no source returned cannot be merged, corrected or counted, so raise it first when a survey feels
  thin.
- **A bigger page means a noisier head, not a better one.** These sources rank by their own
  relevance and nothing here re-ranks, so a two-word query like `layer decomposition` puts
  mathematics and finance papers first. Recall comes from the page size; **precision comes from the
  query** — use the words the target literature uses (`image layer decomposition RGBA`) and run
  several short queries rather than one long phrase. arXiv and Semantic Scholar treat
  space-separated terms restrictively, so a long phrase can return *less* than two words do.

## Accepted, submitted, or preprint

Each result reports where it stands, and **which service said so**:

```
     2021 · 已收录于会议或期刊 · Neural Information Processing Systems（据 semanticscholar）
     机构: Microsoft Research, Tsinghua University
```

Four states, and the raw venue string is always printed next to the classification, so a wrong
call is checkable instead of hidden:

| State | Meaning |
|---|---|
| `有会议或期刊版本` | a service named a journal or conference |
| `投稿或评审中` | the record describes a submission (OpenReview's "… Conference Submission") |
| `仅见预印本版本` | **the services reached this run** named only a preprint server |
| `来源未给出发表信息` | no service carried a venue |

**An unstated venue is `来源未给出发表信息`, never `仅见预印本版本`.** A service with no venue has
told us nothing, and calling that "just a preprint" would be an invented conclusion.

**Why the wording is `仅见预印本版本`.** A preprint and its published version are normally **two
separate records** — OpenAlex keeps the arXiv work and the ICCV work apart, each with its own DOI —
so a run that reached only the preprint record must not report the absence of a publication. Two
things fix it, in order of effect:

1. **Set `SEMANTIC_SCHOLAR_API_KEY`.** Semantic Scholar merges the versions of one work into a
   single entry and reports its venue, making it the largest correction here. It is also the main
   source of preprint affiliations and citation counts, so it fixes three complaints at once.
2. **Raise `--max-papers`.** A published record only reaches the merge if some source returned it.

A paper is often **both** — an arXiv version plus a published one — so when sources disagree the
stronger claim wins and the weaker one is still reported (`注: 同一工作另有预印本版本被索引`).
Showing only the winner would hide the preprint; showing only the preprint would hide the
acceptance.

Sources: OpenAlex `type` + `primary_location.source.type` (it states outright when a work is a
preprint or sits in a repository), Semantic Scholar `publicationVenue`, Crossref `type` +
`container-title`, OpenReview's venue string, and arXiv's structured `journal_ref`. arXiv
free-text comments are **not** parsed, so an acceptance recorded only in a comment will not show
until another service reports it.

## Affiliations

Up to three institutions, then a count:

```
     机构: Microsoft Research, Tsinghua University（另有 2 个）
```

They come from Semantic Scholar's `authors.affiliations` and OpenAlex's parsed ROR institutions,
de-duplicated, in first-seen order so the lead authors' affiliations come first.

**Coverage is uneven and empty is common.** Semantic Scholar is the main source for preprint
affiliations where OpenAlex usually has none, so when Semantic Scholar rate-limits (no API key)
most results will read `机构: 各来源均未提供`. That means the services did not say, **not** that
the authors are unaffiliated — setting `SEMANTIC_SCHOLAR_API_KEY` improves this materially.

## Links

Each result prints three tiers, best first:

```
arXiv:  https://arxiv.org/abs/2106.09685        ← the paper itself
DOI:    https://doi.org/10.48550/arxiv.2106.09685
record: https://www.semanticscholar.org/paper/… ← only where it was found
```

arXiv comes first because that is usually the link people want. Many services report only
arXiv's DOI (`10.48550/arXiv.<id>`), so the ID is recovered from the DOI when no service
supplied it directly. A source page is never the primary link when a better one exists.

## Open-source status (code, weights, data)

Two layers, and you decide how far to go.

**Layer 1a — always on.** Code/data URLs **the authors themselves put in the abstract**, marked
as author-declared. Most abstracts do not contain one, so on its own this is usually empty.

**Layer 1b — `--find-artifacts N`, on by default for the first 5 papers.** A paper frequently
carries no link in any metadata field yet does have released code, weights or data, so the skill
searches **GitHub and the Hugging Face Hub for the paper's project name** — the part of the title
before a colon, which is also how these projects name their repositories. `RevealLayer:
Disentangling Hidden and Visible Layers…` carries no link in its abstract or body, and this finds
`github.com/360CVGroup/RevealLayer`, `huggingface.co/qihoo360/RevealLayer` and the
`RevealLayer-100K` dataset.

Every hit is a **name match, not proof of authorship**, and is labelled as such. Two markers help
triage: `描述与论文标题相符` means the repository description repeats the paper title (much
stronger), `仅名称匹配` means only the name is shared. Confirming ownership stays your call —
`--verify` answers a different question, whether a candidate resolves and is not empty.

The line prints even when nothing is found, and keeps **three** outcomes apart: found candidates,
searched and genuinely found nothing, and the search **did not complete** (each endpoint gets one
retry, because a transient network failure must never be reported as "there is no repository").

**Layer 2 — opt-in, `--verify N`.** Runs Re0's bounded resource check on the first N candidates
of the whole run (0–8, default 0) — both layers' candidates — and prints the outcome there:

```
     → https://github.com/microsoft/LoRA
       status metadata_accessible · depth file_listing · provider github
       仓库元数据可访问；扫描到 1189 个文件条目，功能与可复现性尚未验证。
       candidate files: training=12, inference=1, evaluation=12, weights=2, data=1, environment=12
       limit: 仅检查默认分支文件名、README 和前 10 个 Release；未下载权重或数据、未运行代码。
```

This is exactly what a link alone cannot tell you:

| What you see | What the check reports |
|---|---|
| the link resolves but the repository is **empty** | `metadata_accessible` with a **file count near zero** and no candidate files |
| the link **404s** | `indeterminate` — "可能不存在、已移动或无访问权限", **not** "不存在" |
| access is refused or the model is gated | `access_failed` — "可能需要授权，尚不能确定资源状态" |
| not a GitHub/Hugging Face page | `unsupported` — the link is stored, nothing is claimed |

Every outcome carries `本次没有完成内容验证；失败或未支持不等于资源未开放。`

**Why it is off by default, and why not a subagent.** One check costs about four GitHub
requests and the anonymous limit is roughly 60 per hour, so `--verify` defaults to 0, caps at 8,
and counts the candidates it left unchecked rather than hiding them — an unchecked link is printed
as a candidate, never as a failure. Set `GITHUB_TOKEN` to raise the ceiling. A subagent
reading the repository would hand back prose, whereas this check hands back a **status you can
compare across papers** and refuses to turn a failed request into a missing release. Set
`GITHUB_TOKEN` if you need to check more than a couple.

Going further is a separate step with the tools already in this repository — use them through
the MCP server or a Re0 task rather than growing a second pipeline here:

| Question | Tool |
|---|---|
| Is there a matching repository? | `search_repositories` |
| Are there companion weights or datasets? | `search_hub` |
| What did the repository actually publish (releases, file listing, resource links in the README)? | `inspect_resource` |
| What is in that file? | `read_repository_file` |
| Did the authors merely announce a plan to release? | `search_release_discussions` |

Keep Re0's wording when reporting the outcome: `metadata readable ≠ downloaded`,
`a filename ≠ something runnable`, `gated access ≠ closed source`, `not found ≠ does not exist`,
and a link in a README may be a baseline or a dependency rather than this paper's implementation.

## What this deliberately does not do

- **No "model knowledge" source.** Recalling papers from a model's training data is the
  most common way a literature list acquires a plausible title that does not exist. This
  skill returns only what a service actually returned; if a canonical older paper is
  missing from every source, that gap is reported rather than filled from memory.
- **No full text.** These are bibliographic records and abstracts. Nothing here supports
  a claim about a paper's experiments, and a record is never evidence that a result
  reproduces.
- **No semantic relevance filter.** The tool does not silently drop rows it judges
  irrelevant, because a dropped row is invisible to the user. Rejecting a result is the
  reader's call, made on a visible list.

## Related entry points

- `python -m re0.mcp_server` exposes the same retrieval as MCP tools for any MCP client.
- Inside Re0's own task runs, every returned record becomes stored evidence with an ID
  that a report must cite. This skill is a retrieval endpoint and creates no evidence
  IDs, so do not cite one from here.
