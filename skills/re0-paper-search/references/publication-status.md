# Accepted, submitted, or preprint

Reference material for the `re0-paper-search` skill. The entry point is [SKILL.md](../SKILL.md); this file holds the detail that does not need to be read
to run a first search.

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
