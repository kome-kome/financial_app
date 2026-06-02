function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

function showNotif(msg, type='error'){
  const el = document.createElement('div');
  el.textContent = msg;
  el.setAttribute('role', type==='error' ? 'alert' : 'status');
  el.setAttribute('aria-live', type==='error' ? 'assertive' : 'polite');
  const bg = type==='error' ? '#ef4444' : type==='success' ? '#10b981' : '#3b82f6';
  el.style.cssText = `position:fixed;bottom:20px;right:20px;z-index:9999;padding:10px 16px;border-radius:6px;font-size:13px;color:#fff;max-width:420px;background:${bg};box-shadow:0 4px 12px rgba(0,0,0,.4)`;
  document.body.appendChild(el);
  setTimeout(()=>el.remove(), 4000);
}

function apiBase() { return ''; }

function _getCookie(name){
  const m = document.cookie.match('(^|; )' + name + '=([^;]*)');
  return m ? decodeURIComponent(m[2]) : '';
}
async function apiFetch(path) {
  const heads = {'Content-Type': 'application/json'};
  const r = await fetch(apiBase() + path, {headers: heads, credentials: 'same-origin'});
  if (r.status === 401) { location.href = '/login?next=/'; return null; }
  if(!r.ok){
    if(r.status===502||r.status===503||r.status===504)
      throw new Error(`サーバー再起動中 (${r.status})。しばらく待ってから再試行してください`);
    throw new Error(await r.text());
  }
  return r.json();
}

async function loadStats() {
  try {
    const d = await apiFetch('/api/stats');
    if (!d) return;
    document.getElementById('stat-companies').textContent = d.companies.toLocaleString();
    document.getElementById('stat-records').textContent   = d.records.toLocaleString();

    // 最新年度カード
    const yearEl = document.getElementById('stat-year');
    yearEl.textContent = d.latest_year ?? '—';
    const yearCard = document.getElementById('card-year');
    const yearMeta = document.getElementById('stat-year-meta');
    if (d.latest_year != null && d.expected_latest_year != null) {
      const behind = d.expected_latest_year - d.latest_year;
      const periodStr = d.latest_period_end ? `期末: ${d.latest_period_end}` : '';
      if (behind <= 0) {
        yearEl.style.color = '#10b981';
        yearMeta.innerHTML = `${periodStr}<br><span style="color:#86efac">✓ 最新年度を取得済み</span>`;
        yearCard.classList.remove('warn','alert');
      } else if (behind === 1) {
        yearEl.style.color = '#fcd34d';
        yearMeta.innerHTML = `${periodStr}<br><span style="color:#fcd34d">期待: ${d.expected_latest_year}（${behind}年遅れ）</span>`;
        yearCard.classList.add('warn'); yearCard.classList.remove('alert');
      } else {
        yearEl.style.color = '#fca5a5';
        yearMeta.innerHTML = `${periodStr}<br><span style="color:#fca5a5">期待: ${d.expected_latest_year}（${behind}年遅れ）</span>`;
        yearCard.classList.add('alert'); yearCard.classList.remove('warn');
      }
    } else {
      yearMeta.textContent = 'データなし';
    }

    // データ鮮度カード
    const FRESH_LABELS = {
      fresh:    {text: '最新',     cls: 'fr-fresh',    color: '#86efac'},
      ok:       {text: '良好',     cls: 'fr-ok',       color: '#93c5fd'},
      stale:    {text: 'やや古い', cls: 'fr-stale',    color: '#fcd34d'},
      outdated: {text: '古い',     cls: 'fr-outdated', color: '#fca5a5'},
      empty:    {text: 'データなし', cls: 'fr-empty',  color: '#64748b'},
    };
    const f = FRESH_LABELS[d.freshness] || FRESH_LABELS.empty;
    const freshDaysEl  = document.getElementById('stat-fresh-days');
    const freshMetaEl  = document.getElementById('stat-fresh-meta');
    const freshBadgeEl = document.getElementById('stat-fresh-badge');
    const freshCard    = document.getElementById('card-freshness');
    freshDaysEl.style.color = f.color;
    if (d.days_since_update == null) {
      freshDaysEl.textContent = '—';
    } else if (d.days_since_update === 0) {
      freshDaysEl.textContent = '本日';
    } else {
      freshDaysEl.textContent = `${d.days_since_update}日前`;
    }
    freshMetaEl.innerHTML = d.last_db_update
      ? `最終更新:<br>${esc(d.last_db_update)}`
      : '最終更新: —';
    freshBadgeEl.textContent = f.text;
    freshBadgeEl.className = 'freshness-badge ' + f.cls;
    freshCard.classList.remove('warn','alert');
    if (d.freshness === 'stale')    freshCard.classList.add('warn');
    if (d.freshness === 'outdated') freshCard.classList.add('alert');

    // 鮮度バナー（古い・遅れている場合だけ表示）
    const banner = document.getElementById('update-banner');
    const bmsg   = document.getElementById('update-banner-msg');
    const yearBehind = (d.latest_year != null && d.expected_latest_year != null)
      ? (d.expected_latest_year - d.latest_year) : 0;
    if (d.freshness === 'outdated' || yearBehind >= 2) {
      bmsg.textContent = `最新年度が${yearBehind}年遅れています（DB: ${d.latest_year ?? '—'} / 期待: ${d.expected_latest_year ?? '—'}）。差分収集を実行して最新化してください。`;
      banner.classList.add('show');
    } else if (d.freshness === 'stale' || yearBehind === 1) {
      bmsg.textContent = `最新の財務レコードから${d.days_since_update ?? '?'}日経過しています。差分収集の実行を検討してください。`;
      banner.classList.add('show');
    } else {
      banner.classList.remove('show');
    }

    document.getElementById('api-dot').className = 'dot dot-green';
    document.getElementById('api-label').textContent = 'API接続中';
  } catch(e) {
    document.getElementById('api-dot').className = 'dot dot-red';
    document.getElementById('api-label').textContent = 'API未接続';
  }
}


async function initAuth() {
  try {
    const r = await fetch(apiBase() + '/api/auth/status');
    const d = await r.json();
    if (d.auth_required && !_getCookie('csrf_token')) {
      location.href = '/login?next=/';
    }
  } catch(e) { /* API未起動時はスキップ */ }
}

initAuth();
loadStats();
