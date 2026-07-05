function apiBase() { return ''; }


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
        yearEl.style.color = cssVar('--status-good');
        yearMeta.innerHTML = `${periodStr}<br><span style="color:${cssVar('--status-good-text')}">✓ 最新年度を取得済み</span>`;
        yearCard.classList.remove('warn','alert');
      } else if (behind === 1) {
        yearEl.style.color = cssVar('--status-warn-text');
        yearMeta.innerHTML = `${periodStr}<br><span style="color:${cssVar('--status-warn-text')}">期待: ${d.expected_latest_year}（${behind}年遅れ）</span>`;
        yearCard.classList.add('warn'); yearCard.classList.remove('alert');
      } else {
        yearEl.style.color = cssVar('--status-bad-text');
        yearMeta.innerHTML = `${periodStr}<br><span style="color:${cssVar('--status-bad-text')}">期待: ${d.expected_latest_year}（${behind}年遅れ）</span>`;
        yearCard.classList.add('alert'); yearCard.classList.remove('warn');
      }
    } else {
      yearMeta.textContent = 'データなし';
    }

    // データ鮮度カード
    const FRESH_LABELS = {
      fresh:    {text: '最新',     cls: 'fr-fresh',    color: cssVar('--status-good-text')},
      ok:       {text: '良好',     cls: 'fr-ok',       color: cssVar('--status-info-text')},
      stale:    {text: 'やや古い', cls: 'fr-stale',    color: cssVar('--status-warn-text')},
      outdated: {text: '古い',     cls: 'fr-outdated', color: cssVar('--status-bad-text')},
      empty:    {text: 'データなし', cls: 'fr-empty',  color: cssVar('--text-muted')},
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



window.onThemeChange = loadStats;
initAuth();
loadStats();
