import {e, link, timeLabel} from './core.js';
import {RUN_LABELS, TOOL_LABELS, SHIPPED_DEFAULTS, activeRun, budgetSummary, canResume, consentText, eventText, normalizeDefaults} from './agent-core.js';

const workspace = document.querySelector('#workspace');
const settings = document.querySelector('#settings');
let config = {}, runs = [], current = null, events = [], tab = 'trace', epoch = 0, timer, toastTimer;
const examples = [
  ['文献发现', '找几篇支持约束条件的 Layout 生成论文，查找代码和权重，列出资源缺口，不要把未找到当作不存在。'],
  ['资源深查', '搜索图层分解与图层生成的论文，区分官方声明、代码、checkpoint 与数据集的实际线索，给出来源。'],
  ['Baseline 筛选', '检索图推荐系统的可复现实验工作，比较数据划分、评测脚本和训练代码线索，标记需要进一步确认的条件。']
];
async function api(path, data, method = data === undefined ? 'GET' : 'POST') {
  const response = await fetch('/api/agent' + path, {method, headers:{'Content-Type':'application/json','X-Re0-Client':'web'}, ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  let result;
  try { result = await response.json(); } catch { throw new Error('服务响应无效，请确认 Python 服务仍在运行。'); }
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : (result.detail || []).map(x => `${(x.loc||[]).join('.')}: ${x.msg}`).join('；') || '操作未完成');
  return result;
}
function notice(text) {
  clearTimeout(toastTimer);
  const node = document.querySelector('#notice');
  node.textContent = text; node.classList.add('show');
  toastTimer = setTimeout(() => node.classList.remove('show'), 7000);
}
function sidebar() {
  document.querySelector('#history').innerHTML = runs.length ? runs.map(r => `<button class="history-item ${r.id === current?.id ? 'is-current' : ''}" data-run="${e(r.id)}"><strong>${e(r.goal)}</strong><small><i class="dot ${e(r.status)}"></i>${e(RUN_LABELS[r.status] || r.status)} · ${e(timeLabel(r.created_at))}</small></button>`).join('') : '<p class="subtle">尚无研究任务。<br>从右侧提出第一个问题。</p>';
  document.querySelector('#model-status').innerHTML = `<i class="dot ${config.configured ? 'completed' : ''}"></i><span>${config.configured ? e(config.model) : '尚未接入模型'}</span><small>${config.configured ? '模型配置仅保留在进程内存' : '接入 API 或本地模型后开始'}</small>`;
}
function home(goal = '') {
  epoch++; clearTimeout(timer); current = null; events = []; sidebar();
  const defaults = normalizeDefaults(config.task_defaults);
  document.querySelector('#breadcrumb').textContent = '新研究';
  workspace.innerHTML = `<section class="home"><div class="eyebrow">AN AGENT FOR YOUR NEXT DISCOVERY</div>
    <h1>从一个问题，<br>开始研究<span>。</span></h1>
    <p class="lead">让 agent 检索文献、追查资源、整理证据。<br>你决定研究目标，也保留对结论的判断。</p>
    ${!config.configured ? '<div class="setup-note"><span>开始前，需要连接你自己的模型。</span><button class="text-button" data-action="settings">配置模型 →</button></div>' : ''}
    <form id="task-form" class="composer"><label class="sr-only" for="goal">研究目标</label><textarea id="goal" name="goal" minlength="5" maxlength="6000" rows="5" placeholder="描述你的研究问题、筛选条件，以及希望得到的结果…" required>${e(goal)}</textarea>
      <div class="composer-footer"><span><i class="dot"></i> 自主检索 · 证据可追溯 · 入库需确认</span><div class="composer-actions"><button type="button" class="budget-link" data-action="settings">${e(budgetSummary(defaults))}</button><button class="primary" type="submit" ${!config.configured || config.busy ? 'disabled' : ''}>开始研究 ↗</button></div></div>
      <label class="check consent"><input type="checkbox" name="consent_to_send" required>${e(consentText(defaults))}</label>
    </form>
    <div class="example-heading">从这些研究任务开始 <span>示例提示，不是预置研究结果</span></div><div class="examples">${examples.map(([title,text],i) => `<button data-example="${i}"><small>0${i+1} / ${e(title)}</small><p>${e(text)}</p><span>使用这个问题 ↗</span></button>`).join('')}</div>
    <div class="scope"><strong>当前能力边界</strong><p>检索论文元数据与摘要，阅读仓库文本，核查资源线索。不会自动读取 PDF 全文、执行陌生代码或认定论文已复现。${config.web_search_enabled ? '公开网页搜索已配置。' : '未配置 Tavily 时，只使用论文与资源平台搜索，不冒充全网检索。'}</p></div>
  </section>`;
}
function runView() {
  if (!current) return;
  const opened=[...document.querySelectorAll('#result-panel details[open]')].map(x=>x.closest('article')?.id);
  document.querySelector('#breadcrumb').textContent = '研究任务';
  workspace.innerHTML = `<section class="run-page"><div class="run-heading"><div><div class="eyebrow">RESEARCH TASK <span>${e(timeLabel(current.created_at))}</span></div><h1>${e(current.goal)}</h1></div><span class="status ${e(current.status)}">${e(RUN_LABELS[current.status])}</span></div>
    <div class="run-controls"><span>${e(current.config.model)} · 模型 ${current.model_calls || 0}/${current.params.max_model_calls} · 工具 ${current.tool_calls || 0}/${current.params.max_tool_calls}</span><div>${activeRun(current) ? '<button class="danger" data-action="cancel">停止研究</button>' : ''}${canResume(current) ? '<button class="primary" data-action="resume">从检查点继续</button>' : ''}<a class="button" href="/api/agent/runs/${e(current.id)}/export" download>导出任务</a></div></div>
    ${current.error ? `<div class="error-panel">${e(current.error)}</div>` : ''}
    <div class="run-layout"><aside class="plan-panel"><div class="section-label">公开行动计划</div>${current.plan?.length ? `<ol>${current.plan.map(s=>`<li>${e(s)}</li>`).join('')}</ol>` : '<p class="subtle">agent 将在执行时制定计划。</p>'}<div class="plan-bottom">${current.evidence.length}<span>条来源证据</span></div><p class="subtle">计划由模型生成。执行记录来自实际工具调用，不展示模型私有思维。</p></aside>
      <section class="results"><div class="tabs" role="tablist">${[['trace','执行记录'],['report','研究报告'],['evidence',`来源证据 (${current.evidence.length})`]].map(([key,title])=>`<button role="tab" aria-selected="${tab===key}" data-tab="${key}" class="${tab===key?'active':''}">${title}</button>`).join('')}</div><div id="result-panel" role="tabpanel"></div></section></div>
    <p class="footnote">模型报告是待复核分析；引用编号存在，不代表引用内容已经支持全部结论。Token 数以提供商实际返回为准，不估算金额。</p></section>`;
  panel(); sidebar();
  for(const id of opened){const details=document.getElementById(id)?.querySelector('details');if(details)details.open=true;}
}
function panel() {
  const node = document.querySelector('#result-panel');
  if (!node || !current) return;
  if (tab === 'trace') {
    node.innerHTML = `<div class="trace">${events.length ? events.map(ev => `<article class="trace-row"><span class="trace-marker ${ev.kind==='tool_finished' && !ev.data.ok?'bad':''}"></span><div><small>${e(timeLabel(ev.at))} · ${e(ev.kind)}</small><p>${e(eventText(ev))}</p></div></article>`).join('') : '<p class="subtle">正在读取执行记录…</p>'}${activeRun(current)?'<div class="working"><span></span>正在执行；可离开此页面，任务由本地服务继续处理。</div>':''}</div>`;
  } else if (tab === 'report') {
    const r=current.report;
    node.innerHTML = r ? `<article class="report"><div class="eyebrow">${r.outcome==='insufficient_evidence'?'EVIDENCE IS INCOMPLETE':'RESEARCH FINDINGS'} · 模型生成，待复核</div><h2>${e(r.title)}</h2><p class="report-summary">${e(r.summary)}</p><h3>发现与依据</h3>${r.findings.map((f,i)=>`<section class="finding"><small>${i+1} / ${{observed:'观察',inference:'推断',uncertain:'不确定'}[f.assessment]}</small><p>${e(f.claim)}</p><div class="citations">${f.evidence_ids.map(id=>`<button data-evidence="${e(id)}">${e(id)}</button>`).join('')}</div></section>`).join('') || '<p class="subtle">没有形成有充分依据的发现。</p>'}<h3>检查范围与剩余缺口</h3><ul>${r.limitations.map(x=>`<li>${e(x)}</li>`).join('')}</ul><div class="usage">提供商报告的 Token：${e(current.usage?.total_tokens || 0)}${current.usage?.unreported_calls ? `；${e(current.usage.unreported_calls)} 次调用未报告用量` : ''}</div></article>` : '<div class="empty-result"><h2>报告还未生成</h2><p>报告只有在 agent 提交结构化结果后出现。任务失败或达到预算，不会自动填充虚构结论。</p><button class="button" data-tab="evidence">查看已收集的证据</button></div>';
  } else {
    node.innerHTML = `<div class="evidence-list">${current.evidence.length ? current.evidence.map(ev=>`<article class="evidence-card" id="${e(ev.id)}"><div class="evidence-meta"><code>${e(ev.id)}</code><span>${e(ev.kind)}</span></div><h3>${e(ev.paper?.title || TOOL_LABELS[ev.tool] || '来源材料')}</h3><p class="locator">${e(ev.locator)} · ${e(timeLabel(ev.retrieved_at))}</p><details><summary>查看来源摘录</summary><pre>${e(ev.content)}</pre></details><footer><a href="${link(ev.source_url)}" target="_blank" rel="noopener noreferrer">打开来源 ↗</a>${ev.kind==='paper' && ev.paper ? `<button class="button" data-import="${e(ev.id)}">确认加入文献库</button>` : ''}</footer></article>`).join('') : '<div class="empty-result"><h2>尚无来源证据</h2><p>只有工具实际取得的材料才会出现在这里。</p></div>'}</div>`;
  }
}
async function refreshHistory() {
  [runs, config] = await Promise.all([api('/runs'),api('/config')]); sidebar();
}
async function selectRun(id) {
  const token = ++epoch; clearTimeout(timer); tab='trace'; events=[];
  try {
    const [r, ev] = await Promise.all([api('/runs/'+id),api('/runs/'+id+'/events')]);
    if (token!==epoch) return;
    current=r; events=ev; runView(); poll(token);
  } catch(err){notice(err.message);}
}
function poll(token) {
  if (token!==epoch || !current || !activeRun(current)) return;
  timer=setTimeout(async()=>{
    const id=current?.id;
    if(token!==epoch || !id) return;
    try {
      const [r, ev] = await Promise.all([api('/runs/'+id),api('/runs/'+id+'/events?after='+(events.at(-1)?.id || 0))]);
      if(token!==epoch) return;
      current=r; events.push(...ev); runView();
      if(!activeRun(r)) await refreshHistory();
      poll(token);
    } catch(err) {if(token===epoch){notice(err.message);poll(token);}}
  },1400);
}
function openSettings() {
  const defaults = normalizeDefaults(config.task_defaults);
  settings.innerHTML=`<div class="dialog-header"><div><div class="eyebrow">LOCAL WORKSPACE SETTINGS</div><h2 id="settings-title">模型与任务设置</h2></div><button class="close" data-action="close-settings" aria-label="关闭设置">×</button></div><p class="subtle">使用支持 Chat Completions 工具调用的服务。API Key 只放在本地服务进程内存；重启后重新输入，或用环境变量配置。</p>
    <form id="model-form" autocomplete="off"><label>接口地址预设<select id="preset"><option value="">选择接口形状（不代表已验证所有模型）</option><option value="https://api.openai.com/v1">OpenAI-compatible / OpenAI</option><option value="https://api.deepseek.com/v1">DeepSeek-compatible</option><option value="https://dashscope.aliyuncs.com/compatible-mode/v1">DashScope-compatible</option><option value="http://127.0.0.1:11434/v1">本地服务 / 127.0.0.1:11434</option></select></label>
    <label>API Base URL<input name="base_url" id="base-url" type="url" value="${e(config.base_url || '')}" placeholder="https://api.example.com/v1" required></label><p class="field-note">只接受预设可信域名和显式端口的回环地址。自定义域名需设置 RE0_LLM_ALLOWED_HOSTS。</p>
    <label>Model ID<input name="model" value="${e(config.model || '')}" placeholder="填写你实际可用且支持工具调用的模型 ID" required maxlength="150"></label>
    <label>API Key<input name="api_key" type="password" autocomplete="new-password" placeholder="${config.has_api_key?'已配置；重新保存时需再次输入，不会回填旧密钥':'本地无认证服务可留空'}" maxlength="2048"></label>
    <div class="settings-grid"><label>输出预算参数<select name="token_parameter"><option value="max_tokens" ${config.token_parameter==='max_tokens'?'selected':''}>max_tokens</option><option value="max_completion_tokens" ${config.token_parameter==='max_completion_tokens'?'selected':''}>max_completion_tokens</option></select></label><label>单次输出 Token 上限<input name="max_output_tokens" type="number" value="${config.max_output_tokens || 3000}" min="256" max="8192" required></label></div>
    <label class="check"><input type="checkbox" name="trust_endpoint" required>我信任此模型服务，并同意将任务材料发送到这个地址。</label><div class="dialog-actions"><button type="button" class="quiet" data-action="clear-model">清除内存配置</button><button type="submit" class="primary">保存配置</button></div></form>
    <div class="connection-test"><button class="button" data-action="test-model" ${config.configured?'':'disabled'}>测试工具调用</button><span>会发起一次模型请求，可能计费；测试不包含文献数据。</span></div>
    <div class="defaults-block"><h3>任务预算与数据权限</h3><p class="field-note">新建任务时自动应用。已创建的任务保留自己的预算，不受此处修改影响。</p>
    <form id="defaults-form"><div class="budget-fields"><label>模型调用上限<input type="number" name="max_model_calls" min="2" max="24" value="${defaults.max_model_calls}" required></label><label>工具调用上限<input type="number" name="max_tool_calls" min="1" max="40" value="${defaults.max_tool_calls}" required></label><label>单次执行窗口（秒）<input type="number" name="attempt_seconds" min="30" max="900" value="${defaults.attempt_seconds}" required></label></div>
    <label class="check"><input type="checkbox" name="use_library" ${defaults.use_library ? 'checked' : ''}>允许 agent 检索并发送本地文献库的书目、摘要与方向；不包括私人笔记和附件。启用后，新建任务的同意项会一并写明文献库材料将发送到模型服务。</label>
    <div class="dialog-actions"><button type="button" class="quiet" data-action="reset-defaults">恢复初始默认</button><button type="submit" class="primary">保存默认值</button></div></form></div>
    <div class="scope"><strong>其他工具凭证</strong><p>GitHub token 和 Tavily 搜索 Key 通过服务器环境变量配置。网页搜索：${config.web_search_enabled?'已配置':'未配置（仍可搜索论文、GitHub、Hugging Face）'}。首版不支持任意网页全文抓取或 PDF 阅读。</p></div>`;
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
      case 'history':document.querySelector('.side').classList.toggle('mobile-expanded');break;
      case 'new': await refreshHistory();home();break;
      case 'settings': config=await api('/config');openSettings();break;
      case 'close-settings':settings.close();break;
      case 'reset-defaults':{
        const form=document.querySelector('#defaults-form');
        for(const [name,value] of Object.entries(SHIPPED_DEFAULTS)){const field=form.elements[name];if(!field)continue;if(field.type==='checkbox')field.checked=value;else field.value=value;}
        notice('已填入初始默认值；点击「保存默认值」后生效。');break;
      }
      case 'clear-model':config=await api('/config',{},'DELETE');settings.close();sidebar();if(!current)home();notice('内存中的模型配置已清除。');break;
      case 'test-model':button.disabled=true;await api('/config/test',{});notice('工具调用测试通过；不代表科研效果已评测。');button.disabled=false;break;
      case 'cancel':await api('/runs/'+current.id+'/cancel',{});notice('已请求停止，将在当前调用结束或超时后生效。');break;
      case 'resume':if(confirm('使用同一模型从检查点继续？可能再次产生调用费用，累计调用预算不重置。')){await api('/runs/'+current.id+'/resume',{confirmed:true});await selectRun(current.id);}break;
    }
  } catch(err){button.disabled=false;notice(err.message);}
});
document.addEventListener('change', event=>{if(event.target.id==='preset'&&event.target.value)document.querySelector('#base-url').value=event.target.value;});
document.addEventListener('submit', async event=>{
  if(!['task-form','model-form','defaults-form'].includes(event.target.id))return;
  event.preventDefault();const form=event.target, data=new FormData(form), button=form.querySelector('[type=submit]');button.disabled=true;
  try {
    if(form.id==='model-form') {
      config=await api('/config',{base_url:data.get('base_url'),model:data.get('model'),api_key:data.get('api_key'),trust_endpoint:data.has('trust_endpoint'),token_parameter:data.get('token_parameter'),max_output_tokens:Number(data.get('max_output_tokens'))},'PUT');
      form.querySelector('[name=api_key]').value='';settings.close();sidebar();if(!current)home(document.querySelector('#goal')?.value || '');notice('配置已保存到进程内存。建议先测试工具调用。');
    } else if(form.id==='defaults-form') {
      config=await api('/defaults',{max_model_calls:Number(data.get('max_model_calls')),max_tool_calls:Number(data.get('max_tool_calls')),attempt_seconds:Number(data.get('attempt_seconds')),use_library:data.has('use_library')},'PUT');
      settings.close();sidebar();if(!current)home(document.querySelector('#goal')?.value || '');notice('任务默认值已保存；只影响之后新建的任务。');
    } else {
      const defaults=normalizeDefaults(config.task_defaults);
      const r=await api('/runs',{goal:data.get('goal'),max_model_calls:defaults.max_model_calls,max_tool_calls:defaults.max_tool_calls,attempt_seconds:defaults.attempt_seconds,use_library:defaults.use_library,consent_to_send:data.has('consent_to_send')});
      await refreshHistory();await selectRun(r.id);
    }
  }catch(err){notice(err.message);}finally{button.disabled=false;}
});
(async()=>{try{await refreshHistory();home();}catch(err){workspace.innerHTML=`<div class="empty-result"><h1>无法连接本地服务</h1><p>${e(err.message)}</p><p>请用 python run.py 启动后刷新页面。</p></div>`;}})();
