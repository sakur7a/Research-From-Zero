/* The theme, applied before first paint.
 *
 * This was an inline `<script>` in all four pages, which the app's own content-security-policy
 * (`script-src 'self'`, no `'unsafe-inline'`) blocks: every page was logging a policy violation on
 * load, and dark-mode readers saw the light theme flash first. `theme.js` still applies the stored
 * choice later, so nothing appeared broken — which is exactly why it took a real browser reading the
 * real headers to notice. An external file is allowed by the same rule, so nothing about the policy
 * has to be loosened for this to work.
 */
(function () {
  var stored = null;
  try { stored = localStorage.getItem('re0-theme'); } catch (error) { /* private mode, or blocked */ }
  document.documentElement.dataset.theme = stored === 'light' || stored === 'dark' ? stored : 'light';
})();
