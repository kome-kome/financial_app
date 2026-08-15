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
// （FINAPP_AUTO_SHUTDOWN=1）のときだけ途絶検知で自動停止する。
// auto_shutdown=false（Render 本番・手動起動）なら初回応答で送信を打ち切り、
// 無駄なリクエストで free instance の spin-down を妨げない。
(() => {
  let timer = null;
  const beat = async () => {
    try {
      const r = await fetch('/heartbeat', { method: 'POST', credentials: 'same-origin' });
      const j = await r.json();
      if (!j.auto_shutdown && timer) { clearInterval(timer); timer = null; }
    } catch (_) { /* サーバー再起動中・旧サーバー(404)は無視して継続 */ }
  };
  beat();
  timer = setInterval(beat, 5000);
  // スリープ・タブ復帰時は即時送信して誤停止を防ぐ
  document.addEventListener('visibilitychange', () => { if (!document.hidden && timer) beat(); });
})();

// ── 接続先バッジ（#481 B-1） ─────────────────────────────────────────────
// ローカル読取レプリカへ繋いでいるときだけ右下に常時表示する。ミラーは pull/sync した
// 時点で止まるため、**どちらの DB を見ているか分からないと古いスコアを最新と誤読する**
// （#438 の「静かな配信停止」と同型）。本番接続時は何も描画しない＝普段の見た目は不変。
// テンプレートは触らない（common.js は全9ページが読み込むため、ここ1箇所で全画面に出る）。
(() => {
  const show = (info) => {
    if (!info || !info.db_is_local) return;   // 本番接続時は何も出さない
    const el = document.createElement('div');
    el.id = 'db-target-badge';
    el.textContent = `⛁ ${info.db_label} — 同期時点のデータ`;
    el.title = '本番（Supabase）ではなくローカル DB を参照しています。'
             + '最後にミラーを同期した時点のデータで、夜間バッチの最新スコアは含まれません。';
    Object.assign(el.style, {
      position: 'fixed', right: '12px', bottom: '12px', zIndex: '9999',
      padding: '6px 12px', borderRadius: '999px',
      background: 'var(--status-warn-bg, #78350f)',
      color: 'var(--status-warn-text, #fef3c7)',
      border: '1px solid var(--status-warn, #f59e0b)',
      font: '600 12px/1.4 system-ui, sans-serif',
      boxShadow: '0 2px 8px rgba(0,0,0,.25)', pointerEvents: 'auto', cursor: 'default',
    });
    document.body.appendChild(el);
  };
  const load = async () => {
    try {
      const r = await fetch('/api/system/info', { credentials: 'same-origin' });
      if (!r.ok) return;      // ログイン前は 401。認証後の再読込で出るので黙って諦める
      show(await r.json());
    } catch (_) { /* サーバー再起動中などは無視 */ }
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
})();
