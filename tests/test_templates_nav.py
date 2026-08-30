"""グローバルナビ（`.gnav`）が全サブページで揃っていることの静的検証。

gnav は Jinja の継承機構を持たず各テンプレートへコピペで重複しているため、
「画面を足したがナビを貼り忘れる」「貼ったが CSS を貼り忘れる」「色指定だけズレる」
が失敗として現れない。実際に `/morning` は入口だけあって出口が無い袋小路になり、
やさしい解説のリンク色は models/guide だけ緑・他4ファイルは青にズレていた。

ここでは canonical な gnav ブロックを1つ定め、全サブページがそれと一致すること
（`.active` の位置だけが差分であること）を CI で縛る。

DB 不要の静的ページのみを対象にする。TestClient を with 無しで使うことで
lifespan（init_db / Postgres 依存）を回避する（tests/test_security_headers.py と同じ流儀）。
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_RATELIMIT_ENABLED", "false")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

# テンプレート名 → `.active` が付くべき href（None = そのページ自身が gnav の項目に無い）
GNAV_PAGES = {
    "morning.html": "/morning",
    "analysis.html": "/analysis",
    "company.html": "/company",
    "collection.html": "/collection",
    "guide.html": "/guide",
    "models.html": "/models",
    "db.html": None,  # /db は gnav の項目に無い＝どのリンクも active にならない
}

# 意図的に gnav を持たないページ（ホーム＝ナビカードを持つ / 認証前）
NO_GNAV = {"dashboard.html", "login.html"}

# gnav が指す全 href（左の主要導線 → 右のリファレンス）
GNAV_HREFS = ["/", "/morning", "/analysis", "/company", "/collection", "/guide", "/models"]

# gnav の描画に必要な CSS セレクタ（HTML だけ貼って CSS を貼り忘れると無スタイルになる）
GNAV_CSS_SELECTORS = [
    ".gnav{",
    ".gnav-link{",
    ".gnav-link.active{",
    ".gnav-ref{",
    ".gnav-spacer{",
]

_NAV_RE = re.compile(r'<nav class="gnav">.*?</nav>', re.S)


def _read(name):
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
        return f.read()


def _nav_blocks(text):
    return _NAV_RE.findall(text)


def _normalize(nav):
    """`.active` を剥がして正規化する（ページ間の唯一許される差分が active のため）。"""
    return re.sub(r"\s+active(?=[\s\"])", "", nav)


def test_every_template_is_classified():
    """templates/*.html の全ファイルが gnav 有り／無しのどちらかに分類されていること。

    新しい画面を足したのに分類し忘れた場合をここで落とす（/morning と同型の取り残しの再発防止）。
    """
    on_disk = {os.path.basename(p) for p in glob.glob(os.path.join(TEMPLATE_DIR, "*.html"))}
    classified = set(GNAV_PAGES) | NO_GNAV
    assert on_disk == classified, (
        "テンプレートの分類漏れ: 未分類="
        f"{sorted(on_disk - classified)} / 実体なし={sorted(classified - on_disk)}"
    )


def test_every_subpage_has_exactly_one_gnav():
    for name in GNAV_PAGES:
        blocks = _nav_blocks(_read(name))
        assert len(blocks) == 1, f"{name}: gnav が {len(blocks)} 個（1個であること）"
    for name in NO_GNAV:
        assert not _nav_blocks(_read(name)), f"{name}: gnav を持たない設計なのに存在する"


def test_gnav_markup_is_identical():
    """全サブページの gnav が（active を除いて）完全に一致すること。

    リンクの過不足・順序違い・色指定のズレはすべてここで落ちる。
    """
    canonical = None
    canonical_name = None
    for name in sorted(GNAV_PAGES):
        nav = _normalize(_nav_blocks(_read(name))[0])
        if canonical is None:
            canonical, canonical_name = nav, name
            continue
        assert nav == canonical, (
            f"{name} の gnav が {canonical_name} と食い違う（active 以外の差分は禁止）\n"
            f"--- {canonical_name} ---\n{canonical}\n--- {name} ---\n{nav}"
        )

    for href in GNAV_HREFS:
        assert f'href="{href}"' in canonical, f'gnav に href="{href}" が無い'
    assert '<span class="gnav-spacer"></span>' in canonical, "gnav-spacer が無い（右寄せが効かない）"


def test_active_marks_only_self():
    for name, self_href in GNAV_PAGES.items():
        nav = _nav_blocks(_read(name))[0]
        active = re.findall(r'<a href="([^"]+)"[^>]*\bclass="[^"]*\bactive\b', nav)
        expected = [] if self_href is None else [self_href]
        assert active == expected, f"{name}: active が {active}（期待 {expected}）"


def test_gnav_css_present():
    """gnav を持つ全ページに .gnav* の CSS が揃っていること（CSS 貼り忘れ＝無スタイルの防止）。"""
    offenders = []
    for name in GNAV_PAGES:
        text = _read(name)
        missing = [s for s in GNAV_CSS_SELECTORS if s not in text]
        if missing:
            offenders.append(f"{name}: {missing}")
    assert not offenders, "gnav の CSS が欠けている: " + "; ".join(offenders)


def test_gnav_targets_are_served():
    """gnav の各リンク先が実在するページルートであること（リンク切れの防止）。"""
    for href in GNAV_HREFS:
        r = client.get(href)
        assert r.status_code == 200, f"gnav の {href} が {r.status_code}"
