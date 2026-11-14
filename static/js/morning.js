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
// 総合判定は「この並びで発注してよいか」。赤はランキングの元データ（株価・乖離率・夜間バッチ）が
// 古いときだけで、ランキングに使っていない μ̂・マクロの遅れは黄どまり（routers/morning.py `_cap_at_warn`）。
const VERDICT_TEXT = {
  fresh: { cls: 'verdict-fresh', icon: '✓', head: 'ランキングの元データは最新です（この結果で発注して問題ありません）' },
  warn:  { cls: 'verdict-warn',  icon: '!', head: '一部のデータが古めです（内容を確認してから発注してください）' },
  alert: { cls: 'verdict-alert', icon: '×', head: 'この結果で発注しないでください（データが古い/欠けています）' },
};

function levelClass(level) {
  return level === 'fresh' ? '' : (level === 'warn' ? 'warn' : 'alert');
}

function fmt(v, digits = 2) {
  return (v === null || v === undefined) ? '—' : Number(v).toFixed(digits);
}

// 乖離率。`gap_ratio` は保存時点で既に %（plugins/sector_ols.py `gap_ratio_pct`）なので
// **×100 しない**——掛けていた間は分析画面で 37.1% の銘柄がここで 3713.0% になっていた。
// 符号と色は分析画面（analysis.js の fmtPct）に揃える。正＝理論値より安い。
function fmtGap(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return `<span class="${n >= 0 ? 'gap-positive' : 'gap-negative'}">${n >= 0 ? '+' : ''}${n.toFixed(1)}%</span>`;
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
  // 復旧手順は直すものがあるときだけ出す（緑の朝に並ぶと、読むべき導線が埋もれる）
  link.hidden = f.overall_verdict === 'fresh';
}

function freshCard(label, level, value, sub, url) {
  return `<div class="fresh-card ${levelClass(level)}">
    <div class="fresh-card-label">${esc(label)}</div>
    <div class="fresh-card-value">${esc(value)}</div>
    <div class="fresh-card-sub">${sub}</div>
    ${url && level !== 'fresh' ? `<a href="${esc(url)}" target="_blank" rel="noopener">復旧手順を見る →</a>` : ''}
  </div>`;
}

// 畳んだときに見える1行。記号は色だけに頼らない（✓ / ! / ×）。
const LEVEL_MARK = { fresh: ['✓', 'lv-fresh'], warn: ['!', 'lv-warn'] };
function freshChip(label, level) {
  const [mark, cls] = LEVEL_MARK[level] || ['×', 'lv-alert'];
  return `<span class="${cls}">${esc(label)} ${mark}</span>`;
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
  // 日時は途中（「2026-」「11-02」）で折らない。狭いカードでは日時ごと次の行へ送る
  const when = s => `<span class="nowrap">${esc(s)}</span>`;
  const sub = [
    `${LEVEL_LABEL[night.level] || '—'}・${age}（閾値 ${Math.round(night.stale_h || 0)}時間）`,
    `最終成功 ${when(night.last_success || '—')}`,
    ...others.map(r => `${esc(r.label)}: ${when(r.last_run || '未実行')}`
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
  document.getElementById('fresh-summary').innerHTML = 'データ鮮度<span class="fresh-chips">' + [
    freshChip('夜間バッチ', (f.batch || {}).level),
    freshChip('株価', p.level),
    freshChip('乖離率', g.level),
    freshChip('μ̂', m.level),
    freshChip('マクロ', mac.level),
  ].join('') + '</span>';
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
      m.level === 'unknown' ? '蓄積済み・as-of 不明（日付の記録が無い）' :
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

// 鮮度の詳細の開閉は、幅ではなく判定で決める（#1214）。問題なしの朝は畳んで1行の要約だけにし
// （主役のランキングを1画面目に入れる）、注意・警告のときだけ開く。
// **判定が前回と同じなら触らない**——プリセットや件数を選び直すたびに、人が開いた詳細を勝手に閉じない。
let lastVerdict = null;
function syncFreshFold(verdict) {
  if (verdict === lastVerdict) return;
  lastVerdict = verdict;
  document.getElementById('fresh-details').open = verdict !== 'fresh';
}

function renderRanking(rec, priceLevel) {
  const rows = (rec && rec.results) || [];
  const body = document.getElementById('rank-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="10" class="muted rank-empty" style="padding:18px">'
      + '該当銘柄がありません（データ未収集か、指標カバレッジ不足）</td></tr>';
    return;
  }
  // data-label は狭い幅のカード表示で見出しになる（morning.html の 480px メディアクエリ）。
  // col-opt はカードでは隠す列。
  body.innerHTML = rows.map(r => `<tr>
    <td class="rank-num">${r.rank}</td>
    <td class="rank-code">${esc(r.sec_code || '—')}</td>
    <td class="rank-name"><a href="/company/${esc(r.edinet_code)}" target="_blank" rel="noopener">${esc(r.company_name || '—')}</a></td>
    <td class="col-opt">${esc(r.industry || '—')}</td>
    <td class="num rank-score rank-metric" data-label="スコア">${fmt(r.score, 3)}</td>
    <td class="num col-opt">${fmt(r.per, 1)}</td>
    <td class="num col-opt">${fmt(r.pbr, 2)}</td>
    <td class="num col-opt">${fmt(r.roe, 1)}</td>
    <td class="num rank-metric" data-label="乖離率">${fmtGap(r.gap_ratio)}</td>
    <td class="rank-metric ${priceLevel === 'fresh' ? '' : 'stale-asof'}" data-label="株価 as-of">${esc(r.price_asof || '—')}</td>
  </tr>`).join('');
}

// 応答は 3〜4.5 秒かかる。その間に選び直されたら、先に出した要求の応答は捨てる
// （後から届いた古い応答が、新しいプリセットの並びを上書きしないように）。
let loadSeq = 0;

function setRankLoading(on) {
  const wrap = document.querySelector('.rank-wrap');
  wrap.classList.toggle('is-loading', on);
  wrap.setAttribute('aria-busy', on ? 'true' : 'false');
}

async function loadMorning() {
  const seq = ++loadSeq;
  const preset = document.getElementById('mor-preset').value;
  const topN   = document.getElementById('mor-topn').value;
  const dot    = document.getElementById('api-dot');
  const label  = document.getElementById('api-label');
  setRankLoading(true);
  document.getElementById('mor-generated').textContent = '読み込み中…';
  try {
    const d = await apiFetch(`/api/morning?preset=${encodeURIComponent(preset)}&top_n=${topN}`);
    if (seq !== loadSeq) return;
    setRankLoading(false);
    if (!d) return;
    renderVerdict(d.freshness);
    renderFreshness(d.freshness);
    syncFreshFold(d.freshness.overall_verdict);
    markMuAsof((d.freshness.mu || {}).level);
    renderRanking(d.recommend, (d.freshness.price || {}).level);
    document.getElementById('mor-generated').textContent = `集計時刻: ${d.generated_at}`;
    const lv = d.freshness.overall_verdict;
    dot.className = 'dot ' + (lv === 'fresh' ? 'dot-green' : lv === 'warn' ? 'dot-amber' : 'dot-red');
    label.textContent = lv === 'fresh' ? '最新データ' : (lv === 'warn' ? '一部が古い' : '古い/欠測あり');
  } catch (e) {
    if (seq !== loadSeq) return;
    setRankLoading(false);
    document.getElementById('mor-generated').textContent = '';
    // 前の並び（または「読み込み中…」）を残すと、取れなかったことに気づかず古い並びを読む
    document.getElementById('rank-body').innerHTML =
      '<tr><td colspan="10" class="muted rank-empty" style="padding:18px">'
      + 'ランキングを取得できませんでした（上の判定の理由を確認してください）</td></tr>';
    dot.className = 'dot dot-red';
    label.textContent = '取得失敗';
    // 判定を出せなかった＝発注の根拠が無いので赤にする（読み込み中の中立色のまま残さない）
    document.getElementById('verdict').className = 'verdict verdict-alert';
    document.getElementById('verdict-head').textContent = '× 朝の集計を取得できませんでした';
    document.getElementById('verdict-reasons').innerHTML = `<li>${esc(e.message)}</li>`;
  }
}

// ── 今日の売買候補（#1228）──────────────────────────────────────────────────
// 実際の売買は「M-6 と M-2 の上位を買い、保有の下位 1/5 を売る」（ADR-0070）。その一覧を、財務指標の
// 点数のランキングの上に**並べて見せるだけ**——μ̂ を点数へ混ぜない（混ぜるなら昇格ゲートが要る・ADR-0030）。
// 読むのは保存済みの値だけで、プリセットや件数を選び直しても読み直さない（別の並びなので変わらない）。
const CAND_TOP_K = 10;   // 運用の単位（1 モデルの上位 10 社）
const CAND_MODELS = [
  { name: 'macro_enet', key: 'enet' },
  { name: 'macro_gbdt', key: 'gbdt' },
];
const CAND_ACTION = {
  'SELL': ['cand-tag-sell', '売却'],
  'HOLD': ['cand-tag-hold', '保有継続'],
  'データ不足': ['cand-tag-hold', 'データ不足'],
};

// 期待リターン（μ̂・52 週の対数リターン）を % で出す。色は乖離率と同じ（正が良い）。
function fmtMu(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v) * 100;
  return `<span class="${n >= 0 ? 'gap-positive' : 'gap-negative'}">${n >= 0 ? '+' : ''}${n.toFixed(1)}%</span>`;
}

function candEmptyRow(cols, text) {
  return `<tr><td colspan="${cols}" class="cand-empty">${esc(text)}</td></tr>`;
}

function candCompany(r, tags) {
  const name = esc(r.company_name || '—');
  const link = r.edinet_code
    ? `<a href="/company/${esc(r.edinet_code)}" target="_blank" rel="noopener">${name}</a>` : name;
  return link + tags;
}

// M-6 の μ̂ が古いときは、上の鮮度の判定と同じ色で基準日を知らせる（`/api/morning` の応答が後から届く）。
function markMuAsof(level) {
  document.getElementById('cand-enet-asof').classList.toggle('stale-asof', !!level && level !== 'fresh');
}

function renderBuyCandidates(key, res, inBoth, held) {
  const asof = document.getElementById(`cand-${key}-asof`);
  const body = document.getElementById(`cand-${key}-body`);
  if (res.status !== 'fulfilled' || !res.value) {
    asof.textContent = '取得できませんでした';
    body.innerHTML = candEmptyRow(4, (res.reason && res.reason.message) || '応答がありません');
    return;
  }
  const d = res.value;
  if (!d.saved || !(d.rows || []).length) {
    asof.textContent = '保存済みの結果がありません';
    body.innerHTML = candEmptyRow(4, 'バッチがまだ結果を保存していません（分析画面のこのモデルのタブで確かめられます）');
    return;
  }
  const a = d.asof || {};
  asof.textContent = a.snapshot_date
    ? `基準日 ${a.snapshot_date}／今買える ${Number(d.n_total).toLocaleString()} 社のうち上位 ${d.rows.length} 社`
    : '基準日 不明（スコアはあるが日付の記録が無い）';
  body.innerHTML = d.rows.map(r => {
    const tags = (inBoth.has(r.edinet_code) ? '<span class="cand-tag cand-tag-both">両方</span>' : '')
      + (r.sec_code && held.has(r.sec_code) ? '<span class="cand-tag cand-tag-held">保有中</span>' : '');
    return `<tr>
      <td class="rank-num">${Number(r.rank)}</td>
      <td>${esc(r.sec_code || '—')}</td>
      <td>${candCompany(r, tags)}</td>
      <td class="num">${fmtMu(r.mu)}</td>
    </tr>`;
  }).join('');
}

function renderSellCandidates(res) {
  const note = document.getElementById('cand-sell-note');
  const body = document.getElementById('cand-sell-body');
  if (res.status !== 'fulfilled' || !res.value) {
    note.textContent = '取得できませんでした';
    body.innerHTML = candEmptyRow(5, (res.reason && res.reason.message) || '応答がありません');
    return;
  }
  const d = res.value;
  if (!d.n_holdings) {
    note.textContent = '売買の記録に保有がありません';
    body.innerHTML = candEmptyRow(5, '分析画面の「保有を見直す」で売買を記録すると、ここに売り判定が出ます');
    return;
  }
  const rk = d.ranking;
  if (!rk) {
    note.textContent = `保有 ${Number(d.n_holdings)} 銘柄／判定できません`;
    body.innerHTML = candEmptyRow(5, d.reason || '売り判定を作れませんでした');
    return;
  }
  const parts = [`保有 ${Number(d.n_holdings)} 銘柄／売却 ${Number(rk.n_sell)}・保有継続 ${Number(rk.n_hold)}`
    + (rk.n_no_data ? `・データ不足 ${Number(rk.n_no_data)}` : '')];
  if ((rk.n_sell || 0) + (rk.n_hold || 0) < 5) parts.push('5 銘柄未満のため「売却」は付きません（並び順だけ参考にしてください）');
  if (rk.mu_asof && rk.mu_asof.snapshot_date) parts.push(`基準日 ${rk.mu_asof.snapshot_date}`);
  const skipped = [...(rk.not_found || []), ...(rk.invalid || []).map(s => String(s).split(',')[0])];
  if (skipped.length) parts.push(`判定できない保有: ${skipped.join('・')}`);
  note.textContent = parts.join('／');
  body.innerHTML = (rk.results || []).map(r => {
    const [cls, label] = CAND_ACTION[r.action] || ['cand-tag-hold', r.action];
    const pnl = r.pnl_pct === null || r.pnl_pct === undefined ? '—'
      : `<span class="${r.pnl_pct >= 0 ? 'gap-positive' : 'gap-negative'}">${r.pnl_pct >= 0 ? '+' : ''}${Number(r.pnl_pct).toFixed(1)}%</span>`;
    return `<tr>
      <td><span class="cand-tag ${cls}">${esc(label)}</span></td>
      <td>${esc(r.sec_code || '—')}</td>
      <td>${candCompany(r, '')}</td>
      <td class="num">${fmtMu(r.mu)}</td>
      <td class="num">${pnl}</td>
    </tr>`;
  }).join('') || candEmptyRow(5, '判定できる保有がありません');
}

// 3 本は互いに独立なので、1 本が失敗しても他は出す（allSettled）。
async function loadCandidates() {
  const [enet, gbdt, sell] = await Promise.allSettled([
    ...CAND_MODELS.map(m => apiFetch(`/api/plugins/${m.name}/saved?limit=${CAND_TOP_K}`)),
    apiFetch('/api/portfolio/sell-candidates'),
  ]);
  const codes = res => new Set(res.status === 'fulfilled' && res.value && res.value.rows
    ? res.value.rows.map(r => r.edinet_code) : []);
  const a = codes(enet), b = codes(gbdt);
  const inBoth = new Set([...a].filter(ec => b.has(ec)));
  const rk = sell.status === 'fulfilled' && sell.value ? sell.value.ranking : null;
  const held = new Set(rk ? [...(rk.results || []).map(r => r.sec_code), ...(rk.not_found || [])] : []);
  renderBuyCandidates('enet', enet, inBoth, held);
  renderBuyCandidates('gbdt', gbdt, inBoth, held);
  renderSellCandidates(sell);
}

// data-change 属性を持つコントロールの変更で再読込（インラインハンドラを使わない）
document.addEventListener('change', (ev) => {
  if (!(ev.target instanceof HTMLElement) || ev.target.dataset.change !== 'reload') return;
  // 選び直したプリセットと件数を URL に残す（再読込・共有・ブックマークで同じ並びを開く・#1213）
  urlStateSet({
    preset: document.getElementById('mor-preset').value,
    n: document.getElementById('mor-topn').value,
  });
  loadMorning();
});

// URL の ?preset=&n= を選択へ戻す。選択肢に無い値は無視して既定で開く（common.js）。
urlStateApplySelect('mor-preset', 'preset');
urlStateApplySelect('mor-topn', 'n');

loadMorning();
loadCandidates();
