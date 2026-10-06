"""セキュリティレスポンスヘッダの検証。

CSP の追加ハードニングディレクティブ・Permissions-Policy・HSTS（HTTPS 時のみ）を確認する。
script-src からは 'unsafe-inline' を除去（インラインJS/ハンドラを外部化済み）、style-src は据え置きであることを検証する。

DB 不要の静的ページ（"/" = dashboard.html, FileResponse）を対象にする。
TestClient を with 無しで使うことで lifespan（init_db / Postgres 依存）を回避する。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_RATELIMIT_ENABLED", "false")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


def test_csp_contains_hardening_directives():
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("Content-Security-Policy", "")
    for directive in (
        "default-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-src 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in csp, f"CSP に {directive} が含まれていない: {csp}"


def test_permissions_policy_present():
    r = client.get("/")
    pp = r.headers.get("Permissions-Policy", "")
    assert "geolocation=()" in pp and "camera=()" in pp, pp


def test_hsts_only_on_https():
    # 平文 HTTP（既定）では HSTS を付与しない（誤って HTTPS 固定化しないため）
    r_http = client.get("/")
    assert "Strict-Transport-Security" not in r_http.headers
    # X-Forwarded-Proto: https（Render のリバースプロキシ想定）では付与する
    r_https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert r_https.headers.get("Strict-Transport-Security") == "max-age=31536000"


def _directive(csp, name):
    """CSP 文字列から指定ディレクティブの値部分を取り出す。"""
    for part in csp.split(";"):
        part = part.strip()
        if part == name or part.startswith(name + " "):
            return part
    return ""


def test_script_src_has_no_unsafe_inline():
    # インラインJS/ハンドラを外部化したため script-src から 'unsafe-inline' を除去
    r = client.get("/")
    script = _directive(r.headers.get("Content-Security-Policy", ""), "script-src")
    assert "'unsafe-inline'" not in script, script
    assert "https://cdn.jsdelivr.net" in script  # Chart.js CDN は維持


def test_style_src_keeps_unsafe_inline():
    # style-src の 'unsafe-inline' は据え置き（インライン <style>/style= 属性が残るため）
    r = client.get("/")
    style = _directive(r.headers.get("Content-Security-Policy", ""), "style-src")
    assert "'unsafe-inline'" in style, style


def test_no_inline_handlers_or_scripts_in_templates():
    """全テンプレートにインラインJS（src無し<script>）・イベントハンドラが残っていないこと。

    script-src から 'unsafe-inline' を除去したため、これらが再混入するとブラウザで
    ブロックされ画面が壊れる。回帰防止の静的チェック。
    """
    import glob
    import re

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    handler_re = re.compile(
        r'\bon(click|change|input|submit|keydown|keyup|mouse[a-z]+|focus|blur|load)\s*=', re.I)
    inline_script_re = re.compile(r'<script(?![^>]*\ssrc=)[^>]*>', re.I)  # src 無しの <script>
    offenders = []
    for path in sorted(glob.glob(os.path.join(base, "templates", "*.html"))):
        text = open(path, encoding="utf-8").read()
        name = os.path.basename(path)
        if handler_re.search(text):
            offenders.append(f"{name}: インラインイベントハンドラ")
        if inline_script_re.search(text):
            offenders.append(f"{name}: インライン<script>")
    assert not offenders, "CSP違反となるインラインJSが残存: " + "; ".join(offenders)


def test_no_inline_handlers_in_js_generated_html():
    """static/js/*.js が文字列で組み立てる HTML にインラインイベントハンドラが無いこと（#811）。

    テンプレートの検査は JS が innerHTML へ流す HTML を見ないので、M-3 の行の onclick が
    取り残されていた（クリックしても CSP の script-src-attr で黙って止まる）。
    行クリック等は data-click＋委譲（analysis.js の _wireDelegate）で書く。
    `el.onclick = fn` のようなプロパティ代入は CSP に抵触しないので、直前が `.`・英数字の
    ものは除外し、属性値の書き出し（引用符か `${`）が続くものだけを拾う。
    """
    import glob
    import re

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    attr_handler_re = re.compile(
        r'(?<![\w.$])on(click|dblclick|change|input|submit|reset|key[a-z]+|mouse[a-z]+|pointer[a-z]+'
        r'|focus[a-z]*|blur|load|error|scroll|wheel|drag[a-z]*|drop|contextmenu|select|toggle)'
        r'\s*=\s*(["\']|\$\{)', re.I)
    offenders = []
    for path in sorted(glob.glob(os.path.join(base, "static", "js", "*.js"))):
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if attr_handler_re.search(line):
                    offenders.append(f"{name}:{lineno}")
    assert not offenders, (
        "JS が生成する HTML にインラインイベントハンドラが残存（data-click＋委譲へ移すこと）: "
        + ", ".join(offenders))
