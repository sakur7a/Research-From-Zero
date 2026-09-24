/** The retrieval workbench's pure logic: one versioned result in, views out.
 *
 * Nothing here decides anything the backend has not already decided. `result_model` owns the shape,
 * `resource_matrix` owns the rows and their blockers, `models.py` owns the label vocabulary and
 * `service.bibtex_export` owns the citation format — so those four are mirrored, and the label tables
 * are pinned to `models.py` by a test rather than merely copied. A page that quietly invented its own
 * second answer is the exact failure the shared result shape exists to prevent.
 *
 * Two habits carried over on purpose: every label is shown as `中文（raw token）`, so a translation is
 * checkable against the JSON; and a failed source is rendered as a failure, never as an empty result.
 */
import {e} from './core.js';

export const SCHEMA_VERSION = '1';

// Mirrors of models.py. The drift test fails if either side changes alone.
// PUBLICATION_LABELS is deliberately *not* mirrored: `paper_document` puts the label into the payload
// ("so the UI renders the state without keeping a second copy of these labels"), and a page that kept
// its own copy anyway is how two surfaces come to disagree about the same record.
export const AUDIT_LABELS = {not_checked: '未检查', candidate_located: '候选已定位',
  metadata_readable: '元数据可读', access_required: '需申请', partially_available: '部分可用',
  access_failed: '访问失败', not_found_in_scope: '检查范围内未找到', unsupported: '不支持'};
export const COMPONENT_LABELS = {present: '有候选', absent_in_scope: '检查范围内未见',
  not_applicable: '不适用', unknown: '未知', requires_access: '需申请', check_failed: '检查未完成'};
export const AUDIT_COMPONENTS = ['code_training', 'code_inference', 'code_evaluation', 'checkpoint',
  'dataset', 'data_split', 'preprocessing', 'environment'];

// The audit vocabulary is not the library's: `open`/`requires_application` describe what a check
// found, while the library's `gated`/`indeterminate` describe a saved resource. Kept apart on purpose.
export const ACCESS_LABELS = {open: '可公开获取', requires_application: '需申请',
  not_applicable: '不适用', unknown: '未知'};
export const ATTRIBUTION_LABELS = {unconfirmed: '归属待确认', official: '官方（有可打开的交叉证据）',
  third_party: '第三方'};
export const DECLARATION_LABELS = {undeclared: '未记录声明', promised: '声明计划发布',
  released: '声明已发布'};
export const DEPTH_LABELS = {not_checked: '未核验', metadata_only: '仅元数据',
  file_listing: '读到文件列表', content_read: '读到文件内容'};
export const VERSION_MATCH_LABELS = {matched: '版本对应已确认', mismatched: '版本不一致',
  unknown: '版本对应未确认'};
export const SEARCH_STATE_LABELS = {searched: '检索完成', partial: '部分完成', failed: '检索失败',
  skipped: '标题无可检索项目名', 'not-run': '未检索'};

/** `中文（raw）`, so a label can always be checked against the value it came from. */
export function labelled(table, value, fallback = '') {
  const label = table[value];
  if (!label) return e(value || fallback);
  return `${e(label)}（${e(value)}）`;
}

export function parseResult(text) {
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    return {ok: false, error: `不是合法 JSON：${error.message}`};
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return {ok: false, error: '需要一个 JSON 对象（`re0 paper search --json out.json` 写出的那一份）'};
  }
  if (payload.schema_version === undefined) {
    return {ok: false, error: '没有 schema_version：这不像 `re0 paper search --json` 的输出。'
      + '请用同一个命令重新导出，不要手工拼装。'};
  }
  if (Array.isArray(payload.rows) && payload.approval !== undefined) {
    // `--resource-matrix` writes a JSON file too, and it carries the same schema_version. Rendering
    // it as a result would show an empty candidate list, which reads as "the search found nothing".
    return {ok: false, error: '这是 `--resource-matrix` 写出的矩阵文件，不是检索结果。'
      + '这一页读的是 `--json out.json` 那一份，矩阵由同一批 documents 在这里重新渲染。'};
  }
  if (payload.schema_version !== SCHEMA_VERSION) {
    // Still rendered: an unknown field rides along rather than being dropped, and a page that
    // refuses to open a newer file is a page that stops working the day the schema moves.
    return {ok: true, result: payload,
            warning: `这份结果声明 schema_version ${payload.schema_version}，本页面按 ${SCHEMA_VERSION} 渲染；`
              + '认不出的字段会原样列在最后，不会静默丢掉。'};
  }
  return {ok: true, result: payload};
}

/** Best link first, mirroring `skill_search.links_for`: an identifier resolves to the work itself,
 * and the record page is only where it was found. */
export function paperLinks(paper) {
  const links = [];
  if (paper.arxiv_id) links.push({label: 'arXiv', url: `https://arxiv.org/abs/${paper.arxiv_id}`});
  if (paper.doi) links.push({label: 'DOI', url: `https://doi.org/${paper.doi}`});
  if (paper.paper_url && !links.some(item => item.url === paper.paper_url)) {
    links.push({label: 'record', url: paper.paper_url});
  }
  return links;
}

export function candidates(result) {
  return (result.documents || []).map((document, index) => {
    const paper = document.paper || {};
    const publication = document.publication || {};
    const audits = document.resource_audits || [];
    return {
      key: `${index}:${paper.doi || paper.arxiv_id || paper.title || ''}`,
      index,
      title: paper.title || '（无标题）',
      authors: paper.authors || [],
      year: paper.year ?? null,
      venue: document.venue || paper.venue || '',
      // Which services reported this work: a single-source hit and one several agree on are not the
      // same claim, and the field is on the document rather than parsed back out of its text.
      sources: document.sources || [],
      citations: document.citations ?? null,
      institutions: document.institutions || [],
      abstract: paper.abstract || '',
      doi: paper.doi || '',
      arxiv_id: paper.arxiv_id || '',
      publication: publication.state || 'unknown',
      // The backend's own wording, so this page cannot drift from the CLI's. When it is missing the
      // raw token is shown: an invented translation would be a claim nobody made.
      publication_label: publication.label || publication.state || 'unknown',
      publication_venue: publication.venue || '',
      publication_source: publication.source || '',
      preprint_also: document.preprint_also || null,
      links: paperLinks(paper),
      audits,
      audit_count: audits.length,
      checked: audits.filter(row => row.status !== 'not_checked').length,
      open: audits.filter(row => row.access === 'open').length,
      search_state: document.artifact_search || 'not-run',
      search_detail: document.artifact_search_detail || {},
      body: document.body || {excerpt: '', content_chars: 0, truncated: false},
      unrecognised: document.unrecognised || null,
    };
  });
}

export const SORTS = {
  relevance: '检索顺序',
  year: '年份（新→旧）',
  citations: '被引（多→少）',
  audits: '资源候选（多→少）',
  title: '标题（A→Z）',
};

export function filterSort(rows, {query = '', year = '', source = '', publication = '',
                                   openOnly = false, sort = 'relevance'} = {}) {
  const needle = query.trim().toLowerCase();
  const kept = rows.filter(row => {
    if (needle) {
      const haystack = [row.title, row.venue, row.abstract, row.doi, row.arxiv_id,
                        ...(row.authors || []), ...(row.institutions || []),
                        ...(row.sources || [])].join(' ').toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    if (year && String(row.year ?? '') !== String(year)) return false;
    if (source && !(row.sources || []).includes(source)) return false;
    if (publication && row.publication !== publication) return false;
    if (openOnly && !row.open) return false;
    return true;
  });
  const sorted = [...kept];
  if (sort === 'year') sorted.sort((a, b) => (b.year ?? -1) - (a.year ?? -1));
  else if (sort === 'citations') sorted.sort((a, b) => (b.citations ?? -1) - (a.citations ?? -1));
  else if (sort === 'audits') sorted.sort((a, b) => b.audit_count - a.audit_count || a.index - b.index);
  else if (sort === 'title') sorted.sort((a, b) => a.title.localeCompare(b.title));
  return sorted;
}

/** Facet values that actually occur, so a filter cannot offer an empty choice. Publication states
 * carry the label the backend gave them rather than one invented here. */
export function facets(rows) {
  const unique = values => [...new Set(values.filter(Boolean))].sort();
  const states = new Map();
  rows.forEach(row => { if (row.publication) states.set(row.publication, row.publication_label); });
  return {
    years: unique(rows.map(row => row.year)).reverse(),
    sources: unique(rows.flatMap(row => row.sources || [])),
    publications: [...states.entries()].map(([value, label]) => ({value, label}))
      .sort((a, b) => a.value.localeCompare(b.value)),
  };
}

/** Why a row cannot be taken as a drop-in baseline. Every item restates a field on the row; none of
 * them is a new judgement. Mirrors `resource_matrix.blockers`. */
export function blockers(row) {
  const found = [];
  if (row.status === 'not_checked') found.push('未核验（状态是候选，不是结论）');
  if (row.status === 'access_required') found.push('需申请访问');
  else if (row.status === 'access_failed') found.push('本次访问失败，可用性未知');
  else if (row.status === 'partially_available') found.push('仅部分可用');
  else if (row.status === 'unsupported') found.push('链接类型不受支持，未检查');
  if (row.access === 'requires_application') found.push('资源需申请');
  if ((row.limitations || []).some(text => String(text).includes('adapter'))) {
    found.push('疑似 adapter/LoRA 权重，需要对应基础模型');
  }
  if (row.version_match === 'unknown') found.push('论文版本与资源版本的对应关系未确认');
  if (row.attribution === 'unconfirmed') found.push('官方归属未确认（名称匹配或作者自述，未交叉验证）');
  return found;
}

const NO_CANDIDATE = {
  not_found_in_scope: '名称检索完成且未命中，检查范围内未找到',
  access_failed: '名称检索未完成，可用性未知',
  not_checked: '本次未检索（未开启、预算已尽或标题没有可检索的项目名）',
};

function auditRow(row, document) {
  const paper = document.paper || {};
  const coverage = Object.fromEntries(AUDIT_COMPONENTS.map(name => [name,
    COMPONENT_LABELS[((row.coverage || {})[name] || {}).state || 'unknown'] || '未知']));
  const listing = ((row.evidence || []).find(item => item.category === 'provider_metadata'
    && safeLink(item.source_url)) || {}).source_url || '';
  const componentSources = [...new Set(Object.values(row.coverage || {})
    .flatMap(finding => finding.sources || []))].map(safeLink).filter(Boolean);
  const sources = [...new Set([safeLink(listing), ...componentSources].filter(Boolean))].slice(0, 8);
  const flat = {
    paper_title: paper.title || row.paper_title || '',
    work_identifier: row.work_identifier || '',
    work_version: row.work_version || '',
    resource_url: safeLink(row.resource_url || ''),
    resource_type: row.resource_type || 'unknown',
    candidate_origin: row.candidate_origin || '',
    attribution: row.attribution || 'unconfirmed',
    author_declaration: row.author_declaration || 'undeclared',
    status: row.status || 'not_checked',
    status_label: AUDIT_LABELS[row.status || 'not_checked'] || row.status || 'not_checked',
    provider: row.provider || '',
    provider_status: row.provider_status || '',
    summary: row.summary || '',
    access: row.access || 'unknown',
    revision: row.revision || '',
    checked_at: row.checked_at || '',
    scope: row.scope || '',
    verification_depth: row.verification_depth || 'not_checked',
    version_match: row.version_match || 'unknown',
    licences: row.licences || {},
    coverage,
    sources,
    limitations: [...(row.limitations || [])],
    record_kind: 'observation',
    association_status: row.association_status || '',
    paper_evidence_id: row.paper_evidence_id || '',
    resource_evidence_id: row.resource_evidence_id || '',
    association_evidence_ids: [...(row.association_evidence_ids || [])],
    association_sources: [...(row.association_sources || [])],
    association_note: row.association_note || '',
  };
  flat.blockers = blockers(flat);
  return flat;
}

/** A paper with no candidate still gets a row, and its state is the *search's* outcome: dropping it
 * would make "we searched and found nothing" and "the search failed" both read as "no resources". */
function absentRow(document) {
  const paper = document.paper || {};
  const detail = document.artifact_search_detail || {};
  const state = document.artifact_outcome || 'not_checked';
  return {
    paper_title: paper.title || '',
    work_identifier: String(paper.doi || paper.arxiv_id || ''),
    work_version: '',
    resource_url: '',
    resource_type: 'unknown',
    candidate_origin: '',
    attribution: 'unconfirmed',
    author_declaration: 'undeclared',
    status: state,
    status_label: AUDIT_LABELS[state] || state,
    provider: '',
    provider_status: '',
    summary: '',
    access: state === 'not_found_in_scope' ? 'not_applicable' : 'unknown',
    revision: '',
    checked_at: '',
    scope: '',
    verification_depth: 'not_checked',
    version_match: 'unknown',
    licences: {},
    coverage: Object.fromEntries(AUDIT_COMPONENTS.map(name => [name, COMPONENT_LABELS.unknown])),
    sources: [],
    limitations: [...(detail.failures || [])],
    record_kind: 'observation',
    association_status: '',
    paper_evidence_id: '',
    resource_evidence_id: '',
    association_evidence_ids: [],
    association_sources: [],
    association_note: '',
    blockers: ['没有可审计的资源候选：' + (NO_CANDIDATE[state] || state)],
  };
}

/** Only a link a reader can open. Anything else becomes empty rather than appearing in a report as
 * though it were a source. Mirrors `resource_matrix.safe_link`. */
export function safeLink(url) {
  try {
    const parsed = new URL(String(url || ''));
    return ['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password
      ? parsed.href : '';
  } catch { return ''; }
}

export const MATRIX_COLUMNS = ['paper_title', 'work_identifier', 'work_version', 'resource_url',
  'resource_type', 'candidate_origin', 'attribution', 'author_declaration', 'status', 'status_label',
  'provider_status', 'access', 'verification_depth', 'version_match', 'revision', 'checked_at', 'scope', 'coverage', 'licences',
  'sources', 'blockers', 'limitations', 'record_kind', 'association_status', 'paper_evidence_id',
  'resource_evidence_id', 'association_evidence_ids', 'association_sources', 'association_note'];

export const COMPARABLE_PAPERS = 6;

export function matrix(result) {
  const documents = result.documents || [];
  const rows = documents.flatMap(document => {
    const audits = document.resource_audits || [];
    const found = audits.map(row => auditRow(row, document));
    return found.length ? found : [absentRow(document)];
  });
  const titles = new Set(rows.map(row => row.paper_title));
  return {
    papers: titles.size,
    rows,
    comparable: titles.size <= COMPARABLE_PAPERS,
    coverage: result.audit || {},
  };
}

/** The retrieval facts, with their denominators. A count without one reads as a result. */
export function summary(result) {
  const coverage = result.coverage || {};
  const failures = coverage.source_failures || [];
  const failedNames = new Set(failures.map(item => item.source));
  const audit = result.audit || {};
  const search = audit.name_search || {};
  const candidates = audit.candidates || {};
  return {
    schema_version: result.schema_version,
    scope: result.scope || '',
    note: result.note || '',
    queries: result.queries && result.queries.length ? result.queries
      : (result.query ? [result.query] : []),
    sources: (coverage.sources_queried || []).map(name => ({
      name,
      count: (coverage.source_counts || {})[name] ?? 0,
      failed: failedNames.has(name),
      error: (failures.find(item => item.source === name) || {}).error || '',
    })),
    failures,
    incomplete: Boolean(coverage.incomplete_results),
    documents: coverage.documents ?? (result.documents || []).length,
    duplicates_merged: coverage.duplicates_merged ?? null,
    dropped_out_of_range: coverage.dropped_out_of_range ?? null,
    // Present only in results written since the normalized structure started carrying the tool's own
    // coverage block. An older file has none of these, and the views say so rather than guessing.
    requested: coverage.requested || null,
    state: coverage.state || '',
    attempts: coverage.attempts || [],
    truncation: result.truncation || null,
    audit: audit.papers === undefined ? null : {
      papers: audit.papers,
      name_search: {
        denominator: search.denominator ?? 0,
        budget: search.budget ?? null,
        spent: search.spent ?? null,
        remaining: search.remaining ?? null,
        states: search.states || {},
        note: search.note || '',
      },
      candidates,
      audits: audit.audits || {},
      failures: audit.failures || [],
    },
    unrecognised: result.unrecognised || null,
  };
}

/** The command that produced this file, as far as the result actually says.
 *
 * Returns `{command, unknowns}`: the flags the file records, and the ones it does not. An older
 * result carries no `coverage.requested` at all, and a command that looks runnable while retrieving
 * something else is worse than one that admits it is incomplete — so the gaps are listed beside it
 * instead of being filled with a plausible default.
 */
export function commandFor(result) {
  const info = summary(result);
  const wanted = info.requested || {};
  const queries = (wanted.queries || []).length ? wanted.queries : info.queries;
  const sources = (wanted.sources || []).length ? wanted.sources : info.sources.map(item => item.name);
  const flags = [queries.length > 1 ? `--queries "${queries.join('|')}"`
    : `--query "${queries[0] || '…'}"`];
  flags.push(`--sources ${sources.length ? sources.join(',') : 'all'}`);
  const unknowns = [];
  if (wanted.start_year === undefined) {
    unknowns.push('年份窗口（--start-year / --end-year）');
    flags.push('--start-year <结果未记录>');
  } else {
    flags.push(`--start-year ${wanted.start_year}`);
    if (wanted.end_year) flags.push(`--end-year ${wanted.end_year}`);
  }
  if (wanted.limit === undefined) {
    unknowns.push('每源上限（--max-papers）');
    flags.push('--max-papers <结果未记录>');
  } else {
    flags.push(`--max-papers ${wanted.limit}`);
  }
  if (wanted.max_pages) flags.push(`--max-pages ${wanted.max_pages}`);
  const audit = info.audit;
  if (audit) {
    flags.push(`--find-artifacts ${audit.name_search.budget ?? 'N'}`);
    flags.push(`--verify ${audit.candidates.budget ?? 'N'}`);
  } else {
    unknowns.push('两个预算（--find-artifacts / --verify）');
  }
  flags.push('--json out.json', '--resource-matrix ./matrix');
  return {command: `re0 paper search ${flags.slice(0, 1)} \\\n  ${flags.slice(1).join(' \\\n  ')}`,
          unknowns};
}

const BIBTEX_ESCAPES = {'\\': '\\textbackslash{}', '{': '\\{', '}': '\\}', '%': '\\%', '&': '\\&',
  '#': '\\#', '_': '\\_', '$': '\\$', '~': '\\textasciitilde{}', '^': '\\textasciicircum{}'};

/** Mirrors `service.bibtex_export`, including the escape table, so a citation copied from this page
 * and one exported from the library cannot differ for the same paper. The key differs only because a
 * candidate has no library id yet: it is built from the identifier the record itself carries. */
export function bibtex(rows) {
  const tex = value => String(value ?? '').replace(/\n/g, ' ')
    .replace(/[\\{}%&#_$~^]/g, character => BIBTEX_ESCAPES[character]);
  return rows.map(row => {
    const key = (row.doi || row.arxiv_id || row.title).toLowerCase()
      .replace(/[^a-z0-9]+/g, '').slice(0, 24) || 'candidate';
    const fields = {
      title: row.title,
      author: (row.authors || []).join(' and '),
      year: String(row.year ?? ''),
      doi: row.doi,
      url: (row.links.find(item => item.label !== 'record') || {}).url || row.links[0]?.url || '',
      eprint: row.arxiv_id,
      note: row.publication === 'preprint' ? 'preprint only in the sources this search reached' : '',
    };
    const body = Object.entries(fields).filter(([, value]) => value)
      .map(([name, value]) => `  ${name} = {${tex(value)}}`).join(',\n');
    return `@misc{re0_${key},\n${body}\n}`;
  }).join('\n\n') + (rows.length ? '\n' : '');
}

/** A cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return is *executed* by spreadsheet
 * software, and every string here arrived from another service. Mirrors `resource_matrix.csv_cell`. */
export function csvCell(value) {
  let text = value === null || value === undefined ? '' : String(value);
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return text.replace(/[\r\n]/g, ' ');
}

function csvValue(value) {
  if (Array.isArray(value)) return csvCell(value.join('; '));
  if (value && typeof value === 'object') {
    return csvCell(Object.entries(value).map(([name, item]) => `${name}=${item}`).join('; '));
  }
  return csvCell(value);
}

export function matrixCsv(rows) {
  const header = MATRIX_COLUMNS.map(csvCell).join(',');
  const body = rows.map(row => MATRIX_COLUMNS.map(name => csvValue(row[name])).join(','));
  return [header, ...body].join('\r\n') + '\r\n';
}

/** The payload `POST /api/import/resource-audits` accepts, built the same way
 * `resource_matrix.approval_items` builds it: a paper with no audited candidate, or with no title to
 * attribute one to, is left out, because importing either would create a record no source supports. */
export function approvalItems(result) {
  return (result.documents || [])
    .filter(document => (document.resource_audits || []).length && (document.paper || {}).title)
    .map(document => ({paper: document.paper, audits: document.resource_audits}));
}
