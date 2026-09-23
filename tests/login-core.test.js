/** The login page's decisions, where they can actually be tested: no DOM, no browser, no server.
 *
 * Three things are worth pinning hard. `safeNext` is the one function standing between a `?next=`
 * parameter and an open redirect. `loginFailure` must never turn the server's deliberately uniform
 * 401 into a sentence that leaks which of four cases it was. And `sessionView` is what stops the page
 * from offering a login form to a local-mode service that has nothing to log into.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loginFailure, loginRedirect, nextLabel, safeNext, sessionView} from '../web/login-core.js';

const HOSTED_SIGNED_OUT = {
  identity: {user_id: '', workspace: '', authenticated: false, expires_at: ''},
  deployment: {mode: 'hosted', auth_required: true, public_entry: 'https://re0.example.org'},
  accounts_provisioned: 2,
  note: '托管模式：未登录时接口一律 401',
};
const HOSTED_SIGNED_IN = {
  identity: {user_id: 'usr_deadbeef', workspace: 'ws_cafebabe', authenticated: true,
             expires_at: '2026-09-23T12:00:00+00:00'},
  deployment: {mode: 'hosted', auth_required: true},
  accounts_provisioned: 2,
};
const LOCAL = {
  identity: {user_id: 'local', workspace: 'local', authenticated: false},
  deployment: {mode: 'local', auth_required: false},
  accounts_provisioned: 0,
  note: '本地模式：所有数据属于本机唯一所有者，没有登录这一步',
};

// ---------------------------------------------------------------------------------- open redirect

test('a next that is not a path on this origin becomes the workbench', () => {
  for (const attack of ['https://evil.example/', 'http://evil.example', '//evil.example/path',
                        '/\\evil.example', 'javascript:alert(1)', 'javascript&#58;alert(1)',
                        'data:text/html,x', 'mailto:a@b.c', 'evilexample.com/', '/ /x']) {
    assert.equal(safeNext(attack), '/', `safeNext let ${attack} through`);
  }
});

test('a path that merely mentions another origin in its query is still one of ours', () => {
  // This one is worth stating rather than refusing: the redirect target is the whole path, and a
  // leading single slash cannot leave the origin. Whatever reads the inner `next` validates its own
  // value — this function's job is only "stay on this origin".
  assert.equal(safeNext('/x?next=https://evil.example#'), '/x?next=https://evil.example#');
  assert.equal(safeNext('/library?q=%2F%2Fevil.example'), '/library?q=%2F%2Fevil.example');
});

test('a next that survives is only ever a same-origin path', () => {
  assert.equal(safeNext('/library'), '/library');
  assert.equal(safeNext('/static/search.html'), '/static/search.html');
  assert.equal(safeNext('/runs/abc?id=3#x'), '/runs/abc?id=3#x');
  assert.equal(safeNext(''), '/');
  assert.equal(safeNext(undefined), '/');
  assert.equal(safeNext(null), '/');
  assert.equal(safeNext(12345), '/');
  // Not a path, not honoured — even when it starts like one.
  assert.equal(safeNext('/' + 'a'.repeat(300)), '/');
});

test('a next cannot smuggle markup or a break out of the page it is written into', () => {
  const hostile = ['"><img src=x onerror=alert(1)>', '/ok"><b', '/with space', '/tab\there',
                   '/newline\nhere', '/ctl\x01char'];
  for (const value of hostile) {
    assert.equal(safeNext(value), '/', `safeNext let ${JSON.stringify(value)} through`);
  }
});

test('the encoded form of a refused next still cannot leave the origin', () => {
  // What the page actually puts in the URL: whatever came in is percent-encoded after validation, so
  // an attacker-controlled encoding cannot reintroduce a scheme.
  const target = loginRedirect('/x');
  assert.match(target, /^\/login\?next=%2Fx$/);
  const fromAttack = '/login?next=' + encodeURIComponent(safeNext('//evil.example'));
  assert.equal(fromAttack, '/login?next=%2F');
});

// ------------------------------------------------------------------------------------ the bounce

test('a 401 sends the reader to the door, once, with where they came from', () => {
  assert.equal(loginRedirect('/library'), '/login?next=%2Flibrary');
  assert.equal(loginRedirect('/'), '/login?next=%2F');
  assert.equal(loginRedirect(''), '/login?next=%2F');
});

test('the door never bounces itself, or the bounce becomes a loop', () => {
  assert.equal(loginRedirect('/login'), null);
  assert.equal(loginRedirect('/login?next=%2Flibrary'), null);
  // …and a path that merely looks like the door is still bounced, so the guard is not a prefix trap.
  assert.equal(loginRedirect('/loginx'), '/login?next=%2Floginx');
});

// -------------------------------------------------------------------------------- the page's mode

test('the three states the page can be in come from the server, not from the URL', () => {
  const signedOut = sessionView(HOSTED_SIGNED_OUT, '/library');
  assert.equal(signedOut.action, 'sign-in');
  assert.equal(signedOut.authenticated, false);
  assert.equal(signedOut.required, true);
  assert.equal(signedOut.next.path, '/library');
  assert.equal(signedOut.next.label, '文献库');

  const signedIn = sessionView(HOSTED_SIGNED_IN, null);
  assert.equal(signedIn.action, 'signed-in');
  assert.equal(signedIn.who, 'usr_deadbeef');
  assert.equal(signedIn.workspace, 'ws_cafebabe');
  assert.equal(signedIn.expiresAt, '2026-09-23T12:00:00+00:00');
  assert.equal(signedIn.next.path, '/');

  const local = sessionView(LOCAL, '/library');
  assert.equal(local.action, 'no-login');
  // Local mode says so rather than showing a form that cannot work.
  assert.equal(local.required, false);
  assert.equal(local.mode, 'local');
});

test('nothing about a signed-out or local visitor invents an identity', () => {
  // `local` is the owner string on every row; echoing it as "who you are" would read like an account.
  assert.equal(sessionView(LOCAL, null).who, '');
  assert.equal(sessionView(LOCAL, null).workspace, '');
  assert.equal(sessionView({}, null).who, '');
  assert.equal(sessionView(null, null).action, 'no-login');
  assert.equal(sessionView(undefined, undefined).next.path, '/');
});

test('a hostile next cannot survive into the view the page navigates to', () => {
  const view = sessionView(HOSTED_SIGNED_OUT, 'https://evil.example/');
  assert.equal(view.next.path, '/');
  assert.equal(view.next.known, true);
  // An unknown-but-legitimate path is offered under its own name, not a made-up one.
  assert.deepEqual(nextLabel('/runs/abc'), {path: '/runs/abc', label: '/runs/abc', known: false});
});

test('an empty accounts_provisioned is the fact that explains a first failure', () => {
  assert.equal(sessionView({...HOSTED_SIGNED_OUT, accounts_provisioned: 0}, null).accountsProvisioned, 0);
  assert.equal(sessionView(HOSTED_SIGNED_OUT, null).accountsProvisioned, 2);
  assert.equal(sessionView({}, null).accountsProvisioned, 0);
});

// ------------------------------------------------------------------------------ the failure text

test('the server says one thing about four cases and the page keeps it that way', () => {
  const uniform = '用户名或口令不正确；连续失败会暂时锁定该账户';
  const failure = loginFailure(401, uniform);
  // Verbatim. The moment the page rephrases, it becomes a second opinion that can be wrong in public.
  assert.equal(failure.text, uniform);
  // It disclaims the distinction rather than making one. Naming the four cases *as undistinguished*
  // is honest; picking one would turn this page into an enumeration oracle the server refused to be.
  assert.match(failure.hint, /服务器不区分/);
  assert.match(failure.hint, /四种情况/);
  assert.match(failure.hint, /没有注册入口/);
  assert.ok(!/这个账户|该账户已|用户名不存在/.test(failure.text + failure.hint),
    'the hint spoke about this account specifically');
});

test('a 429 on the door is a ceiling, and is not described as a wrong password', () => {
  const failure = loginFailure(429, '该账户请求配额已用尽：120 次/60 秒。本次请求没有执行，没有产生模型调用或费用；约 40.0 秒后可再试。');
  assert.equal(failure.text.startsWith('该账户请求配额已用尽'), true);
  assert.match(failure.hint, /不是口令错了/);
  // The empty-detail case still reads as a sentence rather than a shrug.
  assert.match(loginFailure(429, '').text, /太频繁/);
});

test('every status gets a sentence and a next action, including the ones with no detail', () => {
  for (const status of [400, 401, 403, 409, 429, 500, 502, 503]) {
    const failure = loginFailure(status, '');
    assert.ok(failure.text.length > 3, `status ${status} has no text`);
    assert.ok(failure.hint.length > 3, `status ${status} has no hint`);
    assert.ok(!/undefined|null/.test(failure.text + failure.hint));
  }
  // A detail the server authored is passed through verbatim rather than rephrased.
  assert.equal(loginFailure(409, '本地模式没有账户：所有数据属于本机唯一的所有者 local').text,
               '本地模式没有账户：所有数据属于本机唯一的所有者 local');
});

// ------------------------------------------------------------- what the page must never do (source)

test('the login page keeps no trace of the typed password in script or storage', () => {
  const source = readFileSync(new URL('../web/login.js', import.meta.url), 'utf8');
  assert.match(source, /els\.password\.value = ''/, 'the form must clear the field after every attempt');
  for (const store of ['localStorage', 'sessionStorage', 'indexedDB', 'document.cookie']) {
    assert.ok(!source.includes(store), `login.js touches ${store}`);
  }
  // Server text enters the page as text nodes, so a hostile or misconfigured proxy cannot be markup.
  assert.ok(!/\.innerHTML\s*=/.test(source), 'the page assigns innerHTML; use text nodes');
  assert.match(source, /preventDefault/, 'the form must not do a browser navigation with the password');
});

test('the shared api helper bounces a 401 and does not surface a body it did not read', () => {
  const source = readFileSync(new URL('../web/api.js', import.meta.url), 'utf8');
  assert.match(source, /response\.status === 401/);
  assert.match(source, /loginRedirect/);
  assert.match(source, /window\.location\.replace/);
});

test('the login shell is reachable and carries no data of its own', () => {
  const html = readFileSync(new URL('../web/login.html', import.meta.url), 'utf8');
  assert.match(html, /id="login-form"[^>]*hidden/, 'the form must start hidden until the mode is known');
  assert.match(html, /autocomplete="current-password"/);
  assert.match(html, /aria-live="polite"/, 'a failure must be announced, not only painted');
  assert.match(html, /<label for="username"/);
  assert.match(html, /<label for="password"/);
  // No paper is fetched here: the page is the door, not a reader of anybody's rows.
  assert.ok(!/\/api\/papers|\/api\/agent\/runs/.test(html));
});
