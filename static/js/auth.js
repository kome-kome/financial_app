// auth.js — 認証管理
// localStorage の auth_token キーを使い、未認証時は /login にリダイレクトする。
// app-header の API ステータスと preflight も初期化する。

import { apiFetch } from './core.js';

export async function initAuth() {
  // 認証要件は /api/auth/status から取得
  try {
    const d = await apiFetch('/api/auth/status');
    if (d && d.auth_required && !localStorage.getItem('auth_token')) {
      location.href = '/login';
      return;
    }
  } catch (_) { /* 認証要否が取れない場合は通過 */ }

  // ログアウトボタンを表示
  const btn = document.getElementById('logout-btn');
  if (btn) btn.style.display = 'inline-block';
}

export function logout() {
  localStorage.removeItem('auth_token');
  location.href = '/login';
}

// preflight: API疎通とデータ件数を取得して app-header に反映
export async function preflight() {
  const apiDot = document.getElementById('api-dot');
  try {
    const d = await apiFetch('/api/stats');
    if (apiDot) apiDot.style.background = 'var(--success)';
    // app-header に件数ラベルがあれば反映
    const label = document.getElementById('api-label');
    if (label) {
      label.textContent = `${d.companies.toLocaleString()}社 / ${d.records.toLocaleString()}件`;
      label.style.color = d.records > 0 ? 'var(--success)' : 'var(--danger)';
    }
    // ページ固有の preflight ハンドラがあれば呼ぶ
    if (typeof window.__app.onPreflight === 'function') window.__app.onPreflight(d);
    return d;
  } catch (e) {
    if (apiDot) apiDot.style.background = 'var(--danger)';
    return null;
  }
}

// 公開
window.__app = window.__app || {};
Object.assign(window.__app, { logout, preflight });

// ページ初期化
window.addEventListener('DOMContentLoaded', () => {
  initAuth();
  preflight();
});
