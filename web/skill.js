const command = document.querySelector('#skill-command')?.textContent?.trim() || '';
const notice = document.querySelector('#skill-notice');
let noticeTimer;

function announce(message) {
  if (!notice) return;
  notice.textContent = message;
  notice.classList.add('show');
  window.clearTimeout(noticeTimer);
  noticeTimer = window.setTimeout(() => notice.classList.remove('show'), 2600);
}

document.querySelectorAll('[data-tab]').forEach((tab) => {
  tab.addEventListener('click', () => {
    const selected = tab.dataset.tab;
    document.querySelectorAll('[data-tab]').forEach((item) => {
      const active = item === tab;
      item.setAttribute('aria-selected', String(active));
      item.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('[data-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.panel !== selected;
    });
  });

  tab.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll('[data-tab]')];
    const current = tabs.indexOf(tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 :
      (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    tabs[next].click();
  });
});

document.querySelector('#copy-command')?.addEventListener('click', async () => {
  if (!command) return;
  try {
    await navigator.clipboard.writeText(command);
    announce('Skill 命令已复制；页面没有执行它。');
  } catch {
    announce('浏览器未允许访问剪贴板，请手动复制命令。');
  }
});

const themeButton = document.querySelector('[data-theme-toggle]');
function paintTheme(theme) {
  const dark = theme === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  if (themeButton) {
    themeButton.textContent = dark ? '☼' : '◐';
    themeButton.setAttribute('aria-label', dark ? '切换到浅色模式' : '切换到深色模式');
    themeButton.title = dark ? '切换到浅色模式' : '切换到深色模式';
  }
}
paintTheme(document.documentElement.dataset.theme);
themeButton?.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('re0-theme', next); } catch { /* Storage may be disabled. */ }
  paintTheme(next);
});
