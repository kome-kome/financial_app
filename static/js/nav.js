// nav.js — 二段ナビ管理（aria-current/selected、キーボード操作）
//
// 1) primary-nav: location.pathname に応じて aria-current="page" を付与
// 2) page-tabs:   role="tablist" の中で role="tab" を制御
//    - showTab(tabId): 該当タブをアクティブ化し、対応 tabpanel を表示
//    - 矢印キー（Left/Right）で次/前のタブにフォーカス＆アクティブ化
//    - Home/End で先頭/末尾

window.addEventListener('DOMContentLoaded', () => {
  highlightPrimaryNav();
  initTabsKeyboard();
});

export function highlightPrimaryNav() {
  const path = location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('.primary-nav a[data-route]').forEach(a => {
    const route = a.dataset.route;
    const match = route === path || (route !== '/' && path.startsWith(route));
    if (match) a.setAttribute('aria-current', 'page');
    else       a.removeAttribute('aria-current');
  });
}

/**
 * showTab — page-tabs 内の tabId をアクティブ化。
 * 既存の `id="tab-<tabId>"` のパネルを表示、他を隠す。
 * 互換: 旧 showTab(t) の挙動を維持。
 */
export function showTab(tabId) {
  const tabs = document.querySelectorAll('#page-tabs [role="tab"]');
  tabs.forEach(t => {
    const isActive = t.dataset.tab === tabId;
    t.setAttribute('aria-selected', isActive ? 'true' : 'false');
    t.tabIndex = isActive ? 0 : -1;
  });
  // パネル切替
  const panels = document.querySelectorAll('[role="tabpanel"]');
  panels.forEach(p => {
    const isActive = p.dataset.tab === tabId || p.id === 'tab-' + tabId;
    p.classList.toggle('hidden', !isActive);
  });
}

function initTabsKeyboard() {
  const list = document.getElementById('page-tabs');
  if (!list) return;
  list.addEventListener('keydown', (e) => {
    const tabs = [...list.querySelectorAll('[role="tab"]')];
    if (!tabs.length) return;
    const idx = tabs.indexOf(document.activeElement);
    let nextIdx = -1;
    if (e.key === 'ArrowRight') nextIdx = (idx + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') nextIdx = (idx - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') nextIdx = 0;
    else if (e.key === 'End')  nextIdx = tabs.length - 1;
    if (nextIdx === -1) return;
    e.preventDefault();
    const next = tabs[nextIdx];
    next.focus();
    next.click();
  });
}

// 互換: 旧 onclick="showTab('xxx')" 用に window 公開
window.__app = window.__app || {};
Object.assign(window.__app, { showTab, highlightPrimaryNav });
// グローバル showTab も維持（既存 HTML 内 onclick 属性のため）
if (typeof window.showTab !== 'function') window.showTab = showTab;
