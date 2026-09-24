/** The local retrieval workbench: one versioned result file in, four views out.
 *
 * This page does not search. The retrieval already happened — in the console, in the skill, or inside
 * a task — and what is loaded here is the JSON that run wrote. That is deliberate: the search
 * implementation stays in one place, and a page that could not reach it cannot pretend to have run it.
 *
 * Nothing is uploaded in the static demo. The file is parsed in the browser. In the local backend
 * workbench only, a reader may explicitly preview and commit a same-origin library import.
 */
import {icon} from './icons.js';
import {initTheme} from './theme.js';
import {api} from './api.js';
import {e, link} from './core.js';
import {
  ACCESS_LABELS, ATTRIBUTION_LABELS, AUDIT_COMPONENTS, AUDIT_LABELS, COMPONENT_LABELS,
  DECLARATION_LABELS, DEPTH_LABELS, SEARCH_STATE_LABELS, SORTS,
  VERSION_MATCH_LABELS, approvalItems, bibtex, candidates, commandFor, facets, filterSort, labelled,
  matrix, matrixCsv, parseResult, summary,
} from './search-core.js';

const main = document.querySelector('#workbench');
const crumb = document.querySelector('#crumb');
const notice = document.querySelector('#notice');
const staticDemo = document.body.dataset.staticDemo === 'true';
const MAX_RESULT_BYTES = 5 * 1024 * 1024;

const VIEWS = [['coverage', '检索覆盖'], ['candidates', '候选论文'], ['matrix', '资源审计矩阵'],
               ['export', staticDemo ? '本地导出' : '导出与入库']];

const state = {
  fileName: '', result: null, warning: '', error: '', view: 'load',
  filters: {query: '', year: '', source: '', publication: '', openOnly: false, sort: 'relevance'},
  selected: new Set(), openAbstracts: new Set(), openRows: new Set(),
  importReport: null, busy: false,
};

function say(message, error = false) {
  notice.textContent = message;
  notice.classList.toggle('show', Boolean(message));
  notice.classList.toggle('error', error);
  clearTimeout(say.timer);
  if (message) say.timer = setTimeout(() => notice.classList.remove('show'), error ? 9000 : 4200);
}

function rows() { return candidates(state.result); }
function visible() { return filterSort(rows(), state.filters); }

/* --------------------------------------------------------------------------------------- loading */

function loadView() {
  if (staticDemo) return `<section class="demo-start">
    <div class="eyebrow">RE0 · 交互式历史案例 <span>2026-09-22</span></div>
    <h1>从论文候选，走到可核对的资源证据。</h1>
    <p class="lede">直接体验 LoRA 的一次历史检查：查看检索覆盖、筛选论文、展开资源矩阵，再导出结果。案例由项目公开的历史记录整理；页面不会实时检索，也不会把文件上传到服务器。</p>
    <div class="demo-start-actions"><button type="button" class="button primary" data-action="load-sample">打开历史案例 →</button>
    <a class="button" href="/skill.html">阅读 Skill 核验记录</a></div>
    ${state.error ? `<div class="notice error">${e(state.error)}</div>` : ''}
    <details class="own-result"><summary>已有 Re0 结果 JSON？在浏览器中打开</summary>${localLoader()}</details>
    <p class="filter-note">历史样本仅整理已记录的观察，不代表完整检索结果；来源和检查边界见案例内说明。</p>
  </section>`;
  return `<section>
    <div class="eyebrow">LOCAL RETRIEVAL WORKBENCH<span>读一份已有的检索结果</span></div>
    <h2 style="margin-top:12px">载入 <code>re0 paper search --json</code> 写出的结果</h2>
    <p class="lede">这一页不发检索请求，也没有第二个检索实现：它读的是命令行、skill 或任务已经跑完并写下的那一份
      <b>版本化结果</b>（<code>schema_version</code>）。文件在浏览器里解析，不会上传到任何地方；唯一会发出的请求是你自己点的那次同源入库导入，而它默认只预览。</p>
    <div class="load-panel">
      <div class="row">
        <label class="file-drop">${icon('upload')} 选择结果 JSON<input type="file" id="file" accept=".json,application/json"></label>
        <span class="subtle" id="file-name">${e(state.fileName || '尚未选择文件')}</span>
      </div>
      <div class="row"><span class="subtle">或者把 JSON 粘进来：</span></div>
      <textarea id="paste" spellcheck="false" placeholder='{"schema_version":"1","coverage":{…},"documents":[…]}'></textarea>
      <div class="row" style="margin-top:12px">
        <button type="button" class="button primary" data-action="parse-paste">${icon('search')} 载入粘贴的内容</button>
        ${state.result ? '<button type="button" class="button" data-action="goto-coverage">回到已载入的结果</button>' : ''}
      </div>
      ${state.error ? `<div class="notice error" style="margin:14px 0 0">${e(state.error)}</div>` : ''}
      <pre>还没有结果文件时，先跑一次检索（检索会真实联网，用的是各服务商的公开接口）：

re0 paper search --query "layer decomposition" --start-year 2024 \\
  --find-artifacts 6 --verify 4 \\
  --json out.json --resource-matrix ./matrix

然后把 out.json 拖进来。--resource-matrix 写出的 CSV/Markdown 与这一页的矩阵是同一批行的两种渲染。</pre>
    </div>
  </section>`;
}

function localLoader() {
  return `<div class="load-panel"><div class="row"><label class="file-drop">选择结果 JSON<input type="file" id="file" accept=".json,application/json"></label>
    <span class="subtle">仅在本浏览器解析，最大 5 MB</span></div>
    <textarea id="paste" spellcheck="false" placeholder='{"schema_version":"1","coverage":{…},"documents":[…]}'></textarea>
    <button type="button" class="button" data-action="parse-paste">载入粘贴的 JSON</button></div>`;
}

function accept(text, fileName) {
  if (new Blob([text]).size > MAX_RESULT_BYTES) {
    state.result = null; state.error = '文件超过 5 MB，请缩小后再载入'; state.view = 'load';
    render(); return;
  }
  const parsed = parseResult(text);
  if (!parsed.ok) {
    state.result = null; state.error = parsed.error; state.warning = '';
    state.view = 'load'; state.fileName = fileName;
    render();
    return;
  }
  state.result = parsed.result;
  state.warning = parsed.warning || '';
  state.error = '';
  state.fileName = fileName;
  state.selected = new Set();
  state.openAbstracts = new Set();
  state.openRows = new Set();
  state.importReport = null;
  state.view = 'coverage';
  const info = summary(parsed.result);
  crumb.textContent = `${fileName} · ${info.documents} 篇候选`;
  render();
  say(`已载入 ${fileName}：${info.documents} 篇候选，${info.sources.length} 个来源`
      + (info.failures.length ? `，其中 ${info.failures.length} 个来源失败` : ''),
      Boolean(info.failures.length));
}

/* -------------------------------------------------------------------------------------- coverage */

function fact(label, value, extra = '', tone = '') {
  return `<div class="fact ${tone}"><div class="k">${e(label)}</div>
    <div class="v">${e(String(value))}${extra ? `<small> ${e(extra)}</small>` : ''}</div></div>`;
}

function coverageView() {
  const info = summary(state.result);
  const audit = info.audit;
  const command = commandFor(state.result);
  const facts = [
    fact('候选论文', info.documents, '篇'),
    fact('来源', info.sources.length, `个已查询${info.incomplete ? ' · 结果不完整' : ''}`,
         info.failures.length ? 'bad' : 'good'),
    fact('重复合并', info.duplicates_merged ?? 0, '条'),
    fact('年份窗口外', info.dropped_out_of_range ?? 0, '条被排除'),
  ];
  if (info.requested) {
    facts.push(fact('检索范围', `${info.requested.start_year ?? '不限'}–${info.requested.end_year ?? '今年'}`,
      `每源上限 ${info.requested.limit ?? '?'} 条`));
  } else {
    facts.push(fact('检索范围', '结果未记录', '旧版本写出的文件里没有 coverage.requested', 'bad'));
  }
  if (info.state) facts.push(fact('本次运行', info.state, `${info.attempts.length} 次（检索式 × 来源）尝试`,
    info.state === 'ok' ? 'good' : 'warn'));
  if (audit) {
    facts.push(fact('资源候选', audit.candidates.found ?? 0,
      `个 · 已核验 ${audit.candidates.verified ?? 0}／未核验 ${audit.candidates.unchecked ?? 0}`,
      (audit.candidates.unchecked ?? 0) ? 'warn' : ''));
    facts.push(fact('名称检索预算', audit.name_search.spent ?? 0,
      `／${audit.name_search.budget ?? 0} 次，剩 ${audit.name_search.remaining ?? 0}`));
  }
  if (info.truncation) {
    facts.push(fact('正文被截断', info.truncation.bodies_excerpted ?? 0,
      `篇 · 摘要上限 ${info.truncation.excerpt_chars} 字符`,
      (info.truncation.bodies_excerpted ?? 0) ? 'warn' : ''));
  }

  const sources = info.sources.map(item => `<li>
      <span class="name">${e(item.name)}</span>
      ${item.failed
        ? `<span class="chip bad">失败</span><span class="err">${e(item.error || '未给出原因')}</span>`
        : `<span class="n">${e(String(item.count))} 条</span>`}
    </li>`).join('');

  const states = audit ? Object.entries(audit.name_search.states)
    .map(([name, count]) => `<li><span class="name">${labelled(SEARCH_STATE_LABELS, name)}</span>
      <span class="n">${e(String(count))} 篇</span></li>`).join('') : '';

  return `<section>
    ${staticDemo ? `<div class="demo-context"><b>${state.fileName === '历史案例' ? '历史案例 · 2026-09-22' : '本地导入 · 未经页面核验'}</b><span>这是已有结果的浏览器视图，不会实时检索或保存到云端。</span><button type="button" class="quiet" data-action="back-to-load">切换结果</button></div>` : ''}
    <div class="eyebrow">检索覆盖<span>哪些来源应答了，哪些没有；分母是什么</span></div>
    ${state.warning ? `<div class="notice warn">${e(state.warning)}</div>` : ''}
    ${info.queries.length ? `<p class="lede" style="margin-top:12px"><b>检索式：</b>${
      info.queries.map(item => `<span class="chip">${e(item)}</span>`).join(' ')}</p>` : ''}
    ${info.scope ? `<p class="lede"><b>范围：</b>${e(info.scope)}</p>` : ''}
    <div class="fact-grid">${facts.join('')}</div>
    <h2>来源<span class="count">失败的来源不是"没有这篇论文"</span></h2>
    <ul class="source-list">${sources || '<li><span class="subtle">结果里没有来源列表</span></li>'}</ul>
    ${info.failures.length ? `<div class="notice warn" style="margin-top:12px">
      有 ${info.failures.length} 个来源本次没有被检索到（限流、超时或凭据缺失）。
      这些来源里的记录<b>不在</b>下面的候选里，而"不在候选里"不等于"不存在"。</div>` : ''}
    ${states ? `<h2 style="margin-top:22px">名称检索<span class="count">分母是本次结果里的 ${
      audit.name_search.denominator} 篇</span></h2>
      <ul class="source-list">${states}</ul>
      <p class="filter-note" style="margin-top:10px">${e(audit.name_search.note || '')}</p>` : ''}
    ${audit && (audit.failures || []).length ? `<h2 style="margin-top:22px">审计过程中的失败</h2>
      <ul class="plain-list">${audit.failures.map(item => `<li><span class="err">${e(item)}</span></li>`).join('')}</ul>` : ''}
    ${info.note ? `<p class="lede" style="margin-top:18px"><b>结果自述：</b>${e(info.note)}</p>` : ''}
    ${unrecognised(info)}
    ${staticDemo ? `<details class="own-result"><summary>复现说明与开发者命令</summary>${replayDetails(command)}</details>` : `
    <h2 style="margin-top:24px">复现这次检索</h2>
    <p class="lede">${command.unknowns.length
      ? `结果里没有记录${command.unknowns.map(item => `<b>${e(item)}</b>`).join('、')}，所以那几项写成占位符而不是猜一个值：一条看起来能跑、实际检索范围不同的命令，比一条承认自己不完整的命令更糟。`
      : '这些参数都来自结果文件自己记录的 <code>coverage.requested</code>，不是猜的。'}</p>
    <div class="load-panel"><pre id="command">${e(command.command)}</pre>
      <div class="row" style="margin-top:12px"><button type="button" class="button" data-action="copy-command">${icon('note')} 复制命令</button></div></div>`}
  </section>`;
}

function replayDetails(command) {
  return `<p class="lede">历史样本由公开记录整理，原始运行参数没有完整保存。下面的命令含占位符，不能视为原始命令。</p><pre>${e(command.command)}</pre>`;
}

function unrecognised(info) {
  const names = new Set(Object.keys(info.unrecognised || {}));
  rows().forEach(row => Object.keys(row.unrecognised || {}).forEach(name => names.add(`documents[].${name}`)));
  if (!names.size) return '';
  return `<div class="notice warn" style="margin-top:18px">这份结果带有本页面版本不认识的字段：${
    [...names].sort().map(name => `<code>${e(name)}</code>`).join('、')}。
    它们没有被丢掉，原样保留在 JSON 里；页面只是不知道该怎么显示。</div>`;
}

/* ------------------------------------------------------------------------------------ candidates */

function candidatesView() {
  const all = rows();
  const info = facets(all);
  const shown = visible();
  const option = (value, label, current) =>
    `<option value="${e(value)}" ${String(current) === String(value) ? 'selected' : ''}>${e(label)}</option>`;
  return `<section>
    <div class="eyebrow">候选论文<span>检索返回的是候选，不是排名；判断是读者的</span></div>
    <div class="filter-bar">
      <input type="search" id="f-query" placeholder="在标题、作者、机构、摘要、标识符里找…" value="${e(state.filters.query)}">
      <select id="f-year">${option('', `全部年份（${info.years.length}）`, state.filters.year)}${
        info.years.map(year => option(year, String(year), state.filters.year)).join('')}</select>
      <select id="f-source">${option('', '全部来源', state.filters.source)}${
        info.sources.map(name => option(name, name, state.filters.source)).join('')}</select>
      <select id="f-publication">${option('', '全部发表状态', state.filters.publication)}${
        info.publications.map(item => option(item.value, item.label, state.filters.publication)).join('')}</select>
      <label><input type="checkbox" id="f-open" ${state.filters.openOnly ? 'checked' : ''}> 只看有可公开获取资源的</label>
      <span class="spacer"></span>
      <label>排序<select id="f-sort">${
        Object.entries(SORTS).map(([key, label]) => option(key, label, state.filters.sort)).join('')}</select></label>
    </div>
    <p class="filter-note" id="candidate-count"></p>
    <div id="candidate-list"></div>
  </section>`;
}

function candidateCount(shown, all) {
  const hidden = all - shown;
  return `显示 ${shown} 篇，共 ${all} 篇`
    + (hidden ? `；${hidden} 篇被筛选条件挡住了（筛选只改变这一页显示什么，不改变检索结果本身）` : '')
    + (state.selected.size ? `；已勾选 ${state.selected.size} 篇用于 BibTeX` : '');
}

function candidateCard(row) {
  const open = state.openAbstracts.has(row.key);
  const publication = `${row.publication_label}（${row.publication}）`
    + (row.publication_venue ? ` · ${row.publication_venue}` : '')
    + (row.publication_source ? `（据 ${row.publication_source}）` : '');
  const audits = row.audits.map(item => `<div class="audit-line">
      <a href="${link(item.resource_url)}" rel="noopener noreferrer" target="_blank">${e(item.resource_url || '')}</a>
      <span class="chip ${item.status === 'access_failed' ? 'bad' : item.access === 'open' ? 'good' : ''}">${
        labelled(AUDIT_LABELS, item.status)}</span>
      <span class="meta-line">深度 ${labelled(DEPTH_LABELS, item.verification_depth)} · 归属 ${
        labelled(ATTRIBUTION_LABELS, item.attribution)} · 访问 ${labelled(ACCESS_LABELS, item.access)}</span>
    </div>`).join('');
  return `<article class="candidate" data-key="${e(row.key)}">
    <header>
      <input type="checkbox" data-action="select" data-key="${e(row.key)}" ${state.selected.has(row.key) ? 'checked' : ''}
        aria-label="勾选用于 BibTeX：${e(row.title)}">
      <div style="flex:1">
        <h3 class="paper-title" style="margin:0">${e(row.title)}</h3>
        <div class="paper-authors">${(row.authors || []).slice(0, 12).map(name =>
          `<span class="paper-chip">${e(name)}</span>`).join('')}${
          row.authors.length > 12 ? `<span class="paper-chip">另有 ${row.authors.length - 12} 位</span>` : ''}</div>
      </div>
    </header>
    <div class="paper-card">
      <div>
        <p class="meta-line">${e(row.year ?? '年份未知')} · ${e(publication)}${
          row.venue ? ` · <b>${e(row.venue)}</b>` : ''}${row.sources.length ? ` · 来自 ${row.sources.map(e).join('、')}` : ''}${
          row.citations === null ? '' : ` · 被引 ${e(String(row.citations))}`}</p>
        ${row.preprint_also ? `<p class="meta-line">另有预印本版本：${e(JSON.stringify(row.preprint_also))}</p>` : ''}
        ${row.abstract ? `<p class="paper-abstract" style="${open ? '' : 'display:none'}">${e(row.abstract)}</p>
          <button type="button" class="abstract-toggle" data-action="abstract" data-key="${e(row.key)}">${
            open ? '收起摘要' : `摘要（${row.abstract.length} 字符）`}</button>` : '<p class="meta-line">来源未提供摘要</p>'}
        <p class="meta-line">机构: ${row.institutions.length
          ? e(row.institutions.slice(0, 3).join(', ')) + (row.institutions.length > 3 ? `（另有 ${row.institutions.length - 3} 个）` : '')
          : '各来源均未提供'}</p>
      </div>
      <aside class="paper-aside">
        <div class="paper-links">${row.links.map(item =>
          `<a href="${link(item.url)}" rel="noopener noreferrer" target="_blank">${e(item.label)} ${icon('external')}</a>`).join('')
          || '<span class="subtle">没有可跳转的标识符</span>'}</div>
        <span class="status">资源候选 ${row.audit_count} · 已核验 ${row.checked} · 可公开获取 ${row.open}</span>
        <span class="status">名称检索 ${labelled(SEARCH_STATE_LABELS, row.search_state)}</span>
      </aside>
    </div>
    ${audits ? `<div style="padding:0 18px 16px">${audits}</div>` : `<div style="padding:0 18px 16px">
      <p class="audit-line">没有可审计的资源候选：${e(noCandidateReason(row))}</p></div>`}
  </article>`;
}

/** The search's own outcome, so "nobody searched" never reads as "searched and found nothing". */
function noCandidateReason(row) {
  const detail = row.search_detail || {};
  const name = detail.project_name || '';
  if (row.search_state === 'skipped') return '摘要中未提及链接，标题里也没有可检索的项目名（形如「Name: …」）';
  if ((detail.failures || []).length) {
    return `摘要中未提及链接；按项目名 '${name}' 的检索未完成（${detail.failures.join('；')}），这不代表没有开源`;
  }
  if (row.search_state === 'searched') return `摘要与元数据均无链接，按项目名 '${name}' 检索 GitHub/HF 也无结果`;
  return `摘要中未提及链接；按项目名 '${name}' 的 GitHub/HF 检索未开启（原因 ${detail.reason_not_run || 'disabled'}）`;
}

function renderCandidateList() {
  const list = document.querySelector('#candidate-list');
  const count = document.querySelector('#candidate-count');
  if (!list) return;
  const all = rows();
  const shown = visible();
  count.textContent = candidateCount(shown.length, all.length);
  list.innerHTML = shown.length
    ? shown.map(candidateCard).join('')
    : `<div class="empty-result"><h2>没有符合条件的候选</h2><p>这是筛选的结果，不是检索的结果。
       放宽条件，或者回到「检索覆盖」看有没有来源本次失败了——一个失败的来源会让它的记录整批缺席。</p></div>`;
}

/* ----------------------------------------------------------------------------------------- matrix */

function matrixView() {
  const found = matrix(state.result);
  const columns = [['paper_title', '论文'], ['resource_url', '资源'], ['resource_type', '类别'],
                   ['status', '状态'], ['attribution', '归属'], ['author_declaration', '作者声明'],
                   ['access', '访问'], ['verification_depth', '深度'], ['version_match', '版本对应'],
                   ['blockers', '为什么还不能直接当 baseline']];
  const body = found.rows.map((row, index) => {
    const open = state.openRows.has(index);
    return `<tr>
      <td class="title"><button type="button" class="row-toggle" data-action="row" data-index="${index}">${
        open ? '▾' : '▸'}</button> ${e(row.paper_title)}</td>
      <td class="url">${row.resource_url
        ? `<a href="${link(row.resource_url)}" rel="noopener noreferrer" target="_blank">${e(shorten(row.resource_url))}</a>`
        : '<span class="subtle">没有候选</span>'}</td>
      <td>${e(row.resource_type)}</td>
      <td><span class="chip ${['access_failed', 'unsupported'].includes(row.status) ? 'bad'
        : ['metadata_readable', 'partially_available'].includes(row.status) ? 'good' : ''}">${
        labelled(AUDIT_LABELS, row.status)}</span>${row.provider_status
        && row.provider_status !== row.status ? `<div class="meta-line">提供商状态 ${e(row.provider_status)}</div>` : ''}</td>
      <td>${labelled(ATTRIBUTION_LABELS, row.attribution)}</td>
      <td>${labelled(DECLARATION_LABELS, row.author_declaration)}</td>
      <td>${labelled(ACCESS_LABELS, row.access)}</td>
      <td>${labelled(DEPTH_LABELS, row.verification_depth)}</td>
      <td>${labelled(VERSION_MATCH_LABELS, row.version_match)}</td>
      <td>${row.blockers.length ? `<ul class="blockers">${row.blockers.map(item => `<li>${e(item)}</li>`).join('')}</ul>`
        : '<span class="subtle">无</span>'}</td>
    </tr>${open ? `<tr class="detail"><td colspan="${columns.length}">${matrixDetail(row)}</td></tr>` : ''}`;
  }).join('');
  return `<section>
    <div class="eyebrow">资源审计矩阵<span>一行是一个资源候选的一次检查，不是对这个资源的判决</span></div>
    <p class="lede">${found.papers} 篇论文，${found.rows.length} 行。没有候选的论文也占一行，
      并且写清是"检索过没有命中"还是"这次没有检索"——把两者压成同一句"没有资源"正是这张表要避免的。
      ${found.comparable ? '' : `<b>本次覆盖 ${found.papers} 篇，超出 6 篇的比较集范围，横向对比请先收窄查询。</b>`}</p>
    <div class="matrix-wrap"><table class="matrix">
      <thead><tr>${columns.map(([, label]) => `<th>${e(label)}</th>`).join('')}</tr></thead>
      <tbody>${body}</tbody></table></div>
  </section>`;
}

function matrixDetail(row) {
  const coverage = AUDIT_COMPONENTS.map(name =>
    `<span class="chip">${e(name)}=${e(row.coverage[name] || COMPONENT_LABELS.unknown)}</span>`).join(' ');
  const licences = Object.entries(row.licences || {})
    .map(([name, value]) => `<span class="chip">${e(name)}=${e(value)}</span>`).join(' ');
  return `<div style="padding-top:12px">
    <p class="meta-line"><b>标识</b> ${e(row.work_identifier || '（无）')}${row.work_version ? ` · 版本 ${e(row.work_version)}` : ''}
      ${row.revision ? ` · revision ${e(row.revision)}` : ''}${row.checked_at ? ` · 检查于 ${e(row.checked_at)}` : ''}</p>
    <p class="meta-line"><b>检查范围</b> ${e(row.scope || '结果未记录')}</p>
    <p class="meta-line"><b>候选来源</b> ${e(row.candidate_origin || '（未记录）')}${row.provider ? ` · 提供商 ${e(row.provider)}` : ''}</p>
    ${row.summary ? `<p class="meta-line"><b>摘要</b> ${e(row.summary)}</p>` : ''}
    <p class="meta-line"><b>资源类别覆盖</b></p><div class="cov">${coverage}</div>
    ${licences ? `<p class="meta-line" style="margin-top:9px"><b>许可证（来源声明，不是使用权限结论）</b></p><div class="cov">${licences}</div>` : ''}
    ${row.sources.length ? `<p class="meta-line" style="margin-top:9px"><b>这一行的结论所依据的来源</b></p>
      <ul class="plain-list">${row.sources.map(url =>
        `<li><a href="${link(url)}" rel="noopener noreferrer" target="_blank">${e(url)}</a></li>`).join('')}</ul>` : ''}
    ${row.limitations.length ? `<p class="meta-line" style="margin-top:9px"><b>限制</b></p>
      <ul class="plain-list">${row.limitations.map(item => `<li>${e(item)}</li>`).join('')}</ul>` : ''}
    <p class="meta-line" style="margin-top:9px"><b>记录类型</b> ${e(row.record_kind)}（观察；人工修订是另一种记录，不会覆盖这一行）</p>
  </div>`;
}

function shorten(url) {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.length > 42 ? `…${parsed.pathname.slice(-40)}` : parsed.pathname;
    return `${parsed.hostname}${path}`;
  } catch { return url; }
}

/* ----------------------------------------------------------------------------------------- export */

function exportView() {
  const found = matrix(state.result);
  const items = staticDemo ? [] : approvalItems(state.result);
  const selected = rows().filter(row => state.selected.has(row.key));
  const report = state.importReport;
  return `<section>
    <div class="eyebrow">${staticDemo ? '本地导出' : '导出与入库'}<span>${staticDemo ? '文件和复制内容只在当前浏览器处理' : '候选不进文献库，除非你在这里点一次'}</span></div>
    <div class="export-grid">
      <div class="export-card"><h3>BibTeX</h3>
        <p>勾选候选论文后复制；未勾选时复制当前筛选下显示的全部 ${visible().length} 篇。
          条目按 <code>@misc</code> 输出，转义规则与文献库的 BibTeX 导出一致。
          仅见预印本的会带一条 note，说明这是本次检索到的来源里的情况。</p>
        <div class="actions">
          <button type="button" class="button primary" data-action="copy-bibtex-selected">${icon('note')} 复制勾选的（${selected.length}）</button>
          <button type="button" class="button" data-action="copy-bibtex-shown">复制当前显示的（${visible().length}）</button>
        </div></div>
      <div class="export-card"><h3>资源矩阵</h3>
        <p>${found.rows.length} 行，覆盖 ${found.papers} 篇。CSV 对以 <code>= + - @</code> 开头的单元格做了转义——
          这些字符串来自别的服务，而表格软件会把它们当公式执行。</p>
        <div class="actions">
          <button type="button" class="button" data-action="download-csv">${icon('download')} 下载 CSV</button>
          <button type="button" class="button" data-action="copy-csv">复制 CSV</button>
        </div></div>
      <div class="export-card"><h3>原始结果</h3>
        <p>把载入的 JSON 原样下载回去。这一页不修改它：认不出的字段、正文截断的字符数、失败的来源都还在里面。</p>
        <div class="actions"><button type="button" class="button" data-action="download-json">${icon('download')} 下载 JSON</button></div></div>
      ${staticDemo ? '' : `<div class="export-card" id="import-card"><h3>入文献库</h3>
        <p>${items.length} 篇带有可导入的审计记录（没有候选或没有标题的不算：导入它们等于凭空造一条记录）。
          走的是既有接口 <code>POST /api/import/resource-audits</code>，<b>先预览</b>，确认后才是第二步。
          导入只建立关联，不会覆盖已有论文的笔记或字段。</p>
        <div class="actions">
          <button type="button" class="button" data-action="preview-import" ${state.busy ? 'disabled' : ''}>${icon('shield')} 预览导入</button>
          <button type="button" class="button primary" data-action="commit-import" ${report && report.dry_run ? '' : 'disabled'}>写入文献库</button>
        </div>
        ${report ? importReport(report) : ''}
      </div>`}
    </div>
    <p class="footnote">这一页显示的是<b>检索结果</b>，不是全文，也不是结论。每条状态描述的是本次检查做到了哪一步，
      不是资源是否存在；"检查范围内未找到"与"没有发布"是两件事。要引用的东西请对着论文本身再确认一次。</p>
  </section>`;
}

function importReport(report) {
  const lines = [`<div class="import-report"><b>${report.dry_run ? '预览' : '已写入'}</b>：
    可导入 ${e(String(report.ready ?? 0))} 篇，新建 ${e(String((report.created || []).length))} 条论文，
    关联 ${e(String((report.linked || []).length))} 个资源，跳过 ${e(String((report.skipped || []).length))} 条，
    格式错误 ${e(String((report.errors || []).length))} 条。`];
  if ((report.errors || []).length) {
    lines.push(`<ul>${report.errors.slice(0, 8).map(item =>
      `<li>第 ${e(String(item.index))} 条：${e(item.message)}</li>`).join('')}</ul>`);
  }
  if ((report.skipped || []).length) {
    lines.push(`<ul>${report.skipped.slice(0, 8).map(item =>
      `<li>跳过 ${e(item.url || '')}：${e(item.reason || '')}</li>`).join('')}</ul>`);
  }
  if (report.dry_run && (report.preview || []).length) {
    lines.push(`<ul>${report.preview.map(item =>
      `<li>${e(item.title)} — ${(item.resources || []).length} 个资源</li>`).join('')}</ul>`);
  }
  if (report.note) lines.push(`<div class="meta-line">${e(report.note)}</div>`);
  lines.push('</div>');
  return lines.join('');
}

/* ----------------------------------------------------------------------------------------- render */

function tabs() {
  return `<div class="tabs">${VIEWS.map(([key, label]) =>
    `<button type="button" class="${state.view === key ? 'active' : ''}" data-action="view" data-view="${key}">${
      e(label)}</button>`).join('')}</div>`;
}

function render() {
  initTheme();
  if (staticDemo) document.querySelector('.topbar [data-action="back-to-load"]').hidden = !state.result;
  if (!state.result) {
    crumb.textContent = state.fileName ? `${state.fileName} · 未能载入` : '尚未载入结果';
    main.innerHTML = loadView();
    bindLoad();
    return;
  }
  const body = {coverage: coverageView, candidates: candidatesView, matrix: matrixView,
                export: exportView}[state.view]();
  main.innerHTML = tabs() + `<div class="workbench-inner">${body}</div>`;
  if (state.view === 'candidates') {
    renderCandidateList();
    bindFilters();
  }
}

function bindLoad() {
  const file = document.querySelector('#file');
  if (file) {
    file.addEventListener('change', async () => {
      const picked = file.files && file.files[0];
      if (!picked) return;
      if (picked.size > MAX_RESULT_BYTES) { state.error = '文件超过 5 MB，请缩小后再载入'; render(); return; }
      try { accept(await picked.text(), picked.name); }
      catch { state.error = '文件读取失败，请重新选择'; render(); }
    });
  }
}

function bindFilters() {
  const on = (id, key, event = 'input') => {
    const node = document.querySelector(id);
    if (!node) return;
    node.addEventListener(event, () => {
      state.filters[key] = node.type === 'checkbox' ? node.checked : node.value;
      renderCandidateList();
    });
  };
  on('#f-query', 'query');
  on('#f-year', 'year', 'change');
  on('#f-source', 'source', 'change');
  on('#f-publication', 'publication', 'change');
  on('#f-open', 'openOnly', 'change');
  on('#f-sort', 'sort', 'change');
}

/* ---------------------------------------------------------------------------------------- actions */

async function copy(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    say(`${what}已复制到剪贴板（${text.length} 字符）`);
  } catch {
    // An insecure context has no clipboard API; falling back beats telling the reader it failed.
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { /* Browser denied fallback too. */ }
    area.remove();
    say(ok ? `${what}已复制到剪贴板` : '复制失败：浏览器不允许，请手动选中复制', !ok);
  }
}

function download(name, text, type) {
  let url;
  try { url = URL.createObjectURL(new Blob([text], {type})); }
  catch { say('下载失败：浏览器无法创建文件，请尝试复制内容', true); return; }
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor);
  try { anchor.click(); }
  catch { URL.revokeObjectURL(url); anchor.remove(); say('下载失败：浏览器阻止了下载', true); return; }
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  say(`已下载 ${name}`);
}

function baseName() { return (state.fileName || 'result').replace(/\.json$/i, ''); }

async function importAudits(dryRun) {
  if (staticDemo) return;
  const items = approvalItems(state.result);
  if (!items.length) { say('没有可导入的审计记录', true); return; }
  state.busy = true;
  render();
  try {
    state.importReport = await api('/import/resource-audits',
                                   {method: 'POST', body: {items, dry_run: dryRun}});
    say(dryRun ? `预览完成：${state.importReport.ready} 篇可导入` : '已写入文献库；导入只建立关联');
  } catch (error) {
    state.importReport = null;
    say(`导入失败：${error.message}`, true);
  } finally {
    state.busy = false;
    render();
  }
}

document.addEventListener('click', async (event) => {
  const node = event.target.closest('[data-action]');
  if (!node) return;
  const action = node.dataset.action;
  if (action === 'menu') {
    document.querySelector('.side').classList.toggle('mobile-expanded');
    return;
  }
  if (action === 'back-to-load') { if (staticDemo) state.result = null; state.view = state.result ? 'coverage' : 'load'; render(); return; }
  if (action === 'load-sample') {
    try {
      const response = await fetch('/samples/lora-2026-09-22.json');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      accept(await response.text(), '历史案例');
    } catch { state.error = '历史案例暂时无法载入，请刷新页面重试'; render(); }
    return;
  }
  if (action === 'view') { state.view = node.dataset.view; render(); return; }
  if (action === 'parse-paste') {
    const text = document.querySelector('#paste').value.trim();
    if (!text) { say('先粘贴 JSON，或者用上面的按钮选择文件', true); return; }
    accept(text, '粘贴的内容');
    return;
  }
  if (!state.result) return;
  if (action === 'goto-coverage') { state.view = 'coverage'; render(); return; }
  if (action === 'abstract') {
    const key = node.dataset.key;
    state.openAbstracts.has(key) ? state.openAbstracts.delete(key) : state.openAbstracts.add(key);
    renderCandidateList();
    return;
  }
  if (action === 'select') return;   // handled on change, so the checkbox state stays authoritative
  if (action === 'row') {
    const index = Number(node.dataset.index);
    state.openRows.has(index) ? state.openRows.delete(index) : state.openRows.add(index);
    render();
    return;
  }
  if (action === 'copy-command') return copy(commandFor(state.result).command, '命令');
  if (action === 'copy-csv') return copy(matrixCsv(matrix(state.result).rows), 'CSV');
  if (action === 'download-csv') {
    return download(`${baseName()}-matrix.csv`, matrixCsv(matrix(state.result).rows),
                    'text/csv;charset=utf-8');
  }
  if (action === 'download-json') {
    return download(`${baseName()}.json`, JSON.stringify(state.result, null, 2), 'application/json');
  }
  if (action === 'copy-bibtex-selected' || action === 'copy-bibtex-shown') {
    const picked = action === 'copy-bibtex-selected'
      ? rows().filter(row => state.selected.has(row.key)) : visible();
    if (!picked.length) {
      say(action === 'copy-bibtex-selected' ? '还没有勾选任何候选论文' : '当前筛选下没有候选论文', true);
      return;
    }
    return copy(bibtex(picked), `BibTeX（${picked.length} 条）`);
  }
  if (action === 'preview-import') return importAudits(true);
  if (action === 'commit-import') return importAudits(false);
});

document.addEventListener('change', (event) => {
  const node = event.target.closest('[data-action="select"]');
  if (!node) return;
  if (node.checked) state.selected.add(node.dataset.key);
  else state.selected.delete(node.dataset.key);
  const count = document.querySelector('#candidate-count');
  if (count) count.textContent = candidateCount(visible().length, rows().length);
});

render();
