/* common.js — 全ページ共通ユーティリティ
 * HTML テンプレートからページ固有 JS より先に読み込むこと。
 * apiBase() はページ毎に異なるため各ページ JS に残す。
 */

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── 認証 ────────────────────────────────────────────────────────────

function _getCookie(name) {
  const m = document.cookie.match('(^|; )' + name + '=([^;]*)');
  return m ? decodeURIComponent(m[2]) : '';
}

async function initAuth() {
  try {
    const r = await fetch('/api/auth/status');
    const d = await r.json();
    if (d.auth_required) {
      if (!_getCookie('csrf_token')) {
        location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
        return;
      }
      const logoutBtn = document.getElementById('logout-btn');
      if (logoutBtn) logoutBtn.style.display = '';
    }
  } catch(e) { /* API 未起動時はスキップ */ }
}

async function logout() {
  try { await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }); } catch(e) {}
  location.href = '/login';
}

// ── API ────────────────────────────────────────────────────────────

async function apiFetch(path, opts = {}) {
  const heads = { 'Content-Type': 'application/json' };
  const _m = (opts.method || 'GET').toUpperCase();
  if (_m !== 'GET' && _m !== 'HEAD') heads['X-CSRF-Token'] = _getCookie('csrf_token');
  const r = await fetch(apiBase() + path, { credentials: 'same-origin', ...opts, headers: { ...heads, ...(opts.headers || {}) } });
  if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search); return null; }
  if (!r.ok) {
    if (r.status === 502 || r.status === 503 || r.status === 504)
      throw new Error(`サーバー再起動中 (${r.status})。しばらく待ってから再試行してください`);
    if (r.status === 404) throw new Error('NOT_FOUND');
    throw new Error(await r.text());
  }
  return r.json();
}
