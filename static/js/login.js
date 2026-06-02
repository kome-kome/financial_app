const _nextRaw = new URLSearchParams(location.search).get('next') || '/';
const _next = /^\/[^/\\]/.test(_nextRaw) || _nextRaw === '/' ? _nextRaw : '/';
if(localStorage.getItem('auth_token')) location.href = _next;

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
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pw})
    });
    if(!r.ok){
      err.textContent = 'パスワードが違います';
      err.style.display = 'block';
      document.getElementById('pw').focus();
      return;
    }
    const d = await r.json();
    localStorage.setItem('auth_token', d.token);
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
