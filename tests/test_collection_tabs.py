"""収集画面（templates/collection.html）のタブの入れ子を照合する。

閉じタグが1つ余っているだけで、ブラウザは黙って直して描く。エラーにはならない。
実際に `tab-stockmarket` が早く閉じ、マクロ収集と XBRL 再解析の節がどのタブにも属さなくなって
全タブに表示され、後ろの2タブは `.container` の外へ出ていた（2026-11-13 の /ux-critique で発見）。
失敗として現れないので、ここで構造を照合する。
"""
import os
from html.parser import HTMLParser

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(BASE_DIR, "templates", "collection.html")
JS = os.path.join(BASE_DIR, "static", "js", "collection.js")

TABS = ("tab-collect", "tab-stockmarket", "tab-dataview", "tab-screen")

# どのタブの中に居るべきか（id → タブ）。タブをまたいで漏れた実例と、漏れると誤読を生む欄。
EXPECTED_TAB = {
    "log-box": "tab-collect",
    "market-log-box": "tab-stockmarket",
    "macro-years": "tab-stockmarket",
    "macro-log": "tab-stockmarket",
    "reparse-log": "tab-stockmarket",
    "db-search": "tab-dataview",
    "screen-count": "tab-screen",
}


class _DivTree(HTMLParser):
    """div の開閉だけを追い、id を持つ要素がどの div の中に居るかを記録する。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []            # 開いている div の (id, class)
        self.ancestors = {}        # 要素の id → その時点で開いている div の id の並び
        self.parent_class = {}     # div の id → 直接の親 div の class
        self.unmatched_close = []  # 開いている div が無いのに現れた </div> の行
        self.card_outside_tab = [] # どのタブにも属さない .card の行

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        el_id = a.get("id")
        classes = (a.get("class") or "").split()
        if el_id:
            self.ancestors[el_id] = [i for i, _ in self.stack if i]
        if tag != "div":
            return
        open_ids = [i for i, _ in self.stack if i]
        if "card" in classes and self.stack and not any(i in TABS for i in open_ids):
            self.card_outside_tab.append(self.getpos()[0])
        if el_id:
            self.parent_class[el_id] = self.stack[-1][1] if self.stack else None
        self.stack.append((el_id, classes))

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if not self.stack:
            self.unmatched_close.append(self.getpos()[0])
            return
        self.stack.pop()


def _parse():
    with open(TEMPLATE, encoding="utf-8") as f:
        tree = _DivTree()
        tree.feed(f.read())
    return tree


def test_div_の開閉が釣り合っている():
    tree = _parse()
    assert not tree.unmatched_close, f"相手の無い </div> がある（行）: {tree.unmatched_close}"
    assert not tree.stack, f"閉じていない div が残る: {[i for i, _ in tree.stack]}"


def test_4つのタブは_container_の直下に並ぶ():
    tree = _parse()
    for tab in TABS:
        assert tab in tree.parent_class, f"{tab} が見つからない"
        assert "container" in (tree.parent_class[tab] or []), (
            f"{tab} の親が .container でない（手前の閉じタグが余っている）: {tree.parent_class[tab]}"
        )
        assert not [i for i in tree.ancestors[tab] if i in TABS], f"{tab} が別のタブの中に入っている"


def test_各欄は自分のタブの中に居る():
    tree = _parse()
    for el_id, tab in EXPECTED_TAB.items():
        assert el_id in tree.ancestors, f"#{el_id} が見つからない（照合表が実体とずれた）"
        inside = [i for i in tree.ancestors[el_id] if i in TABS]
        assert inside == [tab], f"#{el_id} は {tab} の中に居るはずが {inside or 'どのタブにも居ない'}"


def test_カードはどれかのタブの中に居る():
    tree = _parse()
    assert not tree.card_outside_tab, (
        f"どのタブにも属さない .card がある（全タブに表示される）。行: {tree.card_outside_tab}"
    )


def test_失敗の知らせはどのタブにも属さない():
    """#tab-error はタブの外に置く。タブの中へ入れると、そのタブを開いていない間は見えない。"""
    tree = _parse()
    assert "tab-error" in tree.ancestors, "#tab-error が無い"
    assert not [i for i in tree.ancestors["tab-error"] if i in TABS]
    with open(JS, encoding="utf-8") as f:
        js = f.read()
    assert "showTabError(msg)" in js, "log() が失敗を #tab-error へ出していない"
