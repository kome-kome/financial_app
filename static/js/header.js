// header.js — 全画面共通の app-header を JS から差し込む
// 各HTMLは <div id="app-header-mount"></div> を <body> 直下に置くだけで OK。

function renderHeader() {
  const mount = document.getElementById('app-header-mount');
  if (!mount) return;
  mount.outerHTML = `
<header class="app-header" role="banner">
  <a href="/" class="brand" aria-label="ホーム">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">
      <path d="M3 3v18h18"/>
      <path d="M7 14l4-4 4 4 5-7"/>
    </svg>
    財務分析
  </a>
  <nav class="primary-nav" role="navigation" aria-label="主要ナビゲーション">
    <a href="/"           data-route="/">ホーム</a>
    <a href="/collection" data-route="/collection">収集</a>
    <a href="/analysis"   data-route="/analysis">分析</a>
    <a href="/models"     data-route="/models">モデル解説</a>
    <a href="/db"         data-route="/db">DB</a>
  </nav>
  <div class="api-status">
    <span class="dot" id="api-dot"></span>
    <span class="text-xs">API:</span>
    <input type="text" id="api-base" value="" placeholder="http://localhost:8000" aria-label="API base URL">
    <button class="btn btn-sm" id="btn-preflight">接続確認</button>
    <span class="text-xs" id="api-label"></span>
  </div>
  <button id="logout-btn" class="btn btn-sm" style="display:none">ログアウト</button>
</header>`;
}

// 同期描画（DOMContentLoaded 前でも、mount が存在すれば即時挿入）
renderHeader();

window.addEventListener('DOMContentLoaded', () => {
  // 接続確認ボタンとログアウトのハンドラ
  const btn = document.getElementById('btn-preflight');
  if (btn) btn.addEventListener('click', () => window.__app.preflight && window.__app.preflight());
  const logout = document.getElementById('logout-btn');
  if (logout) logout.addEventListener('click', () => window.__app.logout && window.__app.logout());
});
