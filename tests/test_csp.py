"""Content-Security-Policy の nonce 動作テスト"""
import re


CSP_HEADER = "Content-Security-Policy"
_NONCE_RE = re.compile(r"'nonce-([A-Za-z0-9_\-]+)'")


def _extract_nonce(csp_header: str) -> str | None:
    m = _NONCE_RE.search(csp_header)
    return m.group(1) if m else None


class TestCSPNonce:
    def test_csp_has_nonce_in_script_src(self, client):
        r = client.get("/")
        csp = r.headers[CSP_HEADER]
        # script-src 'self' 'nonce-XXX'; が含まれる
        assert "script-src" in csp
        assert "'nonce-" in csp

    def test_csp_has_nonce_in_style_src(self, client):
        r = client.get("/")
        csp = r.headers[CSP_HEADER]
        assert "style-src" in csp
        # スクリプトとスタイル両方に nonce が含まれる
        assert csp.count("'nonce-") >= 2

    def test_csp_no_unsafe_inline(self, client):
        r = client.get("/")
        csp = r.headers[CSP_HEADER]
        assert "'unsafe-inline'" not in csp

    def test_nonce_is_per_request(self, client):
        """同じパスを2回叩くと別の nonce が返る（リプレイ防止）"""
        r1 = client.get("/")
        r2 = client.get("/")
        n1 = _extract_nonce(r1.headers[CSP_HEADER])
        n2 = _extract_nonce(r2.headers[CSP_HEADER])
        assert n1 and n2
        assert n1 != n2

    def test_nonce_is_url_safe_base64(self, client):
        r = client.get("/")
        n = _extract_nonce(r.headers[CSP_HEADER])
        assert n is not None
        # secrets.token_urlsafe(16) は約22文字（URL-safe base64）
        assert 20 <= len(n) <= 30
        assert re.match(r"^[A-Za-z0-9_\-]+$", n)

    def test_html_contains_matching_nonce_in_script_tag(self, client):
        """テンプレートに <script nonce="XXX"> が埋め込まれ、CSP と一致する"""
        r = client.get("/")
        n = _extract_nonce(r.headers[CSP_HEADER])
        assert n is not None
        # HTML 本文に nonce が埋め込まれている
        body = r.text
        assert f'<script nonce="{n}"' in body
        assert f'<style nonce="{n}"' in body

    def test_all_template_pages_have_nonce(self, client):
        for path in ("/", "/collection", "/analysis", "/models", "/login"):
            r = client.get(path)
            assert r.status_code == 200, f"{path}: {r.status_code}"
            csp = r.headers[CSP_HEADER]
            n = _extract_nonce(csp)
            assert n, f"{path} has no nonce: {csp}"
            assert f'<script nonce="{n}"' in r.text, f"{path} script tag missing nonce"


class TestNoInlineHandlersInTemplates:
    """全テンプレートに onclick/onchange/oninput/onkeydown 等のインラインハンドラが
    残っていないことを保証する（CSP 厳格化を維持するための回帰防止テスト）。
    """

    def test_no_inline_event_handlers_in_dashboard(self, client):
        body = client.get("/").text
        # `content=` 等のメタ属性に "on" が含まれるため、属性名のみを検出する正規表現を使う
        handlers = re.findall(r'\son(click|change|input|submit|keydown|keyup|keypress|focus|blur|load)="', body)
        assert handlers == [], f"インラインハンドラ検出: {handlers}"

    def test_no_inline_event_handlers_in_collection(self, client):
        body = client.get("/collection").text
        handlers = re.findall(r'\son(click|change|input|submit|keydown|keyup|keypress|focus|blur|load)="', body)
        assert handlers == [], f"インラインハンドラ検出: {handlers}"

    def test_no_inline_event_handlers_in_analysis(self, client):
        body = client.get("/analysis").text
        handlers = re.findall(r'\son(click|change|input|submit|keydown|keyup|keypress|focus|blur|load)="', body)
        assert handlers == [], f"インラインハンドラ検出: {handlers}"

    def test_no_inline_event_handlers_in_login(self, client):
        body = client.get("/login").text
        handlers = re.findall(r'\son(click|change|input|submit|keydown|keyup|keypress|focus|blur|load)="', body)
        assert handlers == [], f"インラインハンドラ検出: {handlers}"
