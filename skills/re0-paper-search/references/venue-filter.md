# Conference venue filters

Conference proceedings are indexed by the existing scholarly sources. Passing `--venue NAME`
does not mean every source applies an exact venue filter: the result records each provider's mode
in `coverage.venue_filter`.

## Provider behavior observed on 2026-09-22

| Route | Recorded behavior |
|---|---|
| OpenAlex `primary_location.source.id` | A venue name is resolved through `/sources?filter=display_name.search:NAME`; non-matching names are discarded, and the remaining stable IDs are used for a strict filter. The run reports `mode=strict` and the resolved names. |
| Semantic Scholar `venue=` | The parameter is documented, but live checks were rate-limited with HTTP 429. The venue is prepended to the query and reported as `mode=hint`, not as a filter. |
| arXiv, Crossref, OpenReview | These routes do not accept a venue filter here. The venue is a query hint and the result is not narrowed by it. |
| DBLP | A non-browser request returned a bot challenge page instead of JSON during the recorded check. |
| OpenAlex source-name filter | `primary_location.source.display_name.search` returned HTTP 400 in the recorded check; stable source IDs were used instead. |

The strict-filter scope can be narrower than a conference family. OpenAlex may index each edition
as a separate source; the recorded CVPR lookup resolved one 2022 source. That is a strict filter for
the resolved source, not a promise to cover every CVPR edition. If no source resolves, the run
reports `resolve_failed` and falls back to a query hint. The result is wider, not silently filtered.

These are dated observations, not a guarantee that a provider behaves the same today. Recheck the
current `coverage.venue_filter` before describing a result as venue-filtered. Adding another broad
index would not answer a request for every accepted paper from an edition unless that source exposes
that coverage explicitly.
