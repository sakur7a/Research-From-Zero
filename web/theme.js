// re0 · 主题切换(浅色/深色),由 / (agent) 与 /library (library) 两个页面共享。
// 初始主题由两个 HTML 头部内联脚本写入 <html data-theme>,避免闪烁;这里负责
// 切换按钮、localStorage 持久化与 meta theme-color 同步。
const KEY = 're0-theme';
const META_COLORS = { light: '#f7f5fa', dark: '#1a1628' };
const SUN = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 13.2A8.5 8.5 0 1 1 10.8 3 6.8 6.8 0 0 0 21 13.2Z"/></svg>';

export function currentTheme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

function paint(theme) {
  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', META_COLORS[theme]);
  const dark = theme === 'dark';
  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.innerHTML = dark ? SUN : MOON;
    button.setAttribute('aria-label', dark ? '切换到浅色模式' : '切换到深色模式');
    button.title = dark ? '切换到浅色模式' : '切换到深色模式';
  });
}

// 幂等:重复调用不会叠加监听器(onclick 覆盖),供 app.js 每次重渲染后调用。
export function initTheme() {
  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    if (button.dataset.themeBound) { paint(currentTheme()); return; }
    button.dataset.themeBound = '1';
    button.onclick = () => {
      const next = currentTheme() === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(KEY, next); } catch { /* 隐私模式等场景忽略 */ }
      paint(next);
    };
  });
  paint(currentTheme());
}
