// core.js — 全画面共通ユーティリティ
// apiFetch / esc / showNotif / apiBase / fmt 系。
// グローバル参照用に window.__app へも公開する（onclick 属性互換のため）。

export function apiBase() {
  const el = document.getElementById('api-base');
  return (el ? el.value : '').trim().replace(/\/$/, '');
}

export async function apiFetch(path, opts = {}) {
  const token = localStorage.getItem('auth_token') || '';
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const r = await fetch(apiBase() + path, { ...opts, headers });
  if (r.status === 401) {
    localStorage.removeItem('auth_token');
    location.href = '/login';
    return;
  }
  if (!r.ok) {
    if ([502, 503, 504].includes(r.status)) {
      throw new Error(`サーバー再起動中 (${r.status})。しばらく待ってから再試行してください`);
    }
    throw new Error(await r.text());
  }
  // 204 No Content や空ボディは null を返す
  const text = await r.text();
  return text ? JSON.parse(text) : null;
}

export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function showNotif(msg, type = 'error') {
  const el = document.createElement('div');
  el.className = 'toast ' + (type === 'success' ? 'success' : type === 'info' ? '' : 'error');
  el.textContent = msg;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.setAttribute('aria-live', type === 'error' ? 'assertive' : 'polite');
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

export const fmt0 = (n) => (n == null ? '-' : Math.round(n).toLocaleString());
export const fmt1 = (n) => (n == null ? '-' : (Math.round(n * 10) / 10).toLocaleString());
export const fmt2 = (n) => (n == null ? '-' : (Math.round(n * 100) / 100).toLocaleString());

// onclick 属性互換のためのグローバル名前空間
window.__app = window.__app || {};
Object.assign(window.__app, { apiBase, apiFetch, esc, showNotif, fmt0, fmt1, fmt2 });

// FOUC 防止解除: app.css の :root visibility を解放
// （DOMContentLoaded を待たず、このスクリプトが評価された時点で開始）
document.documentElement.classList.add('ready');
