function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function apiBase(){return '';}

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

function _getCookie(name){
  const m = document.cookie.match('(^|; )' + name + '=([^;]*)');
  return m ? decodeURIComponent(m[2]) : '';
}
async function apiFetch(path){
  const heads = {'Content-Type':'application/json'};
  const r = await fetch(apiBase() + path, {headers: heads, credentials: 'same-origin'});
  if (r.status === 401){ location.href = '/login?next=' + encodeURIComponent(location.pathname); return null; }
  if (!r.ok){
    if (r.status===502||r.status===503||r.status===504)
      throw new Error(`サーバー再起動中 (${r.status})。しばらく待ってから再試行してください`);
    if (r.status===404) throw new Error('NOT_FOUND');
    throw new Error(await r.text());
  }
  return r.json();
}

// ── 数値フォーマット ──────────────────────────────────────────
const OKU = 1e8;
function toOku(v){ return (v==null || isNaN(v)) ? null : v/OKU; }       // 円 → 億円
function mnToOku(v){ return (v==null || isNaN(v)) ? null : v/100; }     // 百万円 → 億円（market_cap 系）
function fmtNum(v, digits=1){ return (v==null || isNaN(v)) ? '—' : Number(v).toLocaleString('ja-JP',{maximumFractionDigits:digits}); }
function fmtPct(v){ return (v==null || isNaN(v)) ? '—' : Number(v).toFixed(1) + '%'; }
function fmtX(v){ return (v==null || isNaN(v)) ? '—' : Number(v).toFixed(2) + '倍'; }

// ── Chart.js ダークテーマ既定 ────────────────────────────────
if (window.Chart){
  Chart.defaults.color = '#94a3b8';
  Chart.defaults.borderColor = '#1e2235';
  Chart.defaults.font.family = "'Segoe UI', sans-serif";
  Chart.defaults.font.size = 11;
}
const charts = {};
function destroyCharts(){ for (const k in charts){ if (charts[k]){ charts[k].destroy(); delete charts[k]; } } }
let curCompany = {code:null, industry:null, sec_code:null, name:null, latest:null};
let peersLoaded = false;

const baseOpts = (extra={}) => ({
  responsive:true, maintainAspectRatio:false,
  interaction:{mode:'index', intersect:false},
  plugins:{ legend:{labels:{color:'#cbd5e1', boxWidth:12, padding:14}},
            tooltip:{backgroundColor:'#0f1117', borderColor:'#2d3154', borderWidth:1, padding:10} },
  scales:{ x:{ ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
           y:{ ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} } },
  ...extra
});

// ── 企業コードを URL から取得 ────────────────────────────────
function currentCode(){
  // /company/{edinet_code} の {edinet_code} を抽出。
  // EDINET コードは E+数字（実データは E+5桁、例: E02167）。桁数を固定せずパスセグメントをそのまま取得する。
  const m = location.pathname.match(/^\/company\/([^\/?#]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

// ── 検索 ──────────────────────────────────────────────────────
let searchTimer = null;
const searchInput = document.getElementById('search-input');
const searchResults = document.getElementById('search-results');

searchInput.addEventListener('input', () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 1){ searchResults.classList.remove('show'); searchResults.innerHTML=''; return; }
  searchTimer = setTimeout(() => runSearch(q), 250);
});
searchInput.addEventListener('keydown', (e) => {
  if (e.key === 'Escape'){ searchResults.classList.remove('show'); }
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.search-wrap')) searchResults.classList.remove('show');
});

async function runSearch(q){
  try{
    const d = await apiFetch('/api/companies?limit=20&q=' + encodeURIComponent(q));
    if (!d) return;
    if (!d.items || d.items.length === 0){
      searchResults.innerHTML = '<div class="search-item" style="color:#64748b;cursor:default">該当する企業がありません</div>';
      searchResults.classList.add('show');
      return;
    }
    searchResults.innerHTML = d.items.map(c => `
      <div class="search-item" role="option" data-code="${esc(c.edinet_code)}">
        <span class="code">${esc(c.sec_code || '----')}</span>
        <span>${esc(c.name)}</span>
        <span class="ind">${esc(c.industry || '')}</span>
      </div>`).join('');
    searchResults.querySelectorAll('.search-item[data-code]').forEach(el => {
      el.addEventListener('click', () => { location.href = '/company/' + el.dataset.code; });
    });
    searchResults.classList.add('show');
  }catch(e){ showNotif('検索に失敗しました: ' + e.message); }
}

// ── タブ ──────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('panel-' + t.dataset.tab).classList.add('active');
    if (t.dataset.tab === 'peer' && !peersLoaded) loadPeers();
  });
});

// ── 粒度セレクタ（粗 / 中 / 細）共通配線。BS・PL・CF で再利用 ──────────
function wireGran(sel, onPick){
  const btns = document.querySelectorAll(sel + ' .seg-btn');
  btns.forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.classList.contains('active')) return;
      btns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      onPick(btn.dataset.gran);
    });
  });
}
wireGran('#bs-gran', g => { bsGran = g; drawBS(); });
wireGran('#pl-gran', g => { plGran = g; drawPL(); });
wireGran('#cf-gran', g => { cfGran = g; drawCF(); });

// ── 企業データ読み込み・描画 ─────────────────────────────────
async function loadCompany(code){
  const empty = document.getElementById('empty-state');
  const view  = document.getElementById('company-view');
  empty.style.display = 'block';
  empty.querySelector('.big').innerHTML = '<span class="spinner"></span> 読み込み中…';
  empty.querySelector('div:last-child').textContent = code;
  view.style.display = 'none';

  let d;
  try{
    d = await apiFetch('/api/financials/' + encodeURIComponent(code));
  }catch(e){
    if (e.message === 'NOT_FOUND'){
      empty.querySelector('.big').textContent = '財務データが見つかりません';
      empty.querySelector('div:last-child').textContent = `企業コード ${code} の財務レコードがありません。`;
    } else {
      empty.querySelector('.big').textContent = '読み込みに失敗しました';
      empty.querySelector('div:last-child').textContent = e.message;
    }
    return;
  }
  if (!d || !d.records || d.records.length === 0){
    empty.querySelector('.big').textContent = '財務データが見つかりません';
    return;
  }

  const recs = d.records;                 // year 昇順（API 側でソート済み）
  const latest = recs[recs.length - 1];
  const labels = recs.map(r => r.year);
  curCompany = { code: code, industry: latest.industry, sec_code: latest.sec_code, name: latest.company_name, latest: latest };

  // ヘッダ
  document.getElementById('co-name').textContent = latest.company_name || code;
  document.title = (latest.company_name || code) + ' | 企業詳細';
  document.getElementById('co-meta').innerHTML = [
    latest.sec_code ? `<span class="tag code">${esc(latest.sec_code)}</span>` : '',
    `<span class="tag code">${esc(code)}</span>`,
    latest.industry ? `<span class="tag">${esc(latest.industry)}</span>` : '',
  ].join('');

  // サマリーカード（最新年度）
  const cards = [
    {label:`売上高 (${latest.year})`, value: fmtNum(toOku(latest.pl.revenue)), sub:'億円'},
    {label:'営業利益', value: fmtNum(toOku(latest.pl.operating_profit)), sub:'億円'},
    {label:'営業利益率', value: fmtPct(latest.pl.op_margin), sub:''},
    {label:'純利益', value: fmtNum(toOku(latest.pl.net_income)), sub:'億円'},
    {label:'ROE', value: fmtPct(latest.val.roe), sub:''},
    {label:'自己資本比率', value: fmtPct(latest.bs.equity_ratio), sub:''},
    {label:'PER', value: fmtX(latest.val.per), sub:''},
    {label:'PBR', value: fmtX(latest.val.pbr), sub:''},
  ];
  document.getElementById('summary-cards').innerHTML = cards.map(c => `
    <div class="stat-card">
      <div class="stat-label">${esc(c.label)}</div>
      <div class="stat-value">${esc(c.value)}</div>
      ${c.sub ? `<div class="stat-sub">${esc(c.sub)}</div>` : ''}
    </div>`).join('');

  destroyCharts();
  renderPerf(labels, recs);
  renderMargin(labels, recs);
  renderBS(labels, recs);
  renderCF(labels, recs);
  renderPsh(labels, recs);
  renderDiv(labels, recs);
  renderValRatio(labels, recs);
  renderMcap(labels, recs);
  renderZscore(latest);
  renderNC(labels, recs);

  empty.style.display = 'none';
  view.style.display = 'block';

  // 株価（別ソース・取得失敗してもページは表示する）
  try {
    const hist = await apiFetch('/api/stock/history/' + encodeURIComponent(code) + '?days=1825');
    renderPrice(hist || []);
  } catch(e) {
    renderPrice([]);
  }
}

// ── PL：売上高を費用・利益に分解した積み上げ棒（BS と同じ手法）──
//  信頼できる revenue / operating_profit / net_income / cost_of_sales / sga を軸に分解。
//  stored gross_profit は IFRS で不正値があるため使わない。どの粒度でも棒の合計＝売上高。
let plState = { labels:null, recs:null };
let plGran  = 'medium';

function plDatasets(recs, level){
  const num = v => { const x = toOku(v); return (x==null || isNaN(x)) ? 0 : x; };
  const rev = r => num(r.pl.revenue);
  const cos = r => num(r.pl.cost_of_sales);
  const sga = r => num(r.pl.sga);
  const op  = r => num(r.pl.operating_profit);
  const profit = r => Math.max(0, num(r.pl.net_income));   // 純利益（損失は0クランプ）
  // 残差は非負クランプ。profit を基準に各段を算出し、合計が売上高になるよう補正。
  const costTotal = r => Math.max(0, rev(r) - profit(r));                       // 粗: 総費用
  const otherToNi = r => Math.max(0, rev(r) - cos(r) - sga(r) - profit(r));     // 中: その他費用・税金等
  const otherOpEx = r => Math.max(0, rev(r) - cos(r) - sga(r) - op(r));         // 細: その他営業費用
  const nonOpTax  = r => Math.max(0, op(r) - profit(r));                        // 細: 営業外・特別・税金等

  const C = { cos:'#475569', sga:'#f59e0b', other:'#94a3b8', nonop:'#fb923c', profit:'#34d399', cost:'#64748b' };
  const bar = (label, color, fn) =>
    ({ label, backgroundColor:color, stack:'pl', borderRadius:2, data:recs.map(fn) });

  // 下→上の順。費用を底に積み、純利益を最上部に置く。
  if (level === 'coarse'){
    return [ bar('総費用', C.cost, costTotal), bar('純利益', C.profit, profit) ];
  }
  if (level === 'fine'){
    return [
      bar('売上原価',             C.cos,    cos),
      bar('販管費',               C.sga,    sga),
      bar('その他営業費用',       C.other,  otherOpEx),
      bar('営業外・特別・税金等', C.nonop,  nonOpTax),
      bar('純利益',               C.profit, profit),
    ];
  }
  // medium
  return [
    bar('売上原価',         C.cos,    cos),
    bar('販管費',           C.sga,    sga),
    bar('その他費用・税金等', C.other,  otherToNi),
    bar('純利益',           C.profit, profit),
  ];
}

function renderPerf(labels, recs){
  plState = { labels, recs };
  drawPL();
}

function drawPL(){
  const { labels, recs } = plState;
  if (!recs) return;
  if (charts.perf){ charts.perf.destroy(); }
  const datasets = plDatasets(recs, plGran);
  datasets.push({ label:'営業利益率(%)', type:'line', data:recs.map(r=>r.pl.op_margin), yAxisID:'y1',
    borderColor:'#f59e0b', backgroundColor:'#f59e0b', tension:.3, pointRadius:3, order:0 });

  charts.perf = new Chart(document.getElementById('chart-perf'), {
    type:'bar',
    data:{ labels, datasets },
    options: baseOpts({
      plugins:{
        legend:{ labels:{ color:'#cbd5e1', boxWidth:12, padding:10 } },
        tooltip:{ backgroundColor:'#0f1117', borderColor:'#2d3154', borderWidth:1, padding:10,
          callbacks:{ label:(ctx)=>{
            const ds = ctx.dataset, v = ctx.parsed.y;
            if (ds.yAxisID === 'y1') return ` ${ds.label}: ${fmtNum(v)}%`;
            const rev = toOku(plState.recs[ctx.dataIndex].pl.revenue) || 0;
            const pct = rev > 0 ? (v / rev * 100) : null;
            return ` ${ds.label}: ${fmtNum(v)} 億円` + (pct!=null ? `（${fmtNum(pct)}%）` : '');
          }}}
      },
      scales:{
        x:{ stacked:true, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
        y:{ stacked:true, title:{display:true, text:'億円', color:'#64748b'}, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
        y1:{ position:'right', title:{display:true, text:'%', color:'#64748b'}, ticks:{color:'#f59e0b'}, grid:{drawOnChartArea:false} },
      }
    })
  });
}

function renderMargin(labels, recs){
  const ctx = document.getElementById('chart-margin');
  charts.margin = new Chart(ctx, {
    type:'line',
    data:{ labels, datasets:[
      {label:'営業利益率(%)', data:recs.map(r=>r.pl.op_margin), borderColor:'#f59e0b', backgroundColor:'#f59e0b', tension:.3, pointRadius:3},
      {label:'純利益率(%)', data:recs.map(r=>r.pl.net_margin), borderColor:'#34d399', backgroundColor:'#34d399', tension:.3, pointRadius:3},
    ]},
    options: baseOpts()
  });
}

// ── BS：借方（資産）／貸方（負債・純資産）を並べた2本の積み上げ棒 ──
//  粒度（粗/中/細）で内訳の細かさを切り替える。バフェットコード型の対比表示。
//  どの粒度でも 資産バー = 負債純資産バー = 総資産 になるよう各セグメントを補正。
let bsState = { labels:null, recs:null };
let bsGran  = 'medium';   // 'coarse' | 'medium' | 'fine'

function bsDatasets(recs, level){
  // 各アクセサは「億円」を返す（NULL は 0 扱い）
  const num = v => { const x = toOku(v); return (x==null || isNaN(x)) ? 0 : x; };
  const ta   = r => num(r.bs.total_assets);
  const ca   = r => num(r.bs.current_assets);
  const cash = r => num(r.bs.cash);
  const te   = r => num(r.bs.total_equity);
  const std  = r => num(r.bs.short_term_debt);
  const ltd  = r => num(r.bs.long_term_debt);
  // 資産側（合計＝総資産になるよう固定資産で帳尻）
  const fixed   = r => Math.max(0, ta(r) - ca(r));      // 固定資産
  const otherCa = r => Math.max(0, ca(r) - cash(r));    // その他流動資産
  // 負債側（合計＝総資産−純資産＝負債合計）
  const liab    = r => Math.max(0, ta(r) - te(r));      // 負債合計
  const mInt    = r => Math.min(std(r) + ltd(r), liab(r));        // 有利子負債（中）
  const mOth    = r => Math.max(0, liab(r) - mInt(r));            // その他負債（中）
  const fStd    = r => Math.min(std(r), liab(r));                 // 短期有利子負債（細）
  const fLtd    = r => Math.min(ltd(r), Math.max(0, liab(r) - fStd(r))); // 長期有利子負債（細）
  const fOth    = r => Math.max(0, liab(r) - fStd(r) - fLtd(r));  // その他負債（細）

  const C = {
    fixed:'#4f46e5', otherCa:'#3b82f6', cash:'#22d3ee', ca:'#3b82f6',   // 資産＝青系
    equity:'#34d399', otherLi:'#64748b', intDebt:'#f87171',            // 負債純資産
    stDebt:'#f87171', ltDebt:'#fb923c',
  };
  const bar = (label, color, stack, fn) =>
    ({ label, backgroundColor:color, stack, borderRadius:2, data:recs.map(fn) });

  // データセットは「下→上」の順。資産バーは固定資産を底に、負債純資産バーは純資産を底に置く。
  if (level === 'coarse'){
    return [
      bar('固定資産', C.fixed,  'asset', fixed),
      bar('流動資産', C.ca,     'asset', ca),
      bar('純資産',   C.equity, 'le',    te),
      bar('負債',     C.otherLi,'le',    liab),
    ];
  }
  if (level === 'fine'){
    return [
      bar('固定資産',       C.fixed,   'asset', fixed),
      bar('その他流動資産', C.otherCa, 'asset', otherCa),
      bar('現金及び預金',   C.cash,    'asset', cash),
      bar('純資産',         C.equity,  'le',    te),
      bar('その他負債',     C.otherLi, 'le',    fOth),
      bar('長期有利子負債', C.ltDebt,  'le',    fLtd),
      bar('短期有利子負債', C.stDebt,  'le',    fStd),
    ];
  }
  // medium
  return [
    bar('固定資産',       C.fixed,   'asset', fixed),
    bar('その他流動資産', C.otherCa, 'asset', otherCa),
    bar('現金及び預金',   C.cash,    'asset', cash),
    bar('純資産',         C.equity,  'le',    te),
    bar('その他負債',     C.otherLi, 'le',    mOth),
    bar('有利子負債',     C.intDebt, 'le',    mInt),
  ];
}

function renderBS(labels, recs){
  bsState = { labels, recs };
  drawBS();
}

function drawBS(){
  const { labels, recs } = bsState;
  if (!recs) return;
  if (charts.bs){ charts.bs.destroy(); }
  const datasets = bsDatasets(recs, bsGran);
  datasets.push({ label:'自己資本比率(%)', type:'line', data:recs.map(r=>r.bs.equity_ratio),
    yAxisID:'y1', borderColor:'#a78bfa', backgroundColor:'#a78bfa', tension:.3, pointRadius:3, order:0 });

  charts.bs = new Chart(document.getElementById('chart-bs'), {
    type:'bar',
    data:{ labels, datasets },
    options: baseOpts({
      plugins:{
        legend:{ labels:{ color:'#cbd5e1', boxWidth:12, padding:10 } },
        tooltip:{ backgroundColor:'#0f1117', borderColor:'#2d3154', borderWidth:1, padding:10,
          callbacks:{ label:(ctx)=>{
            const ds = ctx.dataset, v = ctx.parsed.y;
            if (ds.yAxisID === 'y1') return ` ${ds.label}: ${fmtNum(v)}%`;
            const ta = toOku(bsState.recs[ctx.dataIndex].bs.total_assets) || 0;
            const pct = ta > 0 ? (v / ta * 100) : null;
            return ` ${ds.label}: ${fmtNum(v)} 億円` + (pct!=null ? `（${fmtNum(pct)}%）` : '');
          }}}
      },
      scales:{
        x:{ stacked:true, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
        y:{ stacked:true, title:{display:true, text:'億円', color:'#64748b'}, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
        y1:{ position:'right', title:{display:true, text:'%', color:'#64748b'}, min:0, max:100, ticks:{color:'#a78bfa'}, grid:{drawOnChartArea:false} },
      }
    })
  });
}

// ── CF：営業／投資／財務CF を粒度別に表示。データ未収集時はメッセージ表示 ──
//  粗: フリーCF+財務CF ／ 中: 営業/投資/財務 ／ 細: 営業/設備投資/その他投資/財務
//  フリーCF は 営業CF+投資CF を実値から再計算（投資CF が無ければ null＝非表示）。
let cfState = { labels:null, recs:null };
let cfGran  = 'medium';

function renderCF(labels, recs){
  cfState = { labels, recs };
  drawCF();
}

function drawCF(){
  const { labels, recs } = cfState;
  if (!recs) return;
  if (charts.cf){ charts.cf.destroy(); charts.cf = null; }
  const canvas = document.getElementById('chart-cf');
  const box = canvas.parentElement;
  const note = document.getElementById('cf-note');
  let emptyEl = box.querySelector('.cf-empty');
  if (emptyEl) emptyEl.remove();

  // 営業/投資/財務のいずれかが非null ならデータあり
  const hasAny = recs.some(r => r.cf.operating_cf!=null || r.cf.investing_cf!=null || r.cf.financing_cf!=null);
  if (!hasAny){
    canvas.style.display = 'none';
    const d = document.createElement('div');
    d.className = 'cf-empty';
    d.style.cssText = 'display:flex;align-items:center;justify-content:center;height:100%;color:#64748b;font-size:13px;text-align:center;padding:20px';
    d.textContent = 'この企業のキャッシュフローデータは未収集です（再収集で取得できます）。';
    box.appendChild(d);
    note.textContent = '棒＝CF区分（億円）／折れ線＝フリーCF（営業CF＋投資CF）。';
    return;
  }
  canvas.style.display = '';

  const numN = v => { const x = toOku(v); return (x==null || isNaN(x)) ? null : x; };
  const op  = r => numN(r.cf.operating_cf);
  const inv = r => numN(r.cf.investing_cf);
  const fin = r => numN(r.cf.financing_cf);
  const free = r => { const o = op(r), i = inv(r); return (o!=null && i!=null) ? o + i : null; };
  // 設備投資（支出＝負で統一）とその他投資CF（合計＝投資CF）
  const capexOut = r => { const c = toOku(r.cf.capex); return (c==null || isNaN(c)) ? null : -Math.abs(c); };
  const otherInv = r => { const i = inv(r); if (i==null) return null; const c = capexOut(r); return (c==null) ? i : (i - c); };

  const bar = (label, color, fn) => ({ label, backgroundColor:color, borderRadius:3, data:recs.map(fn) });
  let datasets;
  if (cfGran === 'coarse'){
    datasets = [ bar('フリーCF（営業+投資）', '#34d399', free), bar('財務CF', '#60a5fa', fin) ];
  } else if (cfGran === 'fine'){
    datasets = [ bar('営業CF', '#34d399', op), bar('設備投資', '#f87171', capexOut),
                 bar('その他投資CF', '#fbbf24', otherInv), bar('財務CF', '#60a5fa', fin) ];
  } else {
    datasets = [ bar('営業CF', '#34d399', op), bar('投資CF', '#f87171', inv), bar('財務CF', '#60a5fa', fin) ];
  }
  if (cfGran !== 'coarse'){
    datasets.push({ label:'フリーCF', type:'line', data:recs.map(free), spanGaps:true,
      borderColor:'#a78bfa', backgroundColor:'#a78bfa', tension:.3, pointRadius:3 });
  }

  // データ品質の注記（投資CF・設備投資が未収集なら明示）
  const invMissing   = !recs.some(r => r.cf.investing_cf != null);
  const capexMissing = !recs.some(r => r.cf.capex != null);
  let warn = '';
  if (invMissing) warn = ' <b style="color:#f59e0b">※投資CFが未収集のため、フリーCFを表示できません（再収集が必要です）。</b>';
  else if (cfGran === 'fine' && capexMissing) warn = ' <b style="color:#f59e0b">※設備投資が未収集のため「その他投資CF」に投資CF全額を表示しています。</b>';
  note.innerHTML = '棒＝CF区分（億円）／折れ線＝フリーCF（営業CF＋投資CF）。粒度で内訳の細かさが変わります。' + warn;

  charts.cf = new Chart(canvas, {
    type:'bar',
    data:{ labels, datasets },
    options: baseOpts({ scales:{
      x:{ ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
      y:{ title:{display:true, text:'億円', color:'#64748b'}, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
    }})
  });
}

function renderPsh(labels, recs){
  charts.psh = new Chart(document.getElementById('chart-psh'), {
    type:'bar',
    data:{ labels, datasets:[
      {label:'EPS(円)', data:recs.map(r=>r.pl.eps), backgroundColor:'#34d399', borderRadius:3},
      {label:'DPS(円)', data:recs.map(r=>r.val.dps), backgroundColor:'#f59e0b', borderRadius:3},
      {label:'BPS(円)', type:'line', data:recs.map(r=>r.bs.bps), yAxisID:'y1',
       borderColor:'#60a5fa', backgroundColor:'#60a5fa', tension:.3, pointRadius:3},
    ]},
    options: baseOpts({ scales:{
      x:{ ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
      y:{ title:{display:true, text:'EPS・DPS（円）', color:'#64748b'}, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
      y1:{ position:'right', title:{display:true, text:'BPS（円）', color:'#64748b'}, ticks:{color:'#60a5fa'}, grid:{drawOnChartArea:false} },
    }})
  });
}

function renderDiv(labels, recs){
  charts.div = new Chart(document.getElementById('chart-div'), {
    type:'line',
    data:{ labels, datasets:[
      {label:'配当利回り(%)', data:recs.map(r=>r.val.div_yield), borderColor:'#f59e0b', backgroundColor:'#f59e0b', tension:.3, pointRadius:3},
      {label:'配当性向(%)', data:recs.map(r=>(r.val.dps!=null && r.pl.eps && r.pl.eps>0) ? r.val.dps/r.pl.eps*100 : null),
       borderColor:'#a78bfa', backgroundColor:'#a78bfa', tension:.3, pointRadius:3},
    ]},
    options: baseOpts()
  });
}

function renderValRatio(labels, recs){
  charts.valratio = new Chart(document.getElementById('chart-valratio'), {
    type:'line',
    data:{ labels, datasets:[
      {label:'PER(倍)', data:recs.map(r=>r.val.per), borderColor:'#60a5fa', backgroundColor:'#60a5fa', tension:.3, pointRadius:3},
      {label:'PBR(倍)', data:recs.map(r=>r.val.pbr), borderColor:'#34d399', backgroundColor:'#34d399', tension:.3, pointRadius:3},
    ]},
    options: baseOpts()
  });
}

function renderMcap(labels, recs){
  charts.mcap = new Chart(document.getElementById('chart-mcap'), {
    type:'bar',
    data:{ labels, datasets:[
      {label:'実績時価総額', data:recs.map(r=>mnToOku(r.val.market_cap)), backgroundColor:'#3b82f6', borderRadius:3},
      {label:'理論時価総額', data:recs.map(r=>mnToOku(r.predicted_market_cap)), backgroundColor:'#a78bfa', borderRadius:3},
      {label:'乖離率(%)', type:'line', data:recs.map(r=>r.gap_ratio), yAxisID:'y1',
       borderColor:'#f59e0b', backgroundColor:'#f59e0b', tension:.3, pointRadius:3},
    ]},
    options: baseOpts({ scales:{
      x:{ ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
      y:{ title:{display:true, text:'億円', color:'#64748b'}, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
      y1:{ position:'right', title:{display:true, text:'乖離率 %', color:'#64748b'}, ticks:{color:'#f59e0b'}, grid:{drawOnChartArea:false} },
    }})
  });
}

function renderPrice(hist){
  const box = document.getElementById('price-empty');
  if (charts.price){ charts.price.destroy(); delete charts.price; }
  if (!hist || hist.length === 0){ if (box) box.style.display = 'block'; return; }
  if (box) box.style.display = 'none';
  charts.price = new Chart(document.getElementById('chart-price'), {
    type:'line',
    data:{ labels:hist.map(h=>h.trade_date), datasets:[
      {label:'終値(円)', data:hist.map(h=>h.close), borderColor:'#38bdf8', backgroundColor:'rgba(56,189,248,.12)',
       fill:true, pointRadius:0, borderWidth:1.5, tension:.1},
    ]},
    options: baseOpts({ scales:{
      x:{ ticks:{color:'#94a3b8', maxTicksLimit:12, autoSkip:true, maxRotation:0}, grid:{color:'#1e2235'} },
      y:{ title:{display:true, text:'円', color:'#64748b'}, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
    }})
  });
}

function renderZscore(latest){
  const z = latest.zscore || {};
  const keys = ['z_revenue','z_op_margin','z_roe','z_equity_ratio','z_cf_ratio','z_eps','z_de_ratio','z_nc_ratio'];
  const vals = keys.map(k => (z[k]==null || isNaN(z[k])) ? 0 : z[k]);
  charts.zscore = new Chart(document.getElementById('chart-zscore'), {
    type:'radar',
    data:{ labels:['売上','営業利益率','ROE','自己資本比率','CF比率','EPS','D/Eレシオ','ネットキャッシュ'],
      datasets:[{ label:`業種内Zスコア (${latest.year})`, data:vals,
        borderColor:'#a78bfa', backgroundColor:'rgba(167,139,250,.2)', pointBackgroundColor:'#a78bfa', borderWidth:2 }] },
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{labels:{color:'#cbd5e1'}}, tooltip:{backgroundColor:'#0f1117', borderColor:'#2d3154', borderWidth:1} },
      scales:{ r:{ min:-2.5, max:2.5, ticks:{stepSize:1, color:'#64748b', backdropColor:'transparent'},
        grid:{color:'#2d3154'}, angleLines:{color:'#2d3154'}, pointLabels:{color:'#cbd5e1', font:{size:11}} } } }
  });
}

function renderNC(labels, recs){
  charts.nc = new Chart(document.getElementById('chart-nc'), {
    type:'bar',
    data:{ labels, datasets:[
      {label:'ネットキャッシュ(億円)', data:recs.map(r=>toOku(r.nc.net_cash)), backgroundColor:'#34d399', borderRadius:3},
      {label:'NC比率(倍)', type:'line', data:recs.map(r=>r.nc.nc_ratio), yAxisID:'y1',
       borderColor:'#f59e0b', backgroundColor:'#f59e0b', tension:.3, pointRadius:3},
    ]},
    options: baseOpts({ scales:{
      x:{ ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
      y:{ title:{display:true, text:'億円', color:'#64748b'}, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
      y1:{ position:'right', title:{display:true, text:'NC比率（倍）', color:'#64748b'}, ticks:{color:'#f59e0b'}, grid:{drawOnChartArea:false} },
    }})
  });
}

// ── 同業比較（遅延ロード） ───────────────────────────────────
async function loadPeers(){
  peersLoaded = true;
  const note = document.getElementById('peer-note');
  const wrap = document.getElementById('peer-table-wrap');
  if (!curCompany.industry){
    note.textContent = '業種データが無いため同業比較を表示できません。';
    wrap.innerHTML = '';
    return;
  }
  let d;
  try{
    d = await apiFetch('/api/companies?include_latest=true&limit=300&industry=' + encodeURIComponent(curCompany.industry));
  }catch(e){
    peersLoaded = false;
    note.textContent = '同業データの取得に失敗しました: ' + e.message;
    return;
  }
  if (!d) return;
  let peers = (d.items || []).filter(it => it.latest);   // 財務データのある企業のみ
  if (peers.length === 0){
    note.textContent = `業種「${curCompany.industry}」に財務データを持つ企業が見つかりません。`;
    wrap.innerHTML = '';
    return;
  }
  // 時価総額の降順でソート
  peers.sort((a, b) => (b.latest.val.market_cap || 0) - (a.latest.val.market_cap || 0));

  // 業種内順位を記録
  const rankMap = {};
  peers.forEach((p, i) => { rankMap[p.edinet_code] = i + 1; });

  // 上位15社。表示中の企業は必ず含める
  let top = peers.slice(0, 15);
  if (!top.some(p => p.edinet_code === curCompany.code)){
    const me = peers.find(p => p.edinet_code === curCompany.code);
    if (me){
      top.push(me);
    } else if (curCompany.latest){
      // APIの件数上限外の場合、保持データでフォールバック
      top.push({ edinet_code: curCompany.code, sec_code: curCompany.sec_code, name: curCompany.name, latest: curCompany.latest });
      rankMap[curCompany.code] = peers.length + 1; // 正確な順位不明
    }
  }

  const myRank = rankMap[curCompany.code];
  const rankText = myRank ? `（業種内 第${myRank}位 / ${peers.length}社）` : '';
  note.textContent = `業種「${curCompany.industry}」の上位${Math.min(15, peers.length)}社を比較${rankText}（時価総額順・最新年度）。色付きが表示中の企業。`;

  const rows = top.map((p, idx) => {
    const L = p.latest, isMe = p.edinet_code === curCompany.code;
    const rank = rankMap[p.edinet_code] || (idx + 1);
    return `<tr class="${isMe ? 'me' : ''}">
      <td class="num" style="color:#64748b">${rank}</td>
      <td>${esc(p.sec_code || '-')}</td>
      <td><a href="/company/${esc(p.edinet_code)}">${esc(p.name)}</a></td>
      <td class="num">${fmtNum(toOku(L.pl.revenue))}</td>
      <td class="num">${fmtPct(L.pl.op_margin)}</td>
      <td class="num">${fmtPct(L.val.roe)}</td>
      <td class="num">${fmtPct(L.bs.equity_ratio)}</td>
      <td class="num">${fmtX(L.val.per)}</td>
      <td class="num">${fmtX(L.val.pbr)}</td>
      <td class="num">${fmtNum(mnToOku(L.val.market_cap))}</td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `<table class="cmp">
    <thead><tr><th>順位</th><th>コード</th><th>企業名</th><th>売上(億)</th><th>営業益率</th><th>ROE</th><th>自己資本比率</th><th>PER</th><th>PBR</th><th>時価総額(億)</th></tr></thead>
    <tbody>${rows}</tbody></table>`;

  const names = top.map(p => p.name.length > 8 ? p.name.slice(0, 8) + '…' : p.name);
  const colors = top.map(p => p.edinet_code === curCompany.code ? '#a78bfa' : '#475569');
  peerBar('chart-peer-opm', names, top.map(p => p.latest.pl.op_margin), colors, '営業利益率(%)');
  peerBar('chart-peer-roe', names, top.map(p => p.latest.val.roe), colors, 'ROE(%)');
}

function peerBar(canvasId, names, data, colors, label){
  if (charts[canvasId]){ charts[canvasId].destroy(); }
  charts[canvasId] = new Chart(document.getElementById(canvasId), {
    type:'bar',
    data:{ labels:names, datasets:[{ label, data, backgroundColor:colors, borderRadius:3 }] },
    options: baseOpts({ indexAxis:'y', plugins:{ legend:{display:false} }, scales:{
      x:{ title:{display:true, text:'%', color:'#64748b'}, ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
      y:{ ticks:{color:'#94a3b8'}, grid:{color:'#1e2235'} },
    }})
  });
}

// ── 認証チェック・起動 ───────────────────────────────────────
async function init(){
  try{
    const r = await fetch(apiBase() + '/api/auth/status');
    const d = await r.json();
    if (d.auth_required && !_getCookie('csrf_token')){
      location.href = '/login?next=' + encodeURIComponent(location.pathname); return;
    }
  }catch(e){ /* API 未起動時はスキップ */ }

  const code = currentCode();
  if (code){ loadCompany(code); }
}
init();
