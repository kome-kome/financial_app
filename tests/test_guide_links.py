"""やさしい解説（`templates/guide.html`）へのリンクが切れていないことの静的検証。

解説は手書きの1ページで、他の画面からは `/guide#<id>` で節へ直接飛ぶ。id を変える・節を消す・
プラグインを足して節を書き忘れる、のどれも**失敗として現れない**——リンク先が無くてもブラウザは
ページの先頭を開くだけで、エラーも 404 も出ない。分析画面の「❓ やさしい解説」は
`static/js/analysis.js` が `/guide#${plugin.name}` を組み立てるので、文字列の照合だけでは
プラグイン側の書き忘れを拾えない。ここでは次を CI で縛る。

1. テンプレートと JS に書かれた `/guide#<id>` と、guide.html 内の `#<id>` がすべて実在する
2. `plugins/*.py` の全プラグイン（`name = "..."`）に同じ id の節がある（動的リンクの受け皿）
3. 各画面のヘッダーの「この画面の見方」が、その画面の節を指している

プラグインは import しない（重い依存を読み込まず、DB も要らない）。
"""
import glob
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = os.path.join(BASE_DIR, "templates", "guide.html")

# 画面 → ヘッダーの「この画面の見方」が指す guide.html の id（ホームだけは入口の節）
HELP_LINKS = {
    "dashboard.html": "start",
    "morning.html": "screen-morning",
    "analysis.html": "screen-analysis",
    "company.html": "screen-company",
    "collection.html": "screen-collection",
    "db.html": "screen-db",
}

# 空振り検知の下限（正規表現や走査が壊れて 0 件になったとき「切れ無し」で静かに通るのを防ぐ）
MIN_EXTERNAL_LINKS = 15   # 他の画面・JS からの /guide#… （2026-11 時点で 20 本超）
MIN_INTERNAL_LINKS = 60   # guide.html 内の #… （目次だけで 30 本超）
MIN_PLUGINS = 10          # plugins/*.py のプラグイン（2026-11 時点で 11）

_ID_RE = re.compile(r'\bid="([^"]+)"')
_EXTERNAL_RE = re.compile(r'/guide#([A-Za-z0-9_-]+)')
_INTERNAL_RE = re.compile(r'href="#([A-Za-z0-9_-]+)"')
_PLUGIN_NAME_RE = re.compile(r'^\s+name\s*=\s*"([a-z0-9_]+)"', re.M)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _guide_ids():
    ids = _ID_RE.findall(_read(GUIDE))
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert not dup, f"guide.html の id が重複している（飛び先が曖昧になる）: {dup}"
    return set(ids)


def _external_links():
    """guide.html 以外の templates/*.html と static/js/*.js に書かれた /guide#<id>。"""
    found = []
    paths = glob.glob(os.path.join(BASE_DIR, "templates", "*.html")) + \
        glob.glob(os.path.join(BASE_DIR, "static", "js", "*.js"))
    for path in paths:
        if os.path.abspath(path) == os.path.abspath(GUIDE):
            continue
        for anchor in _EXTERNAL_RE.findall(_read(path)):
            found.append((os.path.relpath(path, BASE_DIR), anchor))
    return found


class TestGuideAnchors:
    def test_links_from_other_pages_resolve(self):
        ids = _guide_ids()
        links = _external_links()
        assert len(links) >= MIN_EXTERNAL_LINKS, (
            f"/guide#… を {len(links)} 本しか拾えていない（下限 {MIN_EXTERNAL_LINKS}）。走査が壊れている疑い")
        broken = sorted({f"{src}: /guide#{a}" for src, a in links if a not in ids})
        assert not broken, (
            "guide.html に無い id を指しているリンクがある（開くとページの先頭に着くだけで、誰も気づかない）:\n"
            + "\n".join(broken))

    def test_links_inside_guide_resolve(self):
        ids = _guide_ids()
        links = _INTERNAL_RE.findall(_read(GUIDE))
        assert len(links) >= MIN_INTERNAL_LINKS, (
            f"guide.html 内の #… を {len(links)} 本しか拾えていない（下限 {MIN_INTERNAL_LINKS}）")
        broken = sorted({a for a in links if a not in ids})
        assert not broken, f"guide.html の目次・本文が、ページ内に無い id を指している: {broken}"


class TestEveryPluginHasASection:
    def test_analysis_builds_guide_links_from_plugin_names(self):
        # このテストの前提。分析画面がプラグイン名から /guide#… を組み立てるのをやめたら、
        # 下の照合は守るものが無くなるので一緒に見直す
        src = _read(os.path.join(BASE_DIR, "static", "js", "analysis.js"))
        assert re.search(r'/guide#\$\{(?:esc\()?plugin\.name\)?\}', src), (
            "analysis.js が /guide#${esc(plugin.name)} でリンクを組み立てていない")

    def test_every_plugin_name_is_a_guide_section(self):
        ids = _guide_ids()
        names = []
        for path in sorted(glob.glob(os.path.join(BASE_DIR, "plugins", "*.py"))):
            names += _PLUGIN_NAME_RE.findall(_read(path))
        assert len(names) >= MIN_PLUGINS, (
            f"プラグイン名を {len(names)} 個しか拾えていない（下限 {MIN_PLUGINS}）。正規表現が壊れている疑い")
        missing = sorted(n for n in names if n not in ids)
        assert not missing, (
            "やさしい解説に節が無いプラグインがある。templates/guide.html の第3部に "
            f'id="<プラグイン名>" の節を足すこと（退役したモデルも「退役したモデル」の下に置く）: {missing}')


class TestScreenHelpLinks:
    def test_each_screen_points_at_its_own_section(self):
        ids = _guide_ids()
        for tpl, anchor in HELP_LINKS.items():
            html = _read(os.path.join(BASE_DIR, "templates", tpl))
            assert f'<a href="/guide#{anchor}" class="help-link">' in html, (
                f"{tpl} のヘッダーに /guide#{anchor} への「この画面の見方」が無い")
            assert ".help-link{" in html, f"{tpl} に .help-link の CSS が無い（無スタイルになる）"
            assert anchor in ids, f"guide.html に #{anchor} の節が無い"

    def test_no_other_page_has_a_stray_help_link(self):
        # コピペで別の画面の節を指したまま増えるのを防ぐ。足すなら HELP_LINKS に登録する
        pages = {
            os.path.basename(p) for p in glob.glob(os.path.join(BASE_DIR, "templates", "*.html"))
            if 'class="help-link"' in _read(p)
        }
        assert pages == set(HELP_LINKS), (
            f"「この画面の見方」を持つ画面が HELP_LINKS と食い違う: 余分={sorted(pages - set(HELP_LINKS))} "
            f"不足={sorted(set(HELP_LINKS) - pages)}")
