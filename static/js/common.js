/* common.js — 全ページ共通ユーティリティ
 * HTML テンプレートからページ固有 JS より先に読み込むこと。
 * apiBase() はページ毎に異なるため各ページ JS に残す。
 */

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── テーマ（ライト/ダーク） ──────────────────────────────────────────
// data-theme 属性の初期値は各テンプレート <head> 先頭のインラインスクリプトが
// ペイント前に同期設定する（FOUC防止のため common.js より前に確定させる必要がある）。

function currentTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function applyThemeIcon() {
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = currentTheme() === 'dark' ? '☀️' : '🌙';
}

function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('theme', next);
  applyThemeIcon();
  if (typeof window.onThemeChange === 'function') window.onThemeChange();
}

function initTheme() {
  applyThemeIcon();
  document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);
}
initTheme();

// ── 通知トースト ────────────────────────────────────────────────────

function showNotif(msg, type = 'error') {
  const el = document.createElement('div');
  el.textContent = msg;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.setAttribute('aria-live', type === 'error' ? 'assertive' : 'polite');
  el.className = `notif notif-${type}`;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
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
  if (r.status === 401) { document.cookie = 'csrf_token=; max-age=0; path=/'; location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search); return null; }
  if (!r.ok) {
    if (r.status === 502 || r.status === 503 || r.status === 504)
      throw new Error(`サーバー再起動中 (${r.status})。しばらく待ってから再試行してください`);
    if (r.status === 404) throw new Error('NOT_FOUND');
    throw new Error(await r.text());
  }
  return r.json();
}

// ── サーバー生存通知（ブラウザ連動自動停止） ─────────────────────────────
// 全ページから /heartbeat を定期送信する。サーバー側は launch.py 経由
// （FINAPP_AUTO_SHUTDOWN=1）のときだけ途絶検知で自動停止する。本番では無害な no-op。
(() => {
  const beat = () => { fetch('/heartbeat', { method: 'POST', credentials: 'same-origin' }).catch(() => {}); };
  beat();
  setInterval(beat, 5000);
  // スリープ・タブ復帰時は即時送信して誤停止を防ぐ
  document.addEventListener('visibilitychange', () => { if (!document.hidden) beat(); });
})();
