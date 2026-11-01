// next は同じ origin のパスだけ受け付ける。以前の前方一致の正規表現は、URL パーサがタブ・改行を
// 取り除くため `/%09/evil.example` が `//evil.example`（別 origin）に化けて通り抜けた。
// ブラウザ自身の URL パーサで解釈し、origin を比べる。/login 自身へ戻すと無限に再読込するので除く。
function _safeNext(raw){
  try {
    const u = new URL(raw, location.origin);
    if (u.origin !== location.origin || u.pathname === '/login') return '/';
    return u.pathname + u.search + u.hash;
  } catch(e) {
    return '/';
  }
}
const _next = _safeNext(new URLSearchParams(location.search).get('next') || '/');
function _getCookie(name){
  const m = document.cookie.match('(^|; )' + name + '=([^;]*)');
  return m ? decodeURIComponent(m[2]) : '';
}
if(_getCookie('csrf_token')) location.href = _next;

function showReset(){
  document.getElementById('login-panel').style.display = 'none';
  document.getElementById('reset-panel').style.display = 'block';
  document.getElementById('rk').focus();
}
function showLogin(){
  document.getElementById('reset-panel').style.display = 'none';
  document.getElementById('login-panel').style.display = 'block';
  document.getElementById('pw').focus();
}

async function login(){
  const pw  = document.getElementById('pw').value;
  const err = document.getElementById('err');
  const btn = document.getElementById('login-btn');
  err.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'ログイン中...';
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pw})
    });
    if(!r.ok){
      err.textContent = 'パスワードが違います';
      err.style.display = 'block';
      document.getElementById('pw').focus();
      return;
    }
    location.href = _next;
  } catch(e) {
    err.textContent = 'エラー: ' + e.message;
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'ログイン';
  }
}

async function resetPassword(){
  const rk  = document.getElementById('rk').value;
  const np  = document.getElementById('np').value;
  const err = document.getElementById('reset-err');
  const ok  = document.getElementById('reset-ok');
  const btn = document.getElementById('reset-btn');
  err.style.display = 'none';
  ok.style.display  = 'none';
  btn.disabled = true;
  btn.textContent = '更新中...';
  try {
    const r = await fetch('/api/auth/reset-password', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({recovery_key: rk, new_password: np})
    });
    const d = await r.json();
    if(!r.ok){
      err.textContent = d.detail || 'エラーが発生しました';
      err.style.display = 'block';
      return;
    }
    ok.style.display = 'block';
    document.getElementById('rk').value = '';
    document.getElementById('np').value = '';
    setTimeout(showLogin, 2000);
  } catch(e) {
    err.textContent = 'エラー: ' + e.message;
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'パスワードをリセット';
  }
}

// インラインハンドラを CSP 対応で addEventListener 化（script-src 'unsafe-inline' 除去のため）。
function _wireLogin(){
  const pw = document.getElementById('pw');
  if (pw) pw.addEventListener('keydown', e => { if (e.key === 'Enter') login(); });
  const loginBtn = document.getElementById('login-btn');
  if (loginBtn) loginBtn.addEventListener('click', () => login());
  const forgot = document.getElementById('forgot-link');
  if (forgot) forgot.addEventListener('click', () => showReset());
  const rk = document.getElementById('rk');
  if (rk) rk.addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('np').focus(); });
  const np = document.getElementById('np');
  if (np) np.addEventListener('keydown', e => { if (e.key === 'Enter') resetPassword(); });
  const resetBtn = document.getElementById('reset-btn');
  if (resetBtn) resetBtn.addEventListener('click', () => resetPassword());
  const backBtn = document.getElementById('back-btn');
  if (backBtn) backBtn.addEventListener('click', () => showLogin());
}
if (document.readyState !== 'loading') _wireLogin();
else document.addEventListener('DOMContentLoaded', _wireLogin);
