function fmt(v){
  if(v===null||v===undefined) return '<span class="null-cell">NULL</span>';
  if(typeof v==='number'){
    if(Number.isInteger(v)) return v.toLocaleString();
    return v.toLocaleString(undefined,{maximumFractionDigits:4});
  }
  return esc(v);
}
function apiBase(){ return ''; }


// ── 状態 ─────────────────────────────────────────────────────────────
const state = {
  selectedTable: null,
  limit:  50,
  offset: 0,
  sort:   null,
  order:  'desc',
  filterCol: null,
  filterVal: null,
  total: 0,
};

// ── テーブル一覧 ─────────────────────────────────────────────────────
async function loadTables(){
  try{
    const d = await apiFetch('/api/db/tables');
    if(!d) return;
    const grid = document.getElementById('tables-grid');
    grid.innerHTML = '';
    d.tables.forEach(t => {
      const card = document.createElement('div');
      card.className = 'tbl-card';
      card.dataset.name = t.name;
      card.innerHTML = `
        <div class="tbl-card-name">${esc(t.name)}</div>
        <div class="tbl-card-rows">${t.row_count.toLocaleString()}</div>
        <div class="tbl-card-meta">
          <span>${t.column_count} カラム</span>
          <span>${t.last_updated ? t.last_updated.substring(5,16) : '—'}</span>
        </div>
      `;
      card.onclick = () => selectTable(t.name);
      grid.appendChild(card);
    });
  }catch(e){
    document.getElementById('tables-grid').innerHTML = `<div class="error">読み込み失敗: ${esc(e.message)}</div>`;
  }
}

// ── リレーション図 ───────────────────────────────────────────────────
async function loadRelations(){
  try{
    const d = await apiFetch('/api/db/relations');
    if(!d) return;
    const box = document.getElementById('relations-box');

    // companies を中央、左に financial_records、右に株価2本立て（daily/weekly）、下に collection_logs
    const tablesByName = Object.fromEntries(d.tables.map(t => [t.name, t]));

    const layout = `
      <div class="er-canvas">
        ${renderEntity(tablesByName.financial_records)}
        ${renderEntity(tablesByName.companies, true)}
        ${renderEntity(tablesByName.stock_price_daily)}
        ${renderEntity(tablesByName.stock_price_weekly)}
      </div>
      <div style="margin-top:24px">
        ${renderEntity(tablesByName.collection_logs, false, true)}
      </div>
      <div class="er-relations-list">
        ${d.relations.map(r => `
          <div class="er-relation-row">
            <strong style="color:#86efac">${esc(r.from_table)}.${esc(r.from_column)}</strong>
            → <strong style="color:#fcd34d">${esc(r.to_table)}.${esc(r.to_column)}</strong>
            <span style="color:#64748b;margin-left:8px">[${esc(r.label)}]</span>
          </div>
        `).join('')}
        <div style="color:#64748b;font-size:11px;margin-top:6px;padding-left:10px">
          ※ collection_logs は他テーブルと直接リレーションを持たない独立テーブル
        </div>
      </div>
    `;
    box.innerHTML = layout;
  }catch(e){
    document.getElementById('relations-box').innerHTML = `<div class="error">読み込み失敗: ${esc(e.message)}</div>`;
  }
}

function renderEntity(t, highlight, full){
  if(!t) return '';
  const shownCols = full ? t.columns : t.columns.slice(0, 8);
  const more = t.columns.length - shownCols.length;
  const pkSet = new Set(t.pk);
  return `
    <div class="er-entity" style="${highlight?'border-color:#a78bfa;background:#1e1b3a':''}">
      <div class="er-entity-name">${esc(t.name)}</div>
      ${shownCols.map(c => `
        <div class="er-entity-col ${pkSet.has(c)?'pk':(c==='edinet_code'?'fk':'')}">
          ${pkSet.has(c)?'🔑 ':''}${c==='edinet_code'&&!pkSet.has(c)?'🔗 ':''}${esc(c)}
        </div>
      `).join('')}
      ${more > 0 ? `<div class="er-entity-col" style="color:#475569">... 他 ${more} カラム</div>` : ''}
    </div>
  `;
}

// ── テーブル選択 ─────────────────────────────────────────────────────
function selectTable(name){
  state.selectedTable = name;
  state.offset = 0;
  state.sort = null;
  state.filterCol = null;
  state.filterVal = null;
  document.querySelectorAll('.tbl-card').forEach(el => {
    el.classList.toggle('active', el.dataset.name === name);
  });
  document.getElementById('detail-section').style.display = 'block';
  document.getElementById('detail-title').textContent = name;
  document.getElementById('filter-val').value = '';
  loadSchema(name).then(() => loadPreview());
  loadStats(name);
}

// ── スキーマ ────────────────────────────────────────────────────────
async function loadSchema(name){
  const el = document.getElementById('schema-content');
  el.innerHTML = '<div class="loading">読み込み中...</div>';
  try{
    const d = await apiFetch(`/api/db/schema/${name}`);
    if(!d) return;
    // フィルタカラム候補を更新（PK・FK・主要キーのみに絞る）
    const fc = document.getElementById('filter-col');
    fc.innerHTML = '<option value="">（なし）</option>';
    d.columns.forEach(c => {
      if(c.pk || c.fk || ['edinet_code','sec_code','year','status','job_type','trade_date','industry','market'].includes(c.name)){
        fc.innerHTML += `<option value="${esc(c.name)}">${esc(c.name)}</option>`;
      }
    });

    el.innerHTML = `
      <div style="margin-bottom:10px;font-size:12px;color:#64748b">
        全 ${d.row_count.toLocaleString()} 行 / ${d.columns.length} カラム
      </div>
      <div style="overflow-x:auto">
      <table class="schema-table">
        <thead>
          <tr>
            <th>カラム名</th>
            <th>型</th>
            <th>制約</th>
            <th style="text-align:right">NULL率</th>
          </tr>
        </thead>
        <tbody>
          ${d.columns.map(c => `
            <tr>
              <td class="col-name">${esc(c.name)}</td>
              <td class="col-type">${esc(c.type)}</td>
              <td>
                ${c.pk ? '<span class="tag tag-pk">PK</span> ' : ''}
                ${c.fk ? `<span class="tag tag-fk">FK→${esc(c.fk)}</span> ` : ''}
                ${c.nullable ? '<span class="tag tag-null">NULLABLE</span>' : '<span class="tag tag-pk">NOT NULL</span>'}
              </td>
              <td style="text-align:right">
                ${c.null_rate === null ? '—' : `
                  <span class="null-bar"><span class="null-bar-fill" style="width:${c.null_rate}%"></span></span>
                  ${c.null_rate.toFixed(1)}%
                `}
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
      </div>
    `;
  }catch(e){
    el.innerHTML = `<div class="error">読み込み失敗: ${esc(e.message)}</div>`;
  }
}

// ── プレビュー ──────────────────────────────────────────────────────
async function loadPreview(){
  const wrap = document.getElementById('preview-wrap');
  if(!state.selectedTable){ wrap.innerHTML = '<div class="loading">テーブルを選択してください</div>'; return; }
  wrap.innerHTML = '<div class="loading">読み込み中...</div>';
  try{
    const qs = new URLSearchParams({
      limit:  state.limit,
      offset: state.offset,
      order:  state.order,
    });
    if(state.sort) qs.set('sort', state.sort);
    if(state.filterCol && state.filterVal){
      qs.set('filter_col', state.filterCol);
      qs.set('filter_val', state.filterVal);
    }
    const d = await apiFetch(`/api/db/preview/${state.selectedTable}?${qs}`);
    if(!d) return;
    state.total = d.total;
    document.getElementById('page-info').textContent =
      `${state.offset+1}-${Math.min(state.offset+state.limit, d.total)} / ${d.total.toLocaleString()} 行`;
    document.getElementById('prev-page').disabled = state.offset === 0;
    document.getElementById('next-page').disabled = state.offset + state.limit >= d.total;

    if(d.rows.length === 0){
      wrap.innerHTML = '<div class="empty-msg">該当データなし</div>';
      return;
    }
    wrap.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>${d.columns.map(c => {
            const cls = state.sort === c ? (state.order==='desc'?'sort-desc':'sort-asc') : '';
            return `<th class="${cls}" data-col="${esc(c)}">${esc(c)}</th>`;
          }).join('')}</tr>
        </thead>
        <tbody>
          ${d.rows.map(r => `
            <tr>${d.columns.map(c => {
              const v = r[c];
              const isNull = v===null || v===undefined;
              return `<td class="${isNull?'null-cell':''}" title="${esc(v)}">${fmt(v)}</td>`;
            }).join('')}</tr>
          `).join('')}
        </tbody>
      </table>
    `;
    // ソート操作
    wrap.querySelectorAll('th').forEach(th => {
      th.onclick = () => {
        const col = th.dataset.col;
        if(state.sort === col){
          state.order = state.order === 'desc' ? 'asc' : 'desc';
        }else{
          state.sort  = col;
          state.order = 'desc';
        }
        state.offset = 0;
        loadPreview();
      };
    });
  }catch(e){
    wrap.innerHTML = `<div class="error">読み込み失敗: ${esc(e.message)}</div>`;
  }
}

// ── 統計サマリー ─────────────────────────────────────────────────────
async function loadStats(name){
  const el = document.getElementById('stats-content');
  el.innerHTML = '<div class="loading">読み込み中...</div>';
  try{
    const d = await apiFetch(`/api/db/stats/${name}`);
    if(!d) return;
    el.innerHTML = `
      <div style="margin-bottom:10px;font-size:12px;color:#64748b">
        全 ${d.row_count.toLocaleString()} 行を対象に集計
      </div>
      <div style="overflow-x:auto">
      <table class="stats-table">
        <thead>
          <tr>
            <th style="text-align:left">カラム</th>
            <th style="text-align:left">型</th>
            <th>件数 / 異なり値</th>
            <th>MIN</th>
            <th>AVG</th>
            <th>P50</th>
            <th>P99</th>
            <th>MAX</th>
          </tr>
        </thead>
        <tbody>
          ${d.stats.map(s => {
            if(!s.numeric){
              return `
                <tr>
                  <td class="col-name-cell">${esc(s.name)}</td>
                  <td style="text-align:left;color:#93c5fd">${esc(s.type)}</td>
                  <td>${s.distinct!==undefined && s.distinct!==null ? s.distinct.toLocaleString()+' uniq' : '—'}</td>
                  <td class="text-type" colspan="5">（非数値カラム）</td>
                </tr>
              `;
            }
            return `
              <tr>
                <td class="col-name-cell">${esc(s.name)}</td>
                <td style="text-align:left;color:#93c5fd">${esc(s.type)}</td>
                <td>${s.count!==undefined ? s.count.toLocaleString() : '—'}</td>
                <td>${s.min!==undefined && s.min!==null ? fmt(s.min) : '—'}</td>
                <td>${s.avg!==undefined && s.avg!==null ? fmt(s.avg) : '—'}</td>
                <td>${s.p50!==undefined && s.p50!==null ? fmt(s.p50) : '—'}</td>
                <td>${s.p99!==undefined && s.p99!==null ? fmt(s.p99) : '—'}</td>
                <td>${s.max!==undefined && s.max!==null ? fmt(s.max) : '—'}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
      </div>
    `;
  }catch(e){
    el.innerHTML = `<div class="error">読み込み失敗: ${esc(e.message)}</div>`;
  }
}

// ── 企業ドリルダウン ─────────────────────────────────────────────────
async function runDrilldown(){
  const code = document.getElementById('drill-code').value.trim();
  const res  = document.getElementById('drill-result');
  if(!/^E\d{5,6}$/.test(code)){
    res.innerHTML = '<div class="error">EDINETコードは E + 5桁数字（例: E02167）の形式で入力してください</div>';
    return;
  }
  res.innerHTML = '<div class="loading">読み込み中...</div>';
  try{
    const d = await apiFetch(`/api/db/company/${code}`);
    if(!d) return;
    const co = d.company;
    res.innerHTML = `
      <div style="margin-bottom:14px">
        <a href="/company/${esc(code)}" class="co-link" style="font-weight:600">→ この企業の詳細ページ（グラフ表示）を開く</a>
      </div>
      <div class="drilldown-section">
        <h3>📋 companies</h3>
        <div class="kv-grid">
          ${['edinet_code','sec_code','name','industry','market','fiscal_month','accounting_standard','updated_at']
            .map(k => `<div class="kv-row"><span class="kv-key">${esc(k)}</span><span class="kv-val">${fmt(co[k])}</span></div>`)
            .join('')}
        </div>
      </div>
      <div class="drilldown-section">
        <h3>📊 financial_records (${d.financial_records.length} 件)</h3>
        ${d.financial_records.length === 0 ? '<div class="empty-msg">財務レコードなし</div>' : `
          <div class="table-wrap" style="max-height:300px">
            <table class="data-table">
              <thead><tr>
                <th>year</th><th>period_end</th>
                <th>pl_revenue</th><th>pl_operating_profit</th><th>pl_net_income</th>
                <th>bs_total_assets</th><th>bs_total_equity</th>
                <th>market_cap</th><th>per</th><th>pbr</th><th>roe</th>
              </tr></thead>
              <tbody>
                ${d.financial_records.map(r => `
                  <tr>
                    <td>${fmt(r.year)}</td><td>${fmt(r.period_end)}</td>
                    <td>${fmt(r.pl_revenue)}</td><td>${fmt(r.pl_operating_profit)}</td><td>${fmt(r.pl_net_income)}</td>
                    <td>${fmt(r.bs_total_assets)}</td><td>${fmt(r.bs_total_equity)}</td>
                    <td>${fmt(r.market_cap)}</td><td>${fmt(r.per)}</td><td>${fmt(r.pbr)}</td><td>${fmt(r.roe)}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        `}
      </div>
      <div class="drilldown-section">
        <h3>📈 株価（全期間=週次 / 直近プレビュー=日次） (${d.stock_price_history.total.toLocaleString()} 週 / ${d.stock_price_history.oldest_date ?? '—'} 〜 ${d.stock_price_history.newest_date ?? '—'})</h3>
        ${d.stock_price_history.recent.length === 0 ? '<div class="empty-msg">株価データなし</div>' : `
          <div class="table-wrap" style="max-height:240px">
            <table class="data-table">
              <thead><tr><th>trade_date</th><th>close</th><th>volume</th></tr></thead>
              <tbody>
                ${d.stock_price_history.recent.map(r => `
                  <tr>
                    <td>${fmt(r.trade_date)}</td>
                    <td>${fmt(r.close)}</td><td>${fmt(r.volume)}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        `}
      </div>
    `;
  }catch(e){
    res.innerHTML = `<div class="error">${esc(e.message) || '取得失敗'}</div>`;
  }
}

// ── イベントバインド ────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  };
});

document.getElementById('prev-page').onclick = () => {
  state.offset = Math.max(0, state.offset - state.limit);
  loadPreview();
};
document.getElementById('next-page').onclick = () => {
  state.offset += state.limit;
  loadPreview();
};
document.getElementById('apply-filter').onclick = () => {
  state.filterCol = document.getElementById('filter-col').value || null;
  state.filterVal = document.getElementById('filter-val').value.trim() || null;
  state.offset = 0;
  loadPreview();
};
document.getElementById('clear-filter').onclick = () => {
  document.getElementById('filter-col').value = '';
  document.getElementById('filter-val').value = '';
  state.filterCol = null;
  state.filterVal = null;
  state.offset = 0;
  loadPreview();
};
document.getElementById('download-csv').onclick = () => {
  if(!state.selectedTable) return;
  const qs = new URLSearchParams({limit: 10000});
  if(state.filterCol && state.filterVal){
    qs.set('filter_col', state.filterCol);
    qs.set('filter_val', state.filterVal);
  }
  // Cookie 認証のため Authorization 不要。GET なので CSRF も不要（cookie 自動送信）
  fetch(`/api/db/export/${state.selectedTable}?${qs}`, {
    credentials: 'same-origin'
  }).then(r => r.blob()).then(blob => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `${state.selectedTable}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  });
};
document.getElementById('drill-btn').onclick = runDrilldown;
document.getElementById('drill-code').addEventListener('keypress', e => {
  if(e.key === 'Enter') runDrilldown();
});

(async () => {
  await initAuth();
  loadTables();
  loadRelations();
})();
