import {e, link, timeLabel} from './core.js';
import {initTheme} from './theme.js';
import {RUN_LABELS, TOOL_LABELS, SHIPPED_DEFAULTS, RESEARCH_SCOPE_LABELS, activeRun, budgetSummary, canFollowUp, canResume, canRetry, consentText, deltaSummary, eventText, followupPayload, followupProblem, idempotencyKeyFor, ledgerLine, modelOptionIds, normalizeDefaults, paperCard, reuseChoices, shortUrl, turnLabel} from './agent-core.js';

initTheme();
const workspace = document.querySelector('#workspace');
const settings = document.querySelector('#settings');
const side = document.querySelector('#app-side');
let identity = {kind: 'local'};
let config = {}, runs = [], current = null, events = [], workspaces = [], pendingWorkspaceBundle = null,
  resourceMatrix = null, pendingMatrixSelections = [], tab = 'trace', epoch = 0, timer, toastTimer,
  eventCursor = 0, evidenceCursor = 0, pollFailures = 0, lastPollError = '';
// The conversation the open run belongs to: its cumulative ledger and caps. Fetched with the run so
// the composer can show what a follow-up would be added to, rather than only what it may spend.
let conversation = null;
const examples = [
  ['文献发现', '找几篇支持约束条件的 Layout 生成论文，查找代码和权重，列出资源缺口，不要把未找到当作不存在。'],
  ['资源深查', '搜索图层分解与图层生成的论文，区分官方声明、代码、checkpoint 与数据集的实际线索，给出来源。'],
  ['Baseline 筛选', '检索图推荐系统的可复现实验工作，比较数据划分、评测脚本和训练代码线索，标记需要进一步确认的条件。']
];
async function requestJson(base, path, data, method = data === undefined ? 'GET' : 'POST') {
  const response = await fetch(base + path, {method, headers:{'Content-Type':'application/json','X-Re0-Client':'web'}, ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  let result;
  try { result = await response.json(); } catch { throw new Error('服务响应无效，请确认 Python 服务仍在运行。'); }
  if (!response.ok) {
    const error=new Error(typeof result.detail === 'string' ? result.detail : (result.detail || []).map(x => `${(x.loc||[]).join('.')}: ${x.msg}`).join('；') || '操作未完成');
    error.status=response.status;
    const retry=response.headers.get('retry-after');
    error.retryAfter=retry&&/^\d+(?:\.\d+)?$/.test(retry)?Number(retry):0;
    throw error;
  }
  return result;
}
const api = (path, data, method) => requestJson('/api/agent', path, data, method);
const appApi = (path, data, method) => requestJson('/api', path, data, method);
function notice(text) {
  clearTimeout(toastTimer);
  // A modal <dialog> and its blurred ::backdrop occupy the top layer, so a toast
  // left in <body> would paint underneath the scrim and look blurred. Keep the
  // single toast element inside the open dialog while one exists.
  const node = document.querySelector('#notice');
  const host = document.querySelector('dialog[open]') || document.body;
  if (node.parentElement !== host) host.appendChild(node);
  node.textContent = text; node.classList.add('show');
  toastTimer = setTimeout(() => node.classList.remove('show'), 7000);
}
function noticeHome() {
  const node = document.querySelector('#notice');
  if (node && node.parentElement !== document.body) document.body.appendChild(node);
}
function sidebar() {
  document.querySelector('#history').innerHTML = runs.length ? runs.map(r => `<button class="history-item ${r.id === current?.id ? 'is-current' : ''}" data-run="${e(r.id)}"><strong>${e(r.goal)}</strong><small><i class="dot ${e(r.status)}"></i>${e(RUN_LABELS[r.status] || r.status)} · ${e(timeLabel(r.created_at))}</small></button>`).join('') : '<p class="subtle">尚无研究任务。<br>从右侧提出第一个问题。</p>';
  document.querySelector('#model-status').innerHTML = `<i class="dot ${config.configured ? 'completed' : ''}"></i><span>${config.configured ? e(config.model) : '尚未接入模型'}</span><small>${config.configured ? '模型配置仅保留在进程内存' : '接入 API 或本地模型后开始'}</small>`;
  const guest = identity.kind === 'guest';
  document.querySelector('#guest-session-actions').hidden = !guest;
  document.querySelector('#identity-label').innerHTML = guest
    ? 'TEMPORARY GUEST <span>2 小时</span>' : 'LOCAL WORKSPACE <span>v0.2</span>';
}

function guestLanding(deployment) {
  epoch++; clearTimeout(timer); current = null; events = []; resourceMatrix = null; pendingMatrixSelections = []; side.hidden = true;
  document.body.classList.add('guest-door');
  document.querySelector('#breadcrumb').textContent = '开始研究';
  workspace.innerHTML = `<section class="guest-welcome">
    <div class="guest-hero"><div class="eyebrow">RESEARCH FROM ZERO · BYOK WEB DEMO</div>
      <h1>从一个问题开始。<br><span>证据一路可查。</span></h1>
      <p>用自己的模型 API Key，交给 Re0 检索文献、核对代码与数据线索、整理带来源的结果。你决定研究问题，也保留对结论的判断。</p>
    </div>
    <div class="guest-welcome-grid">
      <article class="guest-start-card"><div class="eyebrow">实时研究 · 需要自己的 Key</div>
        <h2>不需要安装或联系管理员开户</h2>
        <p>开始后会创建只属于这个浏览器的临时访客会话。模型 Key 最多保留 8 小时，只存在服务内存；每次任务开始前会明确提示发送范围与调用费用。</p>
        <button class="primary guest-start-button" data-action="begin-guest">创建临时会话并继续 ↗</button>
        <small>项目方没有提供免 Key 试用额度。没准备模型 Key 时，可以先浏览右侧的历史案例。</small>
      </article>
      <article class="guest-history-card"><div class="history-date">历史案例 · 2026-09-22</div>
        <h2>microsoft / LoRA</h2>
        <p>看一次真实历史资源核验：论文记录 → 摘要中的仓库候选 → 固定版本文件清单。它是只读回放，不是本次实时检索。</p>
        <a href="/static/skill.html" target="_blank" rel="noopener noreferrer">打开历史来源链 ↗</a>
      </article>
    </div>
    <div class="guest-retention" role="note"><strong>${deployment.storage_mode === 'ephemeral-demo' ? '临时演示存储' : '临时访客会话'}</strong>
      <span>${deployment.storage_mode === 'ephemeral-demo'
        ? '访客数据最多保留 2 小时；服务休眠、重启或重新部署可能更早清除数据库和工作区文件。请勿保存唯一副本或敏感材料。'
        : '会话最多 2 小时；可随时退出或删除本次数据。'}</span></div>
  </section>`;
  document.querySelector('#guest-session-actions').hidden = true;
}
function home(goal = '') {
  epoch++; clearTimeout(timer); current = null; events = []; resourceMatrix = null; pendingMatrixSelections = []; sidebar();
  const defaults = normalizeDefaults(config.task_defaults);
  const fullText = config.capabilities?.full_text;
  const fullTextCopy = fullText?.enabled
    ? `支持 ${fullText.sources.join(' / ')} 的有界 ${fullText.formats.join(' / ')} 文本读取。`
    : '当前部署没有开放全文读取。';
  document.querySelector('#breadcrumb').textContent = '新研究';
  workspace.innerHTML = `<section class="home"><div class="eyebrow">AN AGENT FOR YOUR NEXT DISCOVERY</div>
    <h1>从一个问题，<br>开始研究<span>。</span></h1>
    <p class="lead">让 agent 检索文献、追查资源、整理证据。<br>你决定研究目标，也保留对结论的判断。</p>
    ${!config.configured ? '<div class="setup-note"><span>开始前，需要连接你自己的模型。</span><button class="text-button" data-action="settings">配置模型 →</button></div>' : ''}
    <form id="task-form" class="composer"><label class="sr-only" for="goal">研究目标</label><textarea id="goal" name="goal" minlength="5" maxlength="6000" rows="5" placeholder="描述你的研究问题、筛选条件，以及希望得到的结果…" required>${e(goal)}</textarea>
      <label class="search-scope">初始检索范围<select name="research_scope"><option value="focused" ${defaults.research_scope==='focused'?'selected':''}>小范围起步 · 约 3 篇、少量来源、1 页</option><option value="expanded" ${defaults.research_scope==='expanded'?'selected':''}>较宽起步 · 多个相关来源、各 1 页</option></select><small>只设起步范围；agent 可按问题和证据缺口继续检索，报告会说明未查来源。</small></label>
      <div class="composer-footer"><span><i class="dot"></i> 自主检索 · 证据可追溯 · 入库需确认</span><div class="composer-actions"><button type="button" class="budget-link" data-action="settings">${e(budgetSummary(defaults))}</button><button class="primary" type="submit" ${!config.configured || config.busy ? 'disabled' : ''}>开始研究 ↗</button></div></div>
      <label class="check consent"><input type="checkbox" name="consent_to_send" required>${e(consentText(defaults))}</label>
    </form>
    <div class="example-heading">从这些研究任务开始 <span>示例提示，不是预置研究结果</span></div><div class="examples">${examples.map(([title,text],i) => `<button data-example="${i}"><small>0${i+1} / ${e(title)}</small><p>${e(text)}</p><span>使用这个问题 ↗</span></button>`).join('')}</div>
    <div class="scope"><strong>当前能力边界</strong><p>检索论文元数据与摘要，阅读仓库文本，核查资源线索。${fullTextCopy}不会执行陌生代码、读取任意网页或认定论文已复现。${config.web_search_enabled ? '公开网页搜索已配置。' : '未配置 Tavily 时，只使用论文与资源平台搜索，不冒充全网检索。'}</p></div>
  </section>`;
}
function requestStats(run) {
  const usage=run.usage||{};
  const unreported=usage.unreported_calls?`（${usage.unreported_calls} 次用量未回报）`:'';
  return `起步预设 ${RESEARCH_SCOPE_LABELS[run.params?.research_scope]||'小范围起步'} · 模型 ${run.model_calls||0}/${run.params?.max_model_calls||'—'} · 工具 ${run.tool_calls||0}/${run.params?.max_tool_calls||'—'} · 上游请求 ${run.upstream_requests||0}/${run.params?.max_upstream_requests||'—'} · 请求体 ${Number(run.model_request_chars||0).toLocaleString()} 字符 / ${Math.round((run.model_request_bytes||0)/1024)} KB（最大 ${Math.round((run.largest_model_request_bytes||0)/1024)} KB） · 提供商 token ${usage.total_tokens||0}${unreported}`;
}
function runView() {
  if (!current) return;
  const opened=[...document.querySelectorAll('#result-panel details[open]')].map(x=>x.closest('article')?.id);
  document.querySelector('#breadcrumb').textContent = '研究任务';
  workspace.innerHTML = `<section class="run-page"><div class="run-heading"><div><div class="eyebrow">RESEARCH TASK <span>${e(timeLabel(current.created_at))}</span></div><h1>${e(current.goal)}</h1></div><span class="status ${e(current.status)}">${e(RUN_LABELS[current.status])}</span></div>
    <div class="run-controls"><span>${e(turnLabel(current))} · ${e(current.config.model)} · ${e(requestStats(current))}</span><div>${activeRun(current) ? '<button class="danger" data-action="cancel">停止研究</button>' : ''}${canResume(current) ? '<button class="primary" data-action="resume">从检查点继续</button>' : ''}<a class="button" href="/api/agent/runs/${e(current.id)}/export" download>导出任务</a>${current.conversation_id ? `<a class="button" href="/api/agent/conversations/${e(current.conversation_id)}/export" download>导出整个会话</a>` : ''}</div></div>
    ${current.error ? `<div class="error-panel">${e(current.error)}</div>` : ''}
    ${canFollowUp(current) ? followupHtml() : ''}
    <div class="run-layout"><aside class="plan-panel"><div class="section-label">公开行动计划</div><div id="public-plan" data-steps="${e(JSON.stringify(current.plan||[]))}">${current.plan?.length ? `<ol>${current.plan.map(s=>`<li>${e(s)}</li>`).join('')}</ol>` : '<p class="subtle">agent 将在执行时制定计划。</p>'}</div><div class="plan-bottom">${current.evidence.length}<span>条来源证据</span></div><p class="subtle">计划由模型生成。执行记录来自实际工具调用，不展示模型私有思维。</p></aside>
      <section class="results"><div class="tabs" role="tablist">${[['trace','执行记录'],['report','研究报告'],...(resourceMatrix?[['matrix',`资源矩阵 (${resourceMatrix.rows.filter(row=>row.resource_url).length})`]]:[]),['evidence',`来源证据 (${current.evidence.length})`]].map(([key,title])=>`<button role="tab" aria-selected="${tab===key}" data-tab="${key}" class="${tab===key?'active':''}">${title}</button>`).join('')}</div><div id="result-panel" role="tabpanel"></div></section></div>
    <p class="footnote">模型报告是待复核分析；引用编号存在，不代表引用内容已经支持全部结论。Token 数以提供商实际返回为准，不估算金额。</p></section>`;
  panel(); sidebar();
  for(const id of opened){const details=document.getElementById(id)?.querySelector('details');if(details)details.open=true;}
}

function updateLiveProgress(run, newEvents, newEvidence) {
  const atBottom=window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-120;
  current=run; events.push(...newEvents);
  const status=document.querySelector('.run-heading .status');
  if(status){status.className='status '+e(current.status);status.textContent=RUN_LABELS[current.status]||current.status;}
  const summary=document.querySelector('.run-page > .run-controls span');
  if(summary){
    summary.textContent=turnLabel(current)+' · '+(current.config?.model||'')+' · '+requestStats(current);
  }
  const plan=document.querySelector('#public-plan');
  const planSteps=JSON.stringify(current.plan||[]);
  if(plan&&plan.dataset.steps!==planSteps){
    plan.dataset.steps=planSteps;
    plan.innerHTML=current.plan?.length
      ? '<ol>'+current.plan.map(step=>'<li>'+e(step)+'</li>').join('')+'</ol>'
      : '<p class="subtle">agent 将在执行时制定计划。</p>';
  }
  const evidenceCount=document.querySelector('.plan-bottom');
  if(evidenceCount)evidenceCount.innerHTML=(current.evidence_count??current.evidence.length)+'<span>条来源证据</span>';
  const evidenceTab=document.querySelector('[data-tab="evidence"]');
  if(evidenceTab)evidenceTab.textContent='来源证据 ('+(current.evidence_count??current.evidence.length)+')';
  if(tab==='trace'){
    const trace=document.querySelector('.trace');
    if(trace)for(const item of newEvents){
      const bad=item.kind==='tool_finished'&&item.data?.ok===false?' bad':'';
      const html='<article class="trace-row"><span class="trace-marker'+bad+'"></span><div><small>'
        +e(timeLabel(item.at))+' · '+e(item.kind)+'</small><p>'+e(eventText(item))+'</p></div></article>';
      const working=trace.querySelector('.working');
      if(working)working.insertAdjacentHTML('beforebegin',html);else trace.insertAdjacentHTML('beforeend',html);
    }
  } else if(tab==='evidence'){
    const list=document.querySelector('.evidence-list');
    if(list)for(const item of newEvidence)list.insertAdjacentHTML('beforeend',evidenceCard(item));
  }
  if(atBottom)window.scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'});
}

function plainCardHtml(ev) {
  return `<article class="evidence-card" id="${e(ev.id)}"><div class="evidence-meta"><code>${e(ev.id)}</code><span>${e(ev.kind)}</span></div><h3>${e(ev.paper?.title || TOOL_LABELS[ev.tool] || '来源材料')}</h3><p class="locator">${e(ev.locator)} · ${e(timeLabel(ev.retrieved_at))}</p><details><summary>查看来源摘录</summary><pre>${e(ev.content)}</pre></details><footer><a href="${link(ev.source_url)}" target="_blank" rel="noopener noreferrer">打开来源 ↗</a>${ev.kind==='paper' && ev.paper ? `<button class="button" data-import="${e(ev.id)}">确认加入文献库</button>` : ''}</footer></article>`;
}

// A paper card: bibliographic text on the left, source and open-source findings on the right.
// There is no thumbnail: Re0 holds no page image and has no PDF pipeline, and inventing one
// would misrepresent what was actually retrieved.
function paperCardHtml(ev) {
  const c = paperCard(ev);
  return `<article class="evidence-card paper-card" id="${e(c.id)}">
    <div class="paper-body">
      <h3 class="paper-title">${e(c.title)}</h3>
      ${c.authors.length ? `<p class="paper-authors">${c.authors.map(a=>`<span class="author-chip">${e(a)}</span>`).join('')}${c.moreAuthors?`<span class="author-more">等 ${c.moreAuthors} 位</span>`:''}</p>` : ''}
      ${c.abstract ? `<p class="paper-abstract">${e(c.abstract)}${c.abstractTruncated?'…':''}</p>` : '<p class="paper-abstract quiet">该来源没有提供摘要。</p>'}
      <div class="paper-chips">
        <span class="paper-state" data-state="${e(c.state)}">${e(c.stateLabel)}</span>
        ${c.publicationVenue?`<span class="paper-chip">${e(c.publicationVenue)}</span>`:''}
        ${c.alsoPreprint?'<span class="paper-chip quiet">另有预印本版本</span>':''}
        ${c.year?`<span class="paper-chip quiet">${e(c.year)}</span>`:''}
        ${c.publicationSource?`<span class="paper-chip quiet">据 ${e(c.publicationSource)}</span>`:''}
      </div>
      ${c.institutions.length?`<p class="paper-institutions">机构：${c.institutions.map(e).join(' · ')}${c.moreInstitutions?` 等 ${c.moreInstitutions} 个`:''}</p>`:''}
      <details><summary>查看来源摘录</summary><pre>${e(ev.content)}</pre></details>
      <footer class="paper-footer"><span class="locator">${e(c.id)} · ${e(c.toolLabel)} · ${e(c.locator)} · ${e(timeLabel(ev.retrieved_at))}</span><button class="button" data-import="${e(c.id)}">确认加入文献库</button></footer>
    </div>
    <aside class="paper-aside">
      <div class="section-label">来源</div>
      <div class="paper-links">${c.links.length?c.links.map(l=>`<a href="${link(l.href)}" target="_blank" rel="noopener noreferrer">${e(l.label)} ↗</a>`).join(''):'<span class="quiet">没有可打开的书目链接</span>'}</div>
      <div class="section-label">开源</div>
      ${c.artifacts.length?`<ul class="artifact-list">${c.artifacts.map(a=>`<li><a href="${link(a.url)}" target="_blank" rel="noopener noreferrer">${e(shortUrl(a.url))}</a><small>${e(a.origin)}</small></li>`).join('')}</ul>`:'<p class="quiet">没有找到开源候选。</p>'}
      ${c.artifactCoverage?`<p class="quiet">${e(c.artifactCoverage)}</p>`:''}
      <p class="quiet">名称匹配不等于官方实现，打开后可继续核对。</p>
    </aside></article>`;
}

function evidenceCard(ev) { return ev.kind === 'paper' && ev.paper ? paperCardHtml(ev) : plainCardHtml(ev); }

function resourceMatrixHtml(payload) {
  const rows = payload?.rows || [];
  const linked = rows.filter(row => row.resource_url);
  const cards = rows.map(row => {
    const evidence = (row.association_evidence || []).map(source =>
      '<div class="matrix-source"><button class="text-button" data-evidence="' + e(source.evidence_id)
      + '">查看来源证据</button>' + (source.source_url
        ? '<a href="' + link(source.source_url) + '" target="_blank" rel="noopener noreferrer">打开来源 ↗</a>' : '')
      + '<small>' + e(source.locator) + ' · ' + e(timeLabel(source.retrieved_at)) + '</small></div>').join('');
    const coverage = Object.entries(row.coverage || {}).map(([name,state]) =>
      '<span class="matrix-chip">' + e(name) + ' · ' + e(state) + '</span>').join('');
    const sources = (row.sources || []).map(url =>
      '<a href="' + link(url) + '" target="_blank" rel="noopener noreferrer">打开核验来源 ↗</a>').join('');
    const blockers = (row.blockers || []).map(item => '<li>' + e(item) + '</li>').join('');
    const canSave = row.resource_url && row.resource_type !== 'unknown'
      && row.paper_evidence_id && row.resource_evidence_id;
    return '<article class="matrix-card"><header><label class="matrix-select">'
      + (canSave ? '<input type="checkbox" name="matrix-selection" data-paper-evidence="'
        + e(row.paper_evidence_id) + '" data-resource-evidence="' + e(row.resource_evidence_id)
        + '"> 选择保存' : '<span class="quiet">未形成可保存的论文—资源关联</span>')
      + '</label><span class="matrix-status">' + e(row.association_status || '未关联') + '</span></header>'
      + '<h3>' + e(row.paper_title || '（论文来源未知）') + '</h3><p class="matrix-ident">'
      + e(row.work_identifier || '') + (row.work_version ? ' · ' + e(row.work_version) : '') + '</p>'
      + (row.resource_url ? '<p class="matrix-resource"><a href="' + link(row.resource_url)
        + '" target="_blank" rel="noopener noreferrer">' + e(row.resource_url) + ' ↗</a><span>'
        + e(row.resource_type) + ' · ' + e(row.candidate_origin || '资源核验证据') + '</span></p>'
        : '<p class="matrix-resource quiet">本任务没有已关联的资源候选。</p>')
      + '<div class="matrix-facts"><span>' + e(row.status_label || row.status || '未检查')
      + '</span><span>访问 ' + e(row.access || 'unknown') + '</span><span>核验 '
      + e(row.verification_depth || 'not_checked') + '</span><span>提供商 '
      + e(row.provider_status || '未报告') + '</span><span>归属 '
      + e(row.attribution || 'unconfirmed') + '</span><span>作者声明 '
      + e(row.author_declaration || 'undeclared') + '</span><span>版本对应 '
      + e(row.version_match || 'unknown') + '</span>'
      + (row.checked_at ? '<span>检查时间 ' + e(row.checked_at) + '</span>' : '') + '</div>'
      + '<details class="matrix-details"><summary>查看覆盖、限制和来源</summary><p><b>检查范围</b> '
      + e(row.scope || '结果未记录') + '</p><div class="matrix-coverage">'
      + coverage + '</div><ul>' + blockers + '</ul><p>' + (row.limitations || []).map(e).join(' · ')
      + '</p><div class="matrix-links">' + sources + '</div></details>'
      + (evidence ? '<details class="matrix-details"><summary>查看候选关联依据</summary><p>'
        + e(row.association_note || '') + '</p>' + evidence + '</details>' : '') + '</article>';
  }).join('');
  const unlinked = (payload?.unlinked_checks || []).map(item =>
    '<li><a href="' + link(item.resource_url) + '" target="_blank" rel="noopener noreferrer">'
    + e(item.resource_url) + '</a> · ' + e(item.status) + ' · ' + e(item.checked_at)
    + ' — ' + e(item.note) + '</li>').join('');
  const summary = payload?.coverage?.agent_run || {};
  return '<section class="resource-matrix"><div class="matrix-toolbar"><div><strong>'
    + e(summary.paper_evidence || 0) + ' 篇论文 · ' + e(summary.proposed_links || 0)
    + ' 条候选关联</strong><p>' + e(payload?.association_note || payload?.note || '') + '</p></div>'
    + '<div class="matrix-downloads"><a class="button" href="/api/agent/runs/' + e(current.id)
    + '/matrix?format=json" download>JSON</a><a class="button" href="/api/agent/runs/' + e(current.id)
    + '/matrix?format=markdown" download>Markdown</a><a class="button" href="/api/agent/runs/'
    + e(current.id) + '/matrix?format=csv" download>CSV</a></div></div>'
    + (linked.length ? '<div class="matrix-batch"><button class="button" data-action="matrix-preview">'
      + '预览所选项</button><div id="matrix-import-preview" aria-live="polite"></div>'
      + '<button class="primary" data-action="matrix-confirm" hidden>确认保存到我的文献库</button></div>'
      : '<div class="empty-result"><h2>本次没有形成论文—资源候选关联</h2><p>'
        + '论文、资源核验结果与未完成的检查仍可在“来源证据”和“执行记录”中查看；'
        + '没有证据支持的关联不会自动补上。</p></div>')
    + '<div class="matrix-cards">' + cards + '</div>'
    + (unlinked ? '<details class="matrix-details"><summary>另有 '
      + e(payload.unlinked_checks.length) + ' 条未关联的资源核验</summary><ul>' + unlinked + '</ul></details>' : '')
    + '</section>';
}

// A continuing turn is a new version, so the difference from the previous one is shown next to the
// report rather than folded into it: the earlier report stays readable and exportable on its own.
function deltaHtml(delta) {
  const summary = deltaSummary(delta);
  if (!summary) return '';
  return `<section class="report-delta"><h3>与第 ${summary.againstTurn} 轮的差异</h3>
    <p>新增 ${summary.added} · 改变 ${summary.changed} · 本轮未再提 ${summary.dropped} · 仍不确定 ${summary.stillUncertain} · 由不确定转为有结论 ${summary.resolved}</p>
    <p>结论状态：${e(summary.outcomeFrom || '（无）')} → ${e(summary.outcomeTo)}</p>
    <p class="subtle">${e(summary.note)}</p></section>`;
}
// The composer only collects an authorization. Whether the reuse is in scope, whether the ledger has
// room and whether the model destination changed are all decided by the shared service, which is the
// same code the console and the API run — so nothing here pre-judges the answer.
function followupHtml() {
  const {choices, hidden} = reuseChoices(current.evidence);
  const defaults = normalizeDefaults(config.task_defaults);
  const availableWorkspaces=workspaces.filter(item=>item.available&&item.sources?.length);
  const workspaceOptions=availableWorkspaces.map(item=>`<option value="${e(item.workspace_id)}">${e(item.workspace_id)} · ${item.total_sources} 条</option>`).join('');
  const workspaceSources=availableWorkspaces.flatMap(item=>item.sources.map(source=>`<label class="reuse workspace-source"><input type="checkbox" name="workspace_reuse" value="${e(source.source_id)}" data-workspace-id="${e(item.workspace_id)}" disabled> <span>${e(source.title||source.locator||source.source_id)}${source.imported_by_user?'<small>用户导入 · 来源声明未经认证</small>':''}</span></label>`)).join('');
  return `<details class="followup"><summary>在这一轮上追问 / 补充约束（第 ${(current.turn || 1) + 1} 轮）</summary>
    <form id="followup-form">
      <label>本轮新增或变更的条件
        <textarea name="goal" rows="3" maxlength="6000" required
          placeholder="例：只保留有训练代码的两篇，并补查它们的数据划分"></textarea></label>
      <label class="search-scope">本轮初始检索范围<select name="research_scope"><option value="focused" ${defaults.research_scope==='focused'?'selected':''}>小范围起步</option><option value="expanded" ${defaults.research_scope==='expanded'?'selected':''}>较宽起步</option></select><small>只控制本轮起步；可按证据缺口继续检索。</small></label>
      <fieldset><legend>复用已有证据（勾选的不会重新抓取）</legend>
        ${choices.map(ev => `<label class="reuse"><input type="checkbox" name="reuse" value="${e(ev.id)}"> ${e((ev.paper && ev.paper.title) || ev.locator || ev.kind || ev.id)}</label>`).join('') || '<p class="subtle">这一轮没有可复用的证据。</p>'}
        ${hidden ? `<p class="subtle">另有 ${hidden} 条未在这里列出；用 <code>re0 session scope</code> 或 API 指名。</p>` : ''}
      </fieldset>
      <fieldset class="workspace-reuse"><legend>导入的来源工作区</legend>
        <label>选择工作区<select name="workspace_id" id="workspace-choice"><option value="">不复用外部来源</option>${workspaceOptions}</select></label>
        ${workspaceSources?`<div class="workspace-source-list">${workspaceSources}</div>`:'<p class="subtle">尚无已导入来源；导入 bundle 后可在这里选择。</p>'}
        <div class="workspace-bundle-import"><label>导入来源 bundle（JSON）<input type="file" id="workspace-bundle-file" accept="application/json,.json"></label><div class="workspace-bundle-actions"><button type="button" class="button" data-action="workspace-preview">预览</button><button type="button" class="button" data-action="workspace-export" disabled>导出所选工作区</button><button type="button" class="button primary" data-action="workspace-import-confirm" hidden>确认导入</button></div><p class="subtle" id="workspace-import-status">预览不会写入；导入只保存来源快照，不会批准论文入库。Bundle 中的工具来源声明不会被加密认证。</p></div>
      </fieldset>
      <label class="consent"><input type="checkbox" name="use_library"> 本轮授权发送本地文献库元数据（不从上一轮继承）</label>
      <label class="consent"><input type="checkbox" name="trust_destination"> 本轮模型服务与上一轮不同，我确认把历史材料发往它</label>
      <label class="consent"><input type="checkbox" name="authorize" required> 我确认这一轮会产生新的模型调用费用；会话累计额度不会因新建一轮而重置</label>
      <p class="subtle ledger-note">${e(ledgerLine(conversation))} · 本轮预算 模型 ${defaults.max_model_calls} / 工具 ${defaults.max_tool_calls} / 上游请求 ${defaults.max_upstream_requests}</p>
      <div class="run-controls"><button class="primary" type="submit">发起追问</button>${canRetry(current) ? '<button class="button" type="button" data-action="retry">按原目标重试一轮</button>' : ''}</div>
    </form></details>`;
}

function panel() {
  const node = document.querySelector('#result-panel');
  if (!node || !current) return;
  if (tab === 'trace') {
    node.innerHTML = `<div class="trace">${events.length ? events.map(ev => `<article class="trace-row"><span class="trace-marker ${ev.kind==='tool_finished' && !ev.data.ok?'bad':''}"></span><div><small>${e(timeLabel(ev.at))} · ${e(ev.kind)}</small><p>${e(eventText(ev))}</p></div></article>`).join('') : '<p class="subtle">正在读取执行记录…</p>'}${activeRun(current)?'<div class="working"><span></span>正在执行；可离开此页面，任务由本地服务继续处理。</div>':''}</div>`;
  } else if (tab === 'matrix') {
    node.innerHTML = resourceMatrixHtml(resourceMatrix);
  } else if (tab === 'report') {
    const r=current.report;
    node.innerHTML = r ? `<article class="report"><div class="eyebrow">${r.outcome==='insufficient_evidence'?'EVIDENCE IS INCOMPLETE':'RESEARCH FINDINGS'} · 模型生成，待复核</div><h2>${e(r.title)}</h2><p class="report-summary">${e(r.summary)}</p><h3>发现与依据</h3>${r.findings.map((f,i)=>`<section class="finding"><small>${i+1} / ${{observed:'观察',inference:'推断',uncertain:'不确定'}[f.assessment]}</small><p>${e(f.claim)}</p><div class="citations">${f.evidence_ids.map(id=>`<button data-evidence="${e(id)}">${e(id)}</button>`).join('')}</div></section>`).join('') || '<p class="subtle">没有形成有充分依据的发现。</p>'}<h3>检查范围与剩余缺口</h3><ul>${r.limitations.map(x=>`<li>${e(x)}</li>`).join('')}</ul><div class="usage">提供商报告的 Token：${e(current.usage?.total_tokens || 0)}${current.usage?.unreported_calls ? `；${e(current.usage.unreported_calls)} 次调用未报告用量` : ''}</div>${deltaHtml(current.report_delta)}</article>` : '<div class="empty-result"><h2>报告还未生成</h2><p>报告只有在 agent 提交结构化结果后出现。任务失败或达到预算，不会自动填充虚构结论。</p><button class="button" data-tab="evidence">查看已收集的证据</button></div>';
  } else {
    node.innerHTML = `<div class="evidence-list">${current.evidence.length ? current.evidence.map(evidenceCard).join('') : '<div class="empty-result"><h2>尚无来源证据</h2><p>只有工具实际取得的材料才会出现在这里。</p></div>'}</div>`;
  }
}
async function refreshHistory() {
  [runs, config] = await Promise.all([api('/runs'),api('/config')]); sidebar();
}
async function selectRun(id) {
  const token = ++epoch; clearTimeout(timer); tab='evidence'; events=[]; eventCursor=0; evidenceCursor=0;
  pollFailures=0;lastPollError='';resourceMatrix=null;pendingMatrixSelections=[];
  try {
    const [r, ev] = await Promise.all([api('/runs/'+id),api('/runs/'+id+'/events')]);
    if (token!==epoch) return;
    current=r; events=ev;eventCursor=ev.at(-1)?.id||0;evidenceCursor=(r.evidence||[]).length;
    if(!activeRun(r)&&r.report){
      try {resourceMatrix=await api('/runs/'+id+'/matrix');}
      catch(err){notice(`资源矩阵未能生成：${err.message}`);}
      tab=resourceMatrix?.coverage?.agent_run?.proposed_links?'matrix':'report';
    }
    conversation = r.conversation_id ? await api('/conversations/'+r.conversation_id) : null;
    try { workspaces=(await appApi('/workspaces')).workspaces||[]; }
    catch(err) { workspaces=[]; notice(`未能读取已导入工作区：${err.message}`); }
    if (token!==epoch) return;
    runView(); poll(token);
  } catch(err){notice(err.message);}
}
function poll(token) {
  if (token!==epoch || !current || (!activeRun(current)&&!current.progress_draining)) return;
  const delay=document.hidden?8000:2500;
  timer=setTimeout(async()=>{
    const id=current?.id;
    if(token!==epoch || !id) return;
    try {
      const response=await api('/runs/'+id+'/progress?after='+eventCursor+'&evidence_after='+evidenceCursor);
      if(token!==epoch) return;
      pollFailures=0;lastPollError='';
      const previous=current;
      const next={...response.run,config:{model:response.run.model},
        evidence:[...(previous.evidence||[]),...(response.evidence||[])]};
      eventCursor=response.next_event_id||eventCursor;
      evidenceCursor=response.evidence_cursor??evidenceCursor;
      const drain=Boolean(response.has_more_events||response.has_more_evidence);
      if(activeRun(next)||drain){
        next.progress_draining=!activeRun(next);
        updateLiveProgress(next,response.events||[],response.evidence||[]);
        poll(token);
        return;
      }
      current=next;
      updateLiveProgress(next,response.events||[],response.evidence||[]);
      if(next.report){
        try {resourceMatrix=await api('/runs/'+id+'/matrix');}
        catch(err){notice(`资源矩阵未能生成：${err.message}`);}
      }
      if(tab==='evidence'&&next.report)
        tab=resourceMatrix?.coverage?.agent_run?.proposed_links?'matrix':'report';
      const scrollY=window.scrollY;
      runView();
      window.scrollTo(0,scrollY);
      await refreshHistory();
    } catch(err) {
      if(token!==epoch)return;
      if(err.status===401){
        clearTimeout(timer);
        notice('会话已失效；任务状态已保留。请重新登录后从历史任务继续查看。');
        return;
      }
      if(err.status===429){
        const retryMs=Math.max(1000,Math.min(86400000,(err.retryAfter||5)*1000));
        const message='进度查询达到请求上限，已按服务器要求等待后重试。';
        if(lastPollError!==message){lastPollError=message;notice(message);}
        timer=setTimeout(()=>poll(token),retryMs);
        return;
      }
      pollFailures+=1;
      const message='暂时无法读取任务进度；保留当前页面，网络恢复后会继续查询。';
      if(lastPollError!==message){lastPollError=message;notice(message);}
      const backoff=Math.min(30000,delay*Math.pow(2,Math.min(4,pollFailures-1)));
      timer=setTimeout(()=>poll(token),backoff);
    }
  },delay);
}
function openSettings() {
  // Settings re-renders the dialog body, which would take the toast with it.
  noticeHome();
  const defaults = normalizeDefaults(config.task_defaults);
  const credentialExpires = config.credential_expires_at
    ? new Date(config.credential_expires_at).toLocaleString() : '';
  const fullText = config.capabilities?.full_text;
  const fullTextCopy = fullText?.enabled
    ? `全文：${(fullText.sources || []).join('、')} 的有界 ${(fullText.formats || []).join('/')} 文本读取`
    : '全文读取：当前不可用';
  const presets = Array.isArray(config.endpoint_presets) ? config.endpoint_presets : [];
  const presetOptions = presets.map(p => `<option value="${e(p.base_url)}" ${p.base_url === config.base_url ? 'selected' : ''}>${e(p.label)}</option>`).join('');
  settings.innerHTML=`<div class="dialog-header"><div><div class="eyebrow">MODEL CONNECTION</div><h2 id="settings-title">连接你自己的模型</h2></div><button class="close" data-action="close-settings" aria-label="关闭设置">×</button></div><p class="subtle">连接测试会向所选服务发送一次工具调用请求，可能产生费用，但不发送你的研究问题或文献材料。成功后 Key 只保存在服务内存，最多 8 小时；退出、会话撤销或服务重启后清除。</p>
    ${config.configured ? `<p class="field-note">当前配置最晚有效至 ${e(credentialExpires || '未知时间')}。重新使用时需要再次配置 Key。</p>` : ''}
    <form id="model-form" autocomplete="off"><label>服务商<select id="preset"><option value="">选择服务商会自动填入下面的地址（不代表已实测模型兼容性）</option>${presetOptions}<option value="__custom">其他 / 自定义地址</option></select></label>
    <label>API Base URL<input name="base_url" id="base-url" type="url" value="${e(config.base_url || '')}" placeholder="https://api.example.com/v1" required></label><p class="field-note">只接受预设可信域名和显式端口的回环地址。自定义域名需设置 RE0_LLM_ALLOWED_HOSTS。</p>
    <label>API Key<input name="api_key" type="password" autocomplete="new-password" placeholder="${config.has_api_key?'已配置；重新保存时需再次输入，不会回填旧密钥':'本地无认证服务可留空'}" maxlength="2048"></label>
    <label>Model ID<input name="model" list="model-options" value="${e(config.model || '')}" placeholder="填写，或从下面拉取后选择" required maxlength="150"></label><datalist id="model-options"></datalist>
    <div class="connection-test"><button type="button" class="button" data-action="fetch-models">获取模型列表（可跳过）</button><span>只查询服务返回的模型 ID；如果接口不支持，直接手动填写即可。列表不代表支持工具调用。</span></div>
    <details class="advanced-settings"><summary>高级模型参数</summary><div class="settings-grid"><label>输出预算参数<select name="token_parameter"><option value="max_tokens" ${config.token_parameter==='max_tokens'?'selected':''}>max_tokens</option><option value="max_completion_tokens" ${config.token_parameter==='max_completion_tokens'?'selected':''}>max_completion_tokens</option></select></label><label>单次输出 Token 上限<input name="max_output_tokens" type="number" value="${config.max_output_tokens || 3000}" min="256" max="8192" required></label></div></details>
    <label class="check"><input type="checkbox" name="trust_endpoint" required>我信任这个模型服务，并授权发送一次不含研究材料的连接测试。</label><div class="dialog-actions"><button type="button" class="quiet" data-action="clear-model">清除内存配置</button><button type="submit" class="primary">测试连接并保存</button></div></form>
    <details class="defaults-block advanced-settings"><summary>高级任务预算与数据范围</summary><p class="field-note">仅影响之后新建的任务；每次任务仍会明确提示材料范围与调用费用。</p>
    <form id="defaults-form"><div class="budget-fields"><label>模型调用上限<input type="number" name="max_model_calls" min="2" max="24" value="${defaults.max_model_calls}" required></label><label>工具调用上限<input type="number" name="max_tool_calls" min="1" max="40" value="${defaults.max_tool_calls}" required></label><label>任务总上游请求<input type="number" name="max_upstream_requests" min="2" max="300" value="${defaults.max_upstream_requests}" required></label><label>单次执行窗口（秒）<input type="number" name="attempt_seconds" min="30" max="900" value="${defaults.attempt_seconds}" required></label></div>
    <label class="search-scope">新任务默认检索范围<select name="research_scope"><option value="focused" ${defaults.research_scope==='focused'?'selected':''}>小范围起步</option><option value="expanded" ${defaults.research_scope==='expanded'?'selected':''}>较宽起步</option></select></label>
    <label class="check"><input type="checkbox" name="use_library" ${defaults.use_library ? 'checked' : ''}>默认允许 agent 检索并发送文献库书目、摘要与方向；不包括私人笔记和附件。每次任务仍需单独确认。</label>
    <div class="dialog-actions"><button type="button" class="quiet" data-action="reset-defaults">恢复初始默认</button><button type="submit" class="primary">保存默认值</button></div></form></details>
    <div class="scope"><strong>当前工具能力</strong><p>论文与资源平台检索 · ${e(fullTextCopy)} · 网页搜索 ${config.web_search_enabled?'已配置':'未配置'}。GitHub token 和 Tavily Key 由服务端配置；不会抓取任意网页或执行陌生代码。</p></div>`;
  settings.showModal();
}
document.addEventListener('click', async event => {
  const button=event.target.closest('[data-action],[data-run],[data-tab],[data-example],[data-evidence],[data-import]');
  if(!button) return;
  try {
    if(button.dataset.run) return selectRun(button.dataset.run);
    if(button.dataset.tab){tab=button.dataset.tab;runView();return;}
    if(button.dataset.example!==undefined){document.querySelector('#goal').value=examples[Number(button.dataset.example)][1];document.querySelector('#goal').focus();return;}
    if(button.dataset.evidence){tab='evidence';runView();document.getElementById(button.dataset.evidence)?.scrollIntoView({behavior:'smooth',block:'center'});return;}
    if(button.dataset.import){
      if(!confirm('将这条来源中的论文元数据加入文献库？已存在的论文和笔记不会被覆盖。'))return;
      button.disabled=true;
      const result=await api(`/runs/${current.id}/evidence/${button.dataset.import}/import`,{confirmed:true});
      notice(result.created?'已加入文献库。':'已有这篇论文，未覆盖原有内容。');button.disabled=false;return;
    }
    switch(button.dataset.action) {
      case 'begin-guest':{
        button.disabled=true;button.textContent='正在创建临时会话…';
        await appApi('/auth/guest',{},'POST');
        window.location.assign('/');
        break;
      }
      case 'delete-guest-data':{
        if(!confirm('删除这次访客会话保存的论文、任务、证据和工作区文件？此操作无法撤销。'))break;
        button.disabled=true;
        const result=await appApi('/auth/guest/data',{},'DELETE');
        notice(result.pending?'访客会话已失效；正在停止任务并清理数据。':'本次数据已删除。');
        window.location.assign('/');
        break;
      }
      case 'logout':{
        if(!confirm('退出会结束本次访客会话，并安排清理这次的数据。'))break;
        await appApi('/auth/logout',{},'POST');
        window.location.assign('/');
        break;
      }
      case 'history':document.querySelector('.side').classList.toggle('mobile-expanded');break;
      case 'new': await refreshHistory();home();break;
      case 'settings': config=await api('/config');openSettings();break;
      case 'close-settings':settings.close();break;
      case 'matrix-preview':{
        const selections=[...document.querySelectorAll('input[name="matrix-selection"]:checked')]
          .map(input=>({paper_evidence_id:input.dataset.paperEvidence,
                        resource_evidence_id:input.dataset.resourceEvidence}));
        if(!selections.length){notice('先选择要一起预览的论文—资源候选。');break;}
        const result=await api(`/runs/${current.id}/matrix/preview`,{selections});
        pendingMatrixSelections=selections;
        const preview=document.querySelector('#matrix-import-preview');
        preview.innerHTML=`<strong>预览：${e(result.ready)} 组来源数据 · ${e(result.errors?.length||0)} 项错误</strong><ul>${(result.preview||[]).map(item=>`<li>${e(item.title)} · ${(item.resources||[]).map(e).join('、')}</li>`).join('')}</ul><p>${e(result.note||'已有论文笔记不会被覆盖；官方归属与版本判断仍待复核。')}</p>`;
        document.querySelector('[data-action="matrix-confirm"]').hidden=!result.ready||Boolean(result.errors?.length);
        break;
      }
      case 'matrix-confirm':{
        if(!pendingMatrixSelections.length){notice('先预览所选论文和资源，再确认保存。');break;}
        const selectedNow=[...document.querySelectorAll('input[name="matrix-selection"]:checked')]
          .map(input=>({paper_evidence_id:input.dataset.paperEvidence,
                        resource_evidence_id:input.dataset.resourceEvidence}));
        const order=items=>items.map(item=>`${item.paper_evidence_id}|${item.resource_evidence_id}`).sort().join('\n');
        if(order(selectedNow)!==order(pendingMatrixSelections)){notice('选择已变化；请重新预览后再确认。');break;}
        const result=await api(`/runs/${current.id}/matrix/confirm`,{selections:pendingMatrixSelections});
        pendingMatrixSelections=[];
        document.querySelectorAll('input[name="matrix-selection"]').forEach(input=>{input.checked=false;});
        const preview=document.querySelector('#matrix-import-preview');
        preview.innerHTML=`<strong>已保存：新增 ${e(result.created?.length||0)} 篇论文，关联 ${e(result.linked?.length||0)} 条资源观察。</strong><p>${e(result.note||'')}</p>`;
        document.querySelector('[data-action="matrix-confirm"]').hidden=true;
        notice('所选论文和资源审计已加入文献库；重复项保持幂等，现有笔记没有覆盖。');
        break;
      }
      case 'reset-defaults':{
        const form=document.querySelector('#defaults-form');
        for(const [name,value] of Object.entries(SHIPPED_DEFAULTS)){const field=form.elements[name];if(!field)continue;if(field.type==='checkbox')field.checked=value;else field.value=value;}
        notice('已填入初始默认值；点击「保存默认值」后生效。');break;
      }
      case 'fetch-models':{
        const form=document.querySelector('#model-form');
        if(!form.elements.trust_endpoint.checked){notice('请先勾选「我信任此模型服务」：拉取模型列表会把 Key 发送到该地址。');break;}
        button.disabled=true;
        const result=await api('/models',{base_url:form.elements.base_url.value,api_key:form.elements.api_key.value,trust_endpoint:true});
        const ids=modelOptionIds(result);
        document.querySelector('#model-options').innerHTML=ids.map(id=>`<option value="${e(id)}"></option>`).join('');
        notice(ids.length?`已获取 ${ids.length} 个模型 ID：在 Model ID 里输入或选择。列表不代表支持工具调用，仍需执行「测试工具调用」。`:'该服务没有返回可用的模型 ID，请手动填写。');
        button.disabled=false;break;
      }
      case 'clear-model':config=await api('/config',{},'DELETE');settings.close();sidebar();if(!current)home();notice('内存中的模型配置已清除。');break;
      case 'test-model':button.disabled=true;await api('/config/test',{});notice('工具调用测试通过；不代表科研效果已评测。');button.disabled=false;break;
      case 'cancel':await api('/runs/'+current.id+'/cancel',{});notice('已请求停止，将在当前调用结束或超时后生效。');break;
      case 'resume':if(confirm('使用同一模型从检查点继续？可能再次产生调用费用，累计调用预算不重置。')){await api('/runs/'+current.id+'/resume',{confirmed:true});await selectRun(current.id);}break;
      case 'retry':if(confirm('按上一轮的目标原文重试一轮？会新增模型调用费用，会话累计额度不重置。')){const r=await api('/retries',{parent_run:current.id,authorize_spend:true,consent_to_send:true,research_scope:current.params?.research_scope||normalizeDefaults(config.task_defaults).research_scope,idempotency_key:`web-retry-${current.id}-${current.turn||1}`});await refreshHistory();await selectRun(r.id);}break;
      case 'workspace-preview':{
        const form=button.closest('#followup-form'), file=form?.querySelector('#workspace-bundle-file')?.files?.[0];
        if(!file){notice('先选择一个 JSON bundle 文件。');break;}
        if(file.size>4*1024*1024){notice('Bundle 超过 4 MiB 上限。');break;}
        const bundle=JSON.parse(await file.text());
        const preview=await appApi('/workspaces/import/preview',{bundle});
        pendingWorkspaceBundle=bundle;
        const status=form.querySelector('#workspace-import-status');
        status.textContent=`工作区 ${preview.workspace_id} · 新来源 ${preview.new.length} · 已有 ${preview.already_present.length} · 冲突 ${preview.conflicts.length}。预览未写入；导入不会确认工具来源或批准论文。`;
        form.querySelector('[data-action="workspace-import-confirm"]').hidden=false;
        break;
      }
      case 'workspace-import-confirm':{
        if(!pendingWorkspaceBundle){notice('请先预览 bundle。');break;}
        const form=button.closest('#followup-form'), runId=current?.id;
        const draft={goal:form?.elements.goal.value||'', reuse:[...form.querySelectorAll('input[name="reuse"]:checked')].map(input=>input.value),
          useLibrary:Boolean(form?.elements.use_library.checked), trustDestination:Boolean(form?.elements.trust_destination.checked),
          authorize:Boolean(form?.elements.authorize.checked)};
        const result=await appApi('/workspaces/import',{bundle:pendingWorkspaceBundle});
        pendingWorkspaceBundle=null;
        workspaces=(await appApi('/workspaces')).workspaces||[];
        if(current?.id===runId){
          runView();
          const replacement=document.querySelector('#followup-form');
          if(replacement){replacement.elements.goal.value=draft.goal;replacement.elements.use_library.checked=draft.useLibrary;
            replacement.elements.trust_destination.checked=draft.trustDestination;replacement.elements.authorize.checked=draft.authorize;
            for(const input of replacement.querySelectorAll('input[name="reuse"]'))input.checked=draft.reuse.includes(input.value);}
        }
        notice(`已导入 ${result.new.length} 条来源快照；来源声明保持未认证，论文未入库。`);
        break;
      }
      case 'workspace-export':{
        const workspaceId=document.querySelector('#workspace-choice')?.value||'';
        if(!workspaceId){notice('先选择要导出的工作区。');break;}
        const bundle=await appApi(`/workspaces/${encodeURIComponent(workspaceId)}/export`);
        const url=URL.createObjectURL(new Blob([JSON.stringify(bundle,null,2)],{type:'application/json'}));
        const anchor=document.createElement('a');anchor.href=url;anchor.download=`re0-workspace-${workspaceId}.json`;
        document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
        break;
      }
    }
  } catch(err){button.disabled=false;notice(err.message);}
});
document.addEventListener('change', event=>{
  if(event.target.id==='workspace-choice'){
    const exportButton=document.querySelector('[data-action="workspace-export"]');
    if(exportButton)exportButton.disabled=!event.target.value;
    for(const input of document.querySelectorAll('input[name="workspace_reuse"]')){
      const enabled=input.dataset.workspaceId===event.target.value;
      if(!enabled)input.checked=false;
      input.disabled=!enabled;
    }
    return;
  }
  if(event.target.id!=='preset')return;
  const value=event.target.value, field=document.querySelector('#base-url');
  // Candidate models belong to one provider; drop them when the provider changes.
  document.querySelector('#model-options').innerHTML='';
  if(value==='__custom'){field.value='';field.focus();return;}
  if(value)field.value=value;
});
// A toast left inside a closed dialog would sit in a display:none subtree.
settings.addEventListener('close', noticeHome);
document.addEventListener('submit', async event=>{
  if(!['task-form','model-form','defaults-form','followup-form'].includes(event.target.id))return;
  event.preventDefault();const form=event.target, data=new FormData(form), button=form.querySelector('[type=submit]');button.disabled=true;
  try {
    if(form.id==='model-form') {
      config=await api('/config/connect',{base_url:data.get('base_url'),model:data.get('model'),api_key:data.get('api_key'),trust_endpoint:data.has('trust_endpoint'),token_parameter:data.get('token_parameter'),max_output_tokens:Number(data.get('max_output_tokens'))},'POST');
      form.querySelector('[name=api_key]').value='';settings.close();sidebar();if(!current)home(document.querySelector('#goal')?.value || '');notice('连接测试通过；Key 已保存到服务内存。测试不代表科研效果已评测。');
    } else if(form.id==='defaults-form') {
      config=await api('/defaults',{max_model_calls:Number(data.get('max_model_calls')),max_tool_calls:Number(data.get('max_tool_calls')),max_upstream_requests:Number(data.get('max_upstream_requests')),attempt_seconds:Number(data.get('attempt_seconds')),use_library:data.has('use_library'),research_scope:data.get('research_scope')},'PUT');
      settings.close();sidebar();if(!current)home(document.querySelector('#goal')?.value || '');notice('任务默认值已保存；只影响之后新建的任务。');
    } else if(form.id==='followup-form') {
      const workspaceId=String(data.get('workspace_id')||'');
      const workspaceReuse=[...form.querySelectorAll('input[name="workspace_reuse"]:checked')]
        .filter(input=>input.dataset.workspaceId===workspaceId).map(input=>input.value);
      const payload=followupPayload({goal:data.get('goal'),reuse:data.getAll('reuse'),workspaceId,
        workspaceReuse,useLibrary:data.has('use_library'),trustNewDestination:data.has('trust_destination'),researchScope:data.get('research_scope')},current,config.task_defaults);
      const problem=followupProblem(payload);
      if(problem){notice(problem);return;}
      payload.idempotency_key=idempotencyKeyFor(current,payload);
      const r=await api('/followups',payload);
      await refreshHistory();await selectRun(r.id);
      notice('已发起新一轮。复用的证据没有重新抓取；本轮花费记在会话累计账本上。');
    } else {
      const defaults=normalizeDefaults(config.task_defaults);
      const r=await api('/runs',{goal:data.get('goal'),max_model_calls:defaults.max_model_calls,max_tool_calls:defaults.max_tool_calls,max_upstream_requests:defaults.max_upstream_requests,attempt_seconds:defaults.attempt_seconds,use_library:defaults.use_library,research_scope:data.get('research_scope'),consent_to_send:data.has('consent_to_send')});
      await refreshHistory();await selectRun(r.id);
    }
  }catch(err){notice(err.message);}finally{button.disabled=false;}
});
(async()=>{
  try {
    const session=await appApi('/auth/session');
    identity=session.identity||{kind:'anonymous'};
    if(session.deployment?.mode==='hosted'&&!identity.authenticated){
      if(session.deployment.guest_access_enabled){guestLanding(session.deployment);return;}
      window.location.replace('/login?next=%2F');return;
    }
    side.hidden=false;
    document.body.classList.remove('guest-door');
    await refreshHistory();home();
  }catch(err){workspace.innerHTML=`<div class="empty-result"><h1>无法连接服务</h1><p>${e(err.message)}</p><p>确认服务仍在运行后刷新页面。</p></div>`;}
})();
