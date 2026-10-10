"""画面の表示切り替えの静的検証（#910）。

テンプレートが要素に `style="display:none"` を直接書き、JS が `hidden` クラスの付け外しで
開閉すると、直接書いた `display:none` が常に勝って要素は二度と出ない。画面は壊れず、
エラーも出ないので失敗として現れない（収集画面の企業詳細モーダルは初回コミットから
一度も表示されていなかった・#910）。

直接書いた `display:none` の要素は `style.display` で開閉する。`hidden` クラスで
開閉するなら、テンプレートでも `class="hidden"` で隠す。

拾えない書き方: 要素をいったん変数に入れてから `classList` を触る形
（`const m = getElementById('x'); m.classList.remove('hidden')`）。

テンプレートと静的 JS をファイルとして読むだけで、アプリも DB も起動しない。
"""
import glob
import os
import re

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = sorted(glob.glob(os.path.join(BASE_DIR, "templates", "*.html")))
STATIC_JS = sorted(glob.glob(os.path.join(BASE_DIR, "static", "js", "*.js")))

_START_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")
_ID_RE = re.compile(r'\bid="([\w-]+)"')
_STYLE_RE = re.compile(r'\bstyle="([^"]*)"')
_DISPLAY_NONE_RE = re.compile(r"display\s*:\s*none")

# 装置の確認に使う既知の要素（どちらも style.display で正しく開閉している）
_KNOWN_INLINE_HIDDEN = {"detail-section", "gap-results-card"}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _inline_hidden_ids():
    """`style` に `display:none` を直接書いた要素の id → テンプレート名。"""
    found = {}
    for path in TEMPLATES:
        for tag in _START_TAG_RE.finditer(_read(path)):
            id_m = _ID_RE.search(tag.group(0))
            style_m = _STYLE_RE.search(tag.group(0))
            if id_m and style_m and _DISPLAY_NONE_RE.search(style_m.group(1)):
                found[id_m.group(1)] = os.path.basename(path)
    return found


def _class_toggle_re(element_id):
    q = r"""['"]"""
    lookup = (rf"(?:getElementById\(\s*{q}{re.escape(element_id)}{q}\s*\)"
              rf"|querySelector\(\s*{q}#{re.escape(element_id)}{q}\s*\))")
    return re.compile(rf"{lookup}\s*\.classList\.(?:add|remove|toggle)\(\s*{q}hidden{q}")


@pytest.fixture(scope="module")
def inline_hidden():
    return _inline_hidden_ids()


def test_scanner_finds_known_inline_hidden_elements(inline_hidden):
    # 正規表現が壊れて 1 つも拾えなくなると、下の照合は黙って通る
    missing = _KNOWN_INLINE_HIDDEN - set(inline_hidden)
    assert not missing, f"style=\"display:none\" の既知の要素を拾えていない: {sorted(missing)}"


def test_inline_hidden_elements_are_not_toggled_by_hidden_class(inline_hidden):
    sources = {os.path.basename(p): _read(p) for p in STATIC_JS}
    hits = []
    for element_id, template in sorted(inline_hidden.items()):
        pattern = _class_toggle_re(element_id)
        for js_name, src in sources.items():
            for m in pattern.finditer(src):
                line = src.count("\n", 0, m.start()) + 1
                hits.append(f"{template}#{element_id} ← {js_name}:{line}")
    assert not hits, (
        "style=\"display:none\" を直接書いた要素を hidden クラスで開閉している"
        f"（直接書いた display:none が勝って開かない）: {hits}"
    )
