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

# Equivalent, once the package is installed. Both call re0.skill_search, so they cannot drift.
re0 paper search --query "KV cache compression for long-context LLMs" --start-year 2024
re0 doctor            # what can run now; no network unless --probe-network is passed

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

## Surveying a topic

Recall is bounded by the **page**, not by how many queries you run, and nothing here re-ranks. So:

1. **Write three to five short queries using the words the target literature uses.**
   `image layer decomposition RGBA` beats `layered representation decomposition single image into
   layers`, because arXiv and Semantic Scholar match space-separated terms restrictively and a long
   phrase can return *less* than two words. Keep a domain qualifier in: a bare `layer decomposition`
   puts mathematics and finance papers first, because each service ranks by its own relevance.
2. **Raise `--max-papers` (default 20, tool maximum 25) before adding queries.** A work no source
   returned cannot be merged, corrected, or counted, so the page is the first lever.
3. **Then raise `--max-pages` (default 1, max 10) rather than the page size again.** `--max-papers`
   is the *per-page* ceiling, so paging widens recall without making the top of every page noisier.
   Only OpenAlex and Semantic Scholar document their cursors; the other three sources are read once
   and their coverage row says `stop_reason=not_supported` instead of looking complete. Read
   `stop_reason=complete` as "the source ran out of pages" and `page_budget` / `request_budget` /
   `cancelled` as "this run stopped first" — the second group always sets `truncated` and keeps the
   cursor. `--max-requests` (default 40) caps the whole call across every source and query; a spent
   budget is reported, never turned into an empty result.
4. **Pass `--venue NAME`** when the target is conference work. Where a stable source ID resolves
   (OpenAlex) the service filters on it and the resolved source names are printed, because a
   conference family is split into per-edition records; everywhere else the name is a query hint and
   is labelled as one. See *Conference papers* below before trusting a strict filter.
5. **Read the `per-source hits:` line and the `coverage:` block before reading the papers.** A
   source sitting at `=0` is a hole in the survey, not a gap in the literature, and `state=partial`
   says a source did not answer while `state=zero_hits` says every source answered and there was
   nothing. Those are different findings.
6. **Pass those queries together with `--queries "a|b|c"` instead of running the command five
   times.** One call merges once, folds a work that several queries found into a single record, and
   keeps *which* queries found it (`queries:` on the record, `coverage.attempts` for the whole run).
   Merging JSON files by hand loses both. The ceiling is 5 phrases per call; beyond that, split into
   separate runs so each keeps its own budget, and expect cross-run duplicates to reappear — write
   each run with `--json` and merge on DOI or arXiv ID.
7. **`--sources` takes `all`, one source, or a comma-separated subset**, so a recheck can cover two
   services together without silently widening to all five.

**A shared title is not enough to merge two works.** Two records that agree on a title but disagree
on real identifiers (different non-arXiv DOIs, different years) are kept apart, because merging them
would mint a paper that does not exist. A preprint and its published version still merge — one of
those DOIs is the arXiv DOI — and the second DOI is kept on the record rather than overwritten.

## Reporting from these results

This tool returns candidates and does not rank them. A report is where the judgement has to happen:

- **Keep only what you judged relevant, and say how many you set aside.** A run that returns 530
  records and 18 relevant ones should produce a report about the 18 plus a count, not 530 entries.
  Never append a "for completeness" section of matches the search returned but you rejected — that
  is noise with a heading, and it reads as thoroughness.
- **Carry the open-source candidates through.** They are printed next to each paper and also written
  to `--json` as `artifact_candidates`, so a report can be built without transcribing stdout. A
  feature whose output is dropped looks like a feature that does not exist.
- **Carry the audit column through, with its state and its scope.** `documents[].resource_audits`
  holds the per-class verdicts, the licence declarations and the sources each rests on. A report that
  keeps "code available" and drops `access_required`, `partially_available` or the commit the listing
  was read at has turned a check into a claim.
- **Carry the publication status and affiliations too**, with their caveats: an absent venue record
  is evidence about the services, not against the paper.
- **State the open-source coverage.** The name search covers `--find-artifacts` papers per run, so in
  a large survey most records have no candidate *because nobody searched them*. The run prints that
  coverage and `--json` carries it as `audit`, **with the denominator each count is out of**; repeat
  both, rather than letting silence read as "no code was released".

## Conference papers

Papers published only at a conference (CVPR, NeurIPS, ACL…) are **already covered**: Crossref,
Semantic Scholar and OpenAlex all index proceedings, and the venue is reported. A live query for
`CVPR diffusion model watermarking` returned the CVPR paper labelled
`已收录于会议或期刊 · 2024 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)`.

`--venue NAME` applies a real filter where one can be verified, and a query hint everywhere else:

```bash
python scripts/paper_search.py --query "diffusion watermarking" --venue CVPR --start-year 2024
```

Which happened is printed, per source, and is also in `coverage.venue_filter`:

| Route | Status |
|---|---|
| OpenAlex `primary_location.source.id` | **Verified live 2026-09-22.** The name is resolved through `/sources?filter=display_name.search:NAME`, a hit that does not actually carry the name is discarded, and the surviving IDs are OR-ed into the filter. Reported as `mode=strict`. |
| DBLP — the obvious conference index | **Unusable.** It answers a non-browser client with a `<title>Making sure you're not a bot!</title>` challenge page instead of JSON. |
| OpenAlex source-*name* filter | **Rejected by the API.** `primary_location.source.display_name.search` returns HTTP 400 — *"is not a valid field"*. Filtering by source needs the ID lookup above. |
| Semantic Scholar `venue=` | Documented, but **still unverified**: every attempt was rate-limited without an API key, most recently HTTP 429 on 2026-09-22. So the name is prepended to the query and reported as `mode=hint`, never as a filter. |
| arXiv, Crossref, OpenReview | No venue parameter here. `mode=hint` — the name joins the query and the result is **not** narrowed by venue. |

**A strict filter is only as wide as what resolved, and that is why the names are printed.** OpenAlex
indexes a conference family as one source per edition, and a live search for `CVPR` returned exactly
one: `2022 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)`. Filtering on it is
genuinely strict and genuinely narrower than "CVPR" means — so the run prints

```
会议/期刊条件（openalex）：来源按解析出的稳定 source ID 严格过滤 — 实际匹配到的 source：2022 IEEE/CVF …(CVPR)
```

and the coverage row carries `resolved_names`. If nothing resolves, the name falls back to a query
hint and the row says `resolve_failed`: the result set got *wider*, not narrower, and nothing was
silently dropped.

**More sources would not fix the remaining gap.** The five sources already hold hundreds of millions
of records including conference proceedings. What a keyword search still cannot ask is "everything
this venue accepted in a year it has no source record for", and a sixth index would not change that.

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
- **The page size is the recall ceiling for one page** (`--max-papers` defaults to 20, tool maximum
  25); `--max-pages` walks further, and `--max-requests` bounds the whole call. How to use them for a
  survey, and why a bigger page is also a noisier one, is in *Surveying a topic* above.
- **A run that stopped is not a run that found nothing.** `coverage.pagination` gives every
  `(query, source)` pair its own `stop_reason`; only `complete` means the source ran out of pages.
  `coverage.scheduling` reports `requests_used` against the ceiling, seconds deferred to a provider's
  own `Retry-After`, and cache hits. Cached answers are scoped by *which* credentials are configured,
  so an anonymous result is never reused for a credentialed call; the scope names the entitlement and
  never the key.

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

**Layer 1b — `--find-artifacts N`, on by default for the first 10 papers.** A paper frequently
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
of the whole run (0–8, default 0) — both layers' candidates — and writes a **field-level audit
row** to `documents[].resource_audits` in `--json`, printing the same row under the paper:

```
     → https://github.com/microsoft/LoRA
       状态 部分可用（partially_available）；提供商状态 metadata_accessible · 深度 file_listing · 提供商 github
       仓库元数据可访问；扫描到 1189 个文件条目，功能与可复现性尚未验证。
       资源类别: code_training=有候选, code_inference=有候选, code_evaluation=有候选, checkpoint=有候选,
                 dataset=有候选, data_split=有候选, preprocessing=有候选, environment=有候选
       许可证（来源声明，不是使用权限结论）: code=MIT
       limit: 检测到 adapter/LoRA 形式的权重候选（examples/NLU/roberta_base_lora_mnli.bin、
              examples/NLU/roberta_large_lora_mnli.bin），通常需要对应的基础模型才能使用；本次未验证该对应关系。
       limit: 未建立论文版本与资源版本的对应关系；version_match=unknown 表示未判断，不表示不匹配。
```

**The status is a closed vocabulary, and every value describes the check, never the resource.**
Write it up that way or not at all:

| Status | It means | It does **not** mean |
|---|---|---|
| `not_checked` | nobody looked, or the look raised | that looking would have failed |
| `candidate_located` | a link exists and nothing more | that it resolves |
| `metadata_readable` | the provider answered and a listing was read | that anything in it runs |
| `access_required` | the provider declares a gate | closed source, or no artifact |
| `partially_available` | usable, with a named gap (truncated listing, adapter-only weights, a secondary check that did not run) | complete |
| `access_failed` | this request did not complete | that the resource is absent |
| `not_found_in_scope` | the search ran to completion and returned nothing in the scope searched | that nothing exists elsewhere |
| `unsupported` | not a GitHub/Hugging Face page | anything at all about the target |

`provider_status` keeps the provider's own answer beside ours — `HTTP 404`, `HTTP 429`, `gated`,
`empty_repository` — **so a dead link and a rate limit never collapse into one sentence**. Quote it
when the distinction matters, which is whenever you are about to write that something is missing.

**Eight artifact classes are recorded separately**: `code_training`, `code_inference`,
`code_evaluation`, `checkpoint`, `dataset`, `data_split`, `preprocessing`, `environment`. Each is
`present` / `absent_in_scope` / `not_applicable` / `unknown` / `requires_access` / `check_failed`.
**`not_applicable` and `unknown` are different answers** — "this work needs no checkpoint" is a
finding, "this check could not tell" is a gap, and collapsing them is how a gap gets published as an
absence. A `present` or `absent_in_scope` always carries the source it rests on, pinned to a commit,
so **every affirmative conclusion can be opened**; the model refuses to store one that cannot.

This is exactly what a link alone cannot tell you:

| What you see | What the audit reports |
|---|---|
| the link resolves but the repository is **empty** | `not_found_in_scope`, `provider_status: empty_repository` — GitHub answered that there are no commits, which is not a reachability failure |
| the link **404s** | `access_failed` with `provider_status: HTTP 404` — "可能不存在、已移动或无访问权限", **not** "不存在" |
| you were **rate-limited** | `access_failed` with `provider_status: HTTP 429` — retryable, and not evidence about the resource |
| access is refused or the model is gated | `access_required`, `access: requires_application`, every class `requires_access` |
| weights are an **adapter/LoRA delta** | `partially_available`, naming the files and the base model they need |
| the file tree is **truncated** | `partially_available`, and every absence downgraded to `unknown` |
| not a GitHub/Hugging Face page | `unsupported` — the link is stored, nothing is claimed |

Every row that did not complete carries `本次检查没有完成；这不代表资源不存在或未开放。`

### What each layer can and cannot yield

| Layer | Fields returned | It answers | It cannot answer |
|---|---|---|---|
| 1a abstract regex | one URL | did the authors publish a link in the abstract | anything about what is behind it |
| 1b name search | `full_name`/`id`, `description`, `updated_at`, `official: unverified` | does something with this project name exist | whether it is this paper's work |
| 2 `--verify N` | a `ResourceAudit` row: `status`, `provider_status`, commit `revision`, `verification_depth`, `access`, per-class `coverage` with the pinned source each conclusion rests on, release asset names with declared sizes, per-class `licences`, `version_match`, `limitations` | is it reachable, is it empty, which artifact classes have candidate files, is a licence declared | whether the code runs, whether the weights download, whether it implements *this* paper |
| 3 `--resource-matrix PREFIX` | the same rows as one table, in JSON + Markdown + CSV, plus a `blockers` column and an `approval` payload | which of 2–6 papers can be compared as baselines, and what stops each one | anything layer 2 could not answer — it re-renders, it never re-checks |

Three things only layer 2 reveals, and each one changes the answer:

- **A public repository can carry no licence.** An empty `licences` is the difference between the
  code being readable and the code being usable, and no amount of name matching shows it. A licence
  is recorded **per artifact class** — code, checkpoint, dataset — because a repository licence is
  not automatically a licence for the weights or the data inside it, and this tool draws no legal
  conclusion from any of them.
- **A repository can hold code but no weights.** `coverage.checkpoint = absent_in_scope` beside
  `code_training = present` means the companion dataset or model link is not optional — it is where
  the artifact actually lives. And `absent_in_scope` names its scope: a truncated listing downgrades
  every absence to `unknown`, because the provider said the listing was incomplete.
- **A repository can be a fork.** A file tree containing a vendored copy of another project's source
  is visible in the listing and never in the name.

What no layer of this tool settles, and what therefore stays with a person or a further agent step:
**official attribution** (a name match is not authorship), **that the code runs**, **that the
weights download**, and **gated access** — which is reported as `access_failed` and must never be
written up as closed source.

**Why it is off by default, and why not a subagent.** One check costs about four GitHub
requests and the anonymous limit is roughly 60 per hour, so `--verify` defaults to 0, caps at 8,
and counts the candidates it left unchecked rather than hiding them — an unchecked link is printed
as a candidate, never as a failure. Set `GITHUB_TOKEN` to raise the ceiling. A subagent
reading the repository would hand back prose, whereas this check hands back a **status you can
compare across papers** and refuses to turn a failed request into a missing release. Set
`GITHUB_TOKEN` if you need to check more than a couple.

### Comparing several papers: `--resource-matrix PREFIX`

Writes `PREFIX.json`, `PREFIX.md` and `PREFIX.csv` from the rows already in the result — it
**re-renders, it never re-checks**, so it costs no requests. One row per resource candidate, for
2–6 papers; wider than that and the file says so rather than pretending to be a comparison set.

Each row carries the audit state, the eight class verdicts, licence declarations, the sources its
conclusions rest on, and a **`blockers` column**: why this row is not a drop-in baseline. Every
blocker is a restatement of a field on the row — unchecked, gated, adapter-only weights, a version
correspondence nobody established, an attribution still unconfirmed — never a new judgement.

**A paper with no candidate still gets a row**, carrying the search's own outcome. Dropping it would
make "searched and found nothing" and "the search failed" look identical in the only artifact a
reader compares from. The CSV escapes cells a spreadsheet would run as a formula, because every
string in it arrived from another service, and only http(s) links are written.

`PREFIX.json` also holds an `approval` block: the same papers and audit rows, with the
source-derived metadata the flattened table dropped. `POST /api/import/resource-audits` accepts it
and **previews unless you pass `dry_run: false`**. Importing links papers and resources idempotently
and never touches an existing paper's notes.

### What the run covered, with its denominators

Every run ends with the coverage block, and it is in `--json` as `audit`:

```
开源审计覆盖（分母 12 篇）：检索完成 0 · 部分完成 3 · 全部失败 0 · 无项目名可检索 5 · 未检索 4（名称检索预算 3，已用 3）
候选 12 个（作者自述 3，名称匹配 9）；已核验 2，未核验 10（核验预算 2，剩余 0）
审计结论分布: 未检查 10 · 元数据可读 1 · 部分可用 1
名称检索失败 6 次（明细见对应论文行；失败不是「没有结果」）
```

Both budgets are **shared across the whole run**, not per paper, and a paper whose title has no
project name does not spend the name-search budget. Report the denominators when you report any of
these counts: "0 verified" is either "nothing to verify" or "the budget ran out", and only the
second is a gap. `skipped` (no searchable project name) and `not-run` (disabled, or budget
exhausted) are **not** "searched and found nothing", and `partial` means some endpoints answered
while others did not — carry the failure list, which is in
`documents[].artifact_search_detail.failures`, into anything you write.

### Confirming what a check could not

`attribution` stays `unconfirmed` and `version_match` stays `unknown` in every row this tool
produces, **including the official repository of the paper you are reading about**: a name match is
not authorship and metadata is not version correspondence. Settling either is a person's act, and it
is stored as a **different kind of record** — `record_kind: confirmation` beside the
`record_kind: observation` it revises, appended rather than overwriting, and refused without a
locatable source. `POST /api/resources/{id}/confirmations` is that path.

**A `confirmed: true` in a tool result is not an approval.** The tool surface is read-only and no
argument reaches a write path, so a host that reports the user agreed has reported something this
server never asked it to carry. Ask the user, and let the confirmation come through the library.

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
