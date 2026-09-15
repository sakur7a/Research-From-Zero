export const RUN_LABELS = {queued:'等待执行', running:'研究中', completed:'报告已生成', cancelled:'已取消', interrupted:'已中断', failed:'执行失败', budget_exhausted:'达到预算上限'};
export const TOOL_LABELS = {update_plan:'制定 / 修订计划', search_papers:'检索论文', resolve_paper:'读取论文元数据', search_repositories:'搜索代码仓库', search_hub:'搜索模型与数据集', inspect_resource:'核查资源线索', read_repository_file:'阅读仓库文本', search_release_discussions:'追查发布讨论', search_library:'查询已授权文献库', search_web:'搜索公开网页', finish_report:'整理研究报告'};
export const activeRun = run => Boolean(run && ['queued','running'].includes(run.status));
export const canResume = run => Boolean(run && ['failed','interrupted'].includes(run.status));
export function eventText(event) {
  const d = event.data || {};
  if (event.kind === 'tool_started') return `正在${TOOL_LABELS[d.tool] || d.tool}`;
  if (event.kind === 'tool_finished') return d.ok ? `${TOOL_LABELS[d.tool] || d.tool} · 收集 ${(d.evidence_ids || []).length} 条证据` : `${TOOL_LABELS[d.tool] || d.tool} · ${d.error || '未完成'}`;
  if (event.kind === 'plan') return '公开行动计划已更新';
  if (event.kind === 'approved_import') return d.created ? '经你确认，论文已加入文献库' : '文献库已有该论文，未覆盖内容';
  return d.message || event.kind;
}
