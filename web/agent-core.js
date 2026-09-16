export const RUN_LABELS = {queued:'等待执行', running:'研究中', completed:'报告已生成', cancelled:'已取消', interrupted:'已中断', failed:'执行失败', budget_exhausted:'达到预算上限'};
export const TOOL_LABELS = {update_plan:'制定 / 修订计划', search_papers:'检索论文', resolve_paper:'读取论文元数据', search_repositories:'搜索代码仓库', search_hub:'搜索模型与数据集', inspect_resource:'核查资源线索', read_repository_file:'阅读仓库文本', search_release_discussions:'追查发布讨论', search_library:'查询已授权文献库', search_web:'搜索公开网页', finish_report:'整理研究报告'};
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
export function eventText(event) {
  const d = event.data || {};
  if (event.kind === 'tool_started') return `正在${TOOL_LABELS[d.tool] || d.tool}`;
  if (event.kind === 'tool_finished') return d.ok ? `${TOOL_LABELS[d.tool] || d.tool} · 收集 ${(d.evidence_ids || []).length} 条证据` : `${TOOL_LABELS[d.tool] || d.tool} · ${d.error || '未完成'}`;
  if (event.kind === 'plan') return '公开行动计划已更新';
  if (event.kind === 'approved_import') return d.created ? '经你确认，论文已加入文献库' : '文献库已有该论文，未覆盖内容';
  return d.message || event.kind;
}
