/** The login page. Policy lives in login-core.js; this only moves text and reacts to clicks. */
import { loginFailure, sessionView } from './login-core.js';
import { initTheme } from './theme.js';

const els = {
  door: document.getElementById('door'),
  title: document.getElementById('door-title'),
  lede: document.getElementById('door-lede'),
  form: document.getElementById('login-form'),
  guestEntry: document.getElementById('guest-entry'),
  guestStart: document.getElementById('guest-start'),
  username: document.getElementById('username'),
  password: document.getElementById('password'),
  submit: document.getElementById('login-submit'),
  alt: document.getElementById('door-alt'),
  signed: document.getElementById('signed-in'),
  who: document.getElementById('who-name'),
  expires: document.getElementById('when-expires'),
  workspace: document.getElementById('which-workspace'),
  cont: document.getElementById('continue-link'),
  logout: document.getElementById('logout'),
  guestDelete: document.getElementById('guest-delete'),
  noLogin: document.getElementById('no-login'),
  storageWarning: document.getElementById('storage-warning'),
  notice: document.getElementById('door-notice'),
  foot: document.getElementById('door-foot'),
};

const HEADINGS = {
  'sign-in': '登录这台 Re0',
  'signed-in': '这个浏览器已经有一张有效的会话票',
  'no-login': '这台服务不需要登录',
};

let view = sessionView({}, new URLSearchParams(window.location.search).get('next'));

/** Server text goes in as text, never as markup: this page renders strings from another process. */
function notice(kind, text, hint) {
  els.notice.textContent = '';
  if (!text) return;
  const line = document.createElement('p');
  line.className = `notice ${kind}`;
  line.textContent = text;
  if (hint) {
    const extra = document.createElement('small');
    extra.textContent = ' ' + hint;
    line.appendChild(extra);
  }
  els.notice.appendChild(line);
}

function render() {
  els.storageWarning.hidden = view.storageMode !== 'ephemeral-demo';
  els.form.hidden = view.action !== 'sign-in';
  els.guestEntry.hidden = view.action !== 'sign-in' || !view.guestAccessAvailable;
  els.signed.hidden = view.action !== 'signed-in';
  els.noLogin.hidden = view.action !== 'no-login';
  els.title.textContent = view.isGuest ? '这个浏览器正在使用临时访客会话' : HEADINGS[view.action];
  els.cont.href = view.next.path;
  els.guestDelete.hidden = !view.isGuest;
  els.foot.textContent = `运行模式 ${view.mode} · 会话票是 HttpOnly + SameSite=Strict 的 Cookie，页面脚本读不到它`
    + (view.isGuest ? ' · 临时会话最多 2 小时；访客没有管理员或正式账户权限'
      : (view.required && !view.guestAccessAvailable ? ' · 账户由部署者在服务器控制台创建' : ''));

  const asked = new URLSearchParams(window.location.search).get('next');
  const ledes = {
    'sign-in': '登录只换取一张属于你这张浏览器的会话票：任务、证据、文献库和模型 Key 都按账户分开，'
      + '别人看不到，你也看不到别人的。',
    'signed-in': view.isGuest ? '这是仅属于当前浏览器的临时空间。退出会清除服务器端访客数据；也可以单独删除本次数据。'
      : '下面这个身份属于这张 Cookie，不属于这台机器上的某个人。共用电脑时请退出。',
    'no-login': '',
  };
  els.lede.textContent = ledes[view.action];
  if (view.action === 'sign-in') {
    els.alt.textContent = view.guestAccessAvailable
      ? '临时访客可跳过管理员开户；实时研究需使用自己的模型 Key，项目方没有配置免 Key 试用额度。'
      : (view.accountsProvisioned === 0
      ? '这台服务还没有任何账户：先由部署者运行 `python -m re0 auth create-user <名字>`，再来登录。'
      : (asked && asked !== view.next.path
        ? `原本想去的地址（${asked}）不是一个本站路径，登录后会回到${view.next.label}。`
        : `登录后回到${view.next.label}。`));
  }
  if (view.action === 'signed-in') {
    els.who.textContent = view.isGuest ? '临时访客' : view.who;
    els.workspace.textContent = view.isGuest ? '本次独立会话数据' : view.workspace;
    els.expires.textContent = view.expiresAt ? new Date(view.expiresAt).toLocaleString() : '未知';
  }
}

els.guestStart.addEventListener('click', async () => {
  els.guestStart.disabled = true;
  els.guestStart.textContent = '正在创建独立会话…';
  try {
    const response = await fetch('/api/auth/guest', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Re0-Client': 'web'}, body: '{}',
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `服务器回答 ${response.status}`);
    window.location.assign(view.next.path);
  } catch (error) {
    els.guestStart.disabled = false;
    els.guestStart.textContent = '开始临时访客会话';
    notice('error', '无法创建访客会话。', error && error.message ? error.message : String(error));
  }
});

async function probe() {
  const response = await fetch('/api/auth/session', {headers: {'X-Re0-Client': 'web'}});
  if (!response.ok) throw new Error(`服务器回答 ${response.status}`);
  return response.json();
}

els.form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = els.username.value.trim();
  const password = els.password.value;
  // The typed secret leaves this function in one direction, and is dropped either way.
  const forget = () => { els.password.value = ''; };
  if (!username || !password) {
    forget();
    return notice('warn', '请输入用户名与口令。',
      '空的口令不表示“没填”，它和填错一样是一次失败的尝试。');
  }
  els.submit.disabled = true;
  els.submit.textContent = '正在验证…';
  notice('', '');
  let status = 0;
  let detail = '';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Re0-Client': 'web'},
      body: JSON.stringify({username, password}),
    });
    status = response.status;
    if (response.ok) {
      notice('ok', '登录成功，正在进入。', '这张会话票现在有效；关掉标签页不等于退出。');
      window.location.assign(view.next.path);
      return;  // stay disabled: the next thing that happens is a navigation
    }
    detail = (await response.json().catch(() => ({}))).detail;
  } catch (error) {
    forget();
    els.submit.disabled = false;
    els.submit.textContent = '登录';
    return notice('error', '连接失败，登录没有提交出去。',
      `${error && error.message ? error.message : error}；这台服务可能已经停止，或中间有一层代理挡住了这个请求。`);
  }
  const failure = loginFailure(status, detail);
  forget();
  els.username.select();
  els.submit.disabled = false;
  els.submit.textContent = '登录';
  notice('error', failure.text, failure.hint);
});

els.logout.addEventListener('click', async () => {
  els.logout.disabled = true;
  let text = view.isGuest ? '访客会话已退出；临时数据已安排清理。' : '已退出：这张会话票已经失效，下一次请求必须重新登录。';
  let hint = '撤销立即生效；关掉标签页从来不是退出。';
  try {
    const response = await fetch('/api/auth/logout', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Re0-Client': 'web'},
    });
    if (!response.ok) hint = `服务器说：${(await response.json().catch(() => ({}))).detail || response.status}。`;
  } catch (error) {
    text = '退出请求没有发出去。';
    hint = `${error && error.message ? error.message : error}；这张票在服务端仍然有效，请在网络恢复后再退出。`;
  }
  try {
    view = sessionView(await probe(), new URLSearchParams(window.location.search).get('next'));
  } catch (error) {
    /* keep the panel as it was; the notice below says what happened */
  }
  render();
  els.logout.disabled = false;
  notice('ok', text, hint);
});

els.guestDelete.addEventListener('click', async () => {
  if (!confirm('删除这次访客会话的论文、任务、证据和工作区文件？此操作无法撤销。')) return;
  els.guestDelete.disabled = true;
  try {
    const response = await fetch('/api/auth/guest/data', {
      method: 'DELETE', headers: {'Content-Type': 'application/json', 'X-Re0-Client': 'web'}, body: '{}',
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `服务器回答 ${response.status}`);
    view = sessionView(await probe(), new URLSearchParams(window.location.search).get('next'));
    render();
    notice('ok', result.pending ? '访客会话已失效；任务停止后会删除剩余数据。' : '本次访客数据已删除。');
  } catch (error) {
    els.guestDelete.disabled = false;
    notice('error', '未完成数据删除。', error && error.message ? error.message : String(error));
  }
});

initTheme();
render();
probe().then(payload => {
  view = sessionView(payload, new URLSearchParams(window.location.search).get('next'));
  render();
  if (view.action === 'sign-in') els.username.focus();
  document.body.dataset.ready = 'yes';
}).catch(error => {
  els.title.textContent = '问不到这台服务的身份';
  els.lede.textContent = `${error && error.message ? error.message : error}。`
    + '页面没有显示登录表单：在确认这台服务的运行模式之前，摆出一个可能根本没在运行的登录框，'
    + '只是把凭据交给不知道是谁的进程。';
  notice('error', '无法确认这是本地模式还是托管模式。', '先确认服务在运行；仍不行请看服务端日志里这个请求的返回。');
});
