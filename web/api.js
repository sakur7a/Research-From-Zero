/** Same-origin API. No API keys or external provider requests in the browser. */
export async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 65000);
  try {
    const response = await fetch('/api' + path, {
      ...options,
      headers: {'Content-Type':'application/json', 'X-Re0-Client':'web', ...options.headers},
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    if (response.status === 204) return null;
    const data = await response.json();
    if (!response.ok) {
      const message = Array.isArray(data.detail) ? data.detail.map(x => `${x.loc?.slice(1).join('.') || ''}: ${x.msg}`).join('；') : data.detail;
      throw new Error(message || `请求失败 (${response.status})`);
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求等待超时；请刷新查看检查是否已保存，避免立即重复提交。');
    if (error instanceof TypeError) throw new Error('连接失败，请确认 re0 本地服务仍在运行。');
    throw error;
  } finally { clearTimeout(timer); }
}
