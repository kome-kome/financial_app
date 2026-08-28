/* morning.js — 朝の推奨ページ（Issue #423 子3）
 *
 * 学習・再計算はしない。GET /api/morning が返す「永続化済みスコア＋鮮度ブロック」を
 * そのまま描画する。鮮度は必須表示で、赤（alert）でもランキングは隠さない
 * ——隠すと別経路で古い値を見に行くだけなので、出した上で発注非推奨を明示する。
 *
 * CSP 対応のためインラインイベントハンドラは使わず data-* ＋イベント委譲で結線する。
 */
function apiBase() { return ''; }

const LEVEL_LABEL = { fresh: '最新', warn: '注意', alert: '古い', empty: 'データなし', unknown: '判定不能' };
const VERDICT_TEXT = {
  fresh: { cls: 'verdict-fresh', icon: '✓', head: 'この結果で発注して問題ありません' },
  warn:  { cls: 'verdict-warn',  icon: '!', head: '一部のデータが古めです（内容を確認してから発注してください）' },
  alert: { cls: 'verdict-alert', icon: '×', head: 'この結果で発注しないでください（データが古い/欠けています）' },
};

function levelClass(level) {
  return level === 'fresh' ? '' : (level === 'warn' ? 'warn' : 'alert');
}

function fmt(v, digits = 2) {
  return (v === null || v === undefined) ? '—' : Number(v).toFixed(digits);
}

function renderVerdict(f) {
  const v = VERDICT_TEXT[f.overall_verdict] || VERDICT_TEXT.alert;
  const box = document.getElementById('verdict');
  box.className = 'verdict ' + v.cls;
  document.getElementById('verdict-head').textContent = `${v.icon} ${v.head}`;
  document.getElementById('verdict-reasons').innerHTML =
    (f.reasons || []).map(r => `<li>${esc(r)}</li>`).join('') ||
    '<li>株価・スコア・マクロのいずれも鮮度基準を満たしています</li>';
  const link = document.getElementById('verdict-link');
  // #503 以降、次に見るのはワークフローの実行履歴ではなくローカル運用の手順書（#561）
  link.href = f.runbook_url || '#';
}

function freshCard(label, level, value, sub, url) {
  return `<div class="fresh-card ${levelClass(level)}">
    <div class="fresh-card-label">${esc(label)}</div>
    <div class="fresh-card-value">${esc(value)}</div>
    <div class="fresh-card-sub">${sub}</div>
    ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">復旧手順を見る →</a>` : ''}
  </div>`;
}

/* 「昨夜そもそもバッチが走ったのか」（#561）。
 *
 * スコアが古いことと、バッチが起動していないことは原因が違う（前者は producer の失敗、
 * 後者は起動の失敗）のに、これが無い間は画面上で同じ顔をしていた。**健全なのに
 * 「止まっているのでは」と疑われる**のを防ぐのがこのカードの役目なので、
 * 正常時も「いつ走ったか」を必ず出す（異常時だけ出すと沈黙が読めない）。
 *
 * verdict へ効くのは夜間バッチだけ。月次と watchdog は同じカードの中に併記する
 * （カードを3枚に割ると主役の株価鮮度が埋もれる）。
 */
function batchCard(b) {
  const rows = b.rows || [];
  const night = rows.find(r => r.gates_verdict) || {};
  const others = rows.filter(r => !r.gates_verdict);
  if (b.level === 'unknown') {
    return freshCard('夜間バッチ', 'unknown', '判定不能',
      '足跡（app_settings）を読めませんでした', b.url);
  }
  const age = (night.age_h === null || night.age_h === undefined)
    ? '—' : `${Math.round(night.age_h)}時間前`;
  const sub = [
    `${LEVEL_LABEL[night.level] || '—'}・${age}（閾値 ${Math.round(night.stale_h || 0)}時間）`,
    `最終成功 ${esc(night.last_success || '—')}`,
    ...others.map(r => `${esc(r.label)}: ${esc(r.last_run || '未実行')}`
      + (r.level === 'fresh' ? '' : `（${LEVEL_LABEL[r.level] || '要確認'}）`)),
    b.command ? `手動実行 <code>${esc(b.command)}</code>` : '',
  ].filter(Boolean).join('<br>');
  return freshCard('夜間バッチ', b.level, night.last_run || '未実行', sub, b.url);
}

function renderFreshness(f) {
  const p = f.price || {};
  const g = f.gap_ratio || {};
  const m = f.mu || {};
  const mac = f.macro || {};
  document.getElementById('fresh-grid').innerHTML = [
    // **バッチを先頭に置く。** 下流（株価・スコア）の古さはバッチが走らなかった結果でしかない
    batchCard(f.batch || {}),
    freshCard('株価 as-of（中央値）', p.level,
      p.price_asof_p50 || '—',
      `${LEVEL_LABEL[p.level] || '—'}・${p.stale_bdays ?? '—'}営業日前<br>${p.n_stale_over_5d ?? 0}銘柄が5営業日超の遅れ`,
      null),
    freshCard('乖離率（sector_ols）', g.level,
      g.age_days === null || g.age_days === undefined ? '—' : `${g.age_days}日前`,
      `${g.computed_at || '未生成'}<br>${g.n_rows ?? 0}社ぶん`,
      g.url),
    freshCard(`μ̂（${esc(m.source || '—')}）`, m.level,
      m.snapshot_date || '—',
      m.level === 'empty' ? '未蓄積' :
        `${LEVEL_LABEL[m.level] || '—'}・${m.age_bdays ?? '—'}営業日前<br>最古 ${m.snapshot_date_min || '—'}／古い銘柄 ${m.n_stale ?? 0}`,
      m.url),
    freshCard('マクロ系列', mac.level,
      mac.n_critical_bad === null || mac.n_critical_bad === undefined
        ? '—' : (mac.n_critical_bad === 0 ? '正常' : `${mac.n_critical_bad}系列 不良`),
      (mac.worst || []).length
        ? esc((mac.worst || []).map(w => `${w.code}(${w.last || '欠測'})`).join('・'))
        : '既定モデルが使う系列はすべて健全',
      mac.url),
  ].join('');
}

function renderRanking(rec, priceLevel) {
  const rows = (rec && rec.results) || [];
  const body = document.getElementById('rank-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="10" class="muted" style="padding:18px">'
      + '該当銘柄がありません（データ未収集か、指標カバレッジ不足）</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => `<tr>
    <td class="rank-num">${r.rank}</td>
    <td>${esc(r.sec_code || '—')}</td>
    <td class="rank-name"><a href="/company/${esc(r.edinet_code)}" style="color:inherit;text-decoration:none">${esc(r.company_name || '—')}</a></td>
    <td>${esc(r.industry || '—')}</td>
    <td class="num rank-score">${fmt(r.score, 3)}</td>
    <td class="num">${fmt(r.per, 1)}</td>
    <td class="num">${fmt(r.pbr, 2)}</td>
    <td class="num">${fmt(r.roe, 1)}</td>
    <td class="num">${r.gap_ratio === null || r.gap_ratio === undefined ? '—' : fmt(r.gap_ratio * 100, 1) + '%'}</td>
    <td class="${priceLevel === 'fresh' ? '' : 'stale-asof'}">${esc(r.price_asof || '—')}</td>
  </tr>`).join('');
}

async function loadMorning() {
  const preset = document.getElementById('mor-preset').value;
  const topN   = document.getElementById('mor-topn').value;
  const dot    = document.getElementById('api-dot');
  const label  = document.getElementById('api-label');
  try {
    const d = await apiFetch(`/api/morning?preset=${encodeURIComponent(preset)}&top_n=${topN}`);
    if (!d) return;
    renderVerdict(d.freshness);
    renderFreshness(d.freshness);
    renderRanking(d.recommend, (d.freshness.price || {}).level);
    document.getElementById('mor-generated').textContent = `集計時刻: ${d.generated_at}`;
    const lv = d.freshness.overall_verdict;
    dot.className = 'dot ' + (lv === 'fresh' ? 'dot-green' : lv === 'warn' ? 'dot-amber' : 'dot-red');
    label.textContent = lv === 'fresh' ? '最新データ' : (lv === 'warn' ? '一部が古い' : '古い/欠測あり');
  } catch (e) {
    dot.className = 'dot dot-red';
    label.textContent = '取得失敗';
    document.getElementById('verdict-head').textContent = '× 朝の集計を取得できませんでした';
    document.getElementById('verdict-reasons').innerHTML = `<li>${esc(e.message)}</li>`;
  }
}

// data-change 属性を持つコントロールの変更で再読込（インラインハンドラを使わない）
document.addEventListener('change', (ev) => {
  if (ev.target instanceof HTMLElement && ev.target.dataset.change === 'reload') loadMorning();
});

loadMorning();
