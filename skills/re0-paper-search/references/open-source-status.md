# Open-source status: code, weights, data

Reference material for the `re0-paper-search` skill. The entry point is [SKILL.md](../SKILL.md); this file holds the detail that does not need to be read
to run a first search.

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

Each row carries the audit state, check time and scope, the eight class verdicts, licence
declarations, the sources its conclusions rest on, and a **`blockers` column**: why this row is not
a drop-in baseline. Every blocker is a restatement of a field on the row — unchecked, gated,
adapter-only weights, a version correspondence nobody established, an attribution still
unconfirmed — never a new judgement. The version 3 matrix contract adds `scope` to JSON, Markdown
and CSV so readers can tell what a negative result actually covered.

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
