import {icon} from './icons.js';
import {api} from './api.js';
import {initTheme} from './theme.js';
import {READING,KINDS,ACCESS,OWNERSHIP,CLAIMS,INDICATORS,e,link,paperInput,needsAttention,filteredPapers,counts,resourceStatus,timeLabel} from './core.js';

const app = document.querySelector('#app');
const overlays = document.querySelector('#overlays');
let toastTimer;
const state = {papers:[],topics:[],relations:null,relationQuery:'',relationType:'',relationOffset:0,relationRequest:0,
  relationPaperChoice:'',view:'library',query:'',topic:'',status:'',sort:'updated',hideDemo:false,mode:'cards',selected:new Set()};
const kindIcon = {code:'code',checkpoint:'box',dataset:'database',evaluation:'shield',environment:'settings',demo:'external'};
const RELATION_LABELS = {uses_method:'采用方法',evaluated_on:'评测对象',has_resource:'关联资源',claim:'主张'};
const ASSERTION_LABELS = {author_statement:'作者声明',model_inference:'模型推断',human_confirmation:'人工确认'};

function toast(message, error = false) {
  const container = document.querySelector('#toasts');
  clearTimeout(toastTimer);
  container.replaceChildren();
  const node = document.createElement('div');
  node.className = 'toast' + (error ? ' toast-error' : '');
  node.textContent = message;
  container.append(node);
  toastTimer = setTimeout(() => { node.remove(); toastTimer = null; }, error ? 9000 : 4500);
}
function button(text, action, {primary=false, cls='', disabled=false} = {}) {
  return `<button type="button" class="btn ${primary?'primary':''} ${cls}" data-action="${action}" ${disabled?'disabled':''}>${text}</button>`;
}
function badge(status, text) { return `<span class="badge badge-${e(status)}"><i></i>${e(text || ACCESS[status] || status)}</span>`; }
function currentPapers() { return filteredPapers(state.papers, {...state, attention:state.view === 'audit'}); }
async function refresh() {
  const [papers,topics] = await Promise.all([api('/papers'),api('/topics')]);
  state.papers = papers; state.topics = topics; state.relations = null;
  const available = new Set(papers.map(p => p.id));
  state.selected.forEach(id => { if(!available.has(id)) state.selected.delete(id); });
  if(state.relationPaperChoice&&!available.has(state.relationPaperChoice))state.relationPaperChoice='';
  render();
}
async function loadRelations() {
  const request=++state.relationRequest;
  const query=new URLSearchParams();
  if(state.relationQuery.trim())query.set('q',state.relationQuery.trim());
  if(state.relationType)query.set('relation_type',state.relationType);
  query.set('limit','50');
  query.set('offset',String(state.relationOffset));
  const suffix=query.toString();
  const result=await api('/knowledge/relations'+(suffix?'?'+suffix:''));
  if(request!==state.relationRequest)return;
  state.relations=result;
  if(state.view==='graph')renderResults();
}
function openDialog(body, {drawer=false, wide=false} = {}) {
  const dialog = document.createElement('dialog');
  dialog.className = drawer ? 'drawer' : 'modal' + (wide?' modal-wide':'');
  dialog.innerHTML = body;
  dialog.setAttribute('aria-label', drawer ? '论文详情与证据' : '编辑窗口');
  const returnFocus = document.activeElement;
  let cleaned = false;
  const cleanup = () => { if(cleaned)return; cleaned = true; dialog.remove(); if(returnFocus?.isConnected)returnFocus.focus(); };
  const nativeClose = dialog.close.bind(dialog);
  dialog.close = (value) => { nativeClose(value); cleanup(); };
  overlays.append(dialog);
  dialog.addEventListener('click', event => {
    if (event.target.closest('[data-close]')) dialog.close();
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      if(event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    }
  });
  dialog.addEventListener('close', cleanup);
  dialog.showModal();
  return dialog;
}
function modalHead(eyebrow,title,subtitle='') {
  return `<div class="modal-head"><div><div class="eyebrow">${e(eyebrow)}</div><h2>${e(title)}</h2>${subtitle?`<p>${e(subtitle)}</p>`:''}</div><button class="icon-btn" type="button" data-close aria-label="关闭">${icon('close')}</button></div>`;
}
async function busy(buttonNode, task) {
  const old = buttonNode.innerHTML;
  buttonNode.disabled = true; buttonNode.innerHTML = '<span class="spinner"></span> 处理中';
  try { return await task(); } catch(error) { toast(error.message,true); }
  finally { if(buttonNode.isConnected) { buttonNode.disabled=false; buttonNode.innerHTML=old; } }
}
function stat(label,value,hint,ico) {
  return `<div class="stat"><div class="stat-label">${e(label)}${icon(ico,17)}</div><div class="stat-value">${value.toString().padStart(2,'0')}</div><div class="stat-hint">${e(hint)}</div></div>`;
}
function render() {
  const total = counts(state.papers);
  const nav = (view,name,ico,extra='') => `<button type="button" class="nav-item ${state.view===view&&!state.topic?'active':''}" data-nav="${view}">${icon(ico)}<span>${name}</span>${extra?`<small>${extra}</small>`:''}</button>`;
  app.innerHTML = `<aside class="sidebar">
    <a href="#" class="brand" data-nav="library" aria-label="re0 首页">re<span>0</span><b>research workspace</b></a>
    <div class="workspace-chip"><span class="workspace-avatar">R</span><div>个人研究空间<small>LOCAL WORKSPACE</small></div><span class="online-dot"></span></div>
    <div class="nav-caption">工作台</div><nav aria-label="主导航">
      ${nav('library','文献库','book',total.papers)}${nav('audit','待核验','shield',total.pending)}${nav('compare','论文对比','compare')}${nav('graph','关系视图','graph')}
    </nav><div class="nav-caption section-label">研究方向<button class="icon-btn" data-action="add-topic" aria-label="添加研究方向">${icon('plus',15)}</button></div>
    <nav aria-label="研究方向">${state.topics.map((topic,i)=>`<button class="nav-item topic-nav ${state.topic===topic?'active':''}" data-topic="${e(topic)}"><span class="topic-dot color-${i%4}"></span><span>${e(topic)}</span><small>${state.papers.filter(p=>p.topics.includes(topic)).length}</small></button>`).join('')}</nav>
    <a href="/" class="nav-item">← 科研 Agent 工作台</a><a href="/static/search.html" class="nav-item">检索工作台（读结果 JSON）</a><div class="sidebar-bottom"><div class="local-note">${icon('leaf',20)}<strong>证据先于结论</strong><p>找到资源不等于可以复现。<br>每次判断，都留下依据。</p></div>
    <button class="nav-item" data-action="settings">${icon('settings')}<span>数据与设置</span></button><div class="sidebar-footer"><span class="online-dot"></span> 本地存储 <span>v0.2.0</span></div></div>
  </aside><div class="workspace-main"><header class="topbar"><div class="breadcrumb">个人研究空间 <span>/</span> ${e(state.topic || {library:'文献库',audit:'待核验',compare:'论文对比',graph:'关系视图'}[state.view])}</div><div class="top-actions"><span class="local-badge">${icon('database',13)} LOCAL FIRST</span><button class="theme-toggle" data-theme-toggle aria-label="切换主题"></button><button class="avatar" data-action="settings" aria-label="数据与设置">R</button></div></header>
  <main id="main"><div class="page-head"><div><div class="eyebrow">${state.view==='audit'?'RESOURCE VERIFICATION':state.view==='compare'?'SIDE BY SIDE':state.view==='graph'?'RESEARCH CONNECTIONS':'YOUR RESEARCH, CONNECTED'}</div><h1>${e(state.topic || {library:'每一篇论文，都有迹可循。',audit:'从资源线索，到检查证据。',compare:'把差异，放在同一张桌面上。',graph:'看见你的研究脉络。'}[state.view])}</h1><p>${e({library:'整理研究方向，核验代码与数据，让阅读真正走向实验。',audit:'保留未知，记录范围。这里没有未经验证的“已复现”。',compare:'对照研究版本与资源条件；不将不同实验协议的结果直接排名。',graph:'由已保存的方向、论文和资源生成；不是自动推断的理论关系图。'}[state.view])}</p></div><div class="page-actions">${button(icon('upload')+' 导入','import')}${button(icon('plus')+' 添加论文','add-paper',{primary:true})}</div></div>
  ${state.papers.some(p=>p.is_demo)?`<div class="demo-banner">${icon('info',16)}<span><strong>演示数据</strong> · 包含虚构论文和模拟核验记录，不可用于学术引用。</span><button data-action="clear-demo">清除演示</button></div>`:''}
  <div class="stats">${stat('文献总数',total.papers,'构建你的研究上下文','book')}${stat('研究方向',total.topics,'一篇论文，多个研究视角','layers')}${stat('待核验资源',total.pending,'未核验、访问失败或无法判断','shield')}${stat('Baseline 候选',total.baseline,'由你标记，不等于已复现','compare')}</div>
  <section class="library-section"><div class="section-top"><div class="section-title"><h2>${e(state.topic || {library:'全部文献',audit:'需要继续核实',compare:'资源条件对比',graph:'已记录的关联'}[state.view])}</h2><span id="result-count" class="count-pill"></span></div><div class="section-tools">${state.view==='library'||state.view==='audit'?`<button class="text-btn" data-action="compare-selected">${icon('compare',16)} 对比所选 <span id="selected-count">${state.selected.size}</span></button><div class="view-toggle"><button class="${state.mode==='cards'?'active':''}" data-mode="cards" aria-label="卡片视图">${icon('grid',16)}</button><button class="${state.mode==='table'?'active':''}" data-mode="table" aria-label="表格视图">${icon('list',16)}</button></div>`:''}</div></div>
  ${['library','audit','graph'].includes(state.view)?`<div class="filters"><label class="search-field">${icon('search',18)}<input id="search" placeholder="搜索标题、作者、标签或笔记…" aria-label="搜索论文" value="${e(state.query)}"><kbd>/</kbd></label><select id="filter-status" aria-label="阅读状态"><option value="">全部阅读状态</option>${Object.entries(READING).map(([k,v])=>`<option value="${k}" ${state.status===k?'selected':''}>${v}</option>`).join('')}</select><select id="sort" aria-label="排序"><option value="updated" ${state.sort==='updated'?'selected':''}>最近更新</option><option value="year" ${state.sort==='year'?'selected':''}>发表年份</option><option value="title" ${state.sort==='title'?'selected':''}>标题排序</option></select></div>`:''}
  <div id="results"></div></section><footer class="page-footer"><span>re0 · 从零开始，积累可追溯的研究。</span><span>本地单用户预览版 · 无自动代码执行</span></footer></main></div>`;
  renderResults();
  bindShell();
  initTheme();
  if(state.view==='graph' && state.relations===null)loadRelations().catch(error=>toast(error.message,true));
}
function bindShell() {
  app.querySelectorAll('[data-nav]').forEach(node=>node.addEventListener('click',event=>{event.preventDefault();state.view=node.dataset.nav;state.topic='';state.query='';state.status='';if(state.view==='graph')state.relations=null;render();}));
  app.querySelectorAll('[data-topic]').forEach(node=>node.addEventListener('click',()=>{state.view='library';state.topic=node.dataset.topic;state.query='';state.status='';render();}));
  app.querySelectorAll('[data-mode]').forEach(node=>node.addEventListener('click',()=>{state.mode=node.dataset.mode;render();}));
  app.querySelectorAll('[data-action]').forEach(node=>{ if(!node.closest('#results'))node.addEventListener('click',()=>handleAction(node.dataset.action,node)); });
  app.querySelector('#search')?.addEventListener('input',event=>{state.query=event.target.value;renderResults();});
  app.querySelector('#filter-status')?.addEventListener('change',event=>{state.status=event.target.value;renderResults();});
  app.querySelector('#sort')?.addEventListener('change',event=>{state.sort=event.target.value;renderResults();});
}
function emptyState(filtered=false) {
  return `<div class="empty"><div class="empty-icon">${icon(filtered?'search':'book',32)}</div><div class="eyebrow">${filtered?'KEEP EXPLORING':'START WITH ONE PAPER'}</div><h2>${filtered?'没有符合筛选条件的论文':'你的下一项研究，从这里开始。'}</h2><p>${filtered?'换个关键词，或清除筛选条件。':'添加第一篇论文，或载入一组明确标记的虚构数据，体验分类与证据工作流。'}</p><div class="empty-actions">${filtered?button('清除筛选','reset-filter'):button(icon('plus')+' 添加第一篇论文','add-paper',{primary:true})+button('体验演示数据 '+icon('arrow',16),'seed-demo')}</div><div class="empty-steps"><span>01 / 收集论文</span><span>02 / 核验资源</span><span>03 / 保留证据</span></div></div>`;
}
function resourceChips(paper) {
  const shown=['code','checkpoint','dataset'];
  return `<div class="resource-chips">${shown.map(kind=>{
    const resources=paper.resources.filter(r=>r.kind===kind);
    const status=resources.length?resourceStatus(resources[0]):'absent';
    const description=resources.length?`${KINDS[kind]}：${ACCESS[status]}${resources.length>1?'（另有 '+(resources.length-1)+' 项）':''}`:`${KINDS[kind]}：未关联资源`;
    return `<span class="resource-chip state-${status}" title="${e(description)}">${icon(kindIcon[kind],13)}${KINDS[kind]}<i></i></span>`;
  }).join('')}</div>`;
}
function paperCard(paper,index) {
  const warnings=paper.resources.filter(needsAttention).length;
  return `<article class="paper-card"><div class="card-top"><span class="paper-number">P${String(index+1).padStart(3,'0')}</span><div class="card-top-right">${paper.is_demo?'<span class="demo-pill">演示</span>':''}<label class="select-paper"><input type="checkbox" data-select="${e(paper.id)}" ${state.selected.has(paper.id)?'checked':''} aria-label="选择 ${e(paper.title)} 进行对比"></label></div></div>
  <button class="paper-title" data-open="${e(paper.id)}">${e(paper.title)}</button><p class="paper-authors">${e(paper.authors.join(' · ')||'作者未填写')}</p><div class="paper-meta"><span>${e(paper.venue||'来源未填写')}</span><span>${paper.year||'年份待补'}</span></div>
  <div class="topic-tags">${paper.topics.map(t=>`<span>${e(t)}</span>`).join('')||'<span>未分类</span>'}</div><div class="card-separator"></div>${resourceChips(paper)}
  <div class="card-bottom">${badge(paper.status,READING[paper.status])}<button class="card-link" data-open="${e(paper.id)}">${warnings?`${warnings} 项待核实`:'查看证据'} ${icon('arrow',14)}</button></div></article>`;
}
function tableView(papers) {
  return `<div class="table-wrap"><table class="paper-table"><thead><tr><th class="check-col">选择</th><th>论文 / 研究方向</th><th>年份</th><th>阅读状态</th><th>关联资源</th><th>操作</th></tr></thead><tbody>${papers.map(p=>`<tr><td><input type="checkbox" data-select="${e(p.id)}" ${state.selected.has(p.id)?'checked':''} aria-label="选择 ${e(p.title)}"></td><td><button class="table-title" data-open="${p.id}">${p.is_demo?'<span class="demo-pill">演示</span> ':''}${e(p.title)}</button><p>${e(p.topics.join(' · ')||'未分类')}</p></td><td>${p.year||'—'}</td><td>${badge(p.status,READING[p.status])}</td><td>${resourceChips(p)}</td><td><button class="text-btn" data-open="${p.id}">查看 ${icon('arrow',14)}</button></td></tr>`).join('')}</tbody></table></div>`;
}
function renderResults() {
  const root=app.querySelector('#results');
  const papers=currentPapers();
  app.querySelector('#result-count').textContent=state.view==='compare'?state.selected.size:papers.length;
  if(state.view==='compare') root.innerHTML=compareView();
  else if(state.view==='graph') root.innerHTML=graphView(papers);
  else if(!papers.length) root.innerHTML=emptyState(state.papers.length>0);
  else root.innerHTML=(state.view==='audit'?'<div class="inline-note">'+icon('info',16)+' 仅列出含未核验、失败或无法判断资源的论文；没有关联资源的论文仍可在文献库中补充。</div>':'')+(state.mode==='table'?tableView(papers):`<div class="paper-grid">${papers.map(paperCard).join('')}</div>`);
  root.querySelectorAll('[data-open]').forEach(node=>node.addEventListener('click',()=>openPaper(node.dataset.open)));
  root.querySelectorAll('[data-action]').forEach(node=>node.addEventListener('click',()=>handleAction(node.dataset.action,node)));
  root.querySelector('#relation-paper')?.addEventListener('change',event=>{state.relationPaperChoice=event.target.value;});
  root.querySelectorAll('[data-select]').forEach(node=>node.addEventListener('change',()=>{
    if(node.checked && state.selected.size>=6) { node.checked=false;toast('首版最多同时对比 6 篇论文。');return; }
    node.checked?state.selected.add(node.dataset.select):state.selected.delete(node.dataset.select);
    const counter=app.querySelector('#selected-count');if(counter)counter.textContent=state.selected.size;
  }));
}
function compareView() {
  const papers=state.papers.filter(p=>state.selected.has(p.id));
  if(papers.length<2) return `<div class="empty"><div class="empty-icon">${icon('compare',32)}</div><h2>让比较有据可依。</h2><p>请回到文献库，勾选 2–6 篇论文，再打开对比。</p>${button('返回文献库','back-library',{primary:true})}</div>`;
  const row=(label,fn)=>`<tr><th>${label}</th>${papers.map(p=>`<td>${fn(p)}</td>`).join('')}</tr>`;
  return `<div class="inline-note">${icon('info',16)} 下表比较已记录的资源条件，不判断方法优劣或实验结果可比性。</div><div class="table-wrap"><table class="compare-table"><thead><tr><th>比较维度</th>${papers.map(p=>`<th><span class="eyebrow">${p.is_demo?'FICTIONAL DEMO':'PAPER'}</span><button data-open="${p.id}">${e(p.title)}</button></th>`).join('')}</tr></thead><tbody>
  ${row('研究方向',p=>e(p.topics.join(' / ')||'未分类'))}${row('论文版本',p=>e(p.arxiv_id||p.version_label||'未记录'))}${row('阅读状态',p=>badge(p.status,READING[p.status]))}
  ${Object.entries(KINDS).filter(([k])=>k!=='demo').map(([kind,label])=>row(label,p=>{
    const resources=p.resources.filter(r=>r.kind===kind);
    return resources.length?resources.map(r=>`<div class="compare-resource">${badge(resourceStatus(r))}<small>${e(r.label)}</small><span>${e(OWNERSHIP[r.ownership])}</span><span>${e(CLAIMS[r.claim])}</span>${r.latest?`<span>${e(timeLabel(r.latest.checked_at))}</span>`:''}</div>`).join(''):'<span class="muted">未关联，不等于不存在</span>';
  })).join('')}${row('研究笔记',p=>`<div class="compare-notes">${e(p.notes||'未记录')}</div>`)}</tbody></table></div>`;
}
function graphView(papers) {
  if(!papers.length)return `${relationIndexView(state.papers)}${emptyState(state.papers.length>0)}`;
  const visible=papers.slice(0,16), topics=[...new Set(visible.flatMap(p=>p.topics))];
  const width=1000,height=Math.max(420,visible.length*70+70);
  let lines='',nodes='';
  const ty=new Map(topics.map((t,i)=>[t,60+i*(height-100)/Math.max(1,topics.length-1)]));
  visible.forEach((p,i)=>{
    const y=55+i*70;
    for(const topic of p.topics)lines+=`<path d="M200 ${ty.get(topic)} C310 ${ty.get(topic)},290 ${y},370 ${y}" class="graph-edge"/>`;
    const res=p.resources.length;
    if(res)lines+=`<path d="M690 ${y} H815" class="graph-edge resource-edge"/>`;
    nodes+=`<g class="graph-paper" data-open="${p.id}" tabindex="0" role="button" aria-label="打开 ${e(p.title)}"><rect x="370" y="${y-22}" width="320" height="44" rx="8"/><circle cx="390" cy="${y}" r="4"/><text x="405" y="${y+4}">${e(p.title.length>32?p.title.slice(0,30)+'…':p.title)}</text></g>`;
    if(res)nodes+=`<g class="graph-resource"><rect x="815" y="${y-18}" width="130" height="36" rx="18"/><text x="880" y="${y+4}" text-anchor="middle">${res} 个关联资源</text></g>`;
  });
  topics.forEach((t,i)=>nodes+=`<g class="graph-topic"><rect x="20" y="${ty.get(t)-23}" width="180" height="46" rx="8"/><text x="110" y="${ty.get(t)+4}" text-anchor="middle">${e(t)}</text></g>`);
  return `${relationIndexView(state.papers)}<div class="graph-legend"><span><i class="topic-dot color-0"></i>研究方向</span><span><i class="topic-dot color-1"></i>论文</span><span><i class="topic-dot color-2"></i>资源汇总</span><small>最多显示当前筛选的 16 篇论文 · 点击论文查看详情</small></div><div class="graph-wrap"><svg viewBox="0 0 ${width} ${height}" role="group" aria-label="已保存的论文和方向关联">${lines}${nodes}</svg></div><p class="graph-disclaimer">图中连线只投影分类和资源关联，不表示研究结论。可追溯的 uses_method / evaluated_on / has_resource / claim 记录见上表，每条都有来源、定位与判断类别。</p>`;
}
function relationIndexView(papers) {
  const result=state.relations;
  const rows=result?.relations||[];
  const table=rows.length?`<div class="table-wrap relation-table-wrap"><table class="relation-table"><thead><tr><th>论文 / 版本</th><th>关系</th><th>对象</th><th>陈述与条件</th><th>判断类别</th><th>来源定位</th><th>操作</th></tr></thead><tbody>${rows.map(row=>`<tr><td><button class="table-title" data-open="${e(row.work_id)}">${e(row.paper_title)}</button><small>${e(row.version_label||'版本未指定')}</small></td><td>${e(RELATION_LABELS[row.relation_type]||row.relation_type)}</td><td>${e(row.target)}</td><td><strong>${e(row.statement)}</strong>${row.conditions?`<small>${e(row.conditions)}</small>`:''}</td><td>${e(ASSERTION_LABELS[row.assertion_kind]||row.assertion_kind)}</td><td><a href="${link(row.source_url)}" target="_blank" rel="noopener noreferrer">${e(row.locator)} ↗</a><small>${e(row.snapshot_kind)} · ${e(timeLabel(row.created_at))}</small></td><td><button class="text-btn" data-action="new-relation" data-paper="${e(row.work_id)}" data-supersedes="${e(row.id)}">追加修订</button></td></tr>`).join('')}</tbody></table></div>`:`<div class="small-empty">${result?'还没有符合筛选条件的结构化关系。':'正在读取结构化关系…'} 图中的分类线不会自动转换成 uses_method 或 claim。</div>`;
  return `<section class="knowledge-relations"><div class="relations-heading"><div><div class="eyebrow">EVIDENCE-LINKED RECORDS</div><h2>结构化关系与主张</h2><p>只显示用户明确保存的关系，不从引用、标题相似或模型记忆自动补边。claim 必须引用全文快照及定位。</p></div></div>
    <div class="relation-tools"><input id="relation-query" aria-label="搜索结构化关系" placeholder="搜索论文、对象、陈述或条件…" value="${e(state.relationQuery)}"><select id="relation-type" aria-label="关系类型"><option value="">全部关系</option>${Object.entries(RELATION_LABELS).map(([key,label])=>`<option value="${key}" ${state.relationType===key?'selected':''}>${e(label)}</option>`).join('')}</select><button class="btn" data-action="search-relations">查询</button><select id="relation-paper" aria-label="选择要添加关系的论文"><option value="">选择论文…</option>${papers.map(p=>`<option value="${e(p.id)}" ${state.relationPaperChoice===p.id?'selected':''}>${e(p.title)}</option>`).join('')}</select><button class="btn primary" data-action="new-relation">添加关系</button></div>
    <p class="relation-count">${result?`匹配 ${result.total} 条 · 本页 ${rows.length} 条`:''}</p>${table}
    ${result&&result.total>result.requested.limit?`<div class="relation-pagination"><button class="btn" data-action="relations-previous" ${result.requested.offset===0?'disabled':''}>上一页</button><span>${result.requested.offset+1}–${Math.min(result.requested.offset+rows.length,result.total)} / ${result.total}</span><button class="btn" data-action="relations-next" ${result.requested.offset+rows.length>=result.total?'disabled':''}>下一页</button></div>`:''}
    <p class="relation-footnote">人工修订会新增一条记录并可指向它修订的上一条；不会覆写原关系。外部模型输出不会直接写入本表。</p></section>`;
}
async function handleAction(action,node) {
  if(action==='add-paper')return openPaperForm();
  if(action==='add-topic')return addTopic();
  if(action==='import')return importDialog();
  if(action==='settings')return settingsDialog();
  if(action==='seed-demo')return busy(node,async()=>{await api('/demo',{method:'POST',body:{}});await refresh();toast('已载入 6 篇虚构演示论文。');});
  if(action==='clear-demo') {
    if(!confirm('仅删除所有演示论文及其关联记录，真实论文不会受影响。继续？'))return;
    return busy(node,async()=>{await api('/demo',{method:'DELETE',body:{}});await refresh();toast('演示数据已清除，真实数据已保留。');});
  }
  if(action==='reset-filter') {state.query='';state.status='';state.topic='';state.view='library';render();return;}
  if(action==='back-library') {state.view='library';state.topic='';render();return;}
  if(action==='compare-selected') {
    if(state.selected.size<2)return toast('请先勾选至少 2 篇论文。');
    state.view='compare';state.topic='';render();
  }
  if(action==='search-relations') {
    state.relationQuery=app.querySelector('#relation-query')?.value||'';
    state.relationType=app.querySelector('#relation-type')?.value||'';
    state.relationOffset=0;
    return loadRelations().catch(error=>toast(error.message,true));
  }
  if(action==='relations-next') {
    state.relationOffset+=50;
    return loadRelations().catch(error=>toast(error.message,true));
  }
  if(action==='relations-previous') {
    state.relationOffset=Math.max(0,state.relationOffset-50);
    return loadRelations().catch(error=>toast(error.message,true));
  }
  if(action==='new-relation') {
    const selection=app.querySelector('#relation-paper')?.value||state.relationPaperChoice;
    const paperId=node.dataset.paper||(state.papers.some(item=>item.id===selection)?selection:'');
    if(!paperId)return toast('先选择一篇论文。',true);
    return openRelationForm(paperId,node.dataset.supersedes||'');
  }
}

function field(label,name,value='',{type='text',required=false,placeholder='',wide=false,rows=0}={}) {
  return `<label class="field ${wide?'span-2':''}"><span>${e(label)}${required?'<b> *</b>':''}</span>${rows?`<textarea name="${name}" rows="${rows}" placeholder="${e(placeholder)}" ${required?'required':''}>${e(value)}</textarea>`:`<input name="${name}" type="${type}" value="${e(value)}" placeholder="${e(placeholder)}" ${required?'required':''}>`}</label>`;
}
function selectField(label,name,values,current='') {
  return `<label class="field"><span>${e(label)}</span><select name="${name}">${Object.entries(values).map(([k,v])=>`<option value="${e(k)}" ${k===current?'selected':''}>${e(v)}</option>`).join('')}</select></label>`;
}
function formError(form,message) {form.querySelector('.form-error').textContent=message;}
function openPaperForm(existing=null) {
  const initial=existing?paperInput(existing):{title:'',authors:[],topics:[],status:'inbox',notes:''};
  const dialog=openDialog(`${modalHead(existing?'EDIT PAPER':'ADD TO YOUR LIBRARY',existing?'编辑论文':'添加一篇论文','从一个准确的条目开始，笔记和证据会逐渐积累。')}
  ${!existing?`<div class="metadata-box"><label for="identifier">通过 DOI / arXiv 填充元数据</label><div><input id="identifier" placeholder="例如：1706.03762 或 10.xxxx/xxxxx"><button type="button" class="btn" id="resolve">获取元数据 ${icon('arrow',15)}</button></div><small>仅发送该标识符到 arXiv 或 Crossref；失败时仍可手动录入。</small></div>`:''}
  <form id="paper-form"><div class="form-grid">${field('论文标题','title',initial.title,{required:true,wide:true,placeholder:'输入完整论文标题'})}${field('作者（用分号分隔）','authors',(initial.authors||[]).join('; '),{wide:true})}${field('发表年份','year',initial.year||'',{type:'number',placeholder:'2026'})}${field('会议 / 期刊','venue',initial.venue||'')}${field('arXiv ID（可带版本号）','arxiv_id',initial.arxiv_id||'')}${field('DOI','doi',initial.doi||'')}${field('论文链接','paper_url',initial.paper_url||'',{type:'url',wide:true,placeholder:'https://...'})}${selectField('阅读状态','status',READING,initial.status)}${field('论文版本标签','version_label',initial.version_label||'',{placeholder:'如 v2 / camera-ready'})}
  <fieldset class="topic-picker span-2"><legend>研究方向（可多选）</legend>${state.topics.map(t=>`<label><input type="checkbox" name="topics" value="${e(t)}" ${(initial.topics||[]).includes(t)?'checked':''}>${e(t)}</label>`).join('')}</fieldset>${field('摘要','abstract',initial.abstract||'',{rows:3,wide:true})}${field('研究笔记','notes',initial.notes||'',{rows:3,wide:true,placeholder:'关注的问题、实验条件、下一步阅读计划…'})}</div>
  <div class="form-error" role="alert"></div><div class="modal-actions"><button class="btn" type="button" data-close>取消</button><button class="btn primary" type="submit">${existing?'保存修改':'加入文献库'} ${icon('arrow',16)}</button></div></form>`,{wide:true});
  const form=dialog.querySelector('#paper-form');
  if(!existing)dialog.querySelector('#resolve').addEventListener('click',event=>busy(event.currentTarget,async()=>{
    const identifier=dialog.querySelector('#identifier').value.trim();
    if(!identifier)throw new Error('请输入 DOI 或 arXiv ID。');
    const data=await api('/metadata/resolve',{method:'POST',body:{identifier}});
    for(const [key,value] of Object.entries(data)) {
      if(['topics','status','notes'].includes(key))continue;
      const control=form.elements.namedItem(key);
      if(control && 'value' in control)control.value=key==='authors'?value.join('; '):(value??'');
    }
    toast('已填充元数据。请核对后保存，尚未自动加入文献库。');
  }));
  form.addEventListener('submit',async event=>{
    event.preventDefault();formError(form,'');
    const data=new FormData(form);
    const payload={...initial,title:String(data.get('title')).trim(),authors:String(data.get('authors')).split(/[;；\n]/).map(x=>x.trim()).filter(Boolean),
      year:data.get('year')?Number(data.get('year')):null,topics:data.getAll('topics'),status:data.get('status')};
    for(const key of ['venue','arxiv_id','doi','paper_url','version_label','abstract','notes'])payload[key]=String(data.get(key)||'').trim();
    const submit=form.querySelector('[type=submit]');submit.disabled=true;
    try {const paper=await api(existing?`/papers/${existing.id}`:'/papers',{method:existing?'PUT':'POST',body:payload});dialog.close();await refresh();toast(existing?'修改已保存。':'论文已加入文献库。');if(!existing)openPaper(paper.id);else{document.querySelectorAll('dialog.drawer').forEach(d=>d.close());openPaper(existing.id);}}
    catch(error){formError(form,error.message);}finally{submit.disabled=false;}
  });
}
function addTopic() {
  const dialog=openDialog(`${modalHead('RESEARCH DIRECTION','添加研究方向','方向是可多选标签；不会将论文限制在唯一文件夹中。')}<form>${field('方向名称','name','',{required:true,placeholder:'例如：可控 Layout 生成'})}<div class="form-error" role="alert"></div><div class="modal-actions"><button class="btn" type="button" data-close>取消</button><button class="btn primary" type="submit">添加方向</button></div></form>`);
  const form=dialog.querySelector('form');
  form.addEventListener('submit',async event=>{event.preventDefault();const b=form.querySelector('[type=submit]');b.disabled=true;
    try{await api('/topics',{method:'POST',body:{name:new FormData(form).get('name')}});dialog.close();await refresh();toast('研究方向已添加。');}catch(error){formError(form,error.message);}finally{b.disabled=false;}});
}
function openPaper(id) {
  const paper=state.papers.find(p=>p.id===id);if(!paper)return;
  const dialog=openDialog(`<div class="drawer-top"><span>${icon('book',16)} PAPER WORKSPACE</span><button type="button" class="icon-btn" data-close aria-label="关闭论文详情">${icon('close')}</button></div><div class="drawer-body">
  <div class="drawer-labels">${paper.is_demo?'<span class="demo-pill">虚构演示</span>':''}${badge(paper.status,READING[paper.status])}</div><h2 class="drawer-title">${e(paper.title)}</h2><p class="drawer-authors">${e(paper.authors.join(' · ')||'作者未填写')}</p><p class="drawer-meta">${e(paper.venue||'来源未填写')} · ${paper.year||'年份待补'} ${paper.arxiv_id?' · arXiv:'+e(paper.arxiv_id):''}</p>
  <div class="topic-tags">${paper.topics.map(t=>`<span>${e(t)}</span>`).join('')}</div><div class="drawer-actions">${paper.paper_url?`<a class="btn" href="${link(paper.paper_url)}" target="_blank" rel="noopener noreferrer">打开论文 ${icon('external',14)}</a>`:''}<button class="btn" id="edit-paper">${icon('edit',15)} 编辑条目</button><button class="icon-btn danger" id="delete-paper" aria-label="删除论文">${icon('trash',16)}</button></div>
  <div class="drawer-tabs" role="tablist"><button class="active" role="tab" aria-selected="true" data-tab="resources">资源与证据 <span>${paper.resources.length}</span></button><button role="tab" aria-selected="false" data-tab="notes">摘要与笔记</button></div>
  <section id="tab-resources" role="tabpanel"><div class="resource-intro">${icon('shield',20)}<div><strong>资源存在 ≠ 可复现</strong><p>机器观察与用户声明分开记录。元数据、文件清单、运行结果是不同的验证层级。</p></div></div><div class="resource-section-head"><h3>关联资源</h3><button class="text-btn" id="add-resource">${icon('plus',16)} 添加资源</button></div>
  ${paper.resources.length?paper.resources.map(resourcePanel).join(''):'<div class="small-empty">尚未关联资源。添加 GitHub / Hugging Face 链接后，可手动触发一次静态核验。<br>没有关联资源，不代表作者没有开放。</div>'}
  <p class="drawer-footnote">首版不会自动搜索整个互联网，也不会运行外部仓库代码。核验仅在你点击后执行。</p></section>
  <section id="tab-notes" role="tabpanel" hidden><h3>摘要</h3><p class="long-text">${e(paper.abstract||'还没有摘要。')}</p><div class="resource-section-head"><h3>研究笔记</h3><button class="text-btn" id="edit-notes">${icon('edit',15)} 编辑</button></div><p class="long-text notes-block">${e(paper.notes||'记录方法差异、实验假设和下一步计划。')}</p><dl class="metadata-list"><dt>DOI</dt><dd>${e(paper.doi||'未填写')}</dd><dt>论文版本</dt><dd>${e(paper.version_label||paper.arxiv_id||'未填写')}</dd><dt>添加时间</dt><dd>${e(timeLabel(paper.created_at))}</dd><dt>更新时间</dt><dd>${e(timeLabel(paper.updated_at))}</dd></dl></section></div>`,{drawer:true});
  dialog.querySelector('#edit-paper').onclick=()=>openPaperForm(paper);
  dialog.querySelector('#edit-notes').onclick=()=>openPaperForm(paper);
  dialog.querySelector('#delete-paper').onclick=event=>{
    if(!confirm('删除这篇论文及其资源、笔记和全部核验历史？此操作不可撤销。'))return;
    busy(event.currentTarget,async()=>{await api(`/papers/${paper.id}`,{method:'DELETE',body:{}});dialog.close();await refresh();toast('论文已删除。');});
  };
  dialog.querySelector('#add-resource').onclick=()=>openResourceForm(paper);
  dialog.querySelectorAll('[data-tab]').forEach(node=>node.onclick=()=>{
    dialog.querySelectorAll('[data-tab]').forEach(n=>{n.classList.toggle('active',n===node);n.setAttribute('aria-selected',String(n===node));});
    ['resources','notes'].forEach(tab=>dialog.querySelector('#tab-'+tab).hidden=tab!==node.dataset.tab);
  });
  dialog.querySelectorAll('[data-check]').forEach(node=>{
    if(paper.is_demo){node.disabled=true;node.title='演示数据不执行真实网络请求';return;}
    node.onclick=()=>busy(node,async()=>{const result=await api(`/resources/${node.dataset.check}/check`,{method:'POST',body:{}});await refresh();dialog.close();openPaper(id);toast(result.cached?'复用 60 秒内的检查记录，未重复请求。':'本次检查已记录，可展开查看证据与限制。');});
  });
  dialog.querySelectorAll('[data-evidence]').forEach(node=>node.onclick=()=>busy(node,()=>openEvidence(paper,paper.resources.find(r=>r.id===node.dataset.evidence))));
  dialog.querySelectorAll('[data-delete-resource]').forEach(node=>node.onclick=()=>{
    if(!confirm('删除这个资源链接及其全部核验历史？论文和其他资源不会受影响。'))return;
    busy(node,async()=>{await api(`/resources/${node.dataset.deleteResource}`,{method:'DELETE',body:{}});await refresh();dialog.close();openPaper(id);toast('资源已删除。');});
  });
}
function resourcePanel(resource) {
  const observation=resource.latest,status=resourceStatus(resource);
  return `<article class="resource-panel"><div class="resource-heading"><span class="resource-kind-icon">${icon(kindIcon[resource.kind],19)}</span><div><strong>${e(resource.label)}</strong><small>${KINDS[resource.kind]} · ${e(OWNERSHIP[resource.ownership])}</small></div>${badge(status)}</div><a class="resource-url" href="${link(resource.url)}" target="_blank" rel="noopener noreferrer">${e(resource.url)} ${icon('external',12)}</a>
  <div class="resource-claims"><span>${e(CLAIMS[resource.claim])}</span>${resource.applicable_version?`<span>标记版本：${e(resource.applicable_version)}</span>`:''}</div>
  ${resource.ownership_evidence||resource.claim_evidence?`<details class="assertion-details"><summary>用户记录的声明与依据（非机器确认）</summary><p>${e(resource.ownership_evidence||'未记录归属依据')}</p><p>${e(resource.claim_evidence||'未记录发布声明依据')}</p></details>`:''}
  <p class="resource-summary">${e(observation?.summary||'这个链接尚未经过核验。点击下方按钮读取允许提供商的公开元数据。')}</p>
  <div class="resource-panel-bottom"><span>${icon('clock',13)} ${e(timeLabel(observation?.checked_at))}</span><div><button class="icon-btn danger" data-delete-resource="${resource.id}" aria-label="删除资源 ${e(resource.label)}">${icon('trash',14)}</button><button class="text-btn" data-evidence="${resource.id}" ${!observation?'disabled':''}>证据与历史</button><button class="btn btn-small" data-check="${resource.id}">${icon('refresh',14)} ${observation?'重新核验':'核验资源'}</button></div></div></article>`;
}
function openResourceForm(paper,candidate=null) {
  const dialog=openDialog(`${modalHead('RESOURCE LINK','关联一个研究资源','自动核验支持 GitHub 仓库首页、Hugging Face 模型或数据集首页。')}
  ${candidate?'<div class="inline-note">'+icon('info',16)+' 这是 README 中的候选链接，可能属于基线或依赖。请先确认它与本论文的关联。</div>':''}
  <form><div class="form-grid">${selectField('资源类型','kind',KINDS,candidate?.kind||'code')}${field('显示名称','label',candidate?KINDS[candidate.kind]+'候选':'',{required:true,placeholder:'如：作者提供的代码仓库'})}${field('资源链接','url',candidate?.url||'',{type:'url',required:true,wide:true,placeholder:'https://github.com/owner/repository'})}${selectField('资源归属（用户判断）','ownership',OWNERSHIP,'unconfirmed')}${selectField('作者发布声明（用户记录）','claim',CLAIMS,'undeclared')}${field('归属判断依据','ownership_evidence','',{rows:2,wide:true,placeholder:'标记官方或第三方时必填：来源链接、原文或论文页码'})}${field('发布声明依据','claim_evidence','',{rows:2,wide:true,placeholder:'记录计划发布/已发布时必填：作者声明原文与来源'})}${field('对应的论文 / 模型版本','applicable_version',paper.arxiv_id||paper.version_label||'',{wide:true,placeholder:'由你记录；自动检查不会确认版本是否匹配'})}</div>
  ${candidate?`<div class="candidate-source"><strong>发现来源</strong><a href="${link(candidate.source_url)}" target="_blank" rel="noopener noreferrer">${e(candidate.source_url)}</a><p>${e(candidate.excerpt)}</p></div>`:''}<div class="form-error" role="alert"></div><div class="modal-actions"><button class="btn" type="button" data-close>取消</button><button class="btn primary" type="submit">保存资源链接</button></div></form>`,{wide:true});
  const form=dialog.querySelector('form');
  form.addEventListener('submit',async event=>{event.preventDefault();formError(form,'');const submit=form.querySelector('[type=submit]');submit.disabled=true;
    try{const data=Object.fromEntries(new FormData(form));await api(`/papers/${paper.id}/resources`,{method:'POST',body:data});dialog.close();document.querySelectorAll('dialog.drawer').forEach(d=>d.close());await refresh();openPaper(paper.id);toast('资源链接已保存。归属仍以你记录的依据为准。');}catch(error){formError(form,error.message);}finally{submit.disabled=false;}});
}
async function openEvidence(paper,resource) {
  const history=await api(`/resources/${resource.id}/observations`);
  if(!history.length)return toast('这个资源尚无检查记录。');
  const dialog=openDialog(`${modalHead('EVIDENCE LOG',resource.label,'每次检查单独保存，保留当时的版本、范围和不确定性。')}<label class="history-select">检查记录<select id="history-select">${history.map((h,i)=>`<option value="${i}">${i===0?'最近一次 · ':''}${e(timeLabel(h.checked_at))} · ${e(ACCESS[h.status])} · #${h.id}</option>`).join('')}</select></label><div id="evidence-content"></div>`,{wide:true});
  function show(index) {
    const observation=history[index],previous=history[index+1];
    const audit=auditForConfirmation(observation);
    dialog.querySelector('#evidence-content').innerHTML=`<div class="observation-summary">${badge(observation.status)}<p>${e(observation.summary)}</p>${previous?`<small>相较上次：${e(ACCESS[previous.status])} → ${e(ACCESS[observation.status])}。状态相同也不代表文件内容未变化。</small>`:''}</div>
    <div class="record-provenance"><span>记录类型：<b>${observation.record_kind==='confirmation'?'人工复核':'来源观察'}</b>${observation.record_origin?` · 来源 ${e(observation.record_origin)}`:''}</span>${audit&&!paper.is_demo?'<button type="button" class="btn btn-small" id="revise-audit">记录人工复核</button>':''}</div>
    <dl class="metadata-list"><dt>核验提供商</dt><dd>${e(observation.provider)}</dd><dt>检查范围</dt><dd>${e(observation.scope)}</dd><dt>验证深度</dt><dd>${e({metadata_only:'元数据',file_listing:'文件清单（非运行验证）',not_verified:'未完成验证',demo:'虚构演示'}[observation.depth]||observation.depth)}</dd><dt>资源版本</dt><dd class="mono">${e(observation.revision||'未取得')}</dd><dt>论文版本快照</dt><dd>${e(typeof observation.paper_version_snapshot==='object'?JSON.stringify(observation.paper_version_snapshot):observation.paper_version_snapshot||'未记录')}</dd><dt>许可证声明</dt><dd>${e(observation.license_id||'未取得；不代表无许可证')}<small>仅记录提供商声明，不判断适用权限。</small></dd></dl>
    ${Object.values(observation.indicators||{}).some(v=>v.length)?`<h3 class="evidence-section-title">候选文件线索 <small>仅按文件名识别</small></h3><div class="indicator-grid">${Object.entries(observation.indicators).filter(([,v])=>v.length).map(([key,files])=>`<div><strong>${e(INDICATORS[key]||key)}</strong>${files.map(f=>`<code>${e(f)}</code>`).join('')}</div>`).join('')}</div>`:''}
    <div class="limitations"><strong>${icon('info',15)} 本次检查的边界</strong>${(observation.limitations||[]).map(text=>`<p>${e(text)}</p>`).join('')}</div>
    <h3 class="evidence-section-title">来源与证据 <span>${observation.evidence.length}</span></h3><div class="evidence-list">${observation.evidence.map((item,i)=>`<article><span class="evidence-number">${String(i+1).padStart(2,'0')}</span><div><strong>${e(item.locator||item.category)}</strong><pre>${e(item.excerpt)}</pre><a href="${link(item.source_url)}" target="_blank" rel="noopener noreferrer">查看来源 ${icon('external',12)}</a></div></article>`).join('')}</div>
    ${observation.discovered?.length?`<h3 class="evidence-section-title">README 中的候选资源</h3><p class="muted">尚未跟随链接或确认与论文的关联；可能是依赖或其他工作。</p>${observation.discovered.map((c,i)=>`<div class="discovered-row"><div><strong>${e(KINDS[c.kind])}候选</strong><a href="${link(c.url)}" target="_blank" rel="noopener noreferrer">${e(c.url)}</a></div><button class="btn btn-small" data-candidate="${i}">检查后添加 ${icon('plus',13)}</button></div>`).join('')}`:''}
    ${observation.content_sha256?`<details class="hash-details"><summary>响应集合指纹（SHA-256）</summary><code>${e(observation.content_sha256)}</code><p>由本次成功读取的 API 响应指纹生成；不是文件完整性或复现成功证明。</p></details>`:''}`;
    dialog.querySelectorAll('[data-candidate]').forEach(node=>node.onclick=()=>{const candidate=observation.discovered[Number(node.dataset.candidate)];dialog.close();openResourceForm(paper,candidate);});
    const revise=dialog.querySelector('#revise-audit');
    if(revise)revise.onclick=()=>openAuditConfirmation(paper,resource,observation,dialog);
  }
  show(0);dialog.querySelector('#history-select').onchange=event=>show(Number(event.target.value));
}

const RESOURCE_AUDIT_FIELDS=['paper_title','work_identifier','work_version','resource_url','resource_type','candidate_origin',
  'attribution','attribution_evidence','author_declaration','author_declaration_evidence','status','provider_status',
  'provider','summary','access','scope','revision','checked_at','verification_depth','coverage','evidence','licences',
  'version_match','version_evidence','limitations'];
function auditForConfirmation(observation) {
  const source=observation?.resource_audit||observation;
  if(!source?.paper_title||!source?.resource_url)return null;
  return Object.fromEntries(RESOURCE_AUDIT_FIELDS.filter(key=>source[key]!==undefined).map(key=>[key,source[key]]));
}
function firstEvidence(rows) { return Array.isArray(rows)&&rows.length?rows[0]:{}; }
function reviewSourceFields(prefix,label,item={}) {
  return `<div class="form-grid"><label class="field span-2"><span>${e(label)}来源 URL</span><input type="url" name="${prefix}_source_url" value="${e(item.source_url||'')}" placeholder="https://..." autocomplete="url"></label><label class="field span-2"><span>来源摘录 / 定位</span><textarea name="${prefix}_excerpt" rows="2" maxlength="8000" placeholder="记录支持这项判断的原文或可定位说明">${e(item.excerpt||'')}</textarea></label></div>`;
}
function openAuditConfirmation(paper,resource,observation,historyDialog) {
  const original=auditForConfirmation(observation);
  if(!original)return toast('这条记录没有字段级审计数据，无法生成结构化复核。',true);
  const attributionEvidence=firstEvidence(original.attribution_evidence);
  const declarationEvidence=firstEvidence(original.author_declaration_evidence);
  const versionEvidence={source_url:'',excerpt:original.version_evidence||''};
  const dialog=openDialog(`${modalHead('HUMAN REVIEW','记录一次人工复核','复核结果会作为新记录追加；原始观察和此前判断都保留。')}
    <div class="inline-note">请只填写你亲自检查过的依据。资源可访问、作者声明和官方归属是不同判断；把“未确认”保留为当前结论也是有效复核。</div>
    <form id="audit-confirmation-form"><div class="form-grid">
      ${selectField('资源归属判断','attribution',OWNERSHIP,original.attribution||'unconfirmed')}
      ${selectField('作者发布声明','author_declaration',CLAIMS,original.author_declaration||'undeclared')}
      ${selectField('论文与资源版本对应','version_match',{unknown:'未知',matched:'已确认对应',mismatched:'不对应'},original.version_match||'unknown')}
      <div class="field span-2"><span>固定对象</span><input value="${e(resource.url)}" readonly></div>
      <div class="field span-2"><span>论文</span><input value="${e(paper.title)}" readonly></div>
      <div class="field span-2"><span>归属判断依据</span>${reviewSourceFields('attribution','归属判断',attributionEvidence)}</div>
      <div class="field span-2"><span>发布声明依据</span>${reviewSourceFields('declaration','发布声明',declarationEvidence)}</div>
      <div class="field span-2"><span>版本对应依据</span>${reviewSourceFields('version','版本对应',versionEvidence)}</div>
      <label class="field span-2"><span>人工复核说明 <b>*</b></span><textarea name="review_note" rows="3" maxlength="1000" required placeholder="说明本次复核了什么、仍有哪些限制"></textarea></label>
    </div><div class="form-error" role="alert"></div><div class="modal-actions"><button type="button" class="btn" data-close>取消</button><button type="submit" class="btn primary">保存人工复核</button></div></form>`,{wide:true});
  const form=dialog.querySelector('#audit-confirmation-form');
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    formError(form,'');
    const submit=form.querySelector('[type=submit]');
    submit.disabled=true;
    try {
      const data=new FormData(form);
      const values=Object.fromEntries(data);
      const evidence=(prefix,oldRows,value,oldValue,affirmative,fieldName)=>{
        const url=String(values[`${prefix}_source_url`]||'').trim();
        const excerpt=String(values[`${prefix}_excerpt`]||'').trim();
        let rows=Array.isArray(oldRows)?[...oldRows]:[];
        if(url||excerpt){
          if(!url||!excerpt)throw new Error(`${fieldName}的来源 URL 和摘录需要同时填写`);
          if(link(url)==='#')throw new Error(`${fieldName}来源必须是无凭据的 http(s) 链接`);
          rows.push({source_url:url,locator:'用户人工复核',excerpt,category:'human_confirmation'});
        }else if(value!==oldValue&&affirmative){
          throw new Error(`更改为肯定的${fieldName}时，请补充本次复核依据`);
        }
        if(!affirmative)return [];
        if(!rows.length)throw new Error(`${fieldName}需要可跳转的来源依据`);
        return rows;
      };
      const attribution=values.attribution;
      const authorDeclaration=values.author_declaration;
      const versionMatch=values.version_match;
      const reviewed={...original,checked_at:new Date().toISOString(),summary:String(values.review_note).trim(),
        attribution,author_declaration:authorDeclaration,version_match:versionMatch,
        attribution_evidence:evidence('attribution',original.attribution_evidence,attribution,original.attribution,
          attribution!=='unconfirmed','资源归属'),
        author_declaration_evidence:evidence('declaration',original.author_declaration_evidence,authorDeclaration,
          original.author_declaration,authorDeclaration!=='undeclared','作者发布声明')};
      const versionUrl=String(values.version_source_url||'').trim();
      const versionExcerpt=String(values.version_excerpt||'').trim();
      if(versionUrl||versionExcerpt){
        if(!versionUrl||!versionExcerpt)throw new Error('版本对应的来源 URL 和摘录需要同时填写');
        if(link(versionUrl)==='#')throw new Error('版本对应来源必须是无凭据的 http(s) 链接');
        reviewed.version_evidence=`${versionExcerpt} (${versionUrl})`;
      }else if(versionMatch!==original.version_match&&versionMatch!=='unknown'){
        throw new Error('更改论文与资源版本的对应关系时，请补充本次复核依据');
      }
      if(versionMatch==='unknown')reviewed.version_evidence='';
      await api(`/resources/${resource.id}/confirmations`,{method:'POST',body:reviewed});
      dialog.close();historyDialog.close();
      document.querySelectorAll('dialog.drawer').forEach(node=>node.close());
      await refresh();
      const updatedPaper=state.papers.find(item=>item.id===paper.id);
      const updatedResource=updatedPaper?.resources.find(item=>item.id===resource.id);
      if(updatedPaper&&updatedResource){openPaper(updatedPaper.id);await openEvidence(updatedPaper,updatedResource);}
      toast('人工复核已追加为独立记录；原始来源观察仍保留。');
    }catch(error){formError(form,error.message);}finally{if(submit.isConnected)submit.disabled=false;}
  });
}
async function openRelationForm(paperId,supersedesId='') {
  const bundle=await api(`/papers/${paperId}/knowledge`);
  const workspaceData=await api('/workspaces');
  const paper=state.papers.find(item=>item.id===paperId);
  if(!paper)return toast('论文不在当前工作区。',true);
  const sources=bundle.source_snapshots.filter(item=>item.source_url);
  const fulltextGroups=new Map();
  for(const workspace of workspaceData.workspaces||[]) {
    if(!workspace.available)continue;
    for(const source of workspace.sources||[]) {
      const fulltext=source.fulltext;
      if(source.kind!=='fulltext_chunk'||!fulltext?.identifier||!source.source_url)continue;
      const key=[source.source_url,fulltext.source,fulltext.identifier,fulltext.version].join('|');
      if(!fulltextGroups.has(key))fulltextGroups.set(key,{workspace_id:workspace.workspace_id,
        fulltext,source_ids:[]});
      fulltextGroups.get(key).source_ids.push(source.source_id);
    }
  }
  const fulltextDocuments=[...fulltextGroups.values()];
  const previousRelation=bundle.relations.find(item=>item.id===supersedesId);
  const selectedSource=previousRelation?.source_snapshot_id||'';
  const fulltext=sources.some(item=>['fulltext','fulltext_chunk','fulltext_snapshot'].includes(item.snapshot_kind));
  const sourceOptions=sources.map(item=>{
    const version=bundle.versions.find(row=>row.id===item.paper_version_id);
    return `<option value="${e(item.id)}" ${item.id===selectedSource?'selected':''}>${e(item.snapshot_kind)} · ${e(version?.arxiv_id||version?.version_label||'版本未绑定')} · ${e(item.locator||'未记录定位')} · ${e(timeLabel(item.retrieved_at))} · ${e(item.source_url)}</option>`;
  }).join('');
  const selectedVersion=previousRelation?.paper_version_id||bundle.versions.find(item=>item.is_current)?.id||'';
  const versionOptions=bundle.versions.map(item=>`<option value="${e(item.id)}" ${item.id===selectedVersion?'selected':''}>${item.is_current?'当前 · ':''}${e(item.arxiv_id||item.version_label||item.doi||item.version_key)}</option>`).join('');
  const previousOptions=bundle.relations.map(item=>`<option value="${e(item.id)}" ${item.id===supersedesId?'selected':''}>${e(RELATION_LABELS[item.relation_type]||item.relation_type)} · ${e(item.target)} · ${e(timeLabel(item.created_at))}</option>`).join('');
  const fulltextOptions=fulltextDocuments.map((item,index)=>`<option value="${index}">${e(item.fulltext.identifier)} · ${e(item.fulltext.version||'版本未标明')} · ${item.source_ids.length} 个全文块 · ${e(item.fulltext.parse_quality||'unknown')}</option>`).join('');
  const dialog=openDialog(`${modalHead('EVIDENCE-LINKED RELATION','新增结构化关系','关系是可追溯的人工记录，不会由相似标题、引用或模型记忆自动生成。')}
    ${sources.length?'':'<div class="inline-note">当前没有带安全 URL 的来源快照。请先导入或核验来源，再记录关系。</div>'}
    <section class="fulltext-import-panel"><h3>导入已读全文块</h3><p>先在 Agent 工作区导入 <code>re0 paper text --workspace</code> 导出的 bundle，再选论文版本。预览会展示全文与 locator；确认后只追加来源快照，不会创建论文或自动生成 claim。</p>
      ${fulltextDocuments.length?`<div class="fulltext-import-controls"><label class="field"><span>全文文档</span><select id="fulltext-document">${fulltextOptions}</select></label><label class="field"><span>绑定论文版本</span><select id="fulltext-version">${versionOptions}</select></label><button type="button" class="btn" id="fulltext-preview">预览全文块</button></div>`:'<div class="inline-note">当前没有可导入的全文块。请先导入含 `fetch_paper_text` 结果的工作区 bundle。</div>'}
      <div id="fulltext-import-result" aria-live="polite"></div></section>
    <form id="relation-form"><div class="form-grid">
      <label class="field"><span>关系类型</span><select name="relation_type">${Object.entries(RELATION_LABELS).map(([key,label])=>`<option value="${key}" ${key===(previousRelation?.relation_type||'uses_method')?'selected':''} ${key==='claim'&&!fulltext?'disabled':''}>${e(label)}${key==='claim'&&!fulltext?'（需要全文来源）':''}</option>`).join('')}</select></label>
      ${selectField('判断类别', 'assertion_kind', ASSERTION_LABELS, previousRelation?.assertion_kind||'human_confirmation')}
      ${field('关系对象','target',previousRelation?.target||'',{required:true,placeholder:'方法、数据集、资源或主张主题'})}
      ${selectField('对应论文版本','paper_version_id',{'':'不指定版本',...Object.fromEntries(bundle.versions.map(item=>[item.id,item.arxiv_id||item.version_label||item.doi||item.version_key]))},selectedVersion)}
      <label class="field span-2"><span>陈述 <b>*</b></span><textarea name="statement" rows="3" maxlength="2000" required placeholder="写明来源实际支持的关系；区分作者原文与个人推断">${e(previousRelation?.statement||'')}</textarea></label>
      ${field('假设 / 适用条件','conditions',previousRelation?.conditions||'',{rows:2,wide:true,placeholder:'主张关系必填；其他类型用于记录比较范围和适用条件'})}
      <label class="field span-2"><span>来源快照 <b>*</b></span><select name="source_snapshot_id" ${sources.length?'required':'disabled'}>${sources.length?'<option value="">选择来源…</option>':'<option value="">没有可用来源</option>'}${sourceOptions}</select></label>
      <label class="field span-2"><span>段落 / 文件定位 <b>*</b></span><input name="locator" value="${e(previousRelation?.locator||'')}" maxlength="1000" required placeholder="例如：论文 §3.2、README 第 4 段或 train.py:20–48"></label>
      <label class="field span-2"><span>修订哪条关系（可选）</span><select name="supersedes_id"><option value="">新增关系</option>${previousOptions}</select></label>
    </div><div class="inline-note">每条关系需要同一 Work 下的来源快照。claim 目前只接受 #8 的全文快照；没有全文时不能把摘要推断写成论文主张。</div><div id="relation-source-note" class="inline-note" hidden></div>
    <div class="form-error" role="alert"></div><div class="modal-actions"><button type="button" class="btn" data-close>取消</button><button type="submit" class="btn primary" ${sources.length?'':'disabled'}>保存关系</button></div></form>`,{wide:true});
  const form=dialog.querySelector('#relation-form');
  function renderFulltextPreview(result,request) {
    const target=dialog.querySelector('#fulltext-import-result');
    const conflicts=(result.conflicts||[]).map(item=>`<li><code>${e(item.source_id||'来源')}</code>：${e(item.reason)}</li>`).join('');
    const present=(result.already_present||[]).map(item=>`<li>${e(item.locator)} · 已导入</li>`).join('');
    const chunks=(result.new||[]).map(item=>`<details class="fulltext-chunk"><summary>${e(item.locator)} · ${item.content.length} 字符 · ${e(item.fulltext.parse_quality||'unknown')}</summary><pre>${e(item.content)}</pre><a href="${link(item.source_url)}" target="_blank" rel="noopener noreferrer">打开原文来源 ↗</a></details>`).join('');
    target.innerHTML=`<div class="inline-note">导入 bundle 的来源声明未经加密认证。请核对版本、链接、解析质量与正文；确认只会追加来源块。</div>${conflicts?`<h4>无法导入</h4><ul>${conflicts}</ul>`:''}${present?`<h4>已存在</h4><ul>${present}</ul>`:''}${chunks?`<h4>待追加 ${result.new.length} 个全文块</h4>${chunks}<button type="button" class="btn primary" id="fulltext-confirm">确认追加全文块</button>`:''}`;
    target.querySelector('#fulltext-confirm')?.addEventListener('click',async event=>{
      event.currentTarget.disabled=true;
      try {
        const applied=await api(`/papers/${paperId}/knowledge/fulltext/import`,{method:'POST',body:request});
        const count=applied.new.length;
        dialog.close();toast(`已追加 ${count} 个全文来源块；来源包未被认证，也没有自动创建 claim。`);
        await openRelationForm(paperId,supersedesId);
      }catch(error){target.textContent=error.message;}
    });
  }
  dialog.querySelector('#fulltext-preview')?.addEventListener('click',async event=>{
    const previewButton=event.currentTarget;
    const document=fulltextDocuments[Number(dialog.querySelector('#fulltext-document').value)];
    if(!document)return;
    const request={workspace_id:document.workspace_id,source_ids:document.source_ids,
      paper_version_id:dialog.querySelector('#fulltext-version').value};
    previewButton.disabled=true;
    try {
      const result=await api(`/papers/${paperId}/knowledge/fulltext/preview`,{method:'POST',body:request});
      renderFulltextPreview(result,request);
    }catch(error){dialog.querySelector('#fulltext-import-result').textContent=error.message;}
    finally{if(previewButton.isConnected)previewButton.disabled=false;}
  });
  form.elements.source_snapshot_id.addEventListener('change',()=>{
    const selected=sources.find(item=>item.id===form.elements.source_snapshot_id.value);
    form.elements.paper_version_id.value=selected?.paper_version_id||'';
    const note=dialog.querySelector('#relation-source-note');
    const provenance=selected?.payload?.provenance_verified;
    note.hidden=provenance!==false;
    if(provenance===false)note.textContent='此来源来自用户导入的 bundle，工具声明未经认证；claim 只能以“人工确认”类别保存。';
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();formError(form,'');
    const submit=form.querySelector('[type=submit]');submit.disabled=true;
    try{
      const payload=Object.fromEntries(new FormData(form));
      await api(`/papers/${paperId}/relations`,{method:'POST',body:payload});
      dialog.close();state.relationOffset=0;await loadRelations();
      toast('关系已追加；来源、版本与判断类别已保留。');
    }catch(error){formError(form,error.message);}finally{if(submit.isConnected)submit.disabled=false;}
  });
}
function settingsDialog() {
  const total=counts(state.papers);
  const dialog=openDialog(`${modalHead('DATA & PRIVACY','数据与设置','这是本地单用户预览版，没有账号系统；请勿直接暴露到公网。')}
  <div class="settings-card"><div>${icon('database',22)}<strong>你的研究，留在本地</strong></div><p>${total.papers} 篇论文 · ${total.resources} 个资源。数据库默认保存在项目的 <code>.data/re0.sqlite3</code>。没有遥测，没有自动上传附件或笔记。</p></div>
  <h3>导出与备份</h3><p class="muted">JSON 导出包含笔记、用户声明和全部核验历史；BibTeX 用于引文工具。完整恢复请使用 SQLite 备份，详见 README。</p><div class="settings-actions"><a class="btn" href="/api/export" download>${icon('download')} 导出 JSON</a><a class="btn" href="/api/export?format=bibtex" download>${icon('download')} 导出 BibTeX</a></div>
  <h3>Zotero 连接</h3><p class="muted">两条路：<strong>导入 CSL JSON</strong>（离线文件，先预览再确认），或<strong>只读增量同步</strong>一个 Zotero 库（按版本游标，只读文献类型，不读取附件、批注或笔记）。远端删除只标记 tombstone，不会删掉这里的笔记、资源或核验记录。仍不支持双向写回。</p><div class="settings-actions"><button class="btn" id="settings-import">${icon('upload')} 导入 CSL JSON</button><button class="btn" id="settings-zotero">${icon('database')} 连接 Zotero 库</button></div>
  <h3>检查行为</h3><div class="settings-facts"><span>触发方式<strong>用户手动点击</strong></span><span>自动执行代码<strong>关闭 / 未实现</strong></span><span>LLM 分析<strong>未接入</strong></span><span>定时复查<strong>未启用</strong></span></div>
  <p class="muted">只有核验或元数据填充操作会请求对应的外部服务。GitHub、Hub 和文献元数据 API 可能限流；60 秒内重复核验会复用历史结果。</p>
  <h3>演示数据</h3><p class="muted">演示论文与检查结果均为虚构，可单独清除，不影响真实论文。</p><div class="settings-actions">${button('载入演示','seed-demo')}${button('清除演示','clear-demo')}</div>`,{wide:true});
  dialog.querySelector('#settings-import').onclick=()=>{dialog.close();importDialog();};
  dialog.querySelector('#settings-zotero').onclick=()=>{dialog.close();zoteroDialog();};
  dialog.querySelectorAll('[data-action]').forEach(node=>node.onclick=async()=>{await handleAction(node.dataset.action,node);dialog.close();});
}
function zoteroDialog() {
  // The key is typed here, sent once per request, and never stored — not in the database, not in
  // localStorage, not in this module's state. Closing the dialog discards it. That is the same
  // arrangement the model key uses, and it is stated in the dialog rather than assumed.
  const dialog=openDialog(`${modalHead('ZOTERO SYNC','只读增量同步一个 Zotero 库。','按版本游标读取文献条目；附件、批注与笔记一律不请求。先预览，再确认。')}
  <form id="zotero-form" class="form-grid">
    <label>库类型<select name="library_type"><option value="user">个人库 (user)</option><option value="group">群组库 (group)</option></select></label>
    <label>库 ID（数字）<input name="library_id" required inputmode="numeric" pattern="[0-9]{1,20}" placeholder="12345"></label>
    <label>API key<input name="api_key" type="password" required autocomplete="off" placeholder="只用于本次请求，不会保存"></label>
    <label>本地备注名<input name="label" maxlength="200" placeholder="例如：博士论文库"></label>
  </form>
  <div class="settings-actions"><button class="btn" id="zotero-collections">${icon('database')} 读取集合列表</button><button class="btn" id="zotero-preview">${icon('search')} 预览同步</button></div>
  <div id="zotero-scope"></div>
  <div id="zotero-result"><div class="inline-note">${icon('info',16)} 同步是<strong>只读</strong>的：这里不会写回 Zotero。远端删除只把映射标记为 tombstone，本地的笔记、资源与核验记录都保留。API key 只发往 api.zotero.org，不写入数据库、不写入日志、关闭本窗口即丢弃。</div></div>`,{wide:true});
  const form=dialog.querySelector('#zotero-form');
  const scope=dialog.querySelector('#zotero-scope');
  const result=dialog.querySelector('#zotero-result');
  const credentials=()=>({library_type:form.elements.library_type.value,
    library_id:form.elements.library_id.value.trim(),
    api_key:form.elements.api_key.value,
    label:form.elements.label.value.trim()});
  const selectedCollections=()=>[...scope.querySelectorAll('input[name=collection]:checked')].map(x=>x.value);
  dialog.querySelector('#zotero-collections').onclick=event=>busy(event.currentTarget,async()=>{
    const listed=await api('/zotero/collections',{method:'POST',body:credentials()});
    scope.innerHTML=listed.collections.length?`<h3>选择要同步的集合</h3><p class="muted">不选则同步整个库。多个集合会一起发送，不会只取第一个。</p>${listed.collections.map(row=>`<label class="check-row"><input type="checkbox" name="collection" value="${e(row.key)}"> ${e(row.name)} <code>${e(row.key)}</code></label>`).join('')}`:'<p class="muted">这个库没有集合；同步会覆盖整个库。</p>';
    toast(`读到 ${listed.collections.length} 个集合（只有名称与 key）。`);
  });
  const renderResult=body=>{
    const c=body.counts;
    const rows=[['新增',c.added],['修改',c.updated],['关联到已有条目',c.linked_existing],['无变化',c.unchanged],['远端删除（tombstone）',c.remote_deleted],['跳过',c.skipped],['未读取到的变更',c.unaccounted]];
    result.innerHTML=`<div class="import-stats">${rows.filter(([,n])=>n).map(([label,n])=>`<div><strong>${n}</strong>${e(label)}</div>`).join('')||'<div><strong>0</strong>没有变更</div>'}</div>
    <p class="muted">游标 ${e(String(body.cursor.from))} → ${e(String(body.cursor.to))}；范围：${e(body.scope.collections?.length?body.scope.collections.join('、'):'整个库')}${body.scope.tags?.length?'；标签 '+e(body.scope.tags.join('、'))+'（并集）':''}</p>
    ${body.preview.length?`<div class="import-titles">${body.preview.map(row=>`<p>${icon('book',14)} ${e(row.title)} <code>${e(row.key)}</code>${row.matched_on?' · 按 '+e(row.matched_on)+' 关联':''}</p>`).join('')}</div>`:''}
    ${body.skipped.length?`<div class="form-error">${body.skipped.slice(0,8).map(row=>'跳过 '+(row.key?e(row.key)+'：':'')+e(row.reason||row.title||'')).join('<br>')}</div>`:''}
    ${body.notes.length?`<ul class="muted">${body.notes.map(note=>`<li>${e(note)}</li>`).join('')}</ul>`:''}
    <div class="modal-actions"><button class="btn" data-close>取消</button>${body.applied?'':`<button class="btn primary" id="zotero-apply" ${c.added+c.updated+c.linked_existing+c.remote_deleted?'':'disabled'}>确认并提交</button>`}</div>
    ${body.applied?'<p class="muted">已提交：映射、条目与游标在同一个事务里写入。</p>':'<p class="muted">这是预览：文献库没有任何改动，游标没有移动。</p>'}`;
    const apply=result.querySelector('#zotero-apply');
    if(apply)apply.onclick=event=>busy(event.currentTarget,async()=>{
      const done=await api('/zotero/sync',{method:'POST',body:{...credentials(),collections:selectedCollections(),apply:true}});
      renderResult(done);dialog.querySelector('#zotero-form').elements.api_key.value='';
      await refresh();
      toast(`已提交：新增 ${done.counts.added}、修改 ${done.counts.updated}、关联 ${done.counts.linked_existing}、远端删除 ${done.counts.remote_deleted}、跳过 ${done.counts.skipped}。`);
    });
  };
  dialog.querySelector('#zotero-preview').onclick=event=>busy(event.currentTarget,async()=>{
    result.innerHTML='<p class="muted">正在读取远端库…</p>';
    renderResult(await api('/zotero/sync',{method:'POST',body:{...credentials(),collections:selectedCollections()}}));
  });
  dialog.addEventListener('close',()=>{form.elements.api_key.value='';});
}
function importDialog() {
  const dialog=openDialog(`${modalHead('IMPORT LIBRARY','带着已有积累，开始。','在 Zotero 中导出 CSL JSON，再导入这里；不读取原文 PDF 或私人批注。')}
  <div class="import-choice"><div>${icon('code',24)}<h3>Zotero · CSL JSON</h3><p>精确重复的 DOI / arXiv 会跳过；交叉标识或版本冲突先复核。同标题但标识符不同的论文会分别保留。</p></div><label class="btn">${icon('upload')} 选择 JSON 文件<input id="import-file" type="file" accept=".json,application/json" hidden></label></div><div id="import-preview"><div class="inline-note">${icon('info',16)} 支持最多 500 条、3 MiB 的 CSL JSON 数组。re0 自身导出的归档 JSON 不是 CSL 格式。</div></div>`,{wide:true});
  let items=null;
  dialog.querySelector('#import-file').onchange=async event=>{
    const file=event.target.files[0];if(!file)return;
    const preview=dialog.querySelector('#import-preview');
    preview.innerHTML='<p class="muted">正在校验导入文件…</p>';
    try{
      if(file.size>3*1024*1024)throw new Error('文件不能超过 3 MiB。');
      items=JSON.parse(await file.text());if(!Array.isArray(items))throw new Error('需要 CSL JSON 数组。请在 Zotero 中选择 CSL JSON 导出格式。');
      const result=await api('/import/csl',{method:'POST',body:{items,dry_run:true}});
      const conflictRows=result.conflicts.map(row=>`<article><strong>第 ${row.index+1} 条 · ${e(row.title)}</strong><p>${e(row.reason)}</p><ul>${row.matches.map(match=>`<li><b>${match.work_id?'现有条目':'本次文件第 '+(match.item_index+1)+' 条'}：${e(match.title)}</b><span>${[match.doi&&'DOI '+match.doi,match.arxiv_id&&'arXiv '+match.arxiv_id].filter(Boolean).map(e).join(' · ')||'无可用标识符'}</span></li>`).join('')}</ul></article>`).join('');
      const canApply=result.ready>0||result.conflicts.length>0;
      const actionLabel=result.ready&&result.conflicts.length?`导入 ${result.ready} 篇并记录 ${result.conflicts.length} 条冲突`:result.ready?`确认导入 ${result.ready} 篇`:`记录 ${result.conflicts.length} 条待复核冲突`;
      preview.innerHTML=`<div class="import-stats"><div><strong>${result.ready}</strong>可导入</div><div class="import-conflict-count"><strong>${result.conflicts.length}</strong>身份待复核</div><div><strong>${result.skipped.length}</strong>重复跳过</div><div><strong>${result.errors.length}</strong>格式错误</div></div>${result.preview.length?`<div class="import-titles">${result.preview.map(p=>`<p>${icon('book',14)} ${e(p.title)}</p>`).join('')}</div>`:''}${result.conflicts.length?`<section class="import-conflicts"><h3>身份冲突 · 不会自动合并</h3><p>确认后会把导入声明追加到匹配条目的待复核记录；不会覆盖已有元数据，也不会把这条声明当作关系证据。</p>${conflictRows}</section>`:''}${result.errors.length?`<div class="form-error">${result.errors.slice(0,5).map(x=>'第 '+(x.index+1)+' 条：'+e(x.message)).join('<br>')}</div>`:''}<p class="muted">确认后只导入校验通过的新条目并记录待复核冲突；重复项不会覆盖已有笔记。未显示全部预览时，仍会处理全部已校验条目。</p><div class="modal-actions"><button class="btn" data-close>取消</button><button class="btn primary" id="confirm-import" ${canApply?'':'disabled'}>${actionLabel}</button></div>`;
      preview.querySelector('#confirm-import').onclick=event=>busy(event.currentTarget,async()=>{const imported=await api('/import/csl',{method:'POST',body:{items,dry_run:false}});dialog.close();await refresh();const recorded=imported.conflicts.filter(row=>row.recorded_snapshots?.length).length;const pending=imported.conflicts.length-recorded;toast(`已导入 ${imported.created.length} 篇；身份待复核 ${recorded} 条${pending?`、未能关联 ${pending} 条`:''}；重复跳过 ${imported.skipped.length} 条，格式错误 ${imported.errors.length} 条。`);});
    }catch(error){preview.innerHTML=`<div class="form-error">${e(error.message)}</div>`;items=null;}
  };
}

document.addEventListener('keydown',event=>{
  if(event.key==='/' && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName) && !document.querySelector('dialog[open]')){event.preventDefault();app.querySelector('#search')?.focus();}
  if((event.key==='Enter'||event.key===' ')&&event.target.matches('.graph-paper')){event.preventDefault();openPaper(event.target.dataset.open);}
});
refresh().catch(error=>{
  app.innerHTML=`<main class="boot"><div class="brand-large">re<span>0</span></div><h1>工作台连接失败</h1><p>${e(error.message)}</p><p>请在项目目录运行 <code>python run.py</code>，再打开本地服务地址。</p><button class="btn primary" id="retry">重新连接</button></main>`;
  app.querySelector('#retry').onclick=()=>location.reload();
});
