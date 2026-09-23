/** Pure logic for the login page and the 401 redirect every hosted page shares (#13).
 *
 * Kept out of the DOM for the same reason the other `*-core.js` modules exist: these are the
 * decisions worth testing, and a browser is a slow, unreliable place to test a decision. Three of
 * them carry weight:
 *
 * * **`safeNext` refuses anything that is not a path on this origin.** A `next` parameter is the
 *   classic open redirect — a login page that honours `next=https://evil.example` hands a stranger's
 *   page a URL the reader just authenticated in — and `//evil.example` is the same trick wearing a
 *   relative disguise, because a leading double slash resolves against the *other* host.
 * * **The failure text never guesses which of the four reasons the server had.** A wrong password, an
 *   unknown account, a disabled account and a locked one are deliberately one answer with one timing
 *   profile in `auth.py`. A page that inferred "at least the account exists" from a response would
 *   undo that with one sentence.
 * * **`sessionView` decides what the page may do.** Local mode has no login step, so offering a form
 *   there would be an invitation to do something that cannot work.
 */

/** Where a signed-out visitor at `current` should be sent, or null when nothing should bounce. */
export function loginRedirect(current) {
  const here = String(current || '');
  // Never bounce the door into itself: a 401 on the login page is an answer, not a redirect.
  if (here === '/login' || here.startsWith('/login?')) return null;
  return '/login?next=' + encodeURIComponent(safeNext(here));
}

/** A same-origin path to return to after login; anything else becomes the workbench. */
export function safeNext(raw) {
  const value = String(raw || '');
  if (!value.startsWith('/') || value.length > 200) return '/';
  // `//host` and `backslash-host` are both scheme-relative in some browsers.
  if (value[1] === '/' || value[1] === '\\') return '/';
  for (const character of value) {
    const code = character.codePointAt(0);
    // Control characters and a space (a path with a space in it is not one this service serves), then
    // quote and the angle brackets, which would let this string re-enter the page as markup, and the
    // backslash, which some parsers treat as a slash.
    if (code <= 0x20 || code === 0x7f) return '/';
    if (code === 34 || code === 60 || code === 62 || code === 92) return '/';
  }
  return value;
}

/** Which page a path is, in the words the reader already sees in the navigation bar. */
export const PAGES = {
  '/': '研究工作台',
  '/library': '文献库',
  '/static/search.html': '检索工作台',
};

/**
 * How to offer the return path.
 *
 * A `next` that is not one of the known pages is still offered — a deep link is legitimate — but it is
 * labelled with its own path rather than an invented name, so the reader can see where they are going.
 */
export function nextLabel(next) {
  const value = safeNext(next);
  return PAGES[value] ? { path: value, label: PAGES[value], known: true }
                      : { path: value, label: value, known: false };
}

/**
 * What the server said about this browser, reduced to what the page can honestly show.
 *
 * `authenticated: false` in hosted mode is not an error state — it is the state in front of the door —
 * so the page renders a form rather than a failure.
 */
export function sessionView(payload, next) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const identity = data.identity && typeof data.identity === 'object' ? data.identity : {};
  const deployment = data.deployment && typeof data.deployment === 'object' ? data.deployment : {};
  const kind = ['guest', 'user', 'local', 'anonymous'].includes(identity.kind) ? identity.kind : 'user';
  const required = deployment.auth_required === true;
  const authenticated = identity.authenticated === true;
  return {
    required,
    authenticated,
    mode: deployment.mode === 'hosted' ? 'hosted' : 'local',
    kind,
    isGuest: authenticated && kind === 'guest',
    guestAccessAvailable: deployment.guest_access_enabled === true,
    storageMode: deployment.storage_mode === 'ephemeral-demo' ? 'ephemeral-demo'
      : (deployment.storage_mode === 'persistent' ? 'persistent' : 'local'),
    who: authenticated ? String(identity.user_id || '') : '',
    workspace: authenticated ? String(identity.workspace || '') : '',
    expiresAt: authenticated ? String(identity.expires_at || '') : '',
    accountsProvisioned: Number(data.accounts_provisioned) || 0,
    note: String(data.note || ''),
    next: nextLabel(next),
    action: required ? (authenticated ? 'signed-in' : 'sign-in') : 'no-login',
  };
}

/**
 * One line for a failed login, plus the one sentence the server cannot know.
 *
 * The server's message is application-authored and never a reflection of the typed password, so it is
 * shown verbatim. A validation failure arrives as a list of field problems rather than a sentence, and
 * is flattened here rather than at the call site so the shape stays in one place.
 *
 * The hints exist because each of these states has an obvious wrong reading: a 429 looks like a wrong
 * password, a 409 on a login form looks like a broken account, a 403 looks like credentials the reader
 * could fix by trying again, and a 422 looks like an auth failure when it is not one — the attempt
 * never reached the password check at all.
 */
export function loginFailure(status, detail) {
  const message = Array.isArray(detail)
    ? detail.map(item => `${(item.loc || []).slice(1).join('.') || '输入'}: ${item.msg || ''}`).join('；')
    : String(detail || '').trim();
  if (status === 401) {
    return { text: message || '用户名或口令不正确。',
             hint: '四种情况（口令错、账户不存在、已停用、正在锁定）在这个页面上是同一句话，服务器不区分它们。'
                   + '账户由部署者在服务器控制台创建，这里没有注册入口。' };
  }
  if (status === 409) {
    return { text: message || '这个服务不需要登录。',
             hint: '本地模式只有一个所有者；出现这句话说明服务以 local 模式运行，而这个页面是给托管模式用的。' };
  }
  if (status === 422) {
    return { text: message || '表单里有字段不符合服务器的要求。',
             hint: '这一项没有到达口令校验，所以它不是一次失败的登录尝试，也不会让账户更接近锁定。' };
  }
  if (status === 429) {
    return { text: message || '请求太频繁，这个地址暂时被挡住了。',
             hint: '这是请求配额的等待时间，不是口令错了；在这段时间里再试不会更快。' };
  }
  if (status === 403) {
    return { text: message || '这个请求被拒绝了。',
             hint: '登录必须来自本页面所在的来源。代理或扩展改写了来源头时会出现这句话，与用户名口令无关。' };
  }
  return { text: message || '无法完成登录。',
           hint: '服务器没有给出原因。稍后再试；持续如此请在服务端查看这次请求，而不是反复提交口令。' };
}
