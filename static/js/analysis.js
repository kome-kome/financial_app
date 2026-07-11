// Chart.js 用コンテナ: responsive+maintainAspectRatio:false は固定高さ+relative の親が必須。
// 親が auto 高さだと ResizeObserver がフィードバックループを起こし無限再描画になる。
function makeChartContainer(id, height) {
  return `<div style="position:relative;height:${height}px;margin-bottom:16px"><canvas id="${id}"></canvas></div>`;
}

let gapResults  = [];
let _gapScatter = null, _gapHist = null;

// バリュエーション分析の利用可否（業種別OLS の実行に依存）
let _gapDataExists    = false;  // /api/stats: 過去にOLSを実行しDBに予測値が残っている
let _olsRanThisSession = false; // このセッションで業種別OLSを実行した
let _modelStatus = null;         // /api/model/status キャッシュ（鮮度バー用）
// URL ?tab= で起動タブを指定（/company からの相互リンク等: /analysis?tab=gap など）
const _urlTab = new URLSearchParams(window.location.search).get('tab');

// 鮮度バーと gap-ready の表示を更新する。
// 利用可能条件: DBに予測値あり（過去実行）または このセッションでOLS実行済み。
function refreshGapAvailability() {
  const available = _gapDataExists || _olsRanThisSession;
  const ready = document.getElementById('gap-ready');
  if (ready) ready.style.display = available ? 'block' : 'none';
  // サイドバーのロック演出を除去（鮮度バーに委譲）
  const btn = document.querySelector('.sidebar-item[data-tab="gap"]');
  if (btn) {
    const baseLabel = (_pluginMeta['gap_analysis'] && _pluginMeta['gap_analysis'].label) || 'バリュエーション分析';
    btn.textContent = baseLabel;
    btn.classList.remove('locked');
    btn.title = '';
  }
  _updateFreshnessBar(available);
}

// /api/model/status を取得してキャッシュし、鮮度バーを再描画する。
async function fetchModelStatus() {
  try {
    _modelStatus = await apiFetch('/api/model/status');
    refreshGapAvailability();
  } catch(e) { /* 取得失敗は無視（バーは現状維持） */ }
}

// モデル鮮度バーの内容を DOM に反映する。
function _updateFreshnessBar(available) {
  const content = document.getElementById('freshness-content');
  if (!content) return;
  if (!available) {
    content.innerHTML =
      `<span class="freshness-badge freshness-none">モデル未計算</span>` +
      ` 業種別OLSモデルがまだ実行されていません。先に` +
      ` <button class="btn btn-primary btn-sm" style="margin:0 6px" data-click="showTab" data-arg="sector_ols">業種別OLSを実行</button>` +
      ` してください。`;
    return;
  }
  const s = _modelStatus;
  if (!s || !s.computed_at) {
    content.innerHTML = '<span class="freshness-badge freshness-none">モデル情報取得中…</span>';
    return;
  }
  const dtStr = s.computed_at.endsWith('Z') ? s.computed_at : s.computed_at + 'Z';
  const dt = new Date(dtStr);
  const dateStr = `${dt.getMonth() + 1}/${dt.getDate()}`;
  const daysAgo = s.staleness_days != null ? s.staleness_days : '?';
  const badgeCls = s.is_stale ? 'freshness-stale' : 'freshness-fresh';
  const badgeText = s.is_stale
    ? `最終計算: ${dateStr}（${daysAgo}日前・要更新）`
    : `最終計算: ${dateStr}（${daysAgo}日前）`;
  let extra = s.is_stale
    ? ' <span class="text-amber">⚠ 財務データが更新されています</span>' : '';
  if (_renderLightMode)
    extra += ' <span style="font-size:11px;color:var(--text-muted)">| 本番は再計算不可・ローカルで更新してください</span>';
  content.innerHTML =
    `<span class="freshness-badge ${badgeCls}">${esc(badgeText)}</span>` +
    ` ${Number(s.n_results).toLocaleString()}社の予測値${extra}`;
}
if (window.Chart){
  Chart.defaults.color = cssVar('--text-secondary');
  Chart.defaults.borderColor = cssVar('--border-subtle');
  Chart.defaults.font.family = "'Segoe UI', sans-serif";
}
window.onThemeChange = function(){
  if (window.Chart){
    Chart.defaults.color = cssVar('--text-secondary');
    Chart.defaults.borderColor = cssVar('--border-subtle');
  }
  fetchModelStatus();
};

// プラグインシステム
const PLUGIN_TAB_MAP = {
  'gap_analysis':       'gap',
  'recommend':          'recommend',
  'sell_ranking':       'sell_ranking',
  'net_cash_analysis':  'net_cash',
  'backtest':           'backtest',   // 特例エントリ。既存の静的タブ #tab-backtest を使用
  'macro_risk_return':  'macro_risk_return',
  'macro_gbdt':         'macro_gbdt',
  'macro_dlm':          'macro_dlm',
};
// サイドバー項目の先頭につける目印（視認性のため。強調は active 状態で表現）
const PLUGIN_ICON = { 'recommend': '★ ', 'gap_analysis': '◆ ', 'net_cash_analysis': '¥ ' };
let _allTabs   = [];        // タブを持つ分析の tabId 一覧（initPlugins が構築）
let _pluginMeta = {};
// Render 軽量モード（true なら重い回帰はローカル実行に限定。UIで無効化＋案内）
let _renderLightMode = false;

async function initLightMode(){
  try {
    const r = await fetch('/api/system/info');
    const d = await r.json();
    _renderLightMode = !!d.render_light_mode;
  } catch(e){ /* 取得失敗は通常モード扱い */ }
}


function apiBase() { return document.getElementById('api-base').value.trim().replace(/\/$/,''); }


let _preflight = { records: 0, stock_price_records: 0 };

async function preflight() {
  try {
    const d = await apiFetch('/api/stats');
    _preflight = d;
    const finOk = d.records > 0;
    const prOk  = (d.stock_price_records ?? 0) > 0;
    // 過去の業種別OLS実行でDBに予測値が残っていればバリュエーション分析を解放
    _gapDataExists = (d.records_with_prediction ?? 0) > 0;
    refreshGapAvailability();   // 即時: gap-ready 表示を反映
    fetchModelStatus();          // 非同期: 鮮度バーの詳細を更新
    document.getElementById('status-fin-dot').style.background  = finOk ? cssVar('--status-good') : cssVar('--status-bad');
    document.getElementById('status-fin-text').textContent       = `${d.companies.toLocaleString()}社 / ${d.records.toLocaleString()}件`;
    document.getElementById('status-fin-text').style.color       = finOk ? cssVar('--status-good') : cssVar('--status-bad');
    document.getElementById('status-price-dot').style.background = prOk  ? cssVar('--status-good') : cssVar('--status-bad');
    document.getElementById('status-price-text').textContent     = `${(d.stock_price_records ?? 0).toLocaleString()}件`;
    document.getElementById('status-price-text').style.color     = prOk  ? cssVar('--status-good') : cssVar('--status-bad');
    document.getElementById('api-dot').style.background = cssVar('--status-good');
    [['btn-gap-analysis', !finOk], ['btn-recommend', !finOk],
     ['btn-backtest', !prOk],
     ['btn-bt-multi', !prOk], ['btn-mrr', !finOk || !prOk]].forEach(([id, disabled]) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.disabled = disabled;
      el.title = disabled
        ? (!finOk ? '財務データを収集してください（収集ページ）'
                  : '株価履歴を収集してください（収集ページ）')
        : '';
    });
  } catch(e) {
    document.getElementById('api-dot').style.background = cssVar('--status-bad');
  }
}

// ── バリュエーション分析 ─────────────────────────────────────────────────────────
async function runGapAnalysis() {
  const params = _collectParamValues('gap_analysis', _pluginMeta['gap_analysis']?.params_schema ?? {});
  const url = '/api/gap-analysis' + (params.year ? `?year=${params.year}` : '');
  try {
    const d = await apiFetch(url);
    gapResults = d.results;
    document.getElementById('gap-count').textContent = d.count + '社';
    document.getElementById('gap-results-card').style.display = 'block';
    renderGap(gapResults);
    renderGapCharts(gapResults);
  } catch(e) {
    showNotif('バリュエーション分析失敗: ' + e.message + '（先に業種別OLS分析を実行してください）');
  }
}

function renderGap(rows) {
  const sortEl = document.getElementById('param-gap_analysis-sort');
  const sort = sortEl ? sortEl.value : 'desc';
  let sorted = [...rows];
  if (sort === 'total_return')
    sorted.sort((a,b) => (b.expected_total_return_pct??0) - (a.expected_total_return_pct??0));
  else if (sort === 'desc') sorted.sort((a,b) => (b.gap_ratio??0) - (a.gap_ratio??0));
  else                      sorted.sort((a,b) => (a.gap_ratio??0) - (b.gap_ratio??0));

  const tbody = document.getElementById('gap-tbody');
  tbody.innerHTML = sorted.map(r => {
    const gap = r.gap_ratio ?? 0;
    const gapCls = gap > 0 ? 'gap-positive' : 'gap-negative';
    const tr = r.expected_total_return_pct ?? 0;
    const trColor = tr > 0 ? cssVar('--val-up') : cssVar('--val-down');
    return `<tr>
      <td><span class="tag tag-blue">${esc(r.sec_code||r.edinet_code)}</span></td>
      <td>${r.edinet_code ? `<a href="/company/${esc(r.edinet_code)}" class="co-link" style="font-weight:500">${esc(r.company_name)}</a>` : esc(r.company_name)}</td>
      <td><span class="tag tag-amber" style="font-size:10px">${esc(r.industry||'-')}</span></td>
      <td style="text-align:right;font-family:monospace">${fmt0((r.actual_market_cap||0)/100)}</td>
      <td style="text-align:right;font-family:monospace">${fmt0((r.predicted_market_cap||0)/100)}</td>
      <td class="${gapCls}" style="text-align:right;font-family:monospace">${gap>0?'+':''}${gap}%</td>
      <td style="text-align:right;font-family:monospace;font-weight:600;color:${trColor}">${tr>0?'+':''}${tr}%</td>
      <td style="text-align:right;font-family:monospace;color:${cssVar('--status-warn')}">${Number(r.div_yield_pct??0)}%</td>
      <td style="text-align:right;font-family:monospace;color:var(--text-muted)">${r.implied_per!=null?Number(r.implied_per):'-'}</td>
      <td style="text-align:right;font-family:monospace;color:var(--text-secondary)">${Number(r.expected_gap_6m)}%</td>
      <td style="text-align:right;font-family:monospace;color:var(--text-secondary)">${Number(r.expected_gap_12m)}%</td>
      <td style="text-align:right">
        <span class="tag ${r.conv_score_12m>60?'tag-green':r.conv_score_12m>40?'tag-amber':'tag-red'}" title="ヒューリスティック参考値">${Number(r.conv_score_12m)}</span>
      </td>
    </tr>`;
  }).join('');
}

// 横断分布: 理論 vs 実際 の散布図 ＋ 乖離率ヒストグラム
function renderGapCharts(rows) {
  const card = document.getElementById('gap-charts-card');
  if (!window.Chart) { return; }   // CDN 未読込時は静かにスキップ

  // 散布図（対数軸・億円）。market_cap は百万円 → /100 で億円
  const pts = rows.filter(r => (r.actual_market_cap||0) > 0 && (r.predicted_market_cap||0) > 0);
  const scatterData = pts.map(r => ({ x: r.actual_market_cap/100, y: r.predicted_market_cap/100 }));
  const ptColors = pts.map(r => (r.predicted_market_cap >= r.actual_market_cap) ? 'rgba(16,185,129,.6)' : 'rgba(239,68,68,.6)');
  let mn = Infinity, mx = -Infinity;
  scatterData.forEach(p => { mn = Math.min(mn, p.x, p.y); mx = Math.max(mx, p.x, p.y); });
  if (!isFinite(mn) || !isFinite(mx) || mn <= 0) { mn = 1; mx = 10; }
  if (_gapScatter) _gapScatter.destroy();
  _gapScatter = new Chart(document.getElementById('chart-gap-scatter'), {
    type:'scatter',
    data:{ datasets:[
      { label:'企業', data:scatterData, pointBackgroundColor:ptColors, pointRadius:3, pointHoverRadius:5 },
      { label:'理論＝実際（基準線）', type:'line', data:[{x:mn,y:mn},{x:mx,y:mx}], showLine:true,
        borderColor:cssVar('--text-muted'), borderDash:[5,4], pointRadius:0, fill:false },
    ]},
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{labels:{color:cssVar('--text-body')}},
        tooltip:{ callbacks:{ label:(c)=>`実際 ${fmt0(c.parsed.x)} / 理論 ${fmt0(c.parsed.y)} 億円` } } },
      scales:{
        x:{ type:'logarithmic', title:{display:true, text:'実際時価総額（億円）', color:cssVar('--text-muted')}, ticks:{color:cssVar('--text-secondary')}, grid:{color:cssVar('--border-subtle')} },
        y:{ type:'logarithmic', title:{display:true, text:'理論時価総額（億円）', color:cssVar('--text-muted')}, ticks:{color:cssVar('--text-secondary')}, grid:{color:cssVar('--border-subtle')} },
      }}
  });

  // 乖離率ヒストグラム
  const BINS = [
    {lo:-Infinity, hi:-50, label:'< -50%'},
    {lo:-50, hi:-30, label:'-50〜-30'},
    {lo:-30, hi:-10, label:'-30〜-10'},
    {lo:-10, hi:0,   label:'-10〜0'},
    {lo:0,   hi:10,  label:'0〜10'},
    {lo:10,  hi:30,  label:'10〜30'},
    {lo:30,  hi:50,  label:'30〜50'},
    {lo:50,  hi:Infinity, label:'> 50%'},
  ];
  const counts = BINS.map(b => rows.filter(r => { const g = r.gap_ratio; return g != null && g >= b.lo && g < b.hi; }).length);
  const histColors = BINS.map(b => b.lo >= 0 ? cssVar('--val-up') : cssVar('--val-down'));
  if (_gapHist) _gapHist.destroy();
  _gapHist = new Chart(document.getElementById('chart-gap-hist'), {
    type:'bar',
    data:{ labels:BINS.map(b=>b.label), datasets:[{ label:'社数', data:counts, backgroundColor:histColors, borderRadius:3 }] },
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{display:false} },
      scales:{ x:{ ticks:{color:cssVar('--text-secondary')}, grid:{color:cssVar('--border-subtle')} },
               y:{ title:{display:true, text:'社数', color:cssVar('--text-muted')}, ticks:{color:cssVar('--text-secondary')}, grid:{color:cssVar('--border-subtle')}, beginAtZero:true } }}
  });

  card.style.display = 'block';
}

function exportGapCSV() {
  if (!gapResults.length) return;
  const h = '証券コード,企業名,業種,実際時価総額,予測時価総額,乖離率%,期待総リターン%,配当利回り%,implied PER,implied PBR,予測株価,実際株価,期待乖離6M%,期待乖離12M%,収束スコア12M(参考)\n';
  const b = gapResults.map(r=>[r.sec_code,r.company_name,r.industry,r.actual_market_cap,r.predicted_market_cap,r.gap_ratio,r.expected_total_return_pct,r.div_yield_pct,r.implied_per??'',r.implied_pbr??'',r.pred_price??'',r.actual_price??'',r.expected_gap_6m,r.expected_gap_12m,r.conv_score_12m].join(',')).join('\n');
  dl('﻿'+h+b, 'valuation_analysis.csv');
}

// ── おすすめ銘柄 ────────────────────────────────────────────────────
const WEIGHT_LABELS = {
  'z_roe':          ['ROE（収益性）',        1.0],
  'z_op_margin':    ['営業利益率（効率性）',  1.0],
  'z_revenue':      ['売上規模',              0.8],
  'z_cf_ratio':     ['CF余力',               0.8],
  'z_equity_ratio': ['財務安全性',            0.5],
  'z_eps':          ['EPS（1株利益）',        0.5],
  'gap_ratio':      ['割安度（回帰分析後）',  0.5],
  'z_de_ratio':     ['D/Eレシオ低さ',        -0.3],
  'z_momentum':     ['価格モメンタム（12-1ヶ月）', 0.5],
};

let recResults = [];
let recPresets = {};

async function initRecommend() {
  try {
    const d = await apiFetch('/api/recommend/presets');
    recPresets = d.presets;
    renderWeightGrid();
  } catch(e) { console.error('プリセット取得失敗:', e); }
}

function renderWeightGrid() {
  const grid = document.getElementById('weight-grid');
  if (!grid) return;
  grid.innerHTML = '';
  for (const [key, [label, def]] of Object.entries(WEIGHT_LABELS)) {
    grid.innerHTML += `
      <div style="background:var(--bg-sunken);border-radius:8px;padding:10px">
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px">${label}</div>
        <div style="display:flex;align-items:center;gap:8px">
          <input type="range" min="-2" max="3" step="0.1" value="${def}"
            style="flex:1;accent-color:var(--accent);cursor:pointer"
            data-input="syncWVal" data-target="w-val-${key}"
            id="range-${key}">
          <span id="w-val-${key}" style="font-size:14px;font-weight:600;color:var(--accent-text);min-width:32px;text-align:right">${def.toFixed(1)}</span>
        </div>
      </div>`;
  }
}

function applyPreset(name) {
  const weights = recPresets[name];
  if (!weights) return;
  for (const key of Object.keys(WEIGHT_LABELS)) {
    const range = document.getElementById(`range-${key}`);
    const label = document.getElementById(`w-val-${key}`);
    if (!range) continue;
    const val = weights[key] ?? 0;
    range.value = val;
    label.textContent = val.toFixed(1);
  }
}

async function runRecommend() {
  const btn = document.getElementById('btn-recommend');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 分析中...';

  const weights = {};
  for (const key of Object.keys(WEIGHT_LABELS)) {
    const val = parseFloat(document.getElementById(`range-${key}`)?.value ?? 0);
    if (val !== 0) weights[key] = val;
  }
  const recMeta = _pluginMeta['recommend'];
  const filterSchema = recMeta ? Object.fromEntries(
    Object.entries(recMeta.params_schema).filter(([k, v]) => v.type !== 'weights' && k !== 'preset')
  ) : {};
  const filterParams = _collectParamValues('recommend', filterSchema);

  try {
    const d = await apiFetch('/api/recommend', {
      method: 'POST',
      body: JSON.stringify({ weights, ...filterParams })
    });
    recResults = d.results;
    document.getElementById('rec-result-title').textContent =
      `分析結果：上位${d.count}社（候補${d.total_candidates}社中）`;

    const tbody = document.getElementById('rec-tbody');
    tbody.innerHTML = '';
    for (const r of d.results) {
      const rankColor = r.rank === 1 ? cssVar('--status-warn') : r.rank <= 3 ? cssVar('--status-warn-text') : cssVar('--text');
      const scoreColor = r.score > 2 ? cssVar('--val-up') : r.score > 0 ? cssVar('--accent-text') : cssVar('--text-secondary');
      const fmtPct = (v, good='positive') => v == null ? '-'
        : `<span class="gap-${v >= 0 ? good : (good==='positive'?'negative':'positive')}">${v >= 0 ? '+' : ''}${v.toFixed(1)}%</span>`;
      const fmtCap = v => v == null ? '-' : Math.round(v / 100).toLocaleString() + '億円';
      tbody.innerHTML += `
        <tr>
          <td style="font-weight:700;color:${rankColor};font-size:15px">${Number(r.rank)}</td>
          <td style="color:${cssVar('--status-info')};font-weight:600">${esc(r.sec_code || '-')}</td>
          <td>${r.edinet_code ? `<a href="/company/${esc(r.edinet_code)}" class="co-link" style="font-weight:600">${esc(r.company_name)}</a>` : esc(r.company_name)}</td>
          <td style="color:var(--text-secondary);font-size:11px">${esc(r.industry || '-')}</td>
          <td style="font-weight:700;color:${scoreColor}">${r.score.toFixed(2)}</td>
          <td>${r.roe != null ? r.roe.toFixed(1) + '%' : '-'}</td>
          <td>${r.op_margin != null ? r.op_margin.toFixed(1) + '%' : '-'}</td>
          <td>${fmtPct(r.rev_growth)}</td>
          <td>${fmtPct(r.gap_ratio)}</td>
          <td style="color:var(--text-secondary);font-size:12px">${fmtCap(r.market_cap)}</td>
        </tr>`;
    }
    document.getElementById('rec-result-card').style.display = 'block';
  } catch(e) {
    showNotif('分析エラー: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg> おすすめ銘柄を分析';
  }
}

function exportRecommendCSV() {
  if (!recResults.length) return;
  const header = '順位,証券コード,企業名,業種,スコア,ROE%,営業利益率%,売上成長率%,割安度%,時価総額(百万円)';
  const rows = recResults.map(r =>
    [r.rank, r.sec_code, r.company_name, r.industry, r.score,
     r.roe, r.op_margin, r.rev_growth, r.gap_ratio, r.market_cap].join(','));
  dl([header, ...rows].join('\n'), 'recommend.csv');
}

// ── 売り候補ランキング（保有銘柄の売り時）────────────────────────────────
// 観点 → [表示ラベル, バランス型の既定ウェイト]。バックエンド SELL_METRICS と一致させる。
const SELL_WEIGHT_LABELS = {
  'gap_ratio':    ['割高度（回帰乖離）',        1.0],
  'roe':          ['ROE（収益性）',             1.0],
  'op_margin':    ['営業利益率',                1.0],
  'cf_ratio':     ['CF余力',                   0.8],
  'rev_growth':   ['売上成長率',                0.6],
  'equity_ratio': ['財務安全性',                0.4],
  'nc_ratio':     ['ネットキャッシュ余力',       0.4],
  'mu':           ['期待リターン μ（M-1/M-2）',  0.5],
  'neg_r_macro':  ['マクロリスク（−Rᴹ・共有）', 0.3],
};
// plugins/sell_ranking.py PRESETS と一致させる（高いほどその観点を売り判断で重視）。
const SELL_PRESETS = {
  // マクロ予測型: μ と −Rᴹ の2軸のみ（既定）。μモデル未実行時は全「データ不足」になる。
  'マクロ予測型': {mu:1.0, neg_r_macro:0.5},
  'バランス型':   {gap_ratio:1.0, roe:1.0, op_margin:1.0, cf_ratio:0.8, rev_growth:0.6, equity_ratio:0.4, nc_ratio:0.4, mu:0.5, neg_r_macro:0.3},
  '割高警戒型':   {gap_ratio:2.5, roe:0.5, op_margin:0.5, rev_growth:0.3, nc_ratio:0.8, neg_r_macro:0.8},
  '業績悪化重視': {roe:2.0, op_margin:1.5, cf_ratio:1.0, rev_growth:1.5, gap_ratio:0.5, nc_ratio:0.3, mu:0.3},
};
const SELL_HOLDINGS_KEY = 'sell_ranking_holdings';
let sellResults = [];

// 売り判定タブの初期化: ウェイトグリッド描画＋保有入力の localStorage 復元。
function initSellRanking() {
  const grid = document.getElementById('sell-weight-grid');
  if (grid) {
    grid.innerHTML = Object.entries(SELL_WEIGHT_LABELS).map(([key, [label, def]]) => `
      <div style="background:var(--bg-sunken);border-radius:8px;padding:10px">
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px">${label}</div>
        <div style="display:flex;align-items:center;gap:8px">
          <input type="range" min="0" max="3" step="0.1" value="${def}"
            style="flex:1;accent-color:${cssVar('--val-down')};cursor:pointer"
            data-input="syncWVal" data-target="sw-val-${key}" id="srange-${key}">
          <span id="sw-val-${key}" style="font-size:14px;font-weight:600;color:${cssVar('--val-down-text')};min-width:32px;text-align:right">${def.toFixed(1)}</span>
        </div>
      </div>`).join('');
    applySellPreset('マクロ予測型');   // 既定=マクロ予測型（μ・−Rᴹ のみ）を初期スライダー状態に反映
  }
  const ta = document.getElementById('param-sell_ranking-holdings');
  if (ta) {
    try { const saved = localStorage.getItem(SELL_HOLDINGS_KEY); if (saved) ta.value = saved; } catch(e) {}
    ta.addEventListener('input', () => {
      try { localStorage.setItem(SELL_HOLDINGS_KEY, ta.value); } catch(e) {}
    });
  }
}

function applySellPreset(name) {
  const weights = SELL_PRESETS[name];
  if (!weights) return;
  for (const key of Object.keys(SELL_WEIGHT_LABELS)) {
    const range = document.getElementById(`srange-${key}`);
    const label = document.getElementById(`sw-val-${key}`);
    if (!range) continue;
    const val = weights[key] ?? 0;
    range.value = val;
    label.textContent = Number(val).toFixed(1);
  }
}

async function runSellRanking() {
  const ta = document.getElementById('param-sell_ranking-holdings');
  const holdings = ta ? ta.value : '';
  try { localStorage.setItem(SELL_HOLDINGS_KEY, holdings); } catch(e) {}
  if (!holdings.trim()) { showNotif('保有銘柄を入力してください'); return; }

  const weights = {};
  for (const key of Object.keys(SELL_WEIGHT_LABELS)) {
    const v = parseFloat(document.getElementById(`srange-${key}`)?.value ?? 0);
    if (v > 0) weights[key] = v;
  }
  const body = {
    holdings,
    weights,
    sell_threshold:   parseFloat(document.getElementById('sell-th')?.value ?? 0.8),
    reduce_threshold: parseFloat(document.getElementById('reduce-th')?.value ?? 0.3),
    timing_adjust:    !!document.getElementById('sell-timing-adjust')?.checked,
    mu_source:        document.getElementById('sell-mu-source')?.value || 'macro_gbdt',
  };

  const btn = document.getElementById('btn-sell-ranking');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 判定中...';
  try {
    const d = await apiFetch('/api/plugins/sell_ranking/run', {method:'POST', body:JSON.stringify(body)});
    renderSellRanking(d);
  } catch(e) {
    showNotif('売り判定に失敗: ' + e.message + '（割高度を使うには先に「業種別OLS分析」を実行してください）');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="2 12 6 12 9 3 15 21 18 12 22 12"/></svg> 売り候補を判定';
  }
}

// トークン名だけを保持し、実色は使用時に cssVar() で解決する（テーマ切替に追従させるため）。
const SELL_ACTION_STYLE = {
  'SELL':    ['--val-down', '売却'],
  'REDUCE':  ['--status-warn', '一部売却'],
  'HOLD':    ['--text-muted', '保有継続'],
  'データ不足': ['--text-muted', 'データ不足'],
};
const SELL_TREND_STYLE = {
  '下落': '--val-down-text', '上昇': '--val-up-text', '横ばい': '--text-secondary', '不明': '--text-muted',
};

function renderSellRanking(d) {
  sellResults = d.results || [];
  document.getElementById('sell-result-title').textContent =
    `判定結果：${d.count}社（売却 ${d.n_sell} / 一部売却 ${d.n_reduce} / 保有継続 ${d.n_hold}）`;

  const notes = [];
  if (d.not_found && d.not_found.length)
    notes.push(`<span class="text-amber">⚠ DBに無い銘柄（未収録/ETF/外国株等）: ${esc(d.not_found.join('、'))}</span>`);
  if (d.invalid && d.invalid.length)
    notes.push(`<span class="text-amber">⚠ 解釈できなかった入力: ${esc(d.invalid.join(' / '))}</span>`);
  if (d.gap_available === false)
    notes.push(`<span style="color:var(--text-muted)">※ 割高度は業種別OLS未実行のため売り判定から除外されています</span>`);
  if (d.mu_available === false) {
    const _lbl = d.mu_source === 'macro_gbdt' ? 'M-2（勾配ブースティング）' : 'M-1（マクロリスク-リターン）';
    const _act = d.mu_source === 'macro_gbdt' ? 'M-2 を分析タブでローカル実行してください' : 'M-1 を実行してください';
    notes.push(`<span style="color:var(--text-muted)">※ ${_lbl} 未実行のため μ・マクロリスク成分は除外されています（${_act}）</span>`);
  }
  document.getElementById('sell-notes').innerHTML = notes.join('<br>');

  const fmtPct = (v, goodIsPos) => {
    if (v == null) return '-';
    const cls = (v >= 0) === goodIsPos ? 'gap-positive' : 'gap-negative';
    return `<span class="${cls}">${v >= 0 ? '+' : ''}${Number(v).toFixed(1)}%</span>`;
  };
  const tbody = document.getElementById('sell-tbody');
  tbody.innerHTML = sellResults.map(r => {
    const [aColorTok, aLabel] = SELL_ACTION_STYLE[r.action] || ['--text-muted', r.action];
    const aColor = cssVar(aColorTok);
    const tColor = cssVar(SELL_TREND_STYLE[r.trend] || '--text-muted');
    const scoreColor = r.score == null ? cssVar('--text-muted') : r.score > 0 ? cssVar('--val-down-text') : cssVar('--val-up-text');
    // 損益: 取得単価が無ければ '-'
    const pnl = r.pnl_pct == null ? '<span style="color:var(--text-muted)">-</span>'
      : `<span class="${r.pnl_pct >= 0 ? 'gap-positive' : 'gap-negative'}">${r.pnl_pct >= 0 ? '+' : ''}${Number(r.pnl_pct).toFixed(1)}%</span>`;
    return `<tr>
      <td style="font-weight:700;color:var(--text)">${Number(r.rank)}</td>
      <td><span class="tag" style="background:${aColor}22;color:${aColor};font-weight:700">${esc(aLabel)}</span></td>
      <td style="color:${cssVar('--status-info')};font-weight:600">${esc(r.sec_code || '-')}</td>
      <td>${r.edinet_code ? `<a href="/company/${esc(r.edinet_code)}" class="co-link" style="font-weight:600">${esc(r.company_name)}</a>` : esc(r.company_name)}</td>
      <td style="color:var(--text-secondary);font-size:11px">${esc(r.industry || '-')}</td>
      <td style="font-weight:700;color:${scoreColor}">${r.score == null ? '-' : Number(r.score).toFixed(2)}</td>
      <td style="color:${tColor};font-weight:600">${esc(r.trend)}</td>
      <td style="text-align:right">${fmtPct(r.ret_13w, true)}</td>
      <td style="text-align:right">${r.drawdown_52w == null ? '-' : `<span class="gap-negative">${Number(r.drawdown_52w).toFixed(1)}%</span>`}</td>
      <td style="text-align:right">${fmtPct(r.gap_ratio, true)}</td>
      <td style="text-align:right">${r.roe != null ? Number(r.roe).toFixed(1) + '%' : '-'}</td>
      <td style="text-align:right">${fmtPct(r.rev_growth, true)}</td>
      <td style="text-align:right">${pnl}</td>
    </tr>`;
  }).join('');
  document.getElementById('sell-result-card').style.display = 'block';
}

function exportSellRankingCSV() {
  if (!sellResults.length) return;
  const h = '順位,判定,証券コード,企業名,業種,売りスコア,トレンド,13週リターン%,52週高値からの下落%,割高度%,ROE%,売上成長%,取得単価,現値,損益%\n';
  const b = sellResults.map(r => [
    r.rank, r.action, r.sec_code, r.company_name, r.industry,
    r.score ?? '', r.trend, r.ret_13w ?? '', r.drawdown_52w ?? '',
    r.gap_ratio ?? '', r.roe ?? '', r.rev_growth ?? '',
    r.avg_cost ?? '', r.last_close ?? '', r.pnl_pct ?? ''
  ].join(',')).join('\n');
  dl('﻿' + h + b, 'sell_ranking.csv');
}

// ── ネットキャッシュ分析（清原達郎） ────────────────────────────────────
let ncResults = [];

async function runNetCash() {
  const params = _collectParamValues('net_cash_analysis', _pluginMeta['net_cash_analysis']?.params_schema ?? {});
  const btn = document.getElementById('btn-net-cash');
  btn.disabled = true;
  btn.textContent = '実行中...';
  try {
    const d = await apiFetch('/api/plugins/net_cash_analysis/run', {
      method:'POST', body:JSON.stringify(params)
    });
    ncResults = d.results || [];

    document.getElementById('nc-stat-very-cheap').textContent = (d.n_very_cheap ?? 0) + '社';
    document.getElementById('nc-stat-cheap').textContent      = (d.n_cheap      ?? 0) + '社';
    document.getElementById('nc-stat-netnet').textContent     = (d.n_graham_netnet ?? 0) + '社';
    document.getElementById('nc-stat-inv-sec').textContent    = (d.n_inv_securities ?? 0) + '社 / ' + (d.count ?? 0) + '社';

    const exParts = [];
    if (d.n_excluded_sanity) exParts.push(`データ品質ガードで ${d.n_excluded_sanity} 社を除外（推計時価総額の崩れによる異常比率）`);
    if (d.n_excluded_trap)   exParts.push(`バリュートラップ除外で ${d.n_excluded_trap} 社を除外（営業CF/純利益マイナス）`);
    document.getElementById('nc-excluded-note').textContent = exParts.length ? '⚠ ' + exParts.join(' / ') : '';

    const indSummary = (d.industry_summary || []).slice(0, 8)
      .map(s => `${esc(s.industry)} (${s.n})`).join('  /  ');
    document.getElementById('nc-industry-summary').textContent =
      indSummary ? '業種分布（上位8業種）: ' + indSummary : '';

    document.getElementById('nc-summary-card').classList.remove('hidden');
    document.getElementById('nc-results-card').classList.remove('hidden');
    document.getElementById('nc-count').textContent = (d.count ?? 0) + '社 / 候補 ' + (d.n_total_candidates ?? 0) + '社';
    renderNetCash();
  } catch(e) {
    showNotif('ネットキャッシュ分析失敗: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg> ネットキャッシュ分析を実行';
  }
}

function renderNetCash() {
  const tbody = document.getElementById('nc-tbody');
  const fmt = v => (v === null || v === undefined) ? '-' : Number(v).toLocaleString();
  tbody.innerHTML = ncResults.map(r => {
    const ratioColor = r.nc_ratio >= 1.0 ? cssVar('--status-warn-text')
                     : r.nc_ratio >= 0.5 ? cssVar('--val-up-text')
                     : r.nc_ratio >= 0   ? cssVar('--text-secondary')
                                          : cssVar('--val-down-text');
    const ncColor = r.net_cash_oku >= 0 ? cssVar('--val-up-text') : cssVar('--val-down-text');
    const ncavRatioColor = (r.ncav_ratio === null || r.ncav_ratio === undefined) ? cssVar('--text-muted')
                         : r.ncav_ratio >= 1.5 ? cssVar('--status-info-text')
                         : r.ncav_ratio >= 1.0 ? cssVar('--val-up-text')
                         : r.ncav_ratio >= 0   ? cssVar('--text-secondary')
                                                : cssVar('--val-down-text');
    const ocfColor = (r.operating_cf_oku === null || r.operating_cf_oku === undefined) ? cssVar('--text-muted')
                   : r.operating_cf_oku >= 0 ? cssVar('--text-secondary') : cssVar('--val-down-text');
    const invNote = r.has_investment_sec ? '' : '<span style="color:var(--text-muted)" title="投資有価証券データ未取得（古いレコード）"> *</span>';
    const netnetBadge = r.is_graham_netnet ? `<span title="グレアムのネットネット（時価総額 < NCAV×2/3）" style="color:${cssVar('--status-info-text')}"> ★</span>` : '';
    return `<tr>
      <td>${r.rank}</td>
      <td><code style="color:${cssVar('--status-info-text')}">${esc(r.sec_code || '')}</code></td>
      <td>${r.edinet_code ? `<a href="/company/${esc(r.edinet_code)}" class="co-link">${esc(r.company_name || '')}</a>` : esc(r.company_name || '')}</td>
      <td style="color:var(--text-secondary)">${esc(r.industry || '')}</td>
      <td>${r.year ?? '-'}</td>
      <td style="text-align:right;color:${ncColor};font-weight:600">${fmt(r.net_cash_oku)}</td>
      <td style="text-align:right;color:${ratioColor};font-weight:600">${Number(r.nc_ratio).toFixed(3)}</td>
      <td style="text-align:right;color:var(--text-body)">${fmt(r.ncav_oku)}</td>
      <td style="text-align:right;color:${ncavRatioColor};font-weight:600">${(r.ncav_ratio === null || r.ncav_ratio === undefined) ? '-' : Number(r.ncav_ratio).toFixed(3)}${netnetBadge}</td>
      <td style="text-align:right">${fmt(r.market_cap_oku)}</td>
      <td style="text-align:right;color:var(--text-secondary)">${fmt(r.current_assets_oku)}</td>
      <td style="text-align:right;color:var(--text-secondary)">${fmt(r.investment_sec_oku)}${invNote}</td>
      <td style="text-align:right;color:var(--text-secondary)">${fmt(r.total_liabilities_oku)}</td>
      <td style="text-align:right;color:${ocfColor}">${fmt(r.operating_cf_oku)}</td>
      <td style="text-align:right">${r.per ?? '-'}</td>
      <td style="text-align:right">${r.pbr ?? '-'}</td>
      <td style="text-align:right">${r.div_yield ?? '-'}</td>
      <td style="text-align:right">${r.roe ?? '-'}</td>
    </tr>`;
  }).join('');
}

function exportNetCashCSV() {
  if (!ncResults.length) return;
  const h = '順位,証券コード,企業名,業種,年度,ネットキャッシュ(億円),NC比率,NCAV(億円),NCAV比率,グレアムネットネット,時価総額(億),流動資産(億),投資有価証券(億),総負債(億),営業CF(億),純利益(億),PER,PBR,配当利回り%,ROE%\n';
  const b = ncResults.map(r => [
    r.rank, r.sec_code, r.company_name, r.industry, r.year,
    r.net_cash_oku, r.nc_ratio, r.ncav_oku ?? '', r.ncav_ratio ?? '', r.is_graham_netnet ? '★' : '',
    r.market_cap_oku, r.current_assets_oku,
    r.investment_sec_oku, r.total_liabilities_oku, r.operating_cf_oku ?? '', r.net_income_oku ?? '',
    r.per ?? '', r.pbr ?? '', r.div_yield ?? '', r.roe ?? ''
  ].join(',')).join('\n');
  dl('﻿' + h + b, 'net_cash_ranking.csv');
}

// ── バックテスト ─────────────────────────────────────────────────────
let btResults = [];
let btSortAsc = false;

async function runBacktest() {
  const preset   = document.getElementById('bt-preset').value;
  const months   = document.getElementById('bt-months').value;
  const topn     = document.getElementById('bt-topn').value;
  const industry = document.getElementById('bt-industry').value.trim();
  const source   = document.getElementById('bt-source').value;
  const btn = document.getElementById('btn-backtest');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 計算中...';
  try {
    let url = `/api/backtest?preset=${encodeURIComponent(preset)}&months_ago=${months}&top_n=${topn}&source=${encodeURIComponent(source)}`;
    if (industry) url += `&industry=${encodeURIComponent(industry)}`;
    const d = await apiFetch(url);
    btResults = d.results;
    document.getElementById('bt-period-label').textContent =
      `基準日: ${d.start_date}（${d.holding_months}ヶ月前）→ ${d.end_date}　対象候補: ${d.total_candidates}社`;
    _renderBtSummary(d.summary);
    _renderBtBenchmark(d.summary);
    _renderBtHistogram(btResults);
    _renderBtTable(btResults);
    document.getElementById('bt-results-card').classList.remove('hidden');
    showNotif(`バックテスト完了（${d.results.length}社）`, 'success');
  } catch(e) {
    showNotif('バックテスト失敗: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> バックテストを実行';
  }
}

function _renderBtSummary(s) {
  const grid = document.getElementById('bt-summary-grid');
  const pctEl = document.getElementById('bt-percentile');
  if (!s) {
    grid.innerHTML = '<div class="text-sm" style="color:var(--text-muted);padding:12px">株価データが不足しているため、リターンを計算できませんでした。株価履歴を収集してから実行してください。</div>';
    if (pctEl) pctEl.innerHTML = '';
    return;
  }
  const fmtR = v => v == null ? '-' : `<span class="${v >= 0 ? 'gap-positive' : 'gap-negative'}">${v >= 0 ? '+' : ''}${v}%</span>`;
  grid.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">平均リターン</div>
      <div class="stat-value" style="font-size:18px">${fmtR(s.avg_return_pct)}</div>
      <div class="card-sub">σ = ${s.std_dev_pct != null ? s.std_dev_pct + '%' : '-'}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">中央値リターン</div>
      <div class="stat-value" style="font-size:18px">${fmtR(s.median_return_pct)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">勝率（プラスリターン）</div>
      <div class="stat-value" style="font-size:18px;color:${s.win_rate_pct >= 50 ? cssVar('--val-up') : cssVar('--val-down')}">${s.win_rate_pct}%</div>
      <div class="card-sub">${s.n_with_data}社中</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">超過収益（対ベンチマーク）</div>
      <div class="stat-value" style="font-size:18px">${fmtR(s.excess_return_pct)}</div>
    </div>`;

  if (!pctEl) return;
  const p5 = s.p5_pct ?? -20, p95 = s.p95_pct ?? 20;
  const span = (p95 - p5) || 1;
  const toX = v => Math.max(1, Math.min(99, ((v - p5) / span) * 98 + 1));
  const p25x = toX(s.p25_pct ?? 0), p75x = toX(s.p75_pct ?? 0);
  const medx = toX(s.median_return_pct ?? 0), avgx = toX(s.avg_return_pct ?? 0);
  pctEl.innerHTML = `
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px">
      パーセンタイル &nbsp;
      <span style="color:var(--text-secondary)">p5 = ${fmtR(s.p5_pct)}</span> &nbsp;
      <span style="color:var(--accent-soft-text)">p25 = ${fmtR(s.p25_pct)}</span> &nbsp;
      <span style="color:var(--accent-text);font-weight:600">中央値 = ${fmtR(s.median_return_pct)}</span> &nbsp;
      <span style="color:var(--accent-soft-text)">p75 = ${fmtR(s.p75_pct)}</span> &nbsp;
      <span style="color:var(--text-secondary)">p95 = ${fmtR(s.p95_pct)}</span>
    </div>
    <div style="position:relative;height:28px">
      <div style="position:absolute;left:1%;right:1%;top:50%;height:1px;background:var(--border-muted)"></div>
      <div style="position:absolute;left:${p25x}%;width:${p75x - p25x}%;top:5px;height:18px;background:var(--accent-soft-bg);border:1px solid var(--accent-hover);border-radius:3px"></div>
      <div style="position:absolute;left:${medx}%;top:3px;width:2px;height:22px;background:var(--accent-text);transform:translateX(-50%)"></div>
      <div style="position:absolute;left:${avgx}%;top:50%;transform:translate(-50%,-50%);width:8px;height:8px;border-radius:50%;background:var(--status-warn)"></div>
      <div style="position:absolute;left:1%;top:6px;width:2px;height:16px;background:var(--text-muted)"></div>
      <div style="position:absolute;right:1%;top:6px;width:2px;height:16px;background:var(--text-muted)"></div>
    </div>
    <div style="font-size:10px;color:var(--text-muted);margin-top:4px">
      <span style="color:var(--text-muted)">| p5〜p95の全範囲</span> &nbsp;
      <span style="color:var(--accent-hover)">■ IQR（p25〜p75）</span> &nbsp;
      <span style="color:var(--accent-text)">| 中央値</span> &nbsp;
      <span style="color:var(--status-warn)">● 平均</span>
    </div>`;
}

function _renderBtBenchmark(s) {
  const box = document.getElementById('bt-benchmark-box');
  if (!s || s.benchmark_avg_pct == null) {
    box.innerHTML = 'ベンチマーク: 株価データなし — 株価履歴を収集後に再実行してください';
    return;
  }
  const b = s.benchmark_avg_pct;
  const e = s.excess_return_pct;
  box.innerHTML = `ベンチマーク平均リターン（スコアリング対象 ${s.n_benchmark}社）:
    <strong style="color:${b >= 0 ? cssVar('--val-up') : cssVar('--val-down')}">${b >= 0 ? '+' : ''}${b}%</strong>
    &nbsp;|&nbsp; 超過収益:
    <strong style="color:${(e ?? 0) >= 0 ? cssVar('--val-up') : cssVar('--val-down')}">${(e ?? 0) >= 0 ? '+' : ''}${e ?? '-'}%</strong>
    <span style="color:var(--text-muted);font-size:11px;margin-left:8px">（ベンチマーク = 同期間のスコアリング対象全社の平均）</span>`;
}

function _renderBtTable(rows) {
  document.getElementById('bt-tbody').innerHTML = rows.map(r => {
    const ret = r.return_pct;
    const cls = ret == null ? '' : ret >= 0 ? 'gap-positive' : 'gap-negative';
    const txt = ret == null ? '-' : `${ret >= 0 ? '+' : ''}${ret}%`;
    return `<tr>
      <td>${r.rank}</td>
      <td><span class="tag tag-blue">${esc(r.sec_code||r.edinet_code)}</span></td>
      <td>${r.edinet_code ? `<a href="/company/${esc(r.edinet_code)}" class="co-link" style="font-weight:500">${esc(r.company_name)}</a>` : esc(r.company_name)}</td>
      <td><span class="tag tag-amber" style="font-size:10px">${esc(r.industry||'-')}</span></td>
      <td style="font-family:monospace;color:var(--accent-text)">${r.score}</td>
      <td style="text-align:right;font-family:monospace">${r.start_price != null ? '&yen;' + Math.round(r.start_price).toLocaleString() : '-'}</td>
      <td style="text-align:right;font-family:monospace">${r.end_price != null ? '&yen;' + Math.round(r.end_price).toLocaleString() : '-'}</td>
      <td class="${cls}" style="text-align:right;font-family:monospace;font-weight:600">${txt}</td>
      <td style="color:var(--text-muted);font-size:11px">${r.start_date||'-'}</td>
    </tr>`;
  }).join('');
}

function btSort() {
  btSortAsc = !btSortAsc;
  const sorted = [...btResults].sort((a, b) => {
    const va = a.return_pct ?? -9999;
    const vb = b.return_pct ?? -9999;
    return btSortAsc ? va - vb : vb - va;
  });
  _renderBtTable(sorted);
}

function exportBtCSV() {
  if (!btResults.length) return;
  const h = '順位,証券コード,企業名,業種,スコア,財務年度,始値,終値,リターン%,始値日,終値日\n';
  const b = btResults.map(r => [
    r.rank, r.sec_code, r.company_name, r.industry, r.score, r.year,
    r.start_price ?? '', r.end_price ?? '', r.return_pct ?? '', r.start_date ?? '', r.end_date ?? ''
  ].join(',')).join('\n');
  dl('﻿' + h + b, 'backtest.csv');
}

function _renderBtHistogram(rows) {
  const el = document.getElementById('bt-histogram');
  if (!el) return;
  const vals = rows.map(r => r.return_pct).filter(v => v != null);
  if (!vals.length) { el.innerHTML = ''; return; }

  const BINS = [
    { lo: -Infinity, hi: -20, label: '<-20%' },
    { lo: -20, hi: -10, label: '-20〜-10%' },
    { lo: -10, hi: -5,  label: '-10〜-5%' },
    { lo: -5,  hi: 0,   label: '-5〜0%' },
    { lo:  0,  hi: 5,   label: '0〜5%' },
    { lo:  5,  hi: 10,  label: '5〜10%' },
    { lo: 10,  hi: 20,  label: '10〜20%' },
    { lo: 20,  hi: Infinity, label: '>20%' },
  ];
  BINS.forEach(b => { b.n = vals.filter(v => v >= b.lo && v < b.hi).length; });
  const maxN = Math.max(...BINS.map(b => b.n), 1);

  const W=520, H=150, PL=28, PB=48, PT=12, PR=8;
  const cW = W - PL - PR, cH = H - PB - PT;
  const bW = cW / BINS.length;

  const bars = BINS.map((b, i) => {
    const bH = Math.max(Math.round((b.n / maxN) * cH), b.n > 0 ? 2 : 0);
    const x = PL + i * bW + 2, y = PT + (cH - bH);
    const col = b.lo >= 0 ? cssVar('--val-up') : b.hi <= 0 ? cssVar('--val-down') : cssVar('--val-neutral');
    return [
      `<rect x="${x}" y="${y}" width="${bW - 4}" height="${bH}" fill="${col}" opacity="0.85" rx="2"/>`,
      b.n > 0 ? `<text x="${x+(bW-4)/2}" y="${y-4}" text-anchor="middle" fill="${cssVar('--text')}" font-size="10">${b.n}</text>` : '',
      `<text x="${x+(bW-4)/2}" y="${PT+cH+16}" text-anchor="middle" fill="${cssVar('--text-muted')}" font-size="9" transform="rotate(-20 ${x+(bW-4)/2} ${PT+cH+16})">${b.label}</text>`,
    ].join('');
  }).join('');

  const yLines = [0, Math.ceil(maxN/2), maxN].map(v => {
    const y = PT + cH - Math.round((v/maxN)*cH);
    return `<line x1="${PL}" y1="${y}" x2="${PL+cW}" y2="${y}" stroke="${cssVar('--border-subtle')}" stroke-width="1"/>
            <text x="${PL-4}" y="${y+3}" text-anchor="end" fill="${cssVar('--text-muted')}" font-size="9">${v}</text>`;
  }).join('');

  el.innerHTML = `
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px">リターン分布ヒストグラム（銘柄数）</div>
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;max-height:160px;display:block;overflow:visible">
      <rect x="${PL}" y="${PT}" width="${cW}" height="${cH}" fill="${cssVar('--bg-sunken')}" rx="2"/>
      ${yLines}
      ${bars}
      <line x1="${PL}" y1="${PT}" x2="${PL}" y2="${PT+cH}" stroke="${cssVar('--border-muted')}" stroke-width="1"/>
      <line x1="${PL}" y1="${PT+cH}" x2="${PL+cW}" y2="${PT+cH}" stroke="${cssVar('--border-muted')}" stroke-width="1"/>
    </svg>`;
}

async function runBtMulti() {
  const preset   = document.getElementById('bt-preset').value;
  const topn     = document.getElementById('bt-topn').value;
  const industry = document.getElementById('bt-industry').value.trim();
  const source   = document.getElementById('bt-source').value;
  const btn = document.getElementById('btn-bt-multi');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 集計中（5期間）...';
  try {
    let url = `/api/backtest/multi?preset=${encodeURIComponent(preset)}&top_n=${topn}&source=${encodeURIComponent(source)}`;
    if (industry) url += `&industry=${encodeURIComponent(industry)}`;
    const d = await apiFetch(url);
    _renderBtMulti(d);
    document.getElementById('bt-multi-card').classList.remove('hidden');
  } catch(e) {
    showNotif('マルチピリオド比較失敗: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg> マルチピリオド比較（3〜24ヶ月）';
  }
}

function _renderBtMulti(data) {
  const fmtR = v => v == null ? '<span style="color:var(--text-muted)">-</span>' :
    `<span class="${v >= 0 ? 'gap-positive' : 'gap-negative'}">${v >= 0 ? '+' : ''}${v}%</span>`;
  document.getElementById('bt-multi-tbody').innerHTML = data.periods.map(p => {
    const s = p.summary;
    const wr = s?.win_rate_pct;
    return `<tr>
      <td style="font-weight:600;color:var(--text-secondary)">${p.holding_months}ヶ月</td>
      <td style="color:var(--text-muted);font-size:11px">${p.start_date || '-'}</td>
      <td style="text-align:right">${s ? fmtR(s.avg_return_pct) : '-'}</td>
      <td style="text-align:right">${s ? fmtR(s.median_return_pct) : '-'}</td>
      <td style="text-align:right;font-family:monospace;color:var(--text-secondary)">${s?.std_dev_pct != null ? '±'+s.std_dev_pct+'%' : '-'}</td>
      <td style="text-align:right;color:${(wr??0)>=50?cssVar('--val-up'):cssVar('--val-down')}">${wr != null ? wr+'%' : '-'}</td>
      <td style="text-align:right">${s ? fmtR(s.excess_return_pct) : '-'}</td>
      <td style="text-align:right;font-family:monospace;font-size:11px;color:var(--text-muted)">${s ? fmtR(s.p5_pct)+'&nbsp;/&nbsp;'+fmtR(s.p95_pct) : '-'}</td>
      <td style="text-align:right;color:var(--text-muted)">${s?.n_with_data ?? 0}社</td>
    </tr>`;
  }).join('');
}

// ── ユーティリティ ───────────────────────────────────────────────────
function fmt0(n) { return n == null ? '-' : Math.round(n).toLocaleString(); }

function showTab(t) {
  _allTabs.forEach(x => {
    const el = document.getElementById('tab-' + x);
    if (el) el.classList.toggle('hidden', x !== t);
  });
  document.querySelectorAll('.sidebar-item').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === t);
  });
}

// ── 自動調整済みハイパーパラメータ（Issue #264・読取専用・重い再計算はしない）───
const _tunedParamsCache = {};

async function _loadTunedBadge(pluginName) {
  const host = document.getElementById(`tuned-badge-${pluginName}`);
  if (!host) return;
  let tuned;
  try {
    tuned = await apiFetch(`/api/plugins/${pluginName}/tuned`);
  } catch (e) {
    host.innerHTML = '';   // 未調整（404）または取得失敗時はバッジ非表示
    return;
  }
  _tunedParamsCache[pluginName] = tuned;
  const dt = tuned.tuned_at ? new Date(tuned.tuned_at).toLocaleString('ja-JP') : '-';
  const val = tuned.objective_value != null ? tuned.objective_value.toFixed(4) : '-';
  host.innerHTML = `<div style="display:inline-flex;align-items:center;gap:8px;padding:4px 10px;background:var(--bg-sunken);border-radius:999px;font-size:12px;color:var(--text-secondary)">
    <span>🔧 自動調整済み: ${esc(tuned.objective_name)}=${esc(val)}（${esc(dt)}）</span>
    <button type="button" class="btn btn-secondary btn-sm" data-click="applyTunedParams" data-arg="${esc(pluginName)}">初期値にリセット</button>
  </div>`;
  // 調整済みパラメータが存在する場合はページ読込時点でフォームへ自動反映する（Issue #294）
  applyTunedParams(pluginName);
}

// フォームへ調整値を書き込むだけ（再計算はしない。「実行」ボタンはユーザーが別途押す）。
// _loadTunedBadge からページ読込時に自動呼び出しされるほか、「初期値にリセット」ボタンからも
// 手動で呼び出せる（ユーザーが値をいじった後に調整済み値へ戻す用途、Issue #294）。
function applyTunedParams(pluginName) {
  const tuned = _tunedParamsCache[pluginName];
  const meta = _pluginMeta[pluginName];
  if (!tuned || !meta) return;
  const schema = meta.params_schema || {};
  for (const [key, value] of Object.entries(tuned.params || {})) {
    const field = schema[key];
    const el = document.getElementById(`param-${pluginName}-${key}`);
    if (!field || !el) continue;
    if (field.type === 'multiselect') {
      const vals = Array.isArray(value) ? value : [];
      [...el.options].forEach(o => { o.selected = vals.includes(o.value); });
    } else if (field.type === 'checkbox') {
      el.checked = !!value;
    } else if (field.type === 'slider') {
      el.value = value;
      const label = document.getElementById(`val-${pluginName}-${key}`);
      if (label) label.textContent = value;
    } else {
      el.value = value;
    }
  }
}

// ── プラグイン動的読み込み（メタ駆動サイドバー）─────────────────────────
async function initPlugins() {
  let plugins;
  try {
    const d = await apiFetch('/api/plugins');
    plugins = d.plugins;
  } catch(e) {
    console.error('プラグイン一覧取得失敗:', e);
    plugins = _fallbackPlugins();
  }
  _allTabs = [];
  for (const plugin of plugins) {
    _pluginMeta[plugin.name] = plugin;
    if (plugin.href) continue;   // 外部リンク（例: スクリーニング→/collection）はタブを持たない
    const tabId = PLUGIN_TAB_MAP[plugin.name] || plugin.name;
    if (!_allTabs.includes(tabId)) _allTabs.push(tabId);
    // 既存タブにマッピングがないプラグイン（sector_ols 等）はタブを動的生成
    if (!PLUGIN_TAB_MAP[plugin.name]) _createDynamicTab(plugin, tabId);
  }
  buildSidebar(plugins);
  // URL ?tab= 指定があれば優先、なければ ui_order 最小（「① 銘柄を探す」先頭）を表示
  const startTab = (_urlTab && _allTabs.includes(_urlTab)) ? _urlTab : _allTabs[0];
  if (startTab) showTab(startTab);
  // バリュエーション分析タブのロック状態を反映（preflight と initPlugins の競合に備え両方で呼ぶ）
  refreshGapAvailability();

  // 静的タブのフォームをメタ駆動で注入（params_schema → _renderParamsForm）
  const _inject = (containerId, pluginName, schemaFilter) => {
    const el = document.getElementById(containerId);
    const meta = _pluginMeta[pluginName];
    if (!el || !meta) return;
    const schema = schemaFilter ? Object.fromEntries(
      Object.entries(meta.params_schema).filter(schemaFilter)
    ) : meta.params_schema;
    el.innerHTML = _renderParamsForm(schema, pluginName);
  };
  _inject('params-form-gap',              'gap_analysis',      null);
  _inject('params-form-net-cash',         'net_cash_analysis', null);
  _inject('params-form-macro_risk_return','macro_risk_return',  null);
  _inject('params-form-macro_gbdt',       'macro_gbdt',         null);
  _inject('params-form-macro_dlm',        'macro_dlm',          null);
  // recommend: weights/preset は静的 HTML に温存、フィルター項目のみ注入
  _inject('params-form-recommend',    'recommend',
    ([k, v]) => v.type !== 'weights' && k !== 'preset');

  // 自動調整済みバッジ（M-1/M-2/M-3・Issue #264。未調整なら404で非表示のまま）
  ['macro_risk_return', 'macro_gbdt', 'macro_dlm'].forEach(_loadTunedBadge);
}

// /api/plugins のメタを category でグルーピングし、ui_order 昇順でサイドバーを生成する。
// href を持つエントリ（スクリーニング等）は別ページへのリンクとして描画する。
function buildSidebar(plugins) {
  const side = document.getElementById('analysis-sidebar');
  if (!side) return;
  const sorted = [...plugins].sort((a, b) => (a.ui_order ?? 999) - (b.ui_order ?? 999));
  const order = [];          // カテゴリの出現順（ui_order 昇順で確定）
  const byCat = {};
  for (const p of sorted) {
    const cat = p.category || 'その他';
    if (!byCat[cat]) { byCat[cat] = []; order.push(cat); }
    byCat[cat].push(p);
  }
  let html = '';
  for (const cat of order) {
    html += `<div class="sidebar-cat">${esc(cat)}</div>`;
    for (const p of byCat[cat]) {
      if (p.href) {
        html += `<a class="sidebar-item" href="${esc(p.href)}">${esc(p.label)}<span class="sidebar-ext">↗</span></a>`;
      } else {
        const tabId = PLUGIN_TAB_MAP[p.name] || p.name;
        const icon = PLUGIN_ICON[p.name] || '';
        html += `<button type="button" class="sidebar-item" data-tab="${esc(tabId)}" data-click="showTab" data-arg="${esc(tabId)}">${esc(icon + p.label)}</button>`;
      }
    }
  }
  side.innerHTML = html;
}

// /api/plugins 取得失敗時のフォールバック（静的タブを持つ分析のみ・カテゴリ付き）
function _fallbackPlugins() {
  return [
    {name:'recommend',         label:'おすすめ銘柄',       category:'① 銘柄を探す',       ui_order:110, depends_on:[],            params_schema:{}},
    {name:'net_cash_analysis', label:'ネットキャッシュ分析', category:'① 銘柄を探す',       ui_order:120, depends_on:[],            params_schema:{}},
    {name:'gap_analysis',      label:'バリュエーション分析', category:'② 割安度を測る',     ui_order:220, depends_on:['sector_ols'], params_schema:{}},
    {name:'backtest',          label:'バックテスト',        category:'④ 戦略を検証',       ui_order:410, depends_on:[],            params_schema:{}},
  ];
}

function _createDynamicTab(plugin, tabId) {
  // 重い回帰は Render 軽量モードでは実行不可（ローカルで実行→共有DBに保存→本番反映）
  const blocked = plugin.heavy && _renderLightMode;
  const div = document.createElement('div');
  div.id = 'tab-' + tabId;
  div.className = 'hidden';
  div.innerHTML = `
    <div class="card">
      <div class="section-title">${esc(plugin.label)}<a class="co-link" href="/guide#${esc(plugin.name)}" target="_blank" rel="noopener" style="float:right;font-size:12px;font-weight:400">❓ やさしい解説</a></div>
      ${plugin.description ? `<div class="info-box" style="margin-bottom:14px">${esc(plugin.description)}</div>` : ''}
      ${plugin.depends_on.length ? `<div class="info-box" style="border-color:${cssVar('--status-warn')};margin-bottom:14px">⚠ 事前実行が必要: ${esc(plugin.depends_on.join('、'))}</div>` : ''}
      ${blocked ? `<div class="info-box" style="border-color:${cssVar('--status-bad')};margin-bottom:14px">⚠ この分析は計算が重いため、Render 環境では実行できません。ローカルPCで実行すると結果が共有DBに保存され、本番にも反映されます。</div>` : ''}
      <div id="form-${esc(tabId)}">${_renderParamsForm(plugin.params_schema, tabId)}</div>
      <button class="btn btn-primary" style="margin-top:16px${blocked ? ';opacity:0.4' : ''}" data-click="runDynamicPlugin" data-arg="${esc(plugin.name)}" data-arg2="${esc(tabId)}"${blocked ? ' disabled title="Render環境ではローカルPCから実行してください"' : ''}>
        ${esc(plugin.label)}を実行
      </button>
    </div>
    <div class="card hidden" id="dynresult-${tabId}">
      <div class="section-title">結果</div>
      <div id="dynresult-content-${tabId}"></div>
    </div>`;
  document.querySelector('.container').appendChild(div);
}

function _renderParamsForm(schema, tabId) {
  let html = '';
  for (const [key, field] of Object.entries(schema)) {
    html += `<div class="form-group"><label>${esc(field.label)}${field.optional ? '（任意）' : ''}</label>`;
    if (field.type === 'select') {
      html += `<select id="param-${esc(tabId)}-${esc(key)}">`;
      (field.options || []).forEach(opt => {
        html += `<option value="${esc(opt.value)}"${opt.value == field.default ? ' selected' : ''}>${esc(opt.label)}</option>`;
      });
      html += '</select>';
    } else if (field.type === 'multiselect') {
      html += `<select id="param-${esc(tabId)}-${esc(key)}" multiple style="height:110px;width:100%">`;
      (field.options || []).forEach(opt => {
        const sel = (field.default || []).includes(opt.value) ? ' selected' : '';
        html += `<option value="${esc(opt.value)}"${sel}>${esc(opt.label)}</option>`;
      });
      html += '</select><div class="text-sm" style="color:var(--text-muted);margin-top:2px">Ctrl+クリックで複数選択</div>';
    } else if (field.type === 'slider') {
      // step は schema が源。未指定なら dtype から安全側に導出する（int→1 / float→'any'）。
      // これにより int スライダーが端数値を吐いて coerce_params に弾かれることはない。
      const step = field.step ?? (field.dtype === 'int' ? 1 : 'any');
      html += `<div style="display:flex;align-items:center;gap:8px">
        <input type="range" id="param-${tabId}-${key}" min="${field.min}" max="${field.max}" step="${step}" value="${field.default}"
          data-input="syncVal" data-target="val-${tabId}-${key}" style="flex:1;accent-color:var(--accent)">
        <span id="val-${tabId}-${key}" style="color:var(--accent-text);font-weight:600;min-width:36px">${field.default}</span>
      </div>`;
    } else if (field.type === 'checkbox') {
      html += `<input type="checkbox" id="param-${tabId}-${key}"${field.default ? ' checked' : ''} style="width:auto;accent-color:var(--accent)">`;
    } else {
      html += `<input type="${field.type === 'number' ? 'number' : 'text'}" id="param-${tabId}-${key}" value="${field.default ?? ''}" placeholder="${field.default ?? ''}">`;
    }
    if (field.description) html += `<div class="text-sm" style="margin-top:4px">${field.description}</div>`;
    html += '</div>';
  }
  return html;
}

function _collectParamValues(tabId, schema) {
  const params = {};
  for (const [key, field] of Object.entries(schema)) {
    const el = document.getElementById(`param-${tabId}-${key}`);
    if (!el) continue;
    if (field.type === 'multiselect') {
      const sel = [...el.selectedOptions].map(o => o.value);
      params[key] = sel.length > 0 ? sel : null;
    } else if (field.type === 'checkbox') {
      params[key] = el.checked;
    } else if (field.type === 'slider' || field.type === 'number') {
      const val = el.value;
      // dtype=int は整数へ丸める（スライダーの端数・手入力の小数点を coerce 前に正規化）。
      params[key] = (val === '' ? null
        : field.dtype === 'int' ? Math.round(parseFloat(val))
        : parseFloat(val));
    } else {
      params[key] = el.value || null;
    }
  }
  return params;
}

async function runDynamicPlugin(pluginName, tabId) {
  const plugin = _pluginMeta[pluginName];
  if (!plugin) return;
  if (plugin.heavy && _renderLightMode) {
    showNotif(`「${plugin.label}」は計算が重いためローカルPCで実行してください（Render環境では無効）`);
    return;
  }
  const btn = this instanceof HTMLElement ? this : null;
  const origHTML = btn ? btn.innerHTML : null;
  if (btn) { btn.disabled = true; btn.textContent = '実行中...'; }
  const params = _collectParamValues(tabId, plugin.params_schema);
  try {
    const d = await apiFetch(`/api/plugins/${pluginName}/run`, {method:'POST', body:JSON.stringify(params)});
    const card = document.getElementById(`dynresult-${tabId}`);
    const content = document.getElementById(`dynresult-content-${tabId}`);
    content.innerHTML = (RESULT_RENDERERS[pluginName] || _renderGenericResult)(d);
    card.classList.remove('hidden');
    // 業種別OLS が完了したらバリュエーション分析を解放し、結果に導線を出す
    if (pluginName === 'sector_ols') {
      _olsRanThisSession = true;
      refreshGapAvailability();   // 即時: gap-ready を表示
      fetchModelStatus();          // 非同期: 鮮度バーを新しい計算結果で更新
      content.insertAdjacentHTML('afterbegin',
        `<div class="info-box" style="border-color:${cssVar('--status-good')};margin-bottom:14px">
          ✓ 業種別OLSが完了しました。各銘柄の理論株価と乖離率を計算しDBに保存しました。
          <button class="btn btn-primary btn-sm" style="margin-left:12px" data-click="showTab" data-arg="gap">→ バリュエーション分析を見る</button>
        </div>`);
      showNotif('バリュエーション分析が利用可能になりました', 'success');
    }
  } catch(e) { showNotif(`実行失敗: ${e.message}`); }
  finally {
    if (btn) { btn.disabled = false; btn.innerHTML = origHTML; }
  }
}

// 結果レンダラ登録制: 動的タブ（プラグイン runner）の結果描画を plugin名 → 描画関数で対応付ける。
// 未登録のプラグインは _renderGenericResult（results 表 or JSON）にフォールバック。
const RESULT_RENDERERS = {
  'sector_ols':        renderSectorOls,
  'macro_risk_return': renderMacroRiskReturn,
  'macro_gbdt':        renderMacroGbdt,
  'macro_dlm':         renderMacroDlm,
};

// 業種別OLS 専用レンダラ: 自動ドロップ警告 + 業種別R²サマリ + ランキング表（汎用部を再利用）
function renderSectorOls(data) {
  let html = '';
  // 欠損が多く自動ドロップした説明変数の警告
  if (Array.isArray(data.dropped_features) && data.dropped_features.length) {
    html += `<div style="margin-bottom:12px;padding:8px 12px;border-left:3px solid ${cssVar('--status-warn')};background:rgba(245,158,11,0.08);font-size:12px;color:${cssVar('--status-warn-text')}">
      欠損が多いため自動除外した説明変数（${data.dropped_features.length}件）:
      ${data.dropped_features.map(f => `${esc(f.label)}（NULL ${Number(f.missing_rate)}% / ${Number(f.missing)}社）`).join('、')}
    </div>`;
  }
  // sector_stats サマリーを先に描画
  if (Array.isArray(data.sector_stats) && data.sector_stats.length) {
    html += `<div style="margin-bottom:16px">
      <div style="font-size:12px;color:var(--accent-text);font-weight:600;margin-bottom:6px">
        業種別 R² サマリー（${Number(data.n_sectors||0)}業種 / ${Number(data.n_total||0)}社 / スキップ ${Number(data.n_skipped_sectors||0)}業種）
      </div>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>業種</th><th>社数</th><th>R²</th><th>調整済みR²</th></tr></thead>
        <tbody>${data.sector_stats.map(s => `<tr>
          <td>${esc(s.industry)}</td><td>${Number(s.n)}</td>
          <td class="${s.r2 > 0.3 ? 'text-green' : s.r2 >= 0 ? '' : 'text-red'}">${Number(s.r2)}</td>
          <td>${Number(s.adj_r2)}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    </div>`;
  }
  return html + _renderGenericResult(data);
}

// マクロ×リスク-リターン専用レンダラ: CV指標 + バブルチャート + ランキング表
// M-1 リスク-リターン可視化。サーバーは全社の raw 値（mu_raw/r1/r2/r3）を返し、
// 効用 U・パレート・並べ替え・top_n は λ／リスク軸に依存する後処理として
// クライアント側で算出する（軸切替・λ調整は再計算なしで即時反映）。
const MRR_AXIS_LABELS = {
  r2:      'R2 実現ボラティリティ',
  r_macro: 'R_macro マクロ起因リスク',
  r1:      'R1 予測不確実性',   // 軸選択肢から除外（縮小駆動に降格）
  r3:      'R3 モデル信頼性',   // 軸選択肢から除外（足切りゲートに降格）
};
// 係数バー用: 特徴量コード → 表示ラベル（既知のものだけ。未知はコードのまま表示）。
const MRR_FEAT_LABELS = {
  per: 'PER', pbr: 'PBR', div_yield: '配当利回り', roe: 'ROE', roa: 'ROA',
  op_margin: '営業利益率', net_margin: '純利益率', asset_turnover: '総資産回転率',
  equity_ratio: '自己資本比率', de_ratio: 'D/E', nc_ratio: 'ネットキャッシュ比率',
  cf_ratio: '営業CF/売上', eps_growth: 'EPS成長率', op_growth: '営業利益成長率', rev_growth: '売上成長率',
  rd_intensity: 'R&D集約度', da_intensity: 'D&A集約度',
  z_op_margin: '営業利益率Z', z_roe: 'ROE Z', z_cf_ratio: 'CF比率Z',
  macro_usdjpy_yoy: 'USD/JPY(YoY)', macro_eurjpy_yoy: 'EUR/JPY(YoY)', macro_dxy_yoy: 'ドル指数(YoY)',
  macro_sp500_yoy: 'S&P500(YoY)', macro_us5y_zscore: '米5年金利(Z)', macro_us10y_zscore: '米10年金利(Z)',
  macro_us30y_zscore: '米30年金利(Z)', macro_nikkei225_yoy: '日経225(YoY)', macro_vix_zscore: 'VIX(Z)',
  macro_wti_yoy: 'WTI原油(YoY)', macro_gold_yoy: '金(YoY)',
  momentum_12m1: 'モメンタム(12-1)',
};
// 種別 → 色（財務/マクロ/交差項/テクニカル）。
const MRR_COEF_COLOR_TOKENS = { fin: '--status-info', macro: '--status-warn-text', cross: '#c084fc', tech: '#34d399' };
const MRR_COEF_COLORS = new Proxy({}, { get: (_, t) => { const v = MRR_COEF_COLOR_TOKENS[t]; return v && v.startsWith('--') ? cssVar(v) : v; } });
const MRR_COEF_TYPE_LABELS = { fin: '財務', macro: 'マクロ', cross: '交差項', tech: 'テクニカル' };
let _mrrChart = null;
let _mrrData  = null;
let _mrrPaintTimer = null;

// #273: r_macro（macro_beta 推論バッチ／DLM自前計算）が全社 null で使えない場合の
// 共通メッセージ。「結果がありません」だけでは理由不明になるため軸別に理由を明示する。
function _riskAxisEmptyMessage(axis) {
  return axis === 'r_macro'
    ? 'R_macro（マクロ起因リスク）データが利用できません。macro_beta推論バッチが未実行か、算出に十分なデータが蓄積されていない可能性があります。実現ボラ「R2」でご確認ください。'
    : '結果がありません（選択リスク軸の値が揃う銘柄がありません）';
}
// risk_axis セレクトの「R_macro」選択肢を availability に応じて無効化する（M-1/M-2 共用）。
// 無効化時に現在の選択が r_macro なら r2 へ戻す（後続の recompute が正しい軸を読めるよう
// この関数は recompute より前に呼ぶこと）。
function _updateRiskAxisOption(tabId, available) {
  const sel = document.getElementById(`param-${tabId}-risk_axis`);
  if (!sel) return;
  const opt = [...sel.options].find(o => o.value === 'r_macro');
  if (!opt) return;
  opt.disabled = !available;
  opt.title = available ? '' : 'macro_beta推論バッチ未実行のため選択できません';
  if (!available && sel.value === 'r_macro') sel.value = 'r2';
}
// キャンバスの親コンテナに「データなし」メッセージ用の兄弟要素を用意し表示/非表示を切替える
// （canvas 要素自体は破棄しない＝データ復活時に再度 Chart.js から参照できるようにする）。
function _toggleChartEmpty(canvas, axis) {
  if (!canvas) return;
  const host = canvas.parentElement;
  if (!host) return;
  let msg = host.querySelector('.chart-empty-msg');
  if (!msg) {
    msg = document.createElement('div');
    msg.className = 'chart-empty-msg text-sm';
    msg.style.cssText = 'padding:20px;text-align:center;color:var(--text-secondary)';
    host.appendChild(msg);
  }
  msg.textContent = _riskAxisEmptyMessage(axis);
  msg.classList.remove('hidden');
  canvas.classList.add('hidden');
}
function _hideChartEmpty(canvas) {
  if (!canvas) return;
  const host = canvas.parentElement;
  const msg = host && host.querySelector('.chart-empty-msg');
  if (msg) msg.classList.add('hidden');
  canvas.classList.remove('hidden');
}

function renderMacroRiskReturn(data) {
  _mrrData = data;
  _updateRiskAxisOption('macro_risk_return', data.r_macro_available !== false);
  _mrrPaintCv(data);                          // CV 指標（リスク軸に非依存・1回）
  const v = _mrrRecompute();
  setTimeout(() => _mrrPaintChart(v), 0);     // チャートは content 注入後に描画
  return _mrrTableHTML(v);
}

// フォームの λ／リスク軸／表示件数／R3 ゲートを読む（未設定はサーバー echo をシード）。
const _MRR_VALID_AXES = ['r2', 'r_macro'];
function _mrrReadParams() {
  const g = (id) => document.getElementById('param-macro_risk_return-' + id);
  const d = _mrrData || {};
  const lamEl = g('lambda_risk'), axEl = g('risk_axis'), tnEl = g('top_n'), gEl = g('r3_gate');
  const lambda = (lamEl && lamEl.value !== '') ? parseFloat(lamEl.value) : (d.lambda_risk ?? 1.0);
  const axis = _MRR_VALID_AXES.includes(axEl && axEl.value)
    ? axEl.value
    : (_MRR_VALID_AXES.includes(d.risk_axis) ? d.risk_axis : 'r2');
  const topN = (tnEl && tnEl.value !== '') ? Math.max(1, Math.round(parseFloat(tnEl.value))) : (d.top_n ?? 30);
  const r3Gate = (gEl && gEl.value !== '') ? parseFloat(gEl.value) : (d.r3_gate ?? 0.0);
  return { lambda, axis, topN, r3Gate };
}

// 効率的フロンティア（最小リスク x・最大リターン y）の非劣解集合を O(n log n) で算出。
function _mrrParetoSet(items, axisKey) {
  const arr = items.map(it => ({ c: it.edinet_code, x: it[axisKey], y: it.mu_raw }))
    .sort((a, b) => (a.x === b.x ? b.y - a.y : a.x - b.x));
  const set = new Set();
  let bestY = -Infinity;
  for (const p of arr) { if (p.y > bestY) { set.add(p.c); bestY = p.y; } }
  return set;
}

// 非効率的フロンティア（最大リスク x・最小リターン y）の反Pareto集合を O(n log n) で算出。
// B が A を支配 ⟺ x_B ≥ x_A かつ y_B ≤ y_A。x 降順に走査し y 新最小点を集合に追加。
function _mrrAntiParetoSet(items, axisKey) {
  const arr = items.map(it => ({ c: it.edinet_code, x: it[axisKey], y: it.mu_raw }))
    .sort((a, b) => (a.x === b.x ? a.y - b.y : a.x - b.x));
  const set = new Set();
  let worstY = Infinity;
  for (let i = arr.length - 1; i >= 0; i--) {
    const p = arr[i];
    if (p.y < worstY) { set.add(p.c); worstY = p.y; }
  }
  return set;
}

// 全社 raw 値から、選択 λ／軸・R3 ゲートで U・D・パレートを算出し U 降順に並べた view を返す。
function _mrrRecompute() {
  const { lambda, axis, topN, r3Gate } = _mrrReadParams();
  const items = (_mrrData && _mrrData.results ? _mrrData.results : [])
    .filter(r => r[axis] != null && r.mu_raw != null)
    .filter(r => r3Gate <= 0 || r.r3 == null || r.r3 <= r3Gate)
    .map(r => ({ ...r, _u: r.mu_raw - lambda * r[axis], _d: lambda * r[axis] - r.mu_raw }));
  const paretoSet = _mrrParetoSet(items, axis);
  const antiParetoSet = _mrrAntiParetoSet(items, axis);
  items.forEach(it => {
    it._pareto = paretoSet.has(it.edinet_code);
    it._anti_pareto = antiParetoSet.has(it.edinet_code);
  });
  items.sort((a, b) => b._u - a._u);
  return { axis, lambda, topN, r3Gate, all: items, top: items.slice(0, topN) };
}

// 配列の分位点（0..1）。空なら null。少数点でも端に丸めず線形補間で安定させる。
function _mrrPctl(vals, q) {
  const xs = vals.filter(v => v != null).sort((a, b) => a - b);
  if (!xs.length) return null;
  const pos = (xs.length - 1) * q;
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  return lo === hi ? xs[lo] : xs[lo] + (xs[hi] - xs[lo]) * (pos - lo);
}

// 描画用の軸範囲 [p1, p99]（＋わずかな余白）。データ過少銘柄の外れ値（過大ボラ・
// 過大μ）で軸が引き伸ばされ全点が隅へ潰れるのを防ぐ。範囲外の<2%は描画されない。
function _mrrAxisRange(pts, key) {
  const vals = pts.map(p => p[key]).filter(v => v != null);
  if (vals.length < 5) return {};
  const lo = _mrrPctl(vals, 0.01), hi = _mrrPctl(vals, 0.99);
  if (lo == null || hi == null || hi <= lo) return {};
  const pad = (hi - lo) * 0.05;
  return { min: lo - pad, max: hi + pad };
}

// 効用 U → 色（スレート→紫の濃淡）。高 U ほど紫が濃い。
function _mrrUColor(u, uMin, uMax, alpha) {
  const t = uMax > uMin ? (u - uMin) / (uMax - uMin) : 0.5;
  const r = Math.round(100 + (167 - 100) * t);
  const g = Math.round(116 + (139 - 116) * t);
  const b = Math.round(139 + (250 - 139) * t);
  return `rgba(${r},${g},${b},${alpha})`;
}

// 負の効用 D → 色（オレンジ→赤の濃淡）。高 D（売り信号強）ほど赤が濃い。
function _mrrDColor(d, dMin, dMax, alpha) {
  const t = dMax > dMin ? Math.min(1, Math.max(0, (d - dMin) / (dMax - dMin))) : 0.5;
  const r = Math.round(251 + (239 - 251) * t);  // 251 → 239
  const g = Math.round(146 + (68  - 146) * t);  // 146 → 68
  const b = Math.round(60  + (68  - 60 ) * t);  // 60  → 68
  return `rgba(${r},${g},${b},${alpha})`;
}

// アウトオブサンプル検証（OOF）: μ̂ が将来リターンを順序付けるか（無リーク・再学習なし・#272）。
// M-2 の _mgPaintCv 内 oofHtml / M-3 の _dlmOofHTML と同じ描画（同一指標で3モデル横並び比較可能）。
function _mrrOofHTML(data) {
  const oof = data.oof_backtest || {};
  const qr  = oof.quantile_returns || [];
  const ic  = oof.rank_ic || {};
  const hasOof = qr.length > 0;
  return `
  <div style="padding:12px 16px;background:var(--bg-sunken);border-radius:8px;border:1px solid var(--border-muted)">
    <div style="font-size:12px;color:var(--accent-text);font-weight:600;margin-bottom:4px">
      アウトオブサンプル検証（OOF）— μ̂ が将来リターンを順序付けるか（無リーク walk-forward 予測）
    </div>
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
      既存「バックテスト」(/api/backtest) とは別物。再学習なし・各期で μ̂ を横断${oof.n_quantiles||5}分位し実現52週リターンを集計。
    </div>
    ${hasOof ? `
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px">
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">rank-IC（Spearman 平均±std）</div>
        <div style="font-size:15px;font-weight:700;color:${(ic.mean||0)>0?cssVar('--val-up-text'):cssVar('--val-down-text')}">${ic.mean!=null?ic.mean.toFixed(3):'-'}<span style="font-size:11px;color:var(--text-muted)"> ±${ic.std!=null?ic.std.toFixed(3):'-'}</span></div>
        <div style="font-size:10px;color:var(--text-muted)">${ic.n||0} fold</div>
      </div>
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">ロングショート spread（top−bottom）</div>
        <div style="font-size:15px;font-weight:700;color:${(oof.long_short_spread||0)>0?cssVar('--val-up-text'):cssVar('--val-down-text')}">${oof.long_short_spread!=null?(oof.long_short_spread*100).toFixed(2)+'%':'-'}</div>
      </div>
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">hit-rate（top&gt;bottom の期）</div>
        <div style="font-size:15px;font-weight:700;color:#c084fc">${oof.hit_rate!=null?(oof.hit_rate*100).toFixed(0)+'%':'-'}</div>
        <div style="font-size:10px;color:var(--text-muted)">${oof.n_periods_quantile||0} 期</div>
      </div>
    </div>
    <div style="font-size:11px;color:var(--text-secondary);margin-bottom:6px">分位別 平均実現リターン（左=最低 μ̂ → 右=最高 μ̂・52週先・期間平均）</div>
    <div style="display:flex;align-items:flex-end;gap:6px;height:92px">
      ${(() => {
        const mx = Math.max(...qr.map(Math.abs), 1e-9);
        return qr.map((v, i) => {
          const h = Math.round(Math.abs(v) / mx * 70) + 2;
          const col = v >= 0 ? cssVar('--val-up-text') : cssVar('--val-down-text');
          return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%">
            <div style="font-size:10px;color:${col}">${(v*100).toFixed(1)}%</div>
            <div style="width:100%;height:${h}px;background:${col};border-radius:3px 3px 0 0"></div>
            <div style="font-size:10px;color:var(--text-muted);margin-top:2px">Q${i+1}</div>
          </div>`;
        }).join('');
      })()}
    </div>` : `<div style="font-size:11px;color:var(--text-muted)">OOF サンプルが期内 ${(oof.n_quantiles||5)*2} 銘柄未満のため分位を表示できません（データ蓄積後に再実行）。rank-IC は ${ic.n||0} fold で算出。</div>`}
  </div>`;
}

// CV 指標パネル（リスク軸に非依存）。
function _mrrPaintCv(data) {
  const cv = data.cv_metrics || {};
  const waiting = document.getElementById('mrr-cv-waiting');
  const cvContent = document.getElementById('mrr-cv-content');
  if (waiting) waiting.classList.add('hidden');
  if (!cvContent) return;
  cvContent.classList.remove('hidden');
  const el = (id) => document.getElementById(id);
  el('mrr-mean-r2').textContent    = cv.mean_r2  != null ? cv.mean_r2.toFixed(3)  : '-';
  el('mrr-mean-rmse').textContent   = cv.mean_rmse != null ? cv.mean_rmse.toFixed(4) : '-';
  el('mrr-n-features').textContent  = (data.selected_features || []).length;
  el('mrr-features-list').textContent = (data.selected_features || []).join('、') || '（なし）';
  _mrrPaintCoefBars(data.feature_coefs || {});
  const folds = cv.folds || [];
  el('mrr-fold-tbody').innerHTML = folds.length
    ? folds.map((f, i) =>
        `<tr><td>${i+1}</td><td>${f.n_train||'-'}</td><td>${f.n_test||'-'}</td>
         <td class="${f.r2>0.3?'text-green':''}">${f.r2!=null?f.r2.toFixed(3):'-'}</td>
         <td>${f.rmse!=null?f.rmse.toFixed(4):'-'}</td></tr>`).join('')
    : '<tr><td colspan="5" style="color:var(--text-muted)">CVフォルドなし（学習月数が不足。株価週次履歴の蓄積を待つ必要があります）</td></tr>';
  const oofEl = el('mrr-oof-content');
  if (oofEl) oofEl.innerHTML = _mrrOofHTML(data);
}

// 特徴量コードを種別分類（交差項 > マクロ > テクニカル > 財務）。
function _mrrCoefType(name) {
  if (name.includes('_x_')) return 'cross';
  if (name.startsWith('macro_')) return 'macro';
  if (name.startsWith('momentum')) return 'tech';
  return 'fin';
}
// 特徴量コードを表示ラベル化。交差項は '_x_' で分割し各要素をラベル化して ' × ' で連結。
// セクターダミー（sec_<safe>_x_<macro>）は 'セクター[safe]' と表示。
function _mrrCoefLabel(name) {
  if (name.includes('_x_')) {
    return name.split('_x_').map(part => {
      if (part.startsWith('sec_')) return `業種[${part.slice(4)}]`;
      return MRR_FEAT_LABELS[part] || part;
    }).join(' × ');
  }
  return MRR_FEAT_LABELS[name] || name;
}
// 標準化係数の横バー（ゼロ中心・正右/負左）。種別で色分け、|係数| 降順に並べる。
// hostId/legendId を省略すると既定の 'mrr-coef-bars'/'mrr-coef-legend' を使用（M-1 後方互換）。
function _mrrPaintCoefBars(coefs, hostId, legendId) {
  const host = document.getElementById(hostId || 'mrr-coef-bars');
  const legend = document.getElementById(legendId || 'mrr-coef-legend');
  if (!host) return;
  const entries = Object.entries(coefs).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  if (!entries.length) { host.innerHTML = '<span style="color:var(--text-muted);font-size:12px">（係数なし）</span>'; if (legend) legend.innerHTML = ''; return; }
  const maxAbs = Math.max(...entries.map(([, v]) => Math.abs(v))) || 1;
  // 凡例（出現した種別のみ）
  if (legend) {
    const used = [...new Set(entries.map(([n]) => _mrrCoefType(n)))];
    legend.innerHTML = used.map(t =>
      `<span style="display:inline-flex;align-items:center;gap:4px"><span style="width:10px;height:10px;border-radius:2px;background:${MRR_COEF_COLORS[t]};display:inline-block"></span>${MRR_COEF_TYPE_LABELS[t]}</span>`
    ).join('');
  }
  host.innerHTML = entries.map(([name, v]) => {
    const t = _mrrCoefType(name);
    const color = MRR_COEF_COLORS[t];
    const w = (Math.abs(v) / maxAbs) * 50;           // 片側 0–50%
    const pos = v >= 0;
    const bar = pos
      ? `<div style="position:absolute;left:50%;width:${w}%;height:14px;background:${color};border-radius:0 3px 3px 0"></div>`
      : `<div style="position:absolute;right:50%;width:${w}%;height:14px;background:${color};border-radius:3px 0 0 3px"></div>`;
    return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">
        <div style="width:190px;flex:none;font-size:11px;color:var(--text-body);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${name}">${_mrrCoefLabel(name)}</div>
        <div style="position:relative;flex:1;height:14px;background:var(--border-subtle);border-radius:3px">
          <div style="position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--text-muted)"></div>${bar}
        </div>
        <div style="width:54px;flex:none;font-size:11px;color:${pos?cssVar('--val-up-text'):cssVar('--val-down-text')};text-align:left">${pos?'+':''}${v.toFixed(3)}</div>
      </div>`;
  }).join('');
}

// バブルチャート: x=選択リスク軸 / y=μ_raw / 色=効用U / 枠線・線=パレート。
// 散布図は全社（リスク-リターンの全体像）を描き、効用上位 top_n を少し大きく濃く強調する。
// 「Uで絞った上位N」だけを描くとリスク方向に潰れるため、母集団を描いてフロンティアを見せる。
// 径は固定（R1 はほぼ一定で径エンコードが無意味なため。R1 はツールチップで参照）。
function _mrrPaintChart(v) {
  const chartCard = document.getElementById('mrr-chart-card');
  const canvas = document.getElementById('chart-mrr-bubble');
  if (!chartCard || !canvas || !window.Chart) return;
  if (_mrrChart) { _mrrChart.destroy(); _mrrChart = null; }
  const pts = v.all;
  if (!pts.length) { chartCard.classList.add('hidden'); return; }
  chartCard.classList.remove('hidden');
  const axisKey = v.axis;
  const topSet = new Set(v.top.map(p => p.edinet_code));
  const us = pts.map(p => p._u);
  const uMin = Math.min(...us), uMax = Math.max(...us);
  // 軸範囲を [p1,p99] に固定（外れ値で潰れない）。範囲外の点は Chart.js が描画省略。
  const xRange = _mrrAxisRange(pts, axisKey);
  const yRange = _mrrAxisRange(pts, 'mu_raw');
  // データ値自体を [p1,p99] にクランプ（外れ値は境界へ積む）。Chart.js は外れ値が
  // 残ると scales.min/max を設定しても軸を引き伸ばすため、値クランプで確実に潰れを防ぐ。
  const clamp = (val, R) => (R.min == null || val == null) ? val : Math.min(R.max, Math.max(R.min, val));
  // D 範囲（反Pareto点のみで算出・着色用）。
  const dsVals = pts.filter(p => p._anti_pareto).map(p => p._d);
  const dMin = dsVals.length ? Math.min(...dsVals) : 0;
  const dMax = dsVals.length && Math.max(...dsVals) > dMin ? Math.max(...dsVals) : dMin + 1;
  // サイズは固定（R1 はほぼ一定で径エンコードが退化するため）。全社の雲が見えるよう
  // 背景点も視認可能な大きさ・不透明度にし、上位 top_n とパレートを少し大きく強調する。
  const bubble = pts.map(p => ({
    x: clamp(p[axisKey], xRange), y: clamp(p.mu_raw, yRange),
    r: (p._pareto || p._anti_pareto) ? 6 : (topSet.has(p.edinet_code) ? 5 : 3),
    _p: p, _top: topSet.has(p.edinet_code),
  }));
  // 反Pareto点はオレンジ→赤グラデーション（D 値）、それ以外は紫（U 値）。
  const bg = pts.map(p =>
    p._anti_pareto
      ? _mrrDColor(p._d, dMin, dMax, topSet.has(p.edinet_code) ? 0.85 : 0.6)
      : _mrrUColor(p._u, uMin, uMax, topSet.has(p.edinet_code) ? 0.9 : 0.55)
  );
  const bc = pts.map(p =>
    p._pareto      ? cssVar('--val-up-text') :
    p._anti_pareto ? cssVar('--val-down-text') :
    topSet.has(p.edinet_code) ? 'rgba(226,232,240,0.9)' : 'rgba(148,163,184,0.4)'
  );
  const bw = pts.map(p => (p._pareto || p._anti_pareto) ? 2.5 : (topSet.has(p.edinet_code) ? 1.2 : 0.5));
  const front = pts.filter(p => p._pareto)
    .map(p => ({ x: clamp(p[axisKey], xRange), y: clamp(p.mu_raw, yRange) }))
    .sort((a, b) => a.x - b.x);
  const antifront = pts.filter(p => p._anti_pareto)
    .map(p => ({ x: clamp(p[axisKey], xRange), y: clamp(p.mu_raw, yRange) }))
    .sort((a, b) => a.x - b.x);
  _mrrChart = new Chart(canvas, {
    data: { datasets: [
      { type: 'line', label: '効率的フロンティア（買い）', data: front,
        borderColor: cssVar('--val-up-text'), borderWidth: 2, pointRadius: 0, fill: false, tension: 0, order: 0 },
      { type: 'line', label: '非効率的フロンティア（売り）', data: antifront,
        borderColor: cssVar('--val-down-text'), borderWidth: 2, borderDash: [6, 3], pointRadius: 0, fill: false, tension: 0, order: 0 },
      { type: 'bubble', label: `銘柄（全${pts.length}社・上位${v.top.length}を強調）`, data: bubble,
        backgroundColor: bg, borderColor: bc, borderWidth: bw, order: 1 },
    ]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top' },
        tooltip: { filter: (ctx) => !ctx.raw || ctx.raw._top !== false || ctx.datasetIndex === 0,
          callbacks: { label: (ctx) => {
          const p = ctx.raw && ctx.raw._p;
          if (!p) return '';
          return [
            p.company_name || p.edinet_code,
            `μ_raw: ${((p.mu_raw ?? 0) * 100).toFixed(2)}%`,
            `${MRR_AXIS_LABELS[axisKey] || axisKey}: ${(p[axisKey] ?? 0).toFixed(4)}`,
            axisKey !== 'r2' && p.r2 != null ? `R2 ボラ: ${(p.r2 * 100).toFixed(2)}%` : null,
            axisKey !== 'r_macro' && p.r_macro != null ? `R_macro: ${(p.r_macro * 100).toFixed(2)}%` : null,
            `R3 信頼性: ${p.r3 != null ? p.r3.toFixed(4) : '-'}`,
            `U（効用）: ${p._u != null ? p._u.toFixed(4) : '-'}`,
            `D（負効用）: ${p._d != null ? p._d.toFixed(4) : '-'}`,
            p._pareto ? '★ 効率的フロンティア（買い）' : '',
            p._anti_pareto ? '▼ 非効率的フロンティア（売り）' : '',
          ].filter(Boolean);
        }}},
      },
      scales: {
        // type:'linear' を明示。frontier の line データセットがあると Chart.js は x 軸を
        // 既定で category スケールにし、数値 min/max を無視して点を等間隔配置するため
        // （= x がクランプされず外れ値で潰れる主因）。linear を強制して数値軸にする。
        x: { type: 'linear', title: { display: true, text: `リスク（${MRR_AXIS_LABELS[axisKey]}）` },
             min: xRange.min, max: xRange.max },
        y: { type: 'linear', title: { display: true, text: '期待リターン（μ_raw・年率）' },
             min: yRange.min, max: yRange.max },
      },
    },
  });
}

// ランキング表（クライアント算出の U・パレートで描画）。
function _mrrTableHTML(v) {
  if (!v.top.length) {
    return `<div class="text-sm" style="padding:20px;text-align:center;color:var(--text-secondary)">${esc(_riskAxisEmptyMessage(v.axis))}</div>`;
  }
  const total = (_mrrData && _mrrData.results ? _mrrData.results.length : v.top.length);
  const header = `<tr><th>順位</th><th>証券コード</th><th>企業名</th><th>業種</th>
    <th><span class="gloss" data-tip="期待リターン（52週=1年先の年率対数リターン、無次元）。小数を%表示しているだけで、10.00%は年率+10%を意味する（OLSモデルの生予測値）。">μ_raw</span></th><th>R2 ボラ</th><th>R_macro</th><th>R3 信頼性</th><th>効用 U</th><th>D（負効用）</th><th>F</th></tr>`;
  const rows = v.top.map((r, i) => {
    const mu = r.mu_raw ?? 0;
    const muClass = mu > 0 ? 'text-green' : 'text-red';
    const frontierTag = r._pareto
      ? `<span style="color:${cssVar('--val-up-text')};font-weight:700" title="効率的フロンティア（買い）">★</span>`
      : r._anti_pareto
        ? `<span style="color:${cssVar('--val-down-text')};font-weight:700" title="非効率的フロンティア（売り）">▼</span>`
        : '';
    const r3Warn = v.r3Gate > 0 && r.r3 != null && r.r3 > v.r3Gate * 0.8
      ? ` style="color:${cssVar('--status-warn-text')}"` : '';
    return `<tr>
      <td>${i+1}</td>
      <td>${esc(r.sec_code||'-')}</td>
      <td><a href="/company/${esc(r.edinet_code||'')}" style="color:var(--status-info)">${esc(r.company_name||'-')}</a></td>
      <td style="font-size:11px">${esc(r.industry||'-')}</td>
      <td class="${muClass}">${(mu*100).toFixed(2)}%</td>
      <td>${r.r2!=null?(r.r2*100).toFixed(2)+'%':'-'}</td>
      <td style="font-size:11px;color:var(--text-secondary)">${r.r_macro!=null?(r.r_macro*100).toFixed(2)+'%':'-'}</td>
      <td style="font-size:11px;color:var(--text-secondary)"${r3Warn}>${r.r3!=null?r.r3.toFixed(4):'-'}</td>
      <td class="text-green">${r._u!=null?r._u.toFixed(4):'-'}</td>
      <td style="color:${cssVar('--val-down-text')}">${r._d!=null?r._d.toFixed(4):'-'}</td>
      <td style="text-align:center">${frontierTag}</td>
    </tr>`;
  }).join('');

  return `
    <div class="flex-between" style="flex-wrap:wrap;gap:8px;margin-bottom:10px">
      <div class="section-title" style="margin-bottom:0">
        リスク-リターンランキング
        <span class="tag tag-purple" style="margin-left:6px">上位${v.top.length} / 全${total}社</span>
        <span class="tag" style="margin-left:6px">横軸: ${MRR_AXIS_LABELS[v.axis]}</span>
        <span class="tag" style="margin-left:6px">λ=${v.lambda}</span>
      </div>
      <div class="text-sm" style="color:var(--text-muted)">λ・リスク軸・表示件数は即時反映（再計算不要）</div>
    </div>
    <div class="text-sm" style="color:var(--text-muted);margin-bottom:6px;font-size:11px">
      ※ μ_raw は52週（1年）先の年率対数リターン予測（例: 表示10.00% = 年率+10%）。モデルの素の銘柄別推定（収縮なし）。検証 R² は低く推定にはノイズを含むため、
      順位は目安。μ_shrunk はセクター平均への収縮後の保守的推定（参考）。
    </div>
    <div style="overflow-x:auto">
      <table><thead>${header}</thead><tbody>${rows}</tbody></table>
    </div>`;
}

// λ／リスク軸／表示件数の変更でクライアント再描画（モデル再学習なし）。
function _mrrRepaint() {
  if (!_mrrData) return;
  const v = _mrrRecompute();
  _mrrPaintChart(v);
  const content = document.getElementById('dynresult-content-macro_risk_return');
  if (content) content.innerHTML = _mrrTableHTML(v);
}
function _mrrScheduleRepaint() {
  if (!_mrrData) return;
  clearTimeout(_mrrPaintTimer);
  _mrrPaintTimer = setTimeout(_mrrRepaint, 80);  // スライダー連続入力をデバウンス
}
// λ/軸/件数/R3ゲートだけを即時クライアント反映（特徴量・マクロ等の変更は「実行」ボタンが必要）。
const _MRR_CLIENT_PARAMS = ['lambda_risk', 'risk_axis', 'top_n', 'r3_gate'];
function _mrrIsClientParam(id) {
  const prefix = 'param-macro_risk_return-';
  return id.startsWith(prefix) && _MRR_CLIENT_PARAMS.includes(id.slice(prefix.length));
}
document.addEventListener('input',  (e) => { if (e.target && e.target.id && _mrrIsClientParam(e.target.id)) _mrrScheduleRepaint(); });
document.addEventListener('change', (e) => { if (e.target && e.target.id && _mrrIsClientParam(e.target.id)) _mrrScheduleRepaint(); });

// ── M-2 マクロ×財務 勾配ブースティング レンダラ ─────────────────────────────

let _mgData  = null;
let _mgChart = null;
let _mgPaintTimer = null;

function renderMacroGbdt(data) {
  _mgData = data;
  _updateRiskAxisOption('macro_gbdt', data.r_macro_available !== false);
  _mgPaintCv(data);
  const v = _mgRecompute();
  setTimeout(() => _mgPaintChart(v), 0);
  return _mgTableHTML(v);
}

// CV 比較（XGB vs OLS ベースライン）
function _mgPaintCv(data) {
  const cv = data.cv_metrics || {};
  const xgb = cv.xgb || {};
  const ols = cv.ols_baseline || {};
  const el = document.getElementById('dynresult-content-macro_gbdt');
  if (!el) return;

  // アウトオブサンプル検証（OOF）: μ̂ が将来リターンを順序付けるか（無リーク・再学習なし）
  const oof = data.oof_backtest || {};
  const qr  = oof.quantile_returns || [];
  const ic  = oof.rank_ic || {};
  const hasOof = qr.length > 0;
  const oofHtml = `
  <div style="margin-bottom:16px;padding:12px 16px;background:var(--bg-sunken);border-radius:8px;border:1px solid var(--border-muted)">
    <div style="font-size:12px;color:var(--accent-text);font-weight:600;margin-bottom:4px">
      アウトオブサンプル検証（OOF）— μ̂ が将来リターンを順序付けるか（無リーク walk-forward 予測）
    </div>
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
      既存「バックテスト」(/api/backtest) とは別物。再学習なし・各期で μ̂ を横断${oof.n_quantiles||5}分位し実現52週リターンを集計。
    </div>
    ${hasOof ? `
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px">
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">rank-IC（Spearman 平均±std）</div>
        <div style="font-size:15px;font-weight:700;color:${(ic.mean||0)>0?cssVar('--val-up-text'):cssVar('--val-down-text')}">${ic.mean!=null?ic.mean.toFixed(3):'-'}<span style="font-size:11px;color:var(--text-muted)"> ±${ic.std!=null?ic.std.toFixed(3):'-'}</span></div>
        <div style="font-size:10px;color:var(--text-muted)">${ic.n||0} fold</div>
      </div>
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">ロングショート spread（top−bottom）</div>
        <div style="font-size:15px;font-weight:700;color:${(oof.long_short_spread||0)>0?cssVar('--val-up-text'):cssVar('--val-down-text')}">${oof.long_short_spread!=null?(oof.long_short_spread*100).toFixed(2)+'%':'-'}</div>
      </div>
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">hit-rate（top&gt;bottom の期）</div>
        <div style="font-size:15px;font-weight:700;color:#c084fc">${oof.hit_rate!=null?(oof.hit_rate*100).toFixed(0)+'%':'-'}</div>
        <div style="font-size:10px;color:var(--text-muted)">${oof.n_periods_quantile||0} 期</div>
      </div>
    </div>
    <div style="font-size:11px;color:var(--text-secondary);margin-bottom:6px">分位別 平均実現リターン（左=最低 μ̂ → 右=最高 μ̂・52週先・期間平均）</div>
    <div style="display:flex;align-items:flex-end;gap:6px;height:92px">
      ${(() => {
        const mx = Math.max(...qr.map(Math.abs), 1e-9);
        return qr.map((v, i) => {
          const h = Math.round(Math.abs(v) / mx * 70) + 2;
          const col = v >= 0 ? cssVar('--val-up-text') : cssVar('--val-down-text');
          return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%">
            <div style="font-size:10px;color:${col}">${(v*100).toFixed(1)}%</div>
            <div style="width:100%;height:${h}px;background:${col};border-radius:3px 3px 0 0"></div>
            <div style="font-size:10px;color:var(--text-muted);margin-top:2px">Q${i+1}</div>
          </div>`;
        }).join('');
      })()}
    </div>` : `<div style="font-size:11px;color:var(--text-muted)">OOF サンプルが期内 ${(oof.n_quantiles||5)*2} 銘柄未満のため分位を表示できません（データ蓄積後に再実行）。rank-IC は ${ic.n||0} fold で算出。</div>`}
  </div>`;

  // CV パネルを先頭に inject（テーブル返却前）
  const cvHtml = `
  <div style="margin-bottom:16px;padding:12px 16px;background:var(--bg-sunken);border-radius:8px;border:1px solid var(--border-muted)">
    <div style="font-size:12px;color:var(--accent-text);font-weight:600;margin-bottom:10px">
      Walk-Forward CV（M-2 XGBoost vs 同一特徴量 OLS ベースライン）
    </div>
    <table style="width:100%;font-size:12px;border-collapse:collapse">
      <thead><tr>
        <th style="text-align:left;padding:4px 8px;color:var(--text-muted)">モデル</th>
        <th style="text-align:right;padding:4px 8px;color:var(--text-muted)">Mean R²</th>
        <th style="text-align:right;padding:4px 8px;color:var(--text-muted)">Mean RMSE</th>
        <th style="text-align:right;padding:4px 8px;color:var(--text-muted)">フォールド数</th>
      </tr></thead>
      <tbody>
        <tr>
          <td style="padding:4px 8px;color:#c084fc;font-weight:600">XGBoost（M-2）</td>
          <td style="padding:4px 8px;text-align:right;color:${(xgb.mean_r2||0)>0?cssVar('--val-up-text'):cssVar('--val-down-text')}">${xgb.mean_r2!=null?xgb.mean_r2.toFixed(3):'-'}</td>
          <td style="padding:4px 8px;text-align:right;color:var(--text-secondary)">${xgb.mean_rmse!=null?xgb.mean_rmse.toFixed(4):'-'}</td>
          <td style="padding:4px 8px;text-align:right;color:var(--text-secondary)">${xgb.n_folds||0}</td>
        </tr>
        <tr>
          <td style="padding:4px 8px;color:var(--status-info)">OLS ベースライン</td>
          <td style="padding:4px 8px;text-align:right;color:${(ols.mean_r2||0)>0?cssVar('--val-up-text'):cssVar('--val-down-text')}">${ols.mean_r2!=null?ols.mean_r2.toFixed(3):'-'}</td>
          <td style="padding:4px 8px;text-align:right;color:var(--text-secondary)">${ols.mean_rmse!=null?ols.mean_rmse.toFixed(4):'-'}</td>
          <td style="padding:4px 8px;text-align:right;color:var(--text-secondary)">${ols.n_folds||0}</td>
        </tr>
      </tbody>
    </table>
    <div style="margin-top:10px;font-size:11px;color:var(--text-muted)">
      best_iteration: ${data.best_iteration||'-'} 木 ／ 学習サンプル: ${(data.n_train_samples||0).toLocaleString()}件 ／ 特徴量: ${(data.selected_features||[]).length}個
    </div>
    <div style="margin-top:8px">
      <div style="font-size:11px;color:var(--text-secondary);margin-bottom:4px">グローバル特徴量寄与 mean|SHAP|（大きさのみ・方向なし）</div>
      <div id="mg-coef-bars" style="margin-top:4px"></div>
    </div>
  </div>
  ${oofHtml}
  ${makeChartContainer('chart-mg-bubble', 360)}`;
  const resultCard = document.getElementById('dynresult-macro_gbdt') || el.closest('.card');
  // CV パネルをテーブルより上に置くため、result area の直前に挿入
  const cvWrap = document.getElementById('mg-cv-wrap');
  if (!cvWrap) {
    const wrap = document.createElement('div');
    wrap.id = 'mg-cv-wrap';
    wrap.innerHTML = cvHtml;
    const tabContent = document.getElementById('tab-macro_gbdt');
    if (tabContent) {
      const dynResult = document.getElementById('dynresult-macro_gbdt');
      if (dynResult) dynResult.insertAdjacentElement('beforebegin', wrap);
    }
  } else {
    cvWrap.innerHTML = cvHtml;
  }
  // SHAP バー
  setTimeout(() => _mrrPaintCoefBars(data.feature_coefs || {}, 'mg-coef-bars', 'mg-coef-legend'), 0);
}

const _MG_VALID_AXES = ['r2', 'r_macro'];
function _mgReadParams() {
  const g = id => document.getElementById('param-macro_gbdt-' + id);
  const d = _mgData || {};
  const lamEl = g('lambda_risk'), axEl = g('risk_axis'), tnEl = g('top_n'), gEl = g('r3_gate');
  const lambda = (lamEl && lamEl.value !== '') ? parseFloat(lamEl.value) : (d.lambda_risk ?? 1.0);
  const axis = _MG_VALID_AXES.includes(axEl && axEl.value)
    ? axEl.value
    : (_MG_VALID_AXES.includes(d.risk_axis) ? d.risk_axis : 'r2');
  const topN = (tnEl && tnEl.value !== '') ? Math.max(1, Math.round(parseFloat(tnEl.value))) : (d.top_n ?? 30);
  const r3Gate = (gEl && gEl.value !== '') ? parseFloat(gEl.value) : (d.r3_gate ?? 0.0);
  return { lambda, axis, topN, r3Gate };
}

function _mgRecompute() {
  const { lambda, axis, topN, r3Gate } = _mgReadParams();
  const items = (_mgData && _mgData.results ? _mgData.results : [])
    .filter(r => r[axis] != null && r.mu_raw != null)
    .filter(r => r3Gate <= 0 || r.r3 == null || r.r3 <= r3Gate)
    .map(r => ({ ...r, _u: r.mu_raw - lambda * r[axis], _d: lambda * r[axis] - r.mu_raw }));
  const paretoSet = _mrrParetoSet(items, axis);
  const antiParetoSet = _mrrAntiParetoSet(items, axis);
  items.forEach(it => {
    it._pareto = paretoSet.has(it.edinet_code);
    it._anti_pareto = antiParetoSet.has(it.edinet_code);
  });
  items.sort((a, b) => b._u - a._u);
  return { axis, lambda, topN, r3Gate, all: items, top: items.slice(0, topN) };
}

function _mgPaintChart(v) {
  const canvas = document.getElementById('chart-mg-bubble');
  if (!canvas || !window.Chart) return;
  if (_mgChart) { _mgChart.destroy(); _mgChart = null; }
  const pts = v.all;
  if (!pts.length) { _toggleChartEmpty(canvas, v.axis); return; }
  _hideChartEmpty(canvas);
  const axisKey = v.axis;
  const topSet = new Set(v.top.map(p => p.edinet_code));
  const us = pts.map(p => p._u);
  const uMin = Math.min(...us), uMax = Math.max(...us);
  const xRange = _mrrAxisRange(pts, axisKey);
  const yRange = _mrrAxisRange(pts, 'mu_raw');
  const clamp = (val, R) => (R.min == null || val == null) ? val : Math.min(R.max, Math.max(R.min, val));
  const bubble = pts.map(p => ({
    x: clamp(p[axisKey], xRange), y: clamp(p.mu_raw, yRange),
    r: (p._pareto || p._anti_pareto) ? 6 : (topSet.has(p.edinet_code) ? 5 : 3),
    _p: p,
  }));
  const bg = pts.map(p =>
    p._anti_pareto
      ? _mrrDColor(p._d, 0, Math.max(...pts.filter(q=>q._anti_pareto).map(q=>q._d)||[1]), 0.75)
      : _mrrUColor(p._u, uMin, uMax, topSet.has(p.edinet_code) ? 0.9 : 0.55)
  );
  const front = pts.filter(p => p._pareto).map(p => ({ x: clamp(p[axisKey], xRange), y: clamp(p.mu_raw, yRange) }));
  const antiFront = pts.filter(p => p._anti_pareto).map(p => ({ x: clamp(p[axisKey], xRange), y: clamp(p.mu_raw, yRange) }));
  const AXIS_LABELS = { r2: '実現ボラ（R2）', r_macro: 'マクロ起因リスク（R_macro）' };
  _mgChart = new Chart(canvas, {
    type: 'bubble',
    data: {
      datasets: [
        { label: '全銘柄', data: bubble, backgroundColor: bg, borderColor: pts.map(p => p._pareto?cssVar('--val-up-text'):p._anti_pareto?cssVar('--val-down-text'):topSet.has(p.edinet_code)?'rgba(226,232,240,0.9)':'rgba(148,163,184,0.3)'), borderWidth: pts.map(p=>(p._pareto||p._anti_pareto)?2.5:topSet.has(p.edinet_code)?1.2:0.4) },
        { label: '効率的フロンティア', data: front.sort((a,b)=>a.x-b.x), type:'line', borderColor:cssVar('--val-up-text'), borderWidth:1.5, pointRadius:0, fill:false, tension:0.3, order:0 },
        { label: '非効率的フロンティア', data: antiFront.sort((a,b)=>a.x-b.x), type:'line', borderColor:cssVar('--val-down-text'), borderWidth:1.5, pointRadius:0, fill:false, tension:0.3, order:0 },
      ]
    },
    options: {
      responsive:true, maintainAspectRatio:false,
      animation:{duration:0},
      plugins:{
        legend:{display:false},
        tooltip:{callbacks:{label:ctx=>{const p=ctx.raw._p;if(!p)return'';return[`${esc(p.company_name||p.edinet_code)} (${esc(p.sec_code||'')})`,`μ=${p.mu_raw!=null?(p.mu_raw*100).toFixed(2)+'%':'-'}  R=${p[axisKey]!=null?p[axisKey].toFixed(3):'-'}  U=${p._u!=null?p._u.toFixed(3):'-'}`,p._pareto?'★ 効率的フロンティア':p._anti_pareto?'▼ 非効率的フロンティア':'']}}}
      },
      scales:{
        x:{title:{display:true,text:AXIS_LABELS[axisKey]||axisKey,color:cssVar('--text-secondary'),font:{size:11}},grid:{color:'rgba(255,255,255,0.05)'},ticks:{color:cssVar('--text-muted')},...(xRange.min!=null?{min:xRange.min,max:xRange.max}:{})},
        y:{title:{display:true,text:'期待リターン μ（52週先対数リターン・年率）',color:cssVar('--text-secondary'),font:{size:11}},grid:{color:'rgba(255,255,255,0.05)'},ticks:{color:cssVar('--text-muted')},...(yRange.min!=null?{min:yRange.min,max:yRange.max}:{})},
      }
    }
  });
}

// per-stock SHAP パネル（クリックで展開）
function _mgShowShap(editnetCode) {
  if (!_mgData) return;
  const item = (_mgData.results||[]).find(r => r.edinet_code === editnetCode);
  if (!item || !item.shap) return;
  const shap = item.shap;
  const entries = Object.entries(shap).sort((a,b) => Math.abs(b[1])-Math.abs(a[1]));
  const maxAbs = Math.max(...entries.map(([,v]) => Math.abs(v))) || 1;
  const bars = entries.map(([name, v]) => {
    const w = (Math.abs(v) / maxAbs) * 50;
    const pos = v >= 0;
    const bar = pos
      ? `<div style="position:absolute;left:50%;width:${w}%;height:14px;background:#c084fc;border-radius:0 3px 3px 0"></div>`
      : `<div style="position:absolute;right:50%;width:${w}%;height:14px;background:var(--status-info);border-radius:3px 0 0 3px"></div>`;
    return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">
      <div style="width:160px;flex:none;font-size:11px;color:var(--text-body);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(MRR_FEAT_LABELS[name]||name)}</div>
      <div style="position:relative;flex:1;height:14px;background:var(--border-subtle);border-radius:3px">
        <div style="position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--text-muted)"></div>${bar}
      </div>
      <div style="width:54px;flex:none;font-size:11px;color:${pos?cssVar('--val-up-text'):cssVar('--val-down-text')};text-align:left">${pos?'+':''}${v.toFixed(3)}</div>
    </div>`;
  }).join('');
  const panel = document.getElementById('mg-shap-panel');
  if (!panel) return;
  panel.innerHTML = `
    <div style="font-size:12px;color:#c084fc;font-weight:600;margin-bottom:8px">
      SHAP 寄与内訳: ${esc(item.company_name||editnetCode)}（${esc(item.sec_code||'')}）
    </div>
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px">正（紫）= 予測リターン↑方向　負（青）= 予測リターン↓方向</div>
    ${bars}`;
  panel.classList.remove('hidden');
}

function _mgTableHTML(v) {
  const { top, axis } = v;
  if (!top.length) {
    return `<div class="text-sm" style="padding:20px;text-align:center;color:var(--text-secondary)">${esc(_riskAxisEmptyMessage(axis))}</div>`;
  }
  const frontierLabel = r => r._pareto ? '★' : r._anti_pareto ? '▼' : '';
  const rows = top.map((r,i) => {
    const frontier = frontierLabel(r);
    return `<tr style="cursor:pointer" onclick="document.dispatchEvent(new CustomEvent('mg-shap',{detail:'${esc(r.edinet_code)}'}))">
      <td>${i+1}</td>
      <td>${esc(r.sec_code||'-')}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.company_name||r.edinet_code)}</td>
      <td>${esc(r.industry||'-')}</td>
      <td class="${r.mu_raw>0?'text-green':''}">${r.mu_raw!=null?(r.mu_raw*100).toFixed(2)+'%':'-'}</td>
      <td>${r[axis]!=null?r[axis].toFixed(3):'-'}</td>
      <td>${r._u!=null?r._u.toFixed(3):'-'}</td>
      <td>${r.r3!=null?r.r3.toFixed(3):'-'}</td>
      <td style="color:${r._pareto?cssVar('--val-up-text'):r._anti_pareto?cssVar('--val-down-text'):cssVar('--text-muted')}">${frontier}</td>
    </tr>`;
  }).join('');
  return `
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">※ μ は52週（1年）先の年率対数リターン予測（例: 表示10.00% = 年率+10%）。M-1と同一ターゲットをXGBoostで学習。</div>
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px">行をクリックすると SHAP 寄与を表示します</div>
    <div id="mg-shap-panel" class="hidden" style="margin-bottom:16px;padding:12px 16px;background:var(--bg-sunken);border-radius:8px;border:1px solid var(--border-muted)"></div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>#</th><th>コード</th><th>銘柄名</th><th>業種</th>
          <th><span class="gloss" data-tip="期待リターン（M-1と同じ52週=1年先の年率対数リターン、無次元）。0.10は年率+10%を意味する（XGBoostモデルの予測値）。">μ</span></th><th>${axis==='r2'?'R2 ボラ':'R_macro'}</th><th>U=μ−λR</th><th>R3</th><th>F</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

document.addEventListener('mg-shap', e => _mgShowShap(e.detail));

function _mgRepaint() {
  if (!_mgData) return;
  const v = _mgRecompute();
  _mgPaintChart(v);
  const content = document.getElementById('dynresult-content-macro_gbdt');
  if (content) content.innerHTML = _mgTableHTML(v);
}
function _mgScheduleRepaint() {
  if (!_mgData) return;
  clearTimeout(_mgPaintTimer);
  _mgPaintTimer = setTimeout(_mgRepaint, 80);
}
const _MG_CLIENT_PARAMS = ['lambda_risk', 'risk_axis', 'top_n', 'r3_gate'];
function _mgIsClientParam(id) {
  const prefix = 'param-macro_gbdt-';
  return id.startsWith(prefix) && _MG_CLIENT_PARAMS.includes(id.slice(prefix.length));
}
document.addEventListener('input',  (e) => { if (e.target && e.target.id && _mgIsClientParam(e.target.id)) _mgScheduleRepaint(); });
document.addEventListener('change', (e) => { if (e.target && e.target.id && _mgIsClientParam(e.target.id)) _mgScheduleRepaint(); });

// ── M-3 ベイズ状態空間（時変マクロβ DLM）専用レンダラ ───────────────────────
// サーバーは µ̂ 上位 N 銘柄の最新 α/β・信用区間・α/β 経路・1期先診断を返す。
// 行クリックで銘柄を選択し、α または各 β の時系列（信用区間バンド）を Chart.js で描く。
// λ スライダー操作で効用 U=µ̂−λR_macro・Pareto 判定をクライアント側で即時再計算。
let _dlmData = null, _dlmChart = null, _dlmBubbleChart = null;
let _dlmSel = null, _dlmSeries = 'alpha', _dlmPaintTimer = null;

// bubble wrap を dynresult-macro_dlm の直前に一度だけ注入する。
function _dlmInjectBubble() {
  let wrap = document.getElementById('dlm-bubble-wrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'dlm-bubble-wrap';
    const target = document.getElementById('dynresult-macro_dlm');
    if (target) target.insertAdjacentElement('beforebegin', wrap);
  }
  wrap.innerHTML = makeChartContainer('chart-dlm-bubble', 360);
}

// λ・topN をパラメータフォームから読む（フォールバック: サーバー返却値）。
function _dlmReadUtilityParams() {
  const g = id => document.getElementById('param-macro_dlm-' + id);
  const d = _dlmData || {};
  const lamEl = g('lambda_risk'), tnEl = g('top_n');
  const lambda = lamEl && lamEl.value !== '' ? parseFloat(lamEl.value) : (d.lambda_risk ?? 1.0);
  const topN   = tnEl  && tnEl.value  !== '' ? Math.max(1, parseInt(tnEl.value)) : ((d.params && d.params.top_n) ?? 50);
  return { lambda, topN };
}

// U = µ̂ − λ × R_macro を全銘柄について計算し、Pareto 判定して U 降順に返す。
// _mrrParetoSet / _mrrAntiParetoSet は mu_raw を y 軸に使うため、mu_raw エイリアスを追加。
function _dlmRecompute() {
  const { lambda, topN } = _dlmReadUtilityParams();
  const items = (_dlmData && _dlmData.results ? _dlmData.results : [])
    .filter(r => r.r_macro != null && r.mu != null)
    .map(r => ({ ...r, mu_raw: r.mu, _u: r.mu - lambda * r.r_macro, _d: lambda * r.r_macro - r.mu }));
  const paretoSet = _mrrParetoSet(items, 'r_macro');
  const antiParetoSet = _mrrAntiParetoSet(items, 'r_macro');
  items.forEach(it => {
    it._pareto = paretoSet.has(it.edinet_code);
    it._anti_pareto = antiParetoSet.has(it.edinet_code);
  });
  items.sort((a, b) => b._u - a._u);
  return { lambda, topN, all: items, top: items.slice(0, topN) };
}

// µ̂ × R_macro バブルチャート（効率的フロンティア付き）。
function _dlmPaintBubbleChart(v) {
  const canvas = document.getElementById('chart-dlm-bubble');
  if (!canvas || !window.Chart) return;
  if (_dlmBubbleChart) { _dlmBubbleChart.destroy(); _dlmBubbleChart = null; }
  const pts = v.all;
  if (!pts.length) { _toggleChartEmpty(canvas, 'r_macro'); return; }
  _hideChartEmpty(canvas);
  const topSet = new Set(v.top.map(p => p.edinet_code));
  const us = pts.map(p => p._u);
  const uMin = Math.min(...us), uMax = Math.max(...us);
  const xRange = _mrrAxisRange(pts, 'r_macro');
  const yRange = _mrrAxisRange(pts, 'mu_raw');
  const clamp = (val, R) => (R.min == null || val == null) ? val : Math.min(R.max, Math.max(R.min, val));
  const bubble = pts.map(p => ({
    x: clamp(p.r_macro, xRange), y: clamp(p.mu_raw, yRange),
    r: (p._pareto || p._anti_pareto) ? 6 : (topSet.has(p.edinet_code) ? 5 : 3),
    _p: p,
  }));
  const antiDs = pts.filter(q => q._anti_pareto).map(q => q._d);
  const dMax = antiDs.length ? Math.max(...antiDs) : 1;
  const bg = pts.map(p =>
    p._anti_pareto
      ? _mrrDColor(p._d, 0, dMax, 0.75)
      : _mrrUColor(p._u, uMin, uMax, topSet.has(p.edinet_code) ? 0.9 : 0.55)
  );
  const border = pts.map(p => p._pareto ? cssVar('--val-up-text') : p._anti_pareto ? cssVar('--val-down-text') : topSet.has(p.edinet_code) ? 'rgba(226,232,240,0.9)' : 'rgba(148,163,184,0.3)');
  const bw = pts.map(p => (p._pareto || p._anti_pareto) ? 2.5 : topSet.has(p.edinet_code) ? 1.2 : 0.4);
  const front     = pts.filter(p => p._pareto).sort((a, b) => a.r_macro - b.r_macro).map(p => ({ x: clamp(p.r_macro, xRange), y: clamp(p.mu_raw, yRange) }));
  const antiFront = pts.filter(p => p._anti_pareto).sort((a, b) => a.r_macro - b.r_macro).map(p => ({ x: clamp(p.r_macro, xRange), y: clamp(p.mu_raw, yRange) }));
  _dlmBubbleChart = new Chart(canvas, {
    type: 'bubble',
    data: {
      datasets: [
        { label: '全銘柄', data: bubble, backgroundColor: bg, borderColor: border, borderWidth: bw },
        { label: '効率的フロンティア（買い）', data: front, type: 'line', borderColor: cssVar('--val-up-text'), borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.3, order: 0 },
        { label: '非効率的フロンティア（売り）', data: antiFront, type: 'line', borderColor: cssVar('--val-down-text'), borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.3, order: 0 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => {
          const p = ctx.raw._p;
          if (!p) return '';
          return [
            `${esc(p.company_name || p.edinet_code)} (${esc(p.sec_code || '')})`,
            `µ̂=${(p.mu * 100).toFixed(1)}%  R_macro=${p.r_macro != null ? (p.r_macro * 100).toFixed(2) + '%' : '-'}  U=${p._u != null ? p._u.toFixed(3) : '-'}`,
            p._pareto ? '★ 効率的フロンティア（買い）' : p._anti_pareto ? '▼ 非効率的フロンティア（売り）' : '',
          ].filter(Boolean);
        }}}
      },
      scales: {
        x: { title: { display: true, text: 'R_macro マクロ起因リスク（年率化）', color: cssVar('--text-secondary'), font: { size: 11 } }, grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: cssVar('--text-muted') }, ...(xRange.min != null ? { min: xRange.min, max: xRange.max } : {}) },
        y: { title: { display: true, text: 'µ̂ 期待リターン（年率化アルファ）', color: cssVar('--text-secondary'), font: { size: 11 } }, grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: cssVar('--text-muted') }, ...(yRange.min != null ? { min: yRange.min, max: yRange.max } : {}) },
      }
    }
  });
}

function renderMacroDlm(data) {
  _dlmData = data;
  const rows = data.results || [];
  _dlmSel = rows.length ? rows[0].edinet_code : null;
  _dlmSeries = 'alpha';
  _dlmInjectBubble();
  const v = _dlmRecompute();
  setTimeout(() => { _dlmPaintBubbleChart(v); _dlmPaintChart(); }, 0);
  return _dlmTableHTML(data, v);
}

function _dlmDiagHTML(data) {
  const d = data.diagnostics || {};
  const p = data.params || {};
  return `<div style="margin-bottom:16px;padding:12px 16px;background:var(--bg-sunken);border-radius:8px;border:1px solid var(--border-muted)">
    <div style="font-size:12px;color:var(--accent-text);font-weight:600;margin-bottom:8px">
      1期先予測診断（バーンイン除外・全 ${data.n_companies || 0} 銘柄平均）
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px"><div style="font-size:10px;color:var(--text-muted)">校正（標準化誤差² 平均・1 が理想）</div><div style="font-size:15px;font-weight:700;color:#c084fc">${d.calibration != null ? d.calibration.toFixed(3) : '-'}</div></div>
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px"><div style="font-size:10px;color:var(--text-muted)">予測 RMSE（週次リターン）</div><div style="font-size:15px;font-weight:700;color:var(--text-secondary)">${d.pred_rmse != null ? d.pred_rmse.toFixed(4) : '-'}</div></div>
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px"><div style="font-size:10px;color:var(--text-muted)">95% 信用区間カバレッジ</div><div style="font-size:15px;font-weight:700;color:${(d.coverage95 || 0) >= 0.9 ? cssVar('--val-up-text') : cssVar('--status-warn-text')}">${d.coverage95 != null ? (d.coverage95 * 100).toFixed(1) + '%' : '-'}</div></div>
    </div>
    <div style="margin-top:8px;font-size:11px;color:var(--text-muted)">δ=${p.state_discount ?? '-'} ／ β_v=${p.var_discount ?? '-'} ／ 最低 ${p.min_weeks ?? '-'} 週 ／ バーンイン ${p.burn_in_weeks ?? '-'} 週</div>
    ${(() => {
      const dropped = d.dropped_factors || [];
      if (!dropped.length) return '';
      const items = dropped.map(x => `${esc(x.label || x.feature)}（被覆 ${x.coverage != null ? (x.coverage * 100).toFixed(0) + '%' : '-'}）`).join('、');
      return `<div style="margin-top:8px;font-size:11px;color:${cssVar('--status-warn-text')}">⚠ データ蓄積不足のためモデルから自動除外した factor: ${items}<span style="color:var(--text-muted)">（企業母集団は維持されます）</span></div>`;
    })()}
  </div>`;
}

function _dlmOofHTML(data) {
  const oof = data.oof_backtest || {};
  const qr  = oof.quantile_returns || [];
  const ic  = oof.rank_ic || {};
  const hasOof = qr.length > 0;
  return `
  <div style="margin-bottom:16px;padding:12px 16px;background:var(--bg-sunken);border-radius:8px;border:1px solid var(--border-muted)">
    <div style="font-size:12px;color:var(--accent-text);font-weight:600;margin-bottom:4px">
      アウトオブサンプル検証（OOF）— α_{t-1} が翌週リターンを横断順序付けするか（無リーク・1期先）
    </div>
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
      DLM フィルタの1期先予測 α を月次クロスセクションで集計。M-1/M-2 の年率OOFと同枠で比較可能。
    </div>
    ${hasOof ? `
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px">
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">rank-IC（Spearman 平均±std）</div>
        <div style="font-size:15px;font-weight:700;color:${(ic.mean||0)>0?cssVar('--val-up-text'):cssVar('--val-down-text')}">${ic.mean!=null?ic.mean.toFixed(3):'-'}<span style="font-size:11px;color:var(--text-muted)"> ±${ic.std!=null?ic.std.toFixed(3):'-'}</span></div>
        <div style="font-size:10px;color:var(--text-muted)">${ic.n||0} fold</div>
      </div>
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">ロングショート spread（top−bottom）</div>
        <div style="font-size:15px;font-weight:700;color:${(oof.long_short_spread||0)>0?cssVar('--val-up-text'):cssVar('--val-down-text')}">${oof.long_short_spread!=null?(oof.long_short_spread*100).toFixed(2)+'%':'-'}</div>
      </div>
      <div style="padding:8px;background:var(--bg-sunken);border-radius:6px">
        <div style="font-size:10px;color:var(--text-muted)">hit-rate（top&gt;bottom の期）</div>
        <div style="font-size:15px;font-weight:700;color:#c084fc">${oof.hit_rate!=null?(oof.hit_rate*100).toFixed(0)+'%':'-'}</div>
        <div style="font-size:10px;color:var(--text-muted)">${oof.n_periods_quantile||0} 期</div>
      </div>
    </div>
    <div style="font-size:11px;color:var(--text-secondary);margin-bottom:6px">分位別 平均実現リターン（左=最低 α → 右=最高 α・週次・期間平均）</div>
    <div style="display:flex;align-items:flex-end;gap:6px;height:92px">
      ${(() => {
        const mx = Math.max(...qr.map(Math.abs), 1e-9);
        return qr.map((v, i) => {
          const h = Math.round(Math.abs(v) / mx * 70) + 2;
          const col = v >= 0 ? cssVar('--val-up-text') : cssVar('--val-down-text');
          return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%">
            <div style="font-size:10px;color:${col}">${(v*100).toFixed(2)}%</div>
            <div style="width:100%;height:${h}px;background:${col};border-radius:3px 3px 0 0"></div>
            <div style="font-size:10px;color:var(--text-muted);margin-top:2px">Q${i+1}</div>
          </div>`;
        }).join('');
      })()}
    </div>` : `<div style="font-size:11px;color:var(--text-muted)">OOF サンプルが期内 ${(oof.n_quantiles||5)*2} 銘柄未満のため分位を表示できません。rank-IC は ${ic.n||0} fold で算出。</div>`}
  </div>`;
}

// v: _dlmRecompute() の戻り値（null 可）。U / Pareto 列の注釈用。
function _dlmTableHTML(data, v) {
  const rows = data.results || [];
  const factors = data.macro_features || [];
  const labels = data.factor_labels || {};
  const seriesOpts = ['<option value="alpha">α（潜在アルファ）</option>']
    .concat(factors.map(f => `<option value="${esc(f)}">β: ${esc(labels[f] || f)}</option>`)).join('');
  const betaHead = factors.map(f => `<th>β:${esc(labels[f] || f)}</th>`).join('');
  const betaCells = r => factors.map(f => {
    const b = (r.beta_latest || {})[f] || {};
    return `<td style="text-align:right;font-family:monospace">${b.mean != null ? b.mean.toFixed(3) : '-'}</td>`;
  }).join('');
  // U / Pareto の参照マップ（r_macro 未計算銘柄は null）。
  const utilMap = {};
  if (v) v.all.forEach(it => { utilMap[it.edinet_code] = it; });
  const lambda = v ? v.lambda : null;
  const trs = rows.map((r, i) => {
    const seld = r.edinet_code === _dlmSel;
    const mu = r.mu;
    const ui = utilMap[r.edinet_code];
    const u = ui ? ui._u : null;
    const frontier = ui && ui._pareto ? '★' : ui && ui._anti_pareto ? '▼' : '';
    const frontColor = ui && ui._pareto ? cssVar('--val-up-text') : ui && ui._anti_pareto ? cssVar('--val-down-text') : cssVar('--text-muted');
    return `<tr style="cursor:pointer;${seld ? 'background:rgba(124,58,237,0.14)' : ''}" onclick="document.dispatchEvent(new CustomEvent('dlm-select',{detail:'${esc(r.edinet_code)}'}))">
      <td>${i + 1}</td>
      <td>${esc(r.sec_code || '-')}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.company_name || r.edinet_code)}</td>
      <td>${esc(r.industry || '-')}</td>
      <td class="${mu > 0 ? 'text-green' : 'text-red'}" style="text-align:right;font-family:monospace">${mu != null ? (mu * 100).toFixed(1) + '%' : '-'}</td>
      <td style="text-align:right;font-family:monospace;color:var(--accent-text)">${r.r_macro != null ? (r.r_macro * 100).toFixed(2) + '%' : '-'}</td>
      <td style="text-align:right;font-family:monospace;color:${u != null && u > 0 ? cssVar('--val-up-text') : cssVar('--val-down-text')}">${u != null ? u.toFixed(3) : '-'}</td>
      <td style="text-align:center;color:${frontColor};font-weight:600">${frontier}</td>
      ${betaCells(r)}
      <td style="text-align:right;font-family:monospace">${r.pred_rmse != null ? r.pred_rmse.toFixed(4) : '-'}</td>
      <td style="text-align:right;font-family:monospace">${r.coverage95 != null ? (r.coverage95 * 100).toFixed(0) + '%' : '-'}</td>
    </tr>`;
  }).join('');
  const lambdaTag = lambda != null ? `<span class="tag" style="margin-left:6px">λ=${lambda}</span>` : '';
  return _dlmDiagHTML(data) + _dlmOofHTML(data) + `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
      <div style="font-size:11px;color:var(--text-muted)">行クリックで銘柄を選択 → 下に α/β の時系列（信用区間バンド）を表示${lambdaTag}</div>
      <span style="flex:1"></span>
      <label style="font-size:11px;color:var(--text-secondary)">表示系列
        <select id="dlm-series" style="margin-left:6px;background:var(--bg-sunken);color:var(--text);border:1px solid var(--border-subtle);border-radius:6px;padding:4px 6px;font-size:12px">${seriesOpts}</select>
      </label>
    </div>
    ${makeChartContainer('chart-dlm', 320)}
    <div style="overflow-x:auto"><table>
      <thead><tr><th>#</th><th>コード</th><th>銘柄名</th><th>業種</th><th><span class="gloss" data-tip="期待リターン（週次の潜在アルファ状態推定値をα×52で年率換算、無次元）。0.10は年率+10%を意味する。カルマンフィルタによる最新推定値のため、M-1/M-2（回帰・機械学習の予測値）とは算出方式が異なる。">µ̂(年率)</span></th><th>R_macro</th><th>U=µ̂−λR</th><th>F</th>${betaHead}<th>RMSE</th><th>被覆</th></tr></thead>
      <tbody>${trs}</tbody>
    </table></div>`;
}

function _dlmPaintChart() {
  const canvas = document.getElementById('chart-dlm');
  if (!canvas || !window.Chart || !_dlmData) return;
  if (_dlmChart) { _dlmChart.destroy(); _dlmChart = null; }
  const row = (_dlmData.results || []).find(r => r.edinet_code === _dlmSel);
  if (!row || !row.path) return;
  const path = row.path;
  const ser = _dlmSeries === 'alpha' ? path.alpha : (path.beta || {})[_dlmSeries];
  if (!ser) return;
  const isAlpha = _dlmSeries === 'alpha';
  const title = isAlpha ? 'α（週次・潜在アルファ）' : ('β: ' + ((_dlmData.factor_labels || {})[_dlmSeries] || _dlmSeries));
  const band = 'rgba(124,58,237,0.16)';
  _dlmChart = new Chart(canvas, {
    type: 'line',
    data: {
      labels: path.dates,
      datasets: [
        { label: '上限', data: ser.hi, borderColor: 'transparent', backgroundColor: band, pointRadius: 0, fill: '+1', tension: 0.2 },
        { label: '下限', data: ser.lo, borderColor: 'transparent', backgroundColor: band, pointRadius: 0, fill: false, tension: 0.2 },
        { label: title, data: ser.mean, borderColor: cssVar('--accent-text'), borderWidth: 1.6, pointRadius: 0, fill: false, tension: 0.2 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
      plugins: {
        legend: { display: true, labels: { filter: it => it.text !== '上限' && it.text !== '下限' } },
        title: { display: true, text: `${esc(row.company_name || row.edinet_code)}（${esc(row.sec_code || '')}） — ${title}`, color: cssVar('--text-body'), font: { size: 12 } },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: { type: 'category', grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: cssVar('--text-muted'), maxTicksLimit: 8 } },
        y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: cssVar('--text-muted') } },
      }
    }
  });
}

function _dlmRepaint() {
  if (!_dlmData) return;
  const v = _dlmRecompute();
  _dlmPaintBubbleChart(v);
  const content = document.getElementById('dynresult-content-macro_dlm');
  if (content) content.innerHTML = _dlmTableHTML(_dlmData, v);
  const sel = document.getElementById('dlm-series');
  if (sel) sel.value = _dlmSeries;          // 行クリック再描画で系列選択を保持
  setTimeout(() => _dlmPaintChart(), 0);
}
function _dlmScheduleRepaint() {
  if (!_dlmData) return;
  clearTimeout(_dlmPaintTimer);
  _dlmPaintTimer = setTimeout(_dlmRepaint, 80);   // λ スライダーのデバウンス
}
const _DLM_CLIENT_PARAMS = ['lambda_risk', 'top_n'];
function _dlmIsClientParam(id) {
  const prefix = 'param-macro_dlm-';
  return id.startsWith(prefix) && _DLM_CLIENT_PARAMS.includes(id.slice(prefix.length));
}
document.addEventListener('dlm-select', e => { _dlmSel = e.detail; _dlmRepaint(); });
document.addEventListener('change', e => {
  if (e.target && e.target.id === 'dlm-series') { _dlmSeries = e.target.value; _dlmPaintChart(); }
  if (e.target && e.target.id && _dlmIsClientParam(e.target.id)) _dlmScheduleRepaint();
});
document.addEventListener('input', e => { if (e.target && e.target.id && _dlmIsClientParam(e.target.id)) _dlmScheduleRepaint(); });

// 汎用結果レンダラ（フォールバック）: results 配列を表に、無ければ JSON を整形表示する。
function _renderGenericResult(data) {
  if (Array.isArray(data.results) && data.results.length) {
    const cols = Object.keys(data.results[0]);
    const header = cols.map(c => `<th>${esc(c)}</th>`).join('');
    const rows = data.results.map(r =>
      `<tr>${cols.map(c => `<td>${esc(String(r[c] ?? '-'))}</td>`).join('')}</tr>`).join('');
    return `<div style="overflow-x:auto"><table><thead><tr>${header}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  return `<pre style="color:var(--text-secondary);font-size:12px;white-space:pre-wrap">${esc(JSON.stringify(data, null, 2))}</pre>`;
}

function dl(content, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([content], {type:'text/csv'}));
  a.download = name;
  a.click();
}

// CSV出力統一: タブごとにバラバラだった export*/dl* を単一の exportCSV(name) へ集約。
// 各ビルダは対応する結果キャッシュ（gapResults 等）を読んで dl() でダウンロードする。
const CSV_EXPORTERS = {
  'gap_analysis':      exportGapCSV,
  'recommend':         exportRecommendCSV,
  'sell_ranking':      exportSellRankingCSV,
  'net_cash_analysis': exportNetCashCSV,
  'backtest':          exportBtCSV,
};
function exportCSV(name) {
  const fn = CSV_EXPORTERS[name];
  if (fn) fn();
}

// 初期化
initAuth();
initRecommend();  // おすすめ銘柄タブのプリセット取得（既存タブ用）
initSellRanking();  // 売り候補タブ: ウェイトグリッド描画＋保有入力の localStorage 復元
// 軽量モード判定を先に解決してから動的タブを生成（重い回帰の無効化に必要）
initLightMode().then(() => initPlugins());
preflight();

// data 属性ハンドラ用ヘルパ（this=対象要素）
function syncVal(){ const el=document.getElementById(this.dataset.target); if(el) el.textContent = this.value; }
function syncWVal(){ const el=document.getElementById(this.dataset.target); if(el) el.textContent = parseFloat(this.value).toFixed(1); }


// ===== CSP: インラインハンドラ撤廃のためのイベント委譲ディスパッチャ =====
// 要素の data-click / data-change / data-input / data-keydown = 呼び出す関数名、
// data-arg / data-arg2 = 引数（'true'/'false' は真偽値に変換）。委譲のため動的生成要素にも有効。
// fn.apply(el, args) により this=要素 を保存する（インラインハンドラ互換）。
function _coerceArg(v){ if(v===undefined) return undefined; if(v==='true') return true; if(v==='false') return false; return v; }
function _wireDelegate(eventType, key){
  document.addEventListener(eventType, function(e){
    const el = e.target.closest('[data-'+key+']');
    if(!el) return;
    const fn = window[el.dataset[key]];
    if(typeof fn !== 'function') return;
    const args=[];
    if('arg' in el.dataset)  args.push(_coerceArg(el.dataset.arg));
    if('arg2' in el.dataset) args.push(_coerceArg(el.dataset.arg2));
    fn.apply(el, args);
  });
}
['click','change','input','keydown'].forEach(function(ev){ _wireDelegate(ev, ev); });
