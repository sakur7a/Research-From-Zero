export const RUN_LABELS = {queued:'等待执行', running:'研究中', completed:'报告已生成', cancelled:'已取消', interrupted:'已中断', failed:'执行失败', budget_exhausted:'达到预算上限'};
export const TOOL_LABELS = {update_plan:'制定 / 修订计划', search_papers:'检索论文', resolve_paper:'读取论文元数据', search_repositories:'搜索代码仓库', search_hub:'搜索模型与数据集', inspect_resource:'核查资源线索', read_repository_file:'阅读仓库文本', read_evidence:'取回证据正文', search_release_discussions:'追查发布讨论', search_library:'查询已授权文献库', search_web:'搜索公开网页', finish_report:'整理研究报告'};
export const activeRun = run => Boolean(run && ['queued','running'].includes(run.status));
export const canResume = run => Boolean(run && ['failed','interrupted'].includes(run.status));
// Mirrors the server schema bounds. The server stays authoritative; this only keeps
// the settings panel and the composer summary from showing an impossible value.
export const BUDGET_LIMITS = {max_model_calls:[2,24], max_tool_calls:[1,40], attempt_seconds:[30,900]};
export const SHIPPED_DEFAULTS = Object.freeze({max_model_calls:12, max_tool_calls:20, attempt_seconds:360, use_library:false});
export function normalizeDefaults(raw) {
  const source = raw && typeof raw === 'object' ? raw : {};
  const pick = name => {
    const value = typeof source[name] === 'number' ? source[name] : Number.parseInt(source[name], 10);
    if (!Number.isFinite(value)) return SHIPPED_DEFAULTS[name];
    const [min, max] = BUDGET_LIMITS[name];
    return Math.min(max, Math.max(min, Math.trunc(value)));
  };
  return {max_model_calls:pick('max_model_calls'), max_tool_calls:pick('max_tool_calls'), attempt_seconds:pick('attempt_seconds'), use_library:Boolean(source.use_library)};
}
export function budgetSummary(raw) {
  const d = normalizeDefaults(raw);
  return `模型 ${d.max_model_calls} 次 · 工具 ${d.max_tool_calls} 次 · 单次 ${d.attempt_seconds} 秒 · 文献库${d.use_library ? '已授权' : '未授权'}`;
}
// The library flag now lives in settings, so the per-task consent must name the
// material that will actually leave this machine.
export function consentText(raw) {
  return normalizeDefaults(raw).use_library
    ? '我同意将任务、检索到的材料，以及本地文献库的书目与摘要发送至所配置的模型服务，并承担相应调用费用。'
    : '我同意将任务及检索到的材料发送至所配置的模型服务，并承担相应调用费用。';
}
// Candidate model IDs for the settings picker. The server already dedupes and
// sorts; this only keeps a malformed or hostile payload from reaching the DOM.
export const MODEL_OPTION_LIMIT = 400;
export function modelOptionIds(payload) {
  const list = payload && Array.isArray(payload.models) ? payload.models : [];
  const ids = list.map(x => (typeof x === 'string' ? x.trim() : '')).filter(Boolean);
  return [...new Set(ids)].slice(0, MODEL_OPTION_LIMIT);
}
export function eventText(event) {
  const d = event.data || {};
  if (event.kind === 'tool_started') return `正在${TOOL_LABELS[d.tool] || d.tool}`;
  if (event.kind === 'tool_finished') return d.ok ? `${TOOL_LABELS[d.tool] || d.tool} · 收集 ${(d.evidence_ids || []).length} 条证据` : `${TOOL_LABELS[d.tool] || d.tool} · ${d.error || '未完成'}`;
  if (event.kind === 'plan') return '公开行动计划已更新';
  if (event.kind === 'approved_import') return d.created ? '经你确认，论文已加入文献库' : '文献库已有该论文，未覆盖内容';
  return d.message || event.kind;
}

// A paper evidence card. The server caps nothing about how many authors, links or artifact
// candidates arrive, so the bounds belong here, where a Node test can hold them.
export const CARD_AUTHOR_LIMIT = 6;
export const CARD_ABSTRACT_CHARS = 300;
export const CARD_ARTIFACT_LIMIT = 4;
export const CARD_INSTITUTION_LIMIT = 3;
export const CARD_TITLE_FALLBACK = '未命名论文';
const cardText = value => (typeof value === 'string' ? value.trim() : '');
// A label short enough for a narrow column. Display only: the href still goes through link(),
// which refuses anything that is not http(s) without credentials.
export function shortUrl(url) {
  try {
    const parsed = new URL(url);
    const path = (parsed.host + parsed.pathname).replace(/\/$/, '');
    return path || String(url).slice(0, 80);
  } catch { return String(url ?? '').slice(0, 80); }
}
// `partial` and `failed` are not "no search happened": a search ran and did not finish. Labelling
// either as 未做开源检索 would hide a rate limit behind what reads as a deliberate choice, and the
// reader would move on instead of re-running.
const COVERAGE_NOTES = {
  searched: '',
  skipped: '标题里没有可检索的项目名，本次未做开源检索',
  partial: '名称检索只完成了一部分（有来源未应答），开源情况不完整，可重跑',
  failed: '名称检索未能完成，开源情况未查；这不代表没有开源',
  'not-run': '本次未做开源检索，开源情况未查'
};
export function artifactCoverage(evidence) {
  const search = evidence && evidence.artifact_search;
  return Object.hasOwn(COVERAGE_NOTES, search) ? COVERAGE_NOTES[search] : COVERAGE_NOTES['not-run'];
}
export function paperCard(evidence) {
  const source = evidence && typeof evidence === 'object' ? evidence : {};
  const paper = source.paper && typeof source.paper === 'object' ? source.paper : {};
  const publication = source.publication && typeof source.publication === 'object' ? source.publication : {};
  const authors = (Array.isArray(paper.authors) ? paper.authors : []).map(cardText).filter(Boolean);
  const institutions = (Array.isArray(source.institutions) ? source.institutions : []).map(cardText).filter(Boolean);
  const abstract = cardText(paper.abstract);
  const links = [];
  const arxiv = cardText(paper.arxiv_id);
  if (arxiv) links.push({label:'arXiv', href:`https://arxiv.org/abs/${arxiv}`});
  const doi = cardText(paper.doi);
  if (doi) links.push({label:'DOI', href:`https://doi.org/${doi}`});
  const record = cardText(paper.paper_url);
  if (record && !links.some(entry => entry.href === record)) links.push({label:'来源记录', href:record});
  const artifacts = (Array.isArray(source.artifact_candidates) ? source.artifact_candidates : [])
    .filter(entry => entry && cardText(entry.url)).slice(0, CARD_ARTIFACT_LIMIT)
    .map(entry => ({url:cardText(entry.url), origin:cardText(entry.origin)}));
  return {
    id: cardText(source.id),
    title: cardText(paper.title) || CARD_TITLE_FALLBACK,
    authors: authors.slice(0, CARD_AUTHOR_LIMIT),
    moreAuthors: Math.max(0, authors.length - CARD_AUTHOR_LIMIT),
    abstract: abstract.slice(0, CARD_ABSTRACT_CHARS),
    abstractTruncated: abstract.length > CARD_ABSTRACT_CHARS,
    year: Number.isInteger(paper.year) ? paper.year : null,
    state: cardText(publication.state) || 'unknown',
    stateLabel: cardText(publication.label) || cardText(publication.state) || 'unknown',
    publicationVenue: cardText(publication.venue),
    publicationSource: cardText(publication.source),
    alsoPreprint: Boolean(source.preprint_also),
    institutions: institutions.slice(0, CARD_INSTITUTION_LIMIT),
    moreInstitutions: Math.max(0, institutions.length - CARD_INSTITUTION_LIMIT),
    links, artifacts,
    artifactCoverage: artifactCoverage(source),
    locator: cardText(source.locator),
    toolLabel: TOOL_LABELS[source.tool] || cardText(source.tool)
  };
}
