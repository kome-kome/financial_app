/* common.js — 全ページ共通ユーティリティ
 * HTML テンプレートからページ固有 JS より先に読み込むこと。
 * apiBase() はページ毎に異なるため各ページ JS に残す。
 */

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── TOPIX 移行措置・新規採用の事実の印（#1013）──────────────────────────────
// 文言（印・注記・表の上の 1 行・バナー）はサーバ（index_event_notes.py）が作る。ここは表示だけ
// （禁止語の検査をサーバの 1 か所でかけるため・ここで文言を足さない）。色は警告系で、赤は使わない。
function indexEventBadge(ev) {
  if (!ev) return '';
  return ` <span class="tag tag-amber" title="${esc(ev.note)}">${esc(ev.label)}</span>`;
}

function indexEventsNote(meta) {
  if (!meta) return '';
  const text = meta.ledger_ok === false ? meta.banner : meta.summary;
  return text ? `<span style="color:var(--text-muted)">※ ${esc(text)}</span>` : '';
}

// ── 金額の単位換算（億） ──────────────────────────────────────────────
// API は財務の金額を「円」、時価総額（market_cap 系）だけを「百万円」で返す（収集が
// 株価×株数/1e6 で作る）。億への換算はここだけに置き、ページ側で書き写さない（#911:
// 収集画面が両方を /1e6 していたため、円の列は 100 倍、時価総額は 1 万分の 1 で出ていた）。
// 空欄（null・NaN）は null のまま返す（`null/1e8` は 0 になり、空欄が「0億」に化ける）。
const OKU = 1e8;
function toOku(v){ return (v==null || isNaN(v)) ? null : v/OKU; }       // 円 → 億円
function mnToOku(v){ return (v==null || isNaN(v)) ? null : v/100; }     // 百万円 → 億円（market_cap 系）

// ── テーマ（ライト/ダーク） ──────────────────────────────────────────
// data-theme 属性の初期値は各テンプレートの <head> が先に読む static/js/theme-init.js が
// ペイント前に同期設定する（FOUC防止のため common.js より前に確定させる必要がある。
// CSP でインラインスクリプトを禁じているので外部ファイルにしてある）。

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

// エラーは閉じるまで残す（読み終える前に消えると、何が失敗したのか分からない）。成功は 4 秒で消す。
// 見た目（.notif-stack / .notif）は showNotif を呼ぶ画面のテンプレートが持つ（tests/test_templates_a11y.py が照合）。
function showNotif(msg, type = 'error') {
  let stack = document.getElementById('notif-stack');
  if (!stack) {
    stack = document.createElement('div');
    stack.id = 'notif-stack';
    stack.className = 'notif-stack';
    document.body.appendChild(stack);
  }
  const el = document.createElement('div');
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.setAttribute('aria-live', type === 'error' ? 'assertive' : 'polite');
  el.className = `notif notif-${type}`;
  const text = document.createElement('span');
  text.className = 'notif-msg';
  text.textContent = msg;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'notif-close';
  close.setAttribute('aria-label', '閉じる');
  close.textContent = '×';
  close.addEventListener('click', () => el.remove());
  el.append(text, close);
  stack.appendChild(el);
  if (type !== 'error') setTimeout(() => el.remove(), 4000);
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
    // 本文の detail だけを message に持たせ、status を付けて投げる。404 でも理由の文を捨てない
    // （以前は 'NOT_FOUND' に置き換えていて「先に業種別OLSを実行してください」が消え、
    //  ほかの失敗は {"detail":…} の生の JSON が通知に出ていた・#1175）。見分けは e.status で行う。
    const err = new Error(_detailText(await r.text()) || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

// API の失敗の本文から、人に見せる文だけを取り出す。FastAPI は {"detail": "…"}、
// 入力の検証エラー（422）は {"detail": [{"msg": "…"}, …]} で返す。JSON でない本文はそのまま返す。
function _detailText(body) {
  if (typeof body !== 'string' || !body) return body || '';
  let j;
  try { j = JSON.parse(body); } catch (_) { return body; }
  const d = j && j.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map(x => (x && x.msg) || JSON.stringify(x)).join(' / ');
  return d != null ? JSON.stringify(d) : body;
}

// 例外から通知に出す文を取り出す（全画面共通）。apiFetch の例外は既に detail だけを持つが、
// fetch を直に使う経路の生の JSON もここで剥がす。
function _errDetail(e) {
  return _detailText(e && e.message != null ? String(e.message) : String(e));
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

// ── 接続先バッジ（#481 B-1・#503 で向きを反転） ──────────────────────────
// **警告を出す向きは「どちらが正本か」で決まる。** 2026-08-20 に正本がローカル PostgreSQL
// へ移った（#503・ADR-0038）ので、ローカル接続は平常運転＝何も描画しない。代わりに
// **Supabase へ繋いでいるとき**に出す——あちらは 2026-08-07 の断面で更新を止めてあり、
// Render の閲覧用に残しているだけなので、そうと知らずに読むと古いスコアを最新と誤読する
// （#438 の「静かな配信停止」と同型）。
//
// 反転前は逆だった（ローカル接続時に警告）。**その向きのまま残すと、平常運転で毎回警告が
// 出て狼少年になり、本当に古い断面を見ているときに気づけなくなる。**
// テンプレートは触らない（common.js は全9ページが読み込むため、ここ1箇所で全画面に出る）。
(() => {
  const show = (info) => {
    if (!info || info.db_is_local) return;   // ローカル＝正本。平常運転なので何も出さない
    const el = document.createElement('div');
    el.id = 'db-target-badge';
    el.textContent = `⛁ ${info.db_label} — 2026-08-07 の断面`;
    el.title = '正本のローカル DB ではなく Supabase を参照しています。'
             + 'Supabase は 2026-08-07 で更新を止めた閲覧用の断面で、'
             + 'それ以降の収集結果と夜間バッチのスコアは含まれません（#503・ADR-0038）。';
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

// ── バッチ実行中の表示（#851） ───────────────────────────────────────────
// ローカルバッチが走っている間は、裏で重い作業をすると同じ入力から違う答えが出る。分析画面は
// heavy の安全柵として専用の帯（#batch-activity-banner）を持つが、他の画面では見えなかった。
// ここ1箇所で全画面に出す（分析画面は専用の帯に任せて重ねない）。読むのはローカルの
// マーカーのファイルだけなので、Render では常に空＝何も出さない。接続先バッジ（右下）と
// 重ならないよう左下に置く。開いた時点の状態だけを出す（ポーリングはしない）。
(() => {
  const load = async () => {
    if (document.getElementById('batch-activity-banner')) return;
    try {
      const r = await fetch('/api/batch/activity', { credentials: 'same-origin' });
      if (!r.ok) return;      // ログイン前は 401
      const d = await r.json();
      if (!d || !d.message) return;
      const el = document.createElement('div');
      el.id = 'batch-activity-badge';
      el.setAttribute('role', 'status');
      el.textContent = `⏳ ${d.message}`;
      Object.assign(el.style, {
        position: 'fixed', left: '12px', bottom: '12px', zIndex: '9999',
        maxWidth: 'calc(100vw - 24px)', boxSizing: 'border-box',
        padding: '6px 12px', borderRadius: '12px',
        background: 'var(--status-warn-bg, #78350f)',
        color: 'var(--status-warn-text, #fef3c7)',
        border: '1px solid var(--status-warn, #f59e0b)',
        font: '600 12px/1.4 system-ui, sans-serif',
        boxShadow: '0 2px 8px rgba(0,0,0,.25)', cursor: 'default',
      });
      document.body.appendChild(el);
    } catch (_) { /* サーバー再起動中などは無視 */ }
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
})();

// ── 画面の状態を URL に残す（#1213）──────────────────────────────────────────
// タブ・プリセット・スクリーニングの条件を URL に載せ、再読込・ブラウザの戻る・URL の共有で
// 同じ状態へ戻れるようにする。**読み書きはここ 1 か所**（朝の推奨・収集・企業詳細が呼ぶ）。
//
// 書くのは replaceState だけ。pushState にしない＝タブを切り替えた回数だけ履歴が積まれ、
// 「戻る」で画面を出られなくなる（分析画面の ?tab= と同じ理由）。
// **読んだ値はそのまま信じない。** URL は人が手で書き換えられるので、呼ぶ側が
// 「タブの一覧にあるか」「選択肢にあるか」「数として読めるか」を確かめ、外れたら既定で開く。
function urlStateGet(key) {
  return new URLSearchParams(window.location.search).get(key);
}

// pairs = {キー: 値}。null / undefined / '' はそのキーを消す。ほかのキーとハッシュは保つ。
function urlStateSet(pairs) {
  const q = new URLSearchParams(window.location.search);
  Object.entries(pairs).forEach(([k, v]) => {
    if (v === null || v === undefined || v === '') q.delete(k);
    else q.set(k, String(v));
  });
  const s = q.toString();
  const next = window.location.pathname + (s ? '?' + s : '') + window.location.hash;
  const now  = window.location.pathname + window.location.search + window.location.hash;
  if (next !== now) history.replaceState(null, '', next);
}

// <select> へ URL の値を戻す。選択肢に無い値は無視して既定のまま（戻せたら true）。
function urlStateApplySelect(id, key) {
  const v  = urlStateGet(key);
  const el = document.getElementById(id);
  if (v === null || !el || ![...el.options].some(o => o.value === v)) return false;
  el.value = v;
  return true;
}

// ── 「自動収集」の表（ホーム #563・収集画面 #1212）─────────────────────────────
// **静的な予定表を持たない。** 以前ホームは「GitHub Actions で毎日 03:00 JST」と緑ドット付きで
// 固定表示していたが、#503 で駆動がローカルのタスクスケジューラへ移った後もそのまま残り、
// **トップページの最初に目に入る場所が嘘をついていた**（しかも 03:00 は GHA 時代ですら誤り）。
// 名目の起動時刻も書かない——実起動は最大 +1h41m ずれる（#551）ので、足跡そのものを出す。
//
// 判定と語彙は /api/morning と同じ（batch_freshness.summarize が両方へ配る）。
// **描く関数はここ 1 か所**（ホームと収集画面が呼ぶ。写すと片方だけ古くなる）。
// 置き場（#sched-dot・#sched-headline・#sched-grid）と CSS は使う画面のテンプレートが持つ。
const SCHED_LEVEL = {
  fresh:   { dot: 'dot-green', head: 'ローカルのタスクスケジューラが毎日実行しています' },
  alert:   { dot: 'dot-red',   head: '夜間バッチが動いていません' },
  unknown: { dot: 'dot-amber', head: 'バッチの足跡を読めませんでした' },
};

// b = /api/stats の batch（{level, rows, url}）。
// opts.manualLink: 末尾に「収集画面から実行」のタイルを出す（ホームだけ。収集画面は自分自身なので出さない）。
function renderSchedule(b, opts) {
  const dot  = document.getElementById('sched-dot');
  const head = document.getElementById('sched-headline');
  const grid = document.getElementById('sched-grid');
  if (!dot || !head || !grid) return;

  const rows  = (b && b.rows) || [];
  const night = rows.find(r => r.gates_verdict);
  const level = SCHED_LEVEL[(b && b.level)] ? b.level : 'unknown';
  const lv    = SCHED_LEVEL[level];
  dot.className = 'dot ' + lv.dot;

  if (!night) {
    head.textContent = lv.head;
    grid.innerHTML = '';
  } else {
    const age = night.age_h === null || night.age_h === undefined
      ? '' : `・${Math.round(night.age_h)}時間前`;
    head.textContent = night.level === 'fresh'
      ? `${lv.head}（最終実行 ${night.last_run}${age}）`
      : `${lv.head}（最終実行 ${night.last_run || '記録なし'}${age}・閾値 ${Math.round(night.stale_h || 0)}時間）`;

    grid.innerHTML = rows.map(r => `
      <div class="sched-item">
        <div class="sched-item-label">${esc(r.label)}</div>
        <div class="sched-item-value"${r.level === 'fresh' ? '' : ' style="color:var(--status-bad-text)"'}>${esc(r.last_run || '未実行')}</div>
        <div class="sched-item-label">${esc(r.task_name)}</div>
      </div>`).join('') + ((opts && opts.manualLink) ? `
      <div class="sched-item">
        <div class="sched-item-label">財務の手動差分収集（株価は夜間バッチ）</div>
        <div class="sched-item-value"><a href="/collection" style="color:var(--status-info);text-decoration:none">収集画面から実行</a></div>
      </div>` : '');
  }

  // 遅れ・未実行・判定不能のときだけ、復旧手順（docs/DEPLOYMENT.md）への入口を見出しの隣に出す（#1212）。
  // 「赤いのは分かったが次に何を見ればいいのか」で止まらせない。http(s) 以外の値は出さない。
  const needsHelp = level !== 'fresh' || rows.some(r => r.level !== 'fresh');
  if (needsHelp && b && /^https?:\/\//.test(b.url || '')) {
    const a = document.createElement('a');
    a.href = b.url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = '復旧手順を見る';
    a.style.cssText = 'margin-left:8px;color:var(--status-info);white-space:nowrap';
    head.appendChild(a);
  }
}

// ── 用語の吹き出し（/guide の .term・分析画面の .gloss）────────────────────────────
// 吹き出し（::after）は用語の左端から右へ伸びる。本文の右端を越える用語だけ .tip-flip で右端揃えに
// して、切れずに読めるようにする（#888）。外寸は CSS から読む——::after は * の box-sizing を受けない
// ので、max-width に余白と枠線を足したものが最大の外寸。狭い本文では右端揃えでも左端を越えるので、
// 外寸を本文の幅までに抑え（--tip-room）、越える分だけ右へ戻す（--tip-shift）。ホバーとフォーカス
// （Tab・タップ）の両方で開くので両方で測る（#893）。本文の箱（.content）が無い画面は画面幅で測る。
// 2 画面で同じ処理を使うため、ここに 1 つだけ置く（複製すると片方だけ直って食い違う・#1137）。
const TIP_SELECTOR = '.term, .gloss';
function placeTip(term) {
  const box = (term.closest('.content') || document.documentElement).getBoundingClientRect();
  const tip = getComputedStyle(term, '::after');
  const frame = ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
    .reduce((sum, prop) => sum + parseFloat(tip[prop]), 0);
  term.style.setProperty('--tip-room', `${box.width - frame}px`);
  const tipWidth = parseFloat(tip.maxWidth) + frame;  // tip は live なので --tip-room を反映した値
  const rect = term.getBoundingClientRect();
  const flip = rect.left + tipWidth > box.right;
  term.classList.toggle('tip-flip', flip);
  term.style.setProperty('--tip-shift', `${flip ? Math.max(0, box.left + tipWidth - rect.right) : 0}px`);
}
['mouseover', 'focusin'].forEach(type => document.addEventListener(type, e => {
  const term = e.target.closest(TIP_SELECTOR);
  if (term) placeTip(term);
}));

// 実行前の確認（画面内の <dialog>・#1185。分析画面と収集画面で共有・#1199）。ブラウザ標準の confirm() は既定の
// ボタンが「OK」で、Enter 一打で数分〜数時間の処理が始まり、文面に強調も持てない。使う画面は
// <dialog id="confirm-dialog"> とその CSS を持つ（DESIGN.md「実行前の確認」）。showModal() で開くと背後は操作できず（フォーカスは
// 窓の中に閉じる）、Esc で閉じる。既定のフォーカスは「やめる」（HTML の autofocus）。
// lines の要素は文字列か {text, strong}。文面は textContent で入れる（分析名を HTML として読まない）。
// <dialog> が使えない環境では confirm() へ戻す（確認を飛ばして実行する側へ倒さない）。
function confirmDialog({title, lines = [], question = '', okLabel = '実行する'}) {
  const items = lines.map(l => (typeof l === 'string' ? {text: l, strong: false} : l));
  const dlg = document.getElementById('confirm-dialog');
  if (!dlg || typeof dlg.showModal !== 'function') {
    return Promise.resolve(confirm(
      `${title}\n\n${items.map(l => `・${l.text}`).join('\n')}${question ? `\n\n${question}` : ''}`));
  }
  if (dlg.open) return Promise.resolve(false);   // 開いている確認に重ねない
  document.getElementById('confirm-dialog-title').textContent = title;
  const ul = document.getElementById('confirm-dialog-lines');
  ul.replaceChildren(...items.map(l => {
    const li = document.createElement('li');
    if (l.strong) {
      const s = document.createElement('strong');
      s.textContent = l.text;
      li.appendChild(s);
    } else {
      li.textContent = l.text;
    }
    return li;
  }));
  const q = document.getElementById('confirm-dialog-question');
  q.textContent = question;
  q.classList.toggle('hidden', !question);
  document.getElementById('confirm-dialog-ok').textContent = okLabel;
  // Esc で閉じたときは returnValue が変わらない＝前回の 'ok' が残るので、開くたびに空へ戻す
  dlg.returnValue = '';
  const opener = document.activeElement;
  const form = dlg.querySelector('form');
  return new Promise(resolve => {
    // 結果は submit（押したボタン）と cancel（Esc）で確定する。close イベントは閉じた後に遅れて届き、
    // 次の確認を開いた後に前回の分が届くことがある（そのとき returnValue は空に戻っている＝「実行する」が
    // 「やめる」にすり替わる）ので結果の主には使わない。close は Esc が cancel を出さずに閉じた場合
    // （連打の抑止）の受け皿で、開き直した後に届いた前回の close には反応しない。
    const finish = ok => {
      form.removeEventListener('submit', onSubmit);
      dlg.removeEventListener('cancel', onCancel);
      dlg.removeEventListener('close', onClose);
      // 閉じ終わってから押したボタンへフォーカスを戻す（submit・cancel は閉じる直前に届く）
      setTimeout(() => {
        if (!dlg.open && opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
      }, 0);
      resolve(ok);
    };
    const onSubmit = e => finish(!!(e.submitter && e.submitter.value === 'ok'));
    const onCancel = () => finish(false);
    const onClose = () => { if (!dlg.open) finish(dlg.returnValue === 'ok'); };
    form.addEventListener('submit', onSubmit);
    dlg.addEventListener('cancel', onCancel);
    dlg.addEventListener('close', onClose);
    dlg.showModal();
  });
}
