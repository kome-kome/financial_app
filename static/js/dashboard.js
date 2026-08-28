function apiBase() { return ''; }

/* 「自動収集」カード（#563）。
 *
 * **静的な予定表を持たない。** 以前ここは「GitHub Actions で毎日 03:00 JST」と緑ドット付きで
 * 固定表示していたが、#503 で駆動がローカルのタスクスケジューラへ移った後もそのまま残り、
 * **トップページの最初に目に入る場所が嘘をついていた**（しかも 03:00 は GHA 時代ですら誤り）。
 * 名目の起動時刻も書かない——実起動は最大 +1h41m ずれる（#551）ので、足跡そのものを出す。
 *
 * 判定と語彙は /api/morning と同じ（batch_freshness.summarize が両方へ配る）。
 */
const SCHED_LEVEL = {
  fresh:   { dot: 'dot-green', head: 'ローカルのタスクスケジューラが毎日実行しています' },
  alert:   { dot: 'dot-red',   head: '夜間バッチが動いていません' },
  unknown: { dot: 'dot-amber', head: 'バッチの足跡を読めませんでした' },
};

function renderSchedule(b) {
  const dot  = document.getElementById('sched-dot');
  const head = document.getElementById('sched-headline');
  const grid = document.getElementById('sched-grid');
  if (!dot || !head || !grid) return;

  const rows  = (b && b.rows) || [];
  const night = rows.find(r => r.gates_verdict);
  const lv    = SCHED_LEVEL[(b && b.level)] || SCHED_LEVEL.unknown;
  dot.className = 'dot ' + lv.dot;

  if (!night) {
    head.textContent = lv.head;
    grid.innerHTML = '';
    return;
  }
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
      </div>`).join('') + `
      <div class="sched-item">
        <div class="sched-item-label">手動差分収集</div>
        <div class="sched-item-value"><a href="/collection" style="color:var(--status-info);text-decoration:none">収集画面から実行</a></div>
      </div>`;
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

    renderSchedule(d.batch);

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
