/** Pure view-model helpers, also exercised with Node's built-in test runner. */
export const READING = {inbox: '待读', reading: '精读中', read: '已读', baseline: 'Baseline 候选', archived: '已归档'};
export const KINDS = {code: '代码', checkpoint: '权重', dataset: '数据集', evaluation: '评测', environment: '环境', demo: '演示'};
export const ACCESS = {unchecked: '未核验', metadata_accessible: '元数据可读', gated: '需申请访问', indeterminate: '暂无法判断', access_failed: '访问失败', unsupported: '仅保存链接'};
export const OWNERSHIP = {unconfirmed: '归属待确认', official: '用户标记官方', third_party: '用户标记第三方'};
export const CLAIMS = {undeclared: '未记录声明', promised: '声明计划发布', released: '声明已发布'};
export const INDICATORS = {training: '训练候选', inference: '推理候选', evaluation: '评测候选', weights: '权重候选', data: '数据候选', environment: '环境文件'};
export const PAPER_KEYS = ['title','authors','year','venue','abstract','doi','arxiv_id','paper_url','topics','status','notes','version_label','zotero_item_key','zotero_library_id','zotero_library_type'];
export const e = (value = '') => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
export function link(url) { try { const u = new URL(url); return ['https:','http:'].includes(u.protocol) && !u.username && !u.password ? e(u.href) : '#'; } catch { return '#'; } }
export function paperInput(paper) { return Object.fromEntries(PAPER_KEYS.filter(k => paper[k] !== undefined).map(k => [k, paper[k]])); }
export function needsAttention(resource) { return !resource.latest || ['indeterminate','access_failed'].includes(resource.latest.status); }
export function filteredPapers(papers, {query = '', topic = '', status = '', attention = false, hideDemo = false, sort = 'updated'} = {}) {
  const q = query.trim().toLocaleLowerCase();
  return papers.filter(p => (!topic || p.topics.includes(topic)) && (!status || p.status === status) &&
    (!hideDemo || !p.is_demo) && (!attention || p.resources.some(needsAttention)) &&
    (!q || [p.title, p.authors.join(' '), p.abstract, p.doi, p.arxiv_id, p.topics.join(' '), p.notes].join(' ').toLocaleLowerCase().includes(q)))
    .sort((a,b) => sort === 'year' ? ((b.year || 0) - (a.year || 0) || a.title.localeCompare(b.title)) : sort === 'title' ? a.title.localeCompare(b.title) : b.updated_at.localeCompare(a.updated_at));
}
export function counts(papers) {
  const resources = papers.flatMap(p => p.resources);
  return {papers: papers.length, resources: resources.length, checked: resources.filter(r => r.latest).length,
    pending: resources.filter(needsAttention).length, baseline: papers.filter(p => p.status === 'baseline').length,
    topics: new Set(papers.flatMap(p => p.topics)).size};
}
export function resourceStatus(resource) { return resource.latest?.status || 'unchecked'; }
export function timeLabel(value) {
  if (!value) return '尚未检查';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return date.toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false});
}
