---
name: re0-paper-search
description: Search literature across arXiv, OpenAlex, Semantic Scholar, OpenReview and Crossref; merge duplicate records, preserve per-source failures and pagination coverage, and optionally audit candidate code/data resources. Use for literature discovery, prior-art checks, topic surveys, and claims that need checkable sources.
---

# Re0 paper search

One search session, five scholarly sources, one de-duplicated candidate list. Submit one focused
phrase with `--query`, or up to five short phrases together with `--queries`; the shared request
budget and coverage report preserve which query and source found each record. The name is deliberately
distinct from a plain `paper_search` skill: this one reports failed sources rather than silently
returning fewer results.

## Installing it outside the repository

The script finds the `re0` package in this order, so a copy can live anywhere:

1. `RE0_HOME=/path/to/re0` — an explicit checkout, checked strictly and reported if wrong;
2. the repository the skill still sits in (`<repo>/skills/<skill>/scripts/`);
3. the ambient environment, i.e. an installed `re0-research` (`pip install -e <repo>`).

Install it with the CLI, which previews first and never overwrites silently. The distribution ships
this directory as package data, so an installed `re0` can put the skill in the host's skills root.
`~/.codex/skills/<skill>/` is another application's directory; never let this skill write to it
during a search. If Codex is the intended host, install it there only with the explicit
`re0 skill install` command above.

```bash
re0 skill show                                                # which directory ships, and its hashes
re0 skill install --target ~/.learnbuddy/skills --dry-run      # preview; writes nothing
re0 skill install --target ~/.learnbuddy/skills                # creates <target>/re0-paper-search
re0 skill package --output /tmp/re0-skill                      # a self-contained copy + MANIFEST.json
```

Every file is classified `new` / `identical` / `conflict` *before* anything is written. A conflict —
same name, different content — **stops the install with exit code 3** and leaves your file
byte-for-byte alone, because it may be an edit you made. `--force` replaces it and renames the old
one aside as `<name>.re0-backup-<UTC timestamp>` rather than deleting it. Re-running an install is
idempotent: identical files are skipped instead of rewritten. `MANIFEST.json` records each file's
sha256 plus a tree hash, so a host can later check whether its copy drifted.

A copy is still a snapshot: re-install after changing the repository, or run it from the repository.

```bash
python scripts/paper_search.py --query "KV cache compression for long-context LLMs" \
    --start-year 2024 --end-year 2026 --json /tmp/papers.json

# Merge several short queries in one bounded search session.
re0 paper search --queries "image layer decomposition RGBA|single image layer separation" \
    --max-papers 20 --max-pages 2 --json ./papers.json

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
- **Or read it in the workbench instead of transcribing it.** `http://127.0.0.1:8000/static/search.html`
  renders the same `--json` file: coverage with its denominators and its failed sources, the candidate
  list, the audit matrix with each row's blockers and sources, and copyable BibTeX. It performs no
  retrieval of its own — the file *is* the run — and it reads `coverage.requested`, `coverage.attempts`
  and `documents[].sources`, which the versioned structure now carries through instead of dropping.

## Conference papers

Conference proceedings are covered by the existing scholarly sources. `--venue NAME` is a strict
filter only where a stable venue ID resolves; elsewhere it is reported as a query hint. The run
prints the mode and any resolved names in `coverage.venue_filter`. See
[references/venue-filter.md](references/venue-filter.md) for provider boundaries and recorded checks.

## Credentials

Which keys change what, how to set `GITHUB_TOKEN`, and the rule that a credential is only ever sent to the provider that owns it. Detail, tables and the reasons behind each rule: **[references/credentials.md](references/credentials.md)**.

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

The four publication states, why an absent venue is never reported as "only a preprint", how a preprint and an accepted version coexist in one record, and what affiliations do and do not mean. Detail, tables and the reasons behind each rule: **[references/publication-status.md](references/publication-status.md)**.

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

How a released artifact is found and checked, the audit state vocabulary, what each layer can and cannot yield, `--resource-matrix`, the coverage denominators, and how to confirm what a check could not. Detail, tables and the reasons behind each rule: **[references/open-source-status.md](references/open-source-status.md)**.

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

## References

SKILL.md is the entry point; detailed credential, status and provider notes live beside it so a first
search does not require reading all of them. Provider-specific live checks are dated observations,
not guarantees of current API behavior.

| File | What it holds |
|---|---|
| [references/credentials.md](references/credentials.md) | which keys change what, `GITHUB_TOKEN`, and per-provider credential scoping |
| [references/publication-status.md](references/publication-status.md) | the four publication states, preprint-versus-accepted, affiliations |
| [references/open-source-status.md](references/open-source-status.md) | artifact discovery and checks, the audit vocabulary, `--resource-matrix`, coverage denominators, confirming by hand |
| [references/venue-filter.md](references/venue-filter.md) | provider-specific conference filtering, strict-filter scope, and query-hint limits |

## Related entry points

A retrieval result is a bibliographic record, never full text. When the question is what a paper
says about *its own* released code, weights or data, its experimental setup, or the limits its
authors wrote down, the answer is usually only in the body:

```bash
re0 paper text 2312.00286v1 --toc              # contents and chunk locators, no text
re0 paper text 2024.acl-long.1 --locator p.3   # one bounded slice
```

It takes an arXiv or ACL Anthology identifier and has **no `url` argument** — the destination is
built from the identifier against a fixed allowlist, so a link inside a paper cannot redirect the
reader, and a DOI is refused rather than resolved into a paywall. Locators come from the bytes that
were read, so re-reading the same version gives the same locators. References and appendices are
marked `back_matter`: a repository cited there is somebody else's. There is no OCR — a scanned PDF
is reported as `scan_only` with its page count instead of being guessed at.

- `python -m re0.mcp_server` exposes the same retrieval as MCP tools for any MCP client.
- Inside Re0's own task runs, every returned record becomes stored evidence with an ID
  that a report must cite. This skill is a retrieval endpoint and creates no evidence
  IDs, so do not cite one from here.
