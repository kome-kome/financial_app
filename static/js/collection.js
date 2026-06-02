function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

let API = '';
let dbPage = 0, dbLimit = 50;
let screenResults = [];
let normData = {};
let searchTimer = null;

// ── 認証 ────────────────────────────────────────────────────────────
function _getCookie(name){
  const m = document.cookie.match('(^|; )' + name + '=([^;]*)');
  return m ? decodeURIComponent(m[2]) : '';
}
async function initAuth(){
  try {
    const r = await fetch('/api/auth/status');
    const d = await r.json();
    if(d.auth_required){
      if(!_getCookie('csrf_token')){
        location.href = '/login?next=' + encodeURIComponent(location.pathname);
        return;
      }
      document.getElementById('logout-btn').style.display = '';
    }
  } catch(e){ /* API 未起動時はスキップ */ }
}
async function logout(){
  try { await fetch('/api/auth/logout', {method:'POST', credentials:'same-origin'}); } catch(e){}
  location.href = '/login';
}

// ── API ────────────────────────────────────────────────────────────
function apiBase(){ return document.getElementById('api-base').value.trim().replace(/\/$/,'') }

async function apiFetch(path, opts={}){
  const heads = {'Content-Type':'application/json'};
  const _m = (opts.method || 'GET').toUpperCase();
  if(_m !== 'GET' && _m !== 'HEAD') heads['X-CSRF-Token'] = _getCookie('csrf_token');
  const r = await fetch(apiBase()+path, {credentials:'same-origin', ...opts, headers:{...heads, ...(opts.headers||{})}});
  if(r.status===401){ location.href='/login'; return; }
  if(!r.ok){
    if(r.status===502||r.status===503||r.status===504)
      throw new Error(`サーバー再起動中 (${r.status})。しばらく待ってから再試行してください`);
    throw new Error(await r.text());
  }
  return r.json();
}

async function checkApi(){
  try{
    const d = await apiFetch('/api/stats');
    document.getElementById('api-dot').style.background='#10b981';
    document.getElementById('s-api').textContent='接続OK';
    document.getElementById('s-api').style.color='#10b981';
    document.getElementById('s-companies').textContent=d.companies.toLocaleString();
    document.getElementById('s-records').textContent=d.records.toLocaleString();
    document.getElementById('s-year').textContent=d.latest_year||'-';
    log('API接続成功: '+d.companies+'社 / '+d.records+'レコード','success');
    loadIndustries();
    initWizardState();
  }catch(e){
    document.getElementById('api-dot').style.background='#ef4444';
    document.getElementById('s-api').textContent='接続失敗';
    document.getElementById('s-api').style.color='#ef4444';
    log('API接続失敗: '+e.message,'error');
  }
}

async function runNow(){
  await startIncremental();
}

// ── EDINETカバレッジ ────────────────────────────────────────────────
async function loadEdinetCoverage(){
  const el = document.getElementById('edinet-coverage-body');
  el.innerHTML = '<p class="text-sm" style="text-align:center;padding:10px">読み込み中...</p>';
  try{
    const d = await apiFetch('/api/collect/edinet-coverage');
    const pct = d.coverage_pct;
    const barColor = pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';
    let html = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px">
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">登録企業数</div>
          <div style="font-size:18px;font-weight:700;color:#a78bfa">${d.total_companies.toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">財務データあり</div>
          <div style="font-size:18px;font-weight:700;color:#10b981">${d.with_records.toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">財務データなし</div>
          <div style="font-size:18px;font-weight:700;color:#ef4444">${d.without_records.toLocaleString()}</div>
        </div>
      </div>
      <div style="margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;font-size:11px;color:#64748b;margin-bottom:3px">
          <span>収録率</span><span>${pct}%</span>
        </div>
        <div class="progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="progress-fill" style="width:${pct}%;background:${barColor}"></div></div>
      </div>`;
    if(d.year_coverage && d.year_coverage.length){
      html += '<div style="font-size:11px;color:#64748b;margin-bottom:4px">年度別収録社数（2019年以降）</div>';
      html += '<div style="overflow-x:auto"><table style="width:100%"><thead><tr><th>年度</th><th>収録社数</th></tr></thead><tbody>';
      d.year_coverage.forEach(r=>{
        html += `<tr><td>${Number(r.year)}年度</td><td>${Number(r.count).toLocaleString()}社</td></tr>`;
      });
      html += '</tbody></table></div>';
    }
    el.innerHTML = html;
  }catch(e){ el.innerHTML = `<p class="text-sm" style="color:#ef4444;text-align:center;padding:10px">取得失敗: ${esc(e.message)}</p>`; }
}

// ── 株価カバレッジ ──────────────────────────────────────────────────
async function loadMarketCoverage(){
  const el = document.getElementById('market-coverage-body');
  el.innerHTML = '<p class="text-sm" style="text-align:center;padding:10px">読み込み中...</p>';
  try{
    const d = await apiFetch('/api/collect/market-coverage');
    const pct = d.coverage_pct;
    const barColor = pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';
    el.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px">
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">証券コードあり</div>
          <div style="font-size:18px;font-weight:700;color:#a78bfa">${d.total_with_sec.toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">株価データあり</div>
          <div style="font-size:18px;font-weight:700;color:#10b981">${d.with_market_data.toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">株価データなし</div>
          <div style="font-size:18px;font-weight:700;color:#ef4444">${d.without_market_data.toLocaleString()}</div>
        </div>
      </div>
      <div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:11px;color:#64748b;margin-bottom:3px">
          <span>取得率</span><span>${pct}%</span>
        </div>
        <div class="progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="progress-fill" style="width:${pct}%;background:${barColor}"></div></div>
      </div>
      <div class="text-sm">最終更新: ${esc(d.latest_update || '未取得')}</div>`;
  }catch(e){ el.innerHTML = `<p class="text-sm" style="color:#ef4444;text-align:center;padding:10px">取得失敗: ${esc(e.message)}</p>`; }
}

// ── データ品質 ───────────────────────────────────────────────────────
async function loadQualityReport(){
  const el = document.getElementById('quality-body');
  el.innerHTML = '<p class="text-sm" style="text-align:center;padding:40px 0"><span class="spinner"></span> チェック中...</p>';
  try{
    const d = await apiFetch('/api/collect/data-quality');
    let html = `<div class="text-sm" style="color:#64748b;margin-bottom:12px">チェック日時: ${esc(d.checked_at)}</div>`;

    // NULL率
    html += `<div class="card" style="margin-bottom:12px">
      <div class="section-title">必須フィールドのNULL率</div>
      <div style="overflow-x:auto"><table><thead><tr><th>フィールド</th><th>NULLレコード数</th><th>NULL率</th></tr></thead><tbody>`;
    d.null_fields.forEach(r=>{
      const color = r.null_pct < 10 ? '#10b981' : r.null_pct < 30 ? '#f59e0b' : '#ef4444';
      html += `<tr><td>${esc(r.label)}</td><td>${Number(r.null_count).toLocaleString()}</td>
        <td style="color:${color};font-weight:600">${Number(r.null_pct)}%</td></tr>`;
    });
    html += '</tbody></table></div></div>';

    // 外れ値
    html += `<div class="card" style="margin-bottom:12px">
      <div class="section-title">外れ値チェック</div>`;
    if(d.outliers.length === 0){
      html += '<p class="text-sm" style="color:#10b981">外れ値は検出されませんでした ✓</p>';
    } else {
      html += '<div style="overflow-x:auto"><table><thead><tr><th>条件</th><th>件数</th></tr></thead><tbody>';
      d.outliers.forEach(r=>{
        html += `<tr><td>${esc(r.label)}</td><td style="color:#f59e0b;font-weight:600">${Number(r.count).toLocaleString()}</td></tr>`;
      });
      html += '</tbody></table></div>';
    }
    html += '</div>';

    // 年度サマリー
    const ys = d.year_summary;
    html += `<div class="card">
      <div class="section-title">収録状況サマリー</div>
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px">
        <div style="background:#0f1117;border-radius:6px;padding:10px">
          <div class="text-sm">収録企業数</div>
          <div style="font-size:18px;font-weight:700;color:#a78bfa">${Number(ys.total_companies).toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px">
          <div class="text-sm">3年以上データあり</div>
          <div style="font-size:18px;font-weight:700;color:#10b981">${Number(ys.three_or_more_years).toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px">
          <div class="text-sm">1年分のみ</div>
          <div style="font-size:18px;font-weight:700;color:#f59e0b">${Number(ys.single_year_only).toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px">
          <div class="text-sm">株価データなし</div>
          <div style="font-size:18px;font-weight:700;color:#ef4444">${Number(ys.no_market_data).toLocaleString()}</div>
        </div>
      </div>
    </div>`;

    // 会計基準別の NULL率・外れ値率（JGAAP / IFRS / US-GAAP の差を可視化）
    if(d.accounting_standard && d.accounting_standard.length){
      html += `<div class="card" style="margin-top:12px">
        <div class="section-title">会計基準別の品質サマリー</div>
        <p class="text-sm" style="color:#64748b;margin-bottom:8px">IFRS と JGAAP では純資産・営業利益・1株指標の定義に差があるため、両者を混在して回帰すると係数が歪む可能性があります。</p>`;
      d.accounting_standard.forEach(grp=>{
        html += `<div style="background:#0f1117;border-radius:6px;padding:10px;margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <span style="font-weight:600;color:#a78bfa">${esc(grp.standard)}</span>
            <span class="text-sm" style="color:#64748b">${Number(grp.total).toLocaleString()} 件（全体の ${grp.share_pct}%）</span>
          </div>
          <div style="overflow-x:auto"><table><thead><tr>
            <th>フィールド</th><th>NULL 率</th><th>外れ値 率</th>
          </tr></thead><tbody>`;
        grp.fields.forEach(f=>{
          const nullColor = f.null_pct < 10 ? '#10b981' : f.null_pct < 30 ? '#f59e0b' : '#ef4444';
          const hasOutlier = (f.outlier_pct !== undefined);
          const outColor = hasOutlier ? (f.outlier_pct < 1 ? '#10b981' : f.outlier_pct < 5 ? '#f59e0b' : '#ef4444') : '#64748b';
          const outCell = hasOutlier
            ? `<span style="color:${outColor}">${Number(f.outlier_pct).toFixed(2)}% (${Number(f.outlier_count).toLocaleString()})</span>`
            : '<span style="color:#64748b">—</span>';
          html += `<tr>
            <td>${esc(f.label)}</td>
            <td><span style="color:${nullColor}">${Number(f.null_pct)}% (${Number(f.null_count).toLocaleString()})</span></td>
            <td>${outCell}</td>
          </tr>`;
        });
        html += '</tbody></table></div></div>';
      });
      html += '</div>';
    }

    el.innerHTML = html;
  }catch(e){ el.innerHTML = `<p class="text-sm" style="color:#ef4444;text-align:center;padding:20px">チェック失敗: ${esc(e.message)}</p>`; }
}

// ── スマート収集 ─────────────────────────────────────────────────────
async function startSmartCollection(force=false){
  const years = parseInt(document.getElementById('smart-years-back').value);
  try{
    await apiFetch('/api/collect/smart-start',{
      method:'POST', body: JSON.stringify({years_back: years, force: force})
    });
    log(force ? 'スマート収集ジョブを強制再開しました' : 'スマート収集ジョブを開始しました','success');
    document.getElementById('smart-progress').classList.remove('hidden');
    const badge = document.getElementById('smart-mode-badge');
    badge.style.display   = 'block';
    badge.textContent     = '判定中...';
    badge.style.color     = '#94a3b8';
    document.getElementById('btn-smart').disabled        = true;
    document.getElementById('btn-smart-stop').style.display = '';
    _startSmartSSE();
  }catch(e){
    if(!force && /既に実行中/.test(e.message)){
      if(confirm('実行中の収集ジョブがあります。\n強制的に上書きして再開しますか？\n（裏で動いているジョブのデータは不完全になる可能性があります）')){
        return startSmartCollection(true);
      }
    }
    log('スマート収集開始失敗: '+e.message,'error');
  }
}

// ── スタックジョブの強制リセット ───────────────────────────────────
async function resetStuckJobs(){
  if(!confirm('実行中状態（running）の収集ジョブを強制的にerror扱いに戻します。\n本当に裏で動いているジョブがある場合、データが不完全になる可能性があります。\n続行しますか？')) return;
  try{
    const d = await apiFetch('/api/collect/reset-stuck',{method:'POST'});
    log(d.message,'success');
    checkJobStatus();
  }catch(e){
    log('強制リセット失敗: '+e.message,'error');
  }
}

let _smartSSE = null;
function _startSmartSSE(){
  if(_smartSSE){ _smartSSE.close(); }
  _smartSSE = new EventSource(apiBase()+'/api/collect/stream');
  _smartSSE.onmessage = function(e){
    const d = JSON.parse(e.data);
    if(d.total > 0){
      const pct = Math.round(d.progress / d.total * 100);
      document.getElementById('smart-progress-fill').style.width = pct+'%';
    }
    if(d.new_logs && d.new_logs.length > 0){
      document.getElementById('smart-progress-label').textContent = d.new_logs[d.new_logs.length-1];
      const modeLog = d.new_logs.find(m => m.includes('[スマート判定]'));
      if(modeLog){
        const badge = document.getElementById('smart-mode-badge');
        if(modeLog.includes('差分収集モード')){
          badge.textContent = '差分モード（全社収集済み）';
          badge.style.color = '#10b981';
        } else if(modeLog.includes('初回チャンク')){
          badge.textContent = 'チャンク 1 / 25（初回収集）';
          badge.style.color = '#a78bfa';
        } else {
          const m = modeLog.match(/チャンク(\d+)（先着(\d+)社/);
          if(m){ badge.textContent = `チャンク ${m[1]} / 25（先着${m[2]}社）`; badge.style.color = '#f59e0b'; }
        }
      }
      d.new_logs.forEach(msg => log(msg,'info'));
    }
    if(!d.running && d.progress > 0){ _smartSSE.close(); _onSmartComplete(); }
  };
  _smartSSE.onerror = function(){ _smartSSE.close(); _onSmartComplete(); };
}

function _onSmartComplete(){
  document.getElementById('smart-progress').classList.add('hidden');
  document.getElementById('btn-smart').disabled        = false;
  document.getElementById('btn-smart-stop').style.display = 'none';
  log('スマート収集完了','success');
  checkApi(); initWizardState();
}

// ── 収集 ───────────────────────────────────────────────────────────
async function startIncremental(){
  try{
    await apiFetch('/api/collect/start',{
      method:'POST',
      body: JSON.stringify({years_back:1, skip_existing:true})
    });
    log('差分収集ジョブを開始しました','success');
    document.getElementById('collect-progress').classList.remove('hidden');
    document.getElementById('btn-incremental').disabled = true;
    document.getElementById('btn-stop').style.display = '';
    startSSEProgress();
  }catch(e){ log('収集開始失敗: '+e.message,'error') }
}

async function startFullCollection(){
  const years = parseInt(document.getElementById('years-back').value);
  if(!confirm(`全上場企業（約3,900社）の収集を開始します。\n遡及年数: ${years}年\n完了まで数時間かかります。よろしいですか？`)) return;
  try{
    await apiFetch('/api/collect/start',{
      method:'POST',
      body: JSON.stringify({years_back:years, max_companies:null, skip_existing:false})
    });
    log('全件収集ジョブを開始しました','success');
    document.getElementById('collect-progress').classList.remove('hidden');
    document.getElementById('btn-collect').disabled = true;
    document.getElementById('btn-stop').style.display = '';
    startSSEProgress();
  }catch(e){ log('収集開始失敗: '+e.message,'error') }
}

async function stopCollection(){
  try{
    await apiFetch('/api/collect/stop',{method:'POST'});
    log('停止リクエストを送信しました。処理中の書類完了後に停止します。','info');
    document.getElementById('btn-stop').disabled = true;
    document.getElementById('btn-stop').textContent = '停止中...';
  }catch(e){ log('停止失敗: '+e.message,'error') }
}

function _onCollectionComplete(){
  document.getElementById('collect-progress').classList.add('hidden');
  document.getElementById('btn-incremental').disabled = false;
  document.getElementById('btn-collect').disabled = false;
  document.getElementById('btn-stop').style.display = 'none';
  document.getElementById('btn-stop').disabled = false;
  document.getElementById('btn-stop').innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/></svg> 停止';
  document.getElementById('btn-smart').disabled        = false;
  document.getElementById('btn-smart-stop').style.display = 'none';
  log('収集ジョブ完了','success');
  checkApi();
  initWizardState();
}

let _collectSSE = null;
let _pollCount = 0;
function startSSEProgress(){
  if(_collectSSE){ _collectSSE.close(); }
  _collectSSE = new EventSource(apiBase()+'/api/collect/stream');
  _collectSSE.onmessage = function(e){
    const d = JSON.parse(e.data);
    if(d.total > 0){
      const pct = Math.round(d.progress / d.total * 100);
      document.getElementById('progress-fill').style.width = pct+'%';
    }
    if(d.new_logs && d.new_logs.length > 0){
      const last = d.new_logs[d.new_logs.length - 1];
      document.getElementById('progress-label').textContent = last;
      d.new_logs.forEach(msg => log(msg,'info'));
    }
    if(!d.running && d.progress > 0){
      _collectSSE.close();
      _onCollectionComplete();
    }
  };
  _collectSSE.onerror = function(){
    _collectSSE.close();
    _pollCount = 0;
    pollJobStatus(); // SSE失敗時はポーリングにフォールバック
  };
}

async function checkJobStatus(){
  try{
    const d = await apiFetch('/api/collect/status');
    d.recent_jobs.forEach(j=>{
      const lvl = j.status==='done'||j.status==='resolved' ? 'success' : j.status==='error' ? 'error' : 'info';
      log(`ジョブ#${j.id}: ${j.status} | ${j.companies}社 | ${j.records}件`, lvl);
    });
  }catch(e){ log('ステータス取得失敗: '+e.message,'error') }
}

async function pollJobStatus(){
  if(++_pollCount > 120){
    log('収集ジョブのポーリングがタイムアウトしました（10分経過）','error');
    document.getElementById('collect-progress').classList.add('hidden');
    _pollCount = 0;
    return;
  }
  try{
    const d = await apiFetch('/api/collect/status');
    if(d.running){
      document.getElementById('progress-label').textContent='収集中...（バックグラウンド実行）';
      document.getElementById('progress-fill').style.width='50%';
      setTimeout(pollJobStatus, 5000);
    } else {
      _pollCount = 0;
      _onCollectionComplete();
    }
  }catch(e){ setTimeout(pollJobStatus, 10000) }
}

async function refreshSingle(){
  const code = document.getElementById('refresh-code').value.trim();
  if(!code){ log('EDINETコードを入力してください', 'error'); return; }
  try{
    await apiFetch(`/api/collect/refresh/${code}`,{method:'POST'});
    log(`${code} の再取得を開始しました`,'info');
  }catch(e){ log('再取得失敗: '+e.message,'error') }
}

// ── DBブラウザ ─────────────────────────────────────────────────────
function debounceSearch(){ clearTimeout(searchTimer); searchTimer=setTimeout(loadDB,400) }

async function loadDB(){
  const q = document.getElementById('db-search').value;
  const ind = document.getElementById('db-industry').value;
  try{
    const d = await apiFetch(`/api/companies?q=${encodeURIComponent(q)}&industry=${encodeURIComponent(ind)}&limit=${dbLimit}&offset=${dbPage*dbLimit}&include_latest=true`);
    const tbody = document.getElementById('db-tbody');
    tbody.innerHTML = '';
    for(const c of d.items){
      const latest = c.latest || null;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="tag tag-blue">${esc(c.sec_code)||'-'}</span></td>
        <td>${c.edinet_code ? `<a href="/company/${esc(c.edinet_code)}" class="co-link" style="font-weight:500">${esc(c.name)}</a>` : esc(c.name)}</td>
        <td><span class="tag tag-amber" style="font-size:10px">${esc(c.industry)||'-'}</span></td>
        <td>${latest?.year!=null?Number(latest.year):'-'}</td>
        <td>${latest ? fmt0(latest.pl.revenue/1e6) : '-'}</td>
        <td class="${latest?.pl.op_margin>0?'text-green':''}">${latest?.pl.op_margin!=null?Number(latest.pl.op_margin)+'%':'-'}</td>
        <td>${latest?.val.roe!=null?Number(latest.val.roe)+'%':'-'}</td>
        <td>${latest?.val.pbr!=null?Number(latest.val.pbr):'-'}</td>
        <td>${latest?.bs.equity_ratio!=null?Number(latest.bs.equity_ratio)+'%':'-'}</td>
        <td>${latest ? fmt0((latest.val.market_cap||0)/1e6) : '-'}</td>
        <td><button class="btn btn-secondary btn-sm" data-click="showDetail" data-arg="${esc(c.edinet_code)}" data-arg2="${esc(c.name)}">詳細</button></td>
      `;
      tbody.appendChild(tr);
    }
    document.getElementById('db-count').textContent = `${d.total}社中 ${d.items.length}社表示`;
    updatePager(d.total, dbPage, dbLimit);
  }catch(e){ log('DB取得失敗: '+e.message,'error') }
}

function updatePager(total, page, limit) {
  const pager = document.getElementById('db-pager');
  pager.innerHTML = '';
  const totalPages = Math.ceil(total / limit);
  if (totalPages <= 1) return;
  const prev = document.createElement('button');
  prev.className = 'btn btn-secondary btn-sm';
  prev.textContent = '← 前へ';
  prev.disabled = page === 0;
  prev.onclick = () => { dbPage--; loadDB(); };
  const info = document.createElement('span');
  info.className = 'text-sm';
  info.textContent = `${page + 1} / ${totalPages}ページ`;
  info.style.cssText = 'align-self:center;color:#94a3b8';
  const next = document.createElement('button');
  next.className = 'btn btn-secondary btn-sm';
  next.textContent = '次へ →';
  next.disabled = (page + 1) >= totalPages;
  next.onclick = () => { dbPage++; loadDB(); };
  pager.append(prev, info, next);
}

async function updateIndustryData(){
  const btn = document.getElementById('btn-update-industry');
  btn.disabled = true; btn.textContent = '更新中...';
  try{
    const d = await apiFetch('/api/collect/industry', {method:'POST'});
    log(`業種更新完了: 企業 ${d.updated_companies}件, 財務レコード ${d.updated_records}件`, 'success');
    await loadIndustries();
    await loadDB();
  }catch(e){
    log('業種更新失敗: '+e.message, 'error');
  }finally{
    btn.disabled = false; btn.textContent = '業種を更新';
  }
}

async function loadIndustries(){
  try{
    const d = await apiFetch('/api/companies?limit=9999');
    const inds = [...new Set(d.items.map(c=>c.industry).filter(Boolean))].sort();
    ['db-industry','sc-industry'].forEach(id=>{
      const sel = document.getElementById(id);
      sel.innerHTML='<option value="">全業種</option>';
      inds.forEach(i=>{ const o=document.createElement('option'); o.value=i; o.textContent=i; sel.appendChild(o); });
    });
    // 正規化ビュー企業リスト
    const sel2 = document.getElementById('norm-company');
    sel2.innerHTML='<option value="">企業を選択</option>';
    d.items.forEach(c=>{
      const o=document.createElement('option'); o.value=c.edinet_code;
      o.textContent=`${c.sec_code||''} ${c.name}`; sel2.appendChild(o);
    });
  }catch(e){}
}

async function showDetail(code, name){
  try{
    const [fin, priceHistory] = await Promise.all([
      apiFetch(`/api/financials/${code}`),
      apiFetch(`/api/stock/history/${code}?days=30`).catch(()=>null),
    ]);
    const r = fin.records[fin.records.length-1];
    let html = `
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">
        ${statCard('売上高', fmt0(r.pl.revenue/1e6)+'億')}
        ${statCard('営業利益率', r.pl.op_margin+'%')}
        ${statCard('ROE', r.val.roe+'%')}
        ${statCard('総資産', fmt0(r.bs.total_assets/1e6)+'億')}
        ${statCard('自己資本比率', r.bs.equity_ratio+'%')}
        ${statCard('時価総額', fmt0((r.val.market_cap||0)/1e6)+'億')}
      </div>
      <div style="font-size:12px;color:#94a3b8;line-height:2">
        <b style="color:#10b981">PL:</b> 純利益 ${fmt0((r.pl.net_income||0)/1e6)}億 / EPS ${r.pl.eps||'-'}円 / 純利益率 ${r.pl.net_margin||'-'}%<br>
        <b style="color:#38bdf8">BS:</b> 純資産 ${fmt0((r.bs.total_equity||0)/1e6)}億 / D/E ${r.derived?.de_ratio||'-'}<br>
        <b style="color:#f59e0b">CF:</b> 営業CF ${fmt0((r.cf.operating_cf||0)/1e6)}億 / フリーCF ${fmt0((r.cf.free_cf||0)/1e6)}億<br>
        <b style="color:#a78bfa">Val:</b> PER ${r.val.per||'-'}倍 / PBR ${r.val.pbr||'-'}倍 / 配当 ${r.val.div_yield||'-'}%
      </div>`;
    if(priceHistory && priceHistory.length > 0){
      html += `<div style="margin-top:14px">
        <div style="font-size:12px;font-weight:600;color:#38bdf8;margin-bottom:6px">株価履歴（直近${priceHistory.length}日）</div>
        <div style="overflow-x:auto;max-height:200px;overflow-y:auto">
          <table style="font-size:11px"><thead><tr><th>日付</th><th>始値</th><th>高値</th><th>安値</th><th>終値</th><th>出来高</th></tr></thead><tbody>`;
      [...priceHistory].reverse().forEach(p=>{
        html += `<tr>
          <td>${esc(p.trade_date)}</td>
          <td>${p.open!=null?Number(p.open).toLocaleString():'-'}</td>
          <td style="color:#10b981">${p.high!=null?Number(p.high).toLocaleString():'-'}</td>
          <td style="color:#ef4444">${p.low!=null?Number(p.low).toLocaleString():'-'}</td>
          <td style="font-weight:600">${p.close!=null?Number(p.close).toLocaleString():'-'}</td>
          <td style="color:#64748b">${p.volume!=null?Number(p.volume).toLocaleString():'-'}</td>
        </tr>`;
      });
      html += '</tbody></table></div></div>';
    }
    document.getElementById('modal-title').textContent = name;
    document.getElementById('modal-body').innerHTML = html;
    document.getElementById('modal-detail').classList.remove('hidden');
  }catch(e){ log('詳細取得失敗: '+e.message,'error') }
}
function statCard(t,v){ return `<div style="background:#0f1117;border-radius:6px;padding:10px"><div style="font-size:10px;color:#64748b">${t}</div><div style="font-size:16px;font-weight:600">${v}</div></div>` }
function closeModal(){ document.getElementById('modal-detail').classList.add('hidden') }

// ── 株価履歴収集 ────────────────────────────────────────────────────
async function loadHistoryCoverage(){
  const el = document.getElementById('history-coverage-body');
  el.innerHTML = '<p class="text-sm" style="text-align:center;padding:10px">読み込み中...</p>';
  try{
    const d = await apiFetch('/api/collect/history/coverage');
    el.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">収録企業数</div>
          <div style="font-size:18px;font-weight:700;color:#a78bfa">${Number(d.companies).toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">総レコード数</div>
          <div style="font-size:18px;font-weight:700;color:#38bdf8">${Number(d.records).toLocaleString()}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">最古データ日</div>
          <div style="font-size:14px;font-weight:600;color:#94a3b8">${esc(d.oldest_date||'未収集')}</div>
        </div>
        <div style="background:#0f1117;border-radius:6px;padding:10px;text-align:center">
          <div class="text-sm">最新データ日</div>
          <div style="font-size:14px;font-weight:600;color:#10b981">${esc(d.newest_date||'未収集')}</div>
        </div>
      </div>`;
  }catch(e){ el.innerHTML = `<p class="text-sm" style="color:#ef4444;text-align:center;padding:10px">取得失敗: ${esc(e.message)}</p>`; }
}

let _historySSE = null;
async function startHistoryCollection(force = false){
  const years = parseInt(document.getElementById('history-years').value);
  const maxCo = numOrNull('history-max-co');
  const skipExisting = document.getElementById('history-skip-existing').checked;
  const backfill = skipExisting && document.getElementById('history-backfill').checked;
  try{
    await apiFetch('/api/collect/history/start',{
      method:'POST',
      body: JSON.stringify({years_back:years, max_companies:maxCo, skip_existing:skipExisting, backfill, force})
    });
    const mode = skipExisting ? (backfill ? '差分＋backfill' : '差分収集') : '全件収集';
    _histLog(`株価履歴収集を開始しました（${years}年分・${mode}）`,'success');
    document.getElementById('history-progress').classList.remove('hidden');
    document.getElementById('btn-history-start').disabled = true;
    document.getElementById('btn-history-force').style.display = 'none';
    document.getElementById('btn-history-stop').style.display = '';
    _startHistorySSE();
  }catch(e){
    const msg = e.message;
    if(msg.includes('既に実行中')){
      _histLog('既に実行中です。「強制再開」ボタンで上書き起動できます','error');
      document.getElementById('btn-history-force').style.display = '';
    } else {
      _histLog('収集開始失敗: '+msg,'error');
    }
  }
}

function _startHistorySSE(){
  if(_historySSE){ _historySSE.close(); }
  _historySSE = new EventSource(apiBase()+'/api/collect/history/stream');
  _historySSE.onmessage = function(e){
    const d = JSON.parse(e.data);
    if(d.total > 0){
      const pct = Math.round(d.progress / d.total * 100);
      document.getElementById('history-progress-fill').style.width = pct+'%';
    }
    if(d.new_logs && d.new_logs.length > 0){
      const last = d.new_logs[d.new_logs.length - 1];
      document.getElementById('history-progress-label').textContent = last;
      d.new_logs.forEach(msg => _histLog(msg,'info'));
    }
    if(!d.running && d.progress > 0){
      _historySSE.close();
      _onHistoryComplete();
    }
  };
  _historySSE.onerror = function(){ _historySSE.close(); _onHistoryComplete(); };
}

function _onHistoryComplete(){
  document.getElementById('history-progress').classList.add('hidden');
  document.getElementById('btn-history-start').disabled = false;
  document.getElementById('btn-history-stop').style.display = 'none';
  document.getElementById('btn-history-stop').disabled = false;
  _histLog('株価履歴収集完了','success');
  loadHistoryCoverage();
}

async function stopHistoryCollection(){
  try{
    await apiFetch('/api/collect/history/stop',{method:'POST'});
    _histLog('停止リクエストを送信しました','info');
    document.getElementById('btn-history-stop').disabled = true;
    document.getElementById('btn-history-stop').textContent = '停止中...';
  }catch(e){ _histLog('停止失敗: '+e.message,'error') }
}

function _histLog(msg,type='info'){
  const box = document.getElementById('history-log');
  const ts = new Date().toTimeString().slice(0,8);
  const el = document.createElement('div'); el.className='log-entry '+type;
  el.textContent = `[${ts}] ${msg}`; box.appendChild(el); box.scrollTop=box.scrollHeight;
}

// ── J-Quants 収集 ───────────────────────────────────────────────────
let _jqSSE = null;
async function startJQuantsCollection(force = false){
  const days = parseInt(document.getElementById('jq-days').value);
  try{
    await apiFetch('/api/collect/jquants/start',{
      method:'POST',
      body: JSON.stringify({days_back: days, force})
    });
    _jqLog(`J-Quants収集を開始しました（${days}日分）`,'success');
    document.getElementById('jq-progress').classList.remove('hidden');
    document.getElementById('btn-jq-start').disabled = true;
    document.getElementById('btn-jq-force').style.display = 'none';
    document.getElementById('btn-jq-stop').style.display = '';
    _startJQuantsSSE();
  }catch(e){
    const msg = e.message;
    if(msg.includes('既に実行中')){
      _jqLog('既に実行中です。「強制再開」で上書き起動できます','error');
      document.getElementById('btn-jq-force').style.display = '';
    } else if(msg.includes('設定エラー') || msg.includes('JQUANTS')){
      _jqLog('APIキーが未設定です。.env に JQUANTS_API_KEY を設定してください（J-Quants ダッシュボード → API Keys）','error');
    } else {
      _jqLog('収集開始失敗: '+msg,'error');
    }
  }
}

function _startJQuantsSSE(){
  if(_jqSSE){ _jqSSE.close(); }
  _jqSSE = new EventSource(apiBase()+'/api/collect/jquants/stream');
  _jqSSE.onmessage = function(e){
    const d = JSON.parse(e.data);
    if(d.total > 0){
      const pct = Math.round(d.progress / d.total * 100);
      document.getElementById('jq-progress-fill').style.width = pct+'%';
    }
    if(d.new_logs && d.new_logs.length > 0){
      const last = d.new_logs[d.new_logs.length-1];
      document.getElementById('jq-progress-label').textContent = last;
      d.new_logs.forEach(msg => _jqLog(msg,'info'));
    }
    if(!d.running && d.progress > 0){
      _jqSSE.close();
      _onJQuantsComplete();
    }
  };
  _jqSSE.onerror = function(){ _jqSSE.close(); _onJQuantsComplete(); };
}

function _onJQuantsComplete(){
  document.getElementById('jq-progress').classList.add('hidden');
  document.getElementById('btn-jq-start').disabled = false;
  document.getElementById('btn-jq-stop').style.display = 'none';
  document.getElementById('btn-jq-stop').disabled = false;
  _jqLog('J-Quants収集完了','success');
  loadHistoryCoverage();
}

async function stopJQuantsCollection(){
  try{
    await apiFetch('/api/collect/jquants/stop',{method:'POST'});
    _jqLog('停止リクエストを送信しました','info');
    document.getElementById('btn-jq-stop').disabled = true;
    document.getElementById('btn-jq-stop').textContent = '停止中...';
  }catch(e){ _jqLog('停止失敗: '+e.message,'error'); }
}

function _jqLog(msg,type='info'){
  const box = document.getElementById('jq-log');
  const ts = new Date().toTimeString().slice(0,8);
  const el = document.createElement('div'); el.className='log-entry '+type;
  el.textContent = `[${ts}] ${msg}`; box.appendChild(el); box.scrollTop=box.scrollHeight;
}

// ── マクロデータ収集（為替・金利・指数・コモディティ）───────────────
let _macroSSE = null;

async function loadMacroCoverage(){
  try{
    const d = await apiFetch('/api/macro/series');
    if(!d) return;
    const tbody = document.getElementById('macro-coverage-tbody');
    const catLabel = {fx:'FX', rate:'金利', equity:'株価指数', commodity:'コモディティ'};
    const catColor = {fx:'tag-blue', rate:'tag-amber', equity:'tag-purple', commodity:'tag-amber'};
    tbody.innerHTML = d.series.map(s => `
      <tr>
        <td><strong>${esc(s.name)}</strong><br><span style="font-size:10px;color:#64748b">${esc(s.code)} / ${esc(s.ticker)}</span></td>
        <td><span class="tag ${catColor[s.category]||'tag-gray'}" style="font-size:10px">${catLabel[s.category]||s.category}</span></td>
        <td>${s.rows.toLocaleString()}</td>
        <td>${s.oldest||'<span style="color:#475569">—</span>'}</td>
        <td>${s.newest||'<span style="color:#475569">—</span>'}</td>
      </tr>
    `).join('');
  }catch(e){ /* ignore */ }
}

async function startMacroCollection(){
  const years = parseInt(document.getElementById('macro-years').value);
  try{
    await apiFetch('/api/collect/macro/start', {
      method:'POST',
      body: JSON.stringify({years_back: years})
    });
    _macroLog(`マクロ収集を開始しました（${years}年分）`,'success');
    document.getElementById('macro-progress').classList.remove('hidden');
    document.getElementById('btn-macro-start').disabled = true;
    document.getElementById('btn-macro-stop').style.display = '';
    _startMacroSSE();
  }catch(e){
    _macroLog('収集開始失敗: '+e.message,'error');
  }
}

function _startMacroSSE(){
  if(_macroSSE){ _macroSSE.close(); }
  _macroSSE = new EventSource(apiBase()+'/api/collect/macro/stream');
  _macroSSE.onmessage = function(e){
    const d = JSON.parse(e.data);
    if(d.total > 0){
      const pct = Math.round(d.progress / d.total * 100);
      document.getElementById('macro-progress-fill').style.width = pct+'%';
    }
    if(d.new_logs && d.new_logs.length > 0){
      const last = d.new_logs[d.new_logs.length-1];
      document.getElementById('macro-progress-label').textContent = last;
      d.new_logs.forEach(msg => _macroLog(msg,'info'));
    }
    if(!d.running && d.progress > 0){
      _macroSSE.close();
      _onMacroComplete();
    }
  };
  _macroSSE.onerror = function(){ _macroSSE.close(); _onMacroComplete(); };
}

function _onMacroComplete(){
  document.getElementById('macro-progress').classList.add('hidden');
  document.getElementById('btn-macro-start').disabled = false;
  document.getElementById('btn-macro-stop').style.display = 'none';
  document.getElementById('btn-macro-stop').disabled = false;
  _macroLog('マクロ収集完了','success');
  loadMacroCoverage();
}

async function stopMacroCollection(){
  try{
    await apiFetch('/api/collect/macro/stop',{method:'POST'});
    _macroLog('停止リクエストを送信しました','info');
    document.getElementById('btn-macro-stop').disabled = true;
  }catch(e){ _macroLog('停止失敗: '+e.message,'error'); }
}

function _macroLog(msg,type='info'){
  const box = document.getElementById('macro-log');
  const ts = new Date().toTimeString().slice(0,8);
  const el = document.createElement('div'); el.className='log-entry '+type;
  el.textContent = `[${ts}] ${msg}`; box.appendChild(el); box.scrollTop=box.scrollHeight;
}

// ── XBRL 再解析 ─────────────────────────────────────────────────────
let _reparseSSE = null;

async function startReparse(){
  const year   = document.getElementById('reparse-year').value.trim();
  const edinet = document.getElementById('reparse-edinet').value.trim();
  const body   = {};
  if(year)   body.year        = parseInt(year);
  if(edinet) body.edinet_code = edinet;
  try{
    await apiFetch('/api/collect/reparse/start', {method:'POST', body: JSON.stringify(body)});
    _reparseLog('再解析ジョブを開始しました', 'success');
    document.getElementById('btn-reparse-start').disabled = true;
    document.getElementById('btn-reparse-stop').style.display = '';
    _startReparseSSE();
  }catch(e){
    _reparseLog('開始失敗: '+e.message, 'error');
  }
}

function _startReparseSSE(){
  if(_reparseSSE){ _reparseSSE.close(); }
  _reparseSSE = new EventSource(apiBase()+'/api/collect/reparse/stream');
  _reparseSSE.onmessage = function(e){
    const d = JSON.parse(e.data);
    if(d.new_logs && d.new_logs.length > 0){
      d.new_logs.forEach(msg => _reparseLog(msg, 'info'));
    }
    if(!d.running && d.progress > 0){
      _reparseSSE.close();
      _onReparseComplete();
    }
  };
  _reparseSSE.onerror = function(){ _reparseSSE.close(); _onReparseComplete(); };
}

function _onReparseComplete(){
  document.getElementById('btn-reparse-start').disabled = false;
  document.getElementById('btn-reparse-stop').style.display = 'none';
  _reparseLog('再解析完了', 'success');
}

async function stopReparse(){
  try{
    await apiFetch('/api/collect/reparse/cancel', {method:'POST'});
    _reparseLog('停止リクエストを送信しました', 'info');
    document.getElementById('btn-reparse-stop').disabled = true;
  }catch(e){ _reparseLog('停止失敗: '+e.message, 'error'); }
}

function _reparseLog(msg, type='info'){
  const box = document.getElementById('reparse-log');
  const ts  = new Date().toTimeString().slice(0,8);
  const el  = document.createElement('div'); el.className = 'log-entry '+type;
  el.textContent = `[${ts}] ${msg}`; box.appendChild(el); box.scrollTop = box.scrollHeight;
}

// ── スクリーニング ──────────────────────────────────────────────────
async function runScreen(){
  const body = {
    min_rev_growth:  numOrNull('sc-rev-growth'),
    min_op_margin:   numOrNull('sc-op-margin'),
    min_net_margin:  numOrNull('sc-net-margin'),
    min_roe:         numOrNull('sc-roe'),
    min_equity_ratio:numOrNull('sc-equity-ratio'),
    max_de_ratio:    numOrNull('sc-de'),
    max_per:         numOrNull('sc-per'),
    max_pbr:         numOrNull('sc-pbr'),
    min_div_yield:   numOrNull('sc-div'),
    min_cf_ratio:    numOrNull('sc-cf'),
    industry: document.getElementById('sc-industry').value||null,
    market:   document.getElementById('sc-market').value||null,
    limit: 500
  };
  try{
    const d = await apiFetch('/api/screen',{method:'POST',body:JSON.stringify(body)});
    screenResults = d.results;
    document.getElementById('screen-count').textContent = d.count+'社';
    renderScreenResults();
    log(`スクリーニング完了: ${d.count}社ヒット`,'success');
  }catch(e){ log('スクリーニング失敗: '+e.message,'error') }
}

function renderScreenResults(){
  document.getElementById('screen-tbody').innerHTML = screenResults.map(r=>{
    const score = calcScore(r);
    return `<tr>
      <td><span class="tag tag-blue">${esc(r.sec_code||r.edinet_code)}</span></td>
      <td>${r.edinet_code ? `<a href="/company/${esc(r.edinet_code)}" class="co-link" style="font-weight:500">${esc(r.company_name)}</a>` : esc(r.company_name)}</td>
      <td><span class="tag tag-amber" style="font-size:10px">${esc(r.industry)||'-'}</span></td>
      <td>${r.val?.per!=null?Number(r.val.per):'-'}</td>
      <td>${r.val?.pbr!=null?Number(r.val.pbr):'-'}</td>
      <td class="${(r.val?.roe||0)>10?'text-green':'text-amber'}">${r.val?.roe!=null?Number(r.val.roe):'-'}</td>
      <td class="${(r.pl?.op_margin||0)>10?'text-green':'text-amber'}">${r.pl?.op_margin!=null?Number(r.pl.op_margin):'-'}</td>
      <td>${r.bs?.equity_ratio!=null?Number(r.bs.equity_ratio):'-'}</td>
      <td>${r.val?.div_yield!=null?Number(r.val.div_yield):'-'}</td>
      <td>
        <div class="score-bar">
          <div class="score-track"><div class="score-fill" style="width:${score}%;background:${score>70?'#10b981':score>40?'#f59e0b':'#ef4444'}"></div></div>
          <span style="font-size:11px">${score}</span>
        </div>
      </td>
    </tr>`;
  }).join('');
}

let _scSortCol = null, _scSortAsc = true;
function sortScreen(col){
  if(_scSortCol === col){ _scSortAsc = !_scSortAsc; }
  else { _scSortCol = col; _scSortAsc = (col === 'per' || col === 'pbr'); }
  const get = r => {
    if(col === 'score')        return calcScore(r);
    if(col === 'per')          return r.val?.per  ?? ((_scSortAsc) ? Infinity : -Infinity);
    if(col === 'pbr')          return r.val?.pbr  ?? ((_scSortAsc) ? Infinity : -Infinity);
    if(col === 'roe')          return r.val?.roe  ?? -Infinity;
    if(col === 'op_margin')    return r.pl?.op_margin     ?? -Infinity;
    if(col === 'equity_ratio') return r.bs?.equity_ratio  ?? -Infinity;
    if(col === 'div_yield')    return r.val?.div_yield    ?? -Infinity;
    return 0;
  };
  screenResults.sort((a,b) => _scSortAsc ? get(a)-get(b) : get(b)-get(a));
  renderScreenResults();
}

function calcScore(r){
  let s=0;
  if((r.val?.roe||0)>10) s+=20;
  if((r.pl?.op_margin||0)>10) s+=20;
  if((r.bs?.equity_ratio||0)>50) s+=15;
  if((r.val?.per||99)<20) s+=20;
  if((r.val?.pbr||99)<2) s+=15;
  if((r.cf?.cf_ratio||0)>8) s+=10;
  return s;
}


// ── 正規化ビュー ────────────────────────────────────────────────────
async function loadNormYears(){
  const code = document.getElementById('norm-company').value;
  if(!code){ document.getElementById('norm-content').innerHTML='<p class="text-sm" style="text-align:center;padding:40px 0">企業を選択してください</p>'; return; }
  try{
    const fin = await apiFetch(`/api/financials/${code}`);
    normData[code] = fin.records;
    const sel = document.getElementById('norm-year');
    sel.innerHTML = fin.records.map(r=>`<option value="${Number(r.year)}">${Number(r.year)}年度</option>`).reverse().join('');
    renderNorm();
  }catch(e){ log('データ取得失敗: '+e.message,'error') }
}

function renderNorm(){
  const code = document.getElementById('norm-company').value;
  const year = parseInt(document.getElementById('norm-year').value);
  const recs = normData[code];
  if(!recs) return;
  const r = recs.find(x=>x.year===year) || recs[recs.length-1];
  if(!r) return;
  document.getElementById('norm-content').innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px">
      <div>
        <div style="color:#a78bfa;font-weight:500;margin-bottom:8px;font-size:12px">▌ BS 再分類</div>
        ${bsRow('流動資産', r.bs.current_assets)} ${bsRow('固定資産', r.bs.noncurrent_assets)}
        ${bsRow('現金・預金', r.bs.cash)} ${bsRow('総資産', r.bs.total_assets, true)}
        ${bsRow('流動負債', r.bs.current_liabilities)} ${bsRow('固定負債', r.bs.noncurrent_liabilities)}
        ${bsRow('純資産', r.bs.total_equity)} ${bsRow('自己資本比率', null,false,r.bs.equity_ratio+'%')}
        ${bsRow('BPS', null,false,(r.bs.bps||'-')+'円')}
      </div>
      <div>
        <div style="color:#10b981;font-weight:500;margin-bottom:8px;font-size:12px">▌ PL 再分類</div>
        ${bsRow('売上高', r.pl.revenue)} ${bsRow('売上総利益', r.pl.gross_profit)}
        ${bsRow('営業利益', r.pl.operating_profit)} ${bsRow('経常利益', r.pl.ordinary_profit)}
        ${bsRow('当期純利益', r.pl.net_income)}
        ${bsRow('営業利益率', null,false,(r.pl.op_margin||'-')+'%')}
        ${bsRow('純利益率', null,false,(r.pl.net_margin||'-')+'%')}
        ${bsRow('売上成長率', null,false,(r.pl.rev_growth||'-')+'%')}
        ${bsRow('EPS', null,false,(r.pl.eps||'-')+'円')}
      </div>
      <div>
        <div style="color:#f59e0b;font-weight:500;margin-bottom:8px;font-size:12px">▌ CF 再分類</div>
        ${bsRow('営業CF', r.cf.operating_cf)} ${bsRow('投資CF', r.cf.investing_cf)}
        ${bsRow('財務CF', r.cf.financing_cf)} ${bsRow('フリーCF', r.cf.free_cf)}
        ${bsRow('設備投資', r.cf.capex)} ${bsRow('CF/売上比', null,false,(r.cf.cf_ratio||'-')+'%')}
        <div style="margin-top:12px"></div>
        <div style="color:#60a5fa;font-weight:500;margin-bottom:8px;font-size:12px">▌ バリュエーション</div>
        ${bsRow('時価総額', r.val.market_cap)}
        ${bsRow('PER', null,false,(r.val.per||'-')+'倍')}
        ${bsRow('PBR', null,false,(r.val.pbr||'-')+'倍')}
        ${bsRow('ROE', null,false,(r.val.roe||'-')+'%')}
        ${bsRow('配当利回', null,false,(r.val.div_yield||'-')+'%')}
      </div>
    </div>
    <div style="margin-top:12px;font-size:11px;color:#64748b">Zスコア: 売上 ${r.zscore?.z_revenue?.toFixed(2)||'-'} / 営業利益率 ${r.zscore?.z_op_margin?.toFixed(2)||'-'} / ROE ${r.zscore?.z_roe?.toFixed(2)||'-'}</div>
  `;
}

function bsRow(label, val, bold=false, rawStr=null){
  const v = rawStr !== null ? rawStr : (val==null?'-': ((val<0?'▲':'')+fmt0(Math.abs(val)/1e6)+'億'));
  const col = rawStr===null && val!=null && val<0 ? 'color:#ef4444' : '';
  return `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #1e2235;font-size:12px">
    <span style="color:#94a3b8">${label}</span>
    <span style="font-weight:${bold?600:400};${col}">${v}</span></div>`;
}

// ── CSV出力 ─────────────────────────────────────────────────────────
async function exportCSV(){
  window.open(apiBase()+'/api/export/csv','_blank');
}
function exportScreenCSV(){
  if(!screenResults.length){ log('先にスクリーニングを実行してください', 'error'); return; }
  const h='証券コード,企業名,業種,PER,PBR,ROE%,営業利益率%,自己資本比率%,配当利回%\n';
  const b=screenResults.map(r=>[r.sec_code,r.company_name,r.industry,r.val?.per,r.val?.pbr,r.val?.roe,r.pl?.op_margin,r.bs?.equity_ratio,r.val?.div_yield].join(',')).join('\n');
  dl('\uFEFF'+h+b,'screening.csv');
}
function dl(content, name){
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([content],{type:'text/csv'})); a.download=name; a.click();
}

// ── ユーティリティ ────────────────────────────────────────────────
function numOrNull(id){ const v=document.getElementById(id).value; return v===''?null:parseFloat(v); }
function fmt0(n){ return n==null?'-':Math.round(n).toLocaleString(); }
const _TABS = ['collect','stockmarket','dataview','screen'];
function showTab(t){
  _TABS.forEach(x=>document.getElementById('tab-'+x).classList.toggle('hidden',x!==t));
  document.querySelectorAll('.nav-btn[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===t));
  if(t==='dataview') loadDB();
  if(t==='stockmarket') loadMacroCoverage();
}

// ── 市場データ更新 ────────────────────────────────────────────────────
let marketSSE = null;

async function startMarketUpdate(force = false) {
  const maxV = document.getElementById('mkt-max').value;
  const max_companies = maxV ? parseInt(maxV) : null;
  try {
    await apiFetch('/api/collect/market-data', {
      method: 'POST',
      body: JSON.stringify({ max_companies, force })
    });
    mktLog('市場データ更新ジョブを開始しました', 'success');
    document.getElementById('market-progress').classList.remove('hidden');
    document.getElementById('btn-market').disabled = true;
    document.getElementById('btn-market-force').style.display = 'none';
    document.getElementById('btn-market-stop').style.display = '';
    startMarketSSE();
  } catch(e) {
    const msg = e.message;
    if (msg.includes('既に実行中')) {
      mktLog('既に実行中です。「強制再開」ボタンで上書き起動できます', 'error');
      document.getElementById('btn-market-force').style.display = '';
    } else {
      mktLog('開始失敗: ' + msg, 'error');
    }
  }
}

async function stopMarketUpdate() {
  try {
    await apiFetch('/api/collect/market-stop', { method: 'POST' });
    mktLog('停止リクエストを送信しました', 'info');
    document.getElementById('btn-market-stop').disabled = true;
    document.getElementById('btn-market-stop').textContent = '停止中...';
  } catch(e) { mktLog('停止失敗: ' + e.message, 'error'); }
}

function startMarketSSE() {
  if (marketSSE) { marketSSE.close(); }
  marketSSE = new EventSource(apiBase() + '/api/collect/market-stream');
  marketSSE.onmessage = function(e) {
    const d = JSON.parse(e.data);
    if (d.total > 0) {
      const pct = Math.round(d.progress / d.total * 100);
      document.getElementById('market-progress-fill').style.width = pct + '%';
      document.getElementById('market-progress-label').textContent = `更新中... ${d.progress}/${d.total}社 (${pct}%)`;
    }
    (d.new_logs || []).forEach(msg => mktLog(msg, 'info'));
    if (!d.running && d.progress > 0) {
      marketSSE.close();
      document.getElementById('market-progress').classList.add('hidden');
      document.getElementById('btn-market').disabled = false;
      const stopBtn = document.getElementById('btn-market-stop');
      stopBtn.style.display = 'none';
      stopBtn.disabled = false;
      stopBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/></svg> 停止';
      mktLog('市場データ更新完了', 'success');
    }
  };
  marketSSE.onerror = function() {
    marketSSE.close();
    document.getElementById('btn-market').disabled = false;
    document.getElementById('btn-market-stop').style.display = 'none';
    checkMarketStatus();
  };
}

async function checkMarketStatus() {
  try {
    const d = await apiFetch('/api/collect/market-data/status');
    mktLog(`ステータス: ${d.running ? '実行中' : '完了'} | ${d.progress}/${d.total}社`, 'info');
    if (d.running) {
      document.getElementById('market-progress').classList.remove('hidden');
      startMarketSSE();
    }
  } catch(e) { mktLog('ステータス取得失敗: ' + e.message, 'error'); }
}

function clearMarketLog() { document.getElementById('market-log-box').innerHTML = ''; }

function mktLog(msg, type='info') {
  const box = document.getElementById('market-log-box');
  const ts = new Date().toTimeString().slice(0,8);
  const el = document.createElement('div');
  el.className = 'log-entry ' + type;
  el.textContent = `[${ts}] ${msg}`;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

// ── ウィザード状態管理 ──────────────────────────────────────────────────
async function initWizardState() {
  try {
    const d = await apiFetch('/api/stats');
    const noFin = d.records === 0;
    ['btn-market', 'btn-history-start', 'btn-jq-start'].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.disabled = noFin;
      el.title = noFin ? '先に財務データを収集してください' : '';
    });
    document.getElementById('stockmarket-prereq-banner').classList.toggle('hidden', !noFin);
  } catch(e) {}
}
function log(msg,type='info'){
  const box=document.getElementById('log-box');
  const ts=new Date().toTimeString().slice(0,8);
  const el=document.createElement('div'); el.className='log-entry '+type;
  el.textContent=`[${ts}] ${msg}`; box.appendChild(el); box.scrollTop=box.scrollHeight;
}
function clearLog(){ document.getElementById('log-box').innerHTML=''; }

// ── 軽量モード初期化 ────────────────────────────────────────────────────
async function initLightMode(){
  try{
    const r = await fetch('/api/system/info');
    const d = await r.json();
    if(!d.render_light_mode) return;
    document.getElementById('light-mode-banner').style.display = '';
    // 全件収集ボタン
    const btnCollect = document.getElementById('btn-collect');
    if(btnCollect){ btnCollect.disabled = true; btnCollect.title = 'Render環境ではローカルPCから実行してください'; btnCollect.style.opacity = '0.4'; }
    // 株価履歴収集ボタン
    const btnHist = document.getElementById('btn-history-start');
    if(btnHist){ btnHist.disabled = true; btnHist.title = 'Render環境ではローカルPCから実行してください'; btnHist.style.opacity = '0.4'; }
    const btnHistF = document.getElementById('btn-history-force');
    if(btnHistF){ btnHistF.disabled = true; btnHistF.style.opacity = '0.4'; }
    // J-Quants 収集ボタン
    const btnJq = document.getElementById('btn-jq-start');
    if(btnJq){ btnJq.disabled = true; btnJq.title = 'Render環境ではローカルPCから実行してください'; btnJq.style.opacity = '0.4'; }
    const btnJqF = document.getElementById('btn-jq-force');
    if(btnJqF){ btnJqF.disabled = true; btnJqF.style.opacity = '0.4'; }
  }catch(e){ /* 取得失敗は無視 */ }
}

// 初期化
initAuth();
initLightMode();
showTab('collect');
checkApi();

// 画面離脱時にタイマー・SSE・debounce タイマーをまとめてクリーンアップ
// （SPA 的なタブ切替はしていないので beforeunload のみで十分）
window.addEventListener('beforeunload', () => {
  if (searchTimer) clearTimeout(searchTimer);
  for (const sse of [_smartSSE, _collectSSE, _historySSE, _jqSSE, marketSSE]) {
    if (sse) { try { sse.close(); } catch(_) {} }
  }
});

// data 属性ハンドラ用ヘルパ（this=対象要素）
function clearById(){ const el=document.getElementById(this.dataset.target); if(el) el.innerHTML=''; }
function toggleDisplay(){ const el=document.getElementById(this.dataset.target); if(el) el.style.display = this.checked ? '' : 'none'; }


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
