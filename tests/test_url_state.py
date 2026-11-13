"""画面の状態（タブ・プリセット・スクリーニングの条件）を URL に残す約束（#1213）。

分析画面だけが `?tab=` を持ち、朝の推奨・収集・企業詳細は状態が URL に無かった。再読込・
ブラウザの戻る・URL の共有で既定へ戻り、スクリーニングの条件と結果が消えていた。

ここが縛るのは、**破っても画面が壊れず失敗として現れないもの**:

- 読み書きを画面ごとに写すと、片方だけ `pushState` になったり検証を忘れたりする。関数は common.js の 1 か所。
- `pushState` で書くと、タブを切り替えた回数だけ「戻る」を押さないと画面を出られない。
- 企業リンクの開き方が画面ごとに違うと、同じ操作で「元の画面が残る／消える」が変わる
  （2026-11-14 に「新しいタブ」へそろえると決めた。分析の重い計算結果を失わないため）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"

HELPERS = ("urlStateGet", "urlStateSet", "urlStateApplySelect")


def _js(name: str) -> str:
    return (STATIC_JS / name).read_text(encoding="utf-8")


class TestUrlStateHelpersAreShared:
    def test_helpers_live_only_in_common_js(self):
        for fn in HELPERS:
            owners = [p.name for p in sorted(STATIC_JS.glob("*.js"))
                      if re.search(rf"function\s+{fn}\b", p.read_text(encoding="utf-8"))]
            assert owners == ["common.js"], f"{fn} は common.js だけに置く: {owners}"

    def test_state_is_written_with_replace_state_only(self):
        """履歴を積まない（タブ切替のたびに積むと「戻る」で画面を出られない）。"""
        for p in sorted(STATIC_JS.glob("*.js")):
            # 呼び出しだけを見る（「pushState にしない」という注記は分析画面にもある）
            assert not re.search(r"\.pushState\s*\(", p.read_text(encoding="utf-8")), (
                f"{p.name}: pushState を使わない")
        body = re.search(r"function urlStateSet\(pairs\)\s*\{(.*?)\n\}", _js("common.js"), flags=re.S)
        assert body and "history.replaceState" in body.group(1)
        # ほかのキーとハッシュを保つ（今ある検索文字列から始める）
        assert "URLSearchParams(window.location.search)" in body.group(1)
        assert "window.location.hash" in body.group(1)

    def test_select_restore_ignores_unknown_values(self):
        body = re.search(r"function urlStateApplySelect\(id, key\)\s*\{(.*?)\n\}", _js("common.js"), flags=re.S)
        assert body, "urlStateApplySelect が無い"
        assert "el.options" in body.group(1), "選択肢にある値だけ戻す（未知の値は既定で開く）"

    def test_pages_load_common_js(self):
        for name in ("morning.html", "collection.html", "company.html"):
            assert "/static/js/common.js" in (TEMPLATES / name).read_text(encoding="utf-8"), name


class TestMorning:
    def test_preset_and_count_round_trip(self):
        js = _js("morning.js")
        assert "urlStateApplySelect('mor-preset', 'preset')" in js
        assert "urlStateApplySelect('mor-topn', 'n')" in js
        assert re.search(r"urlStateSet\(\{\s*preset:.*?n:", js, flags=re.S)
        # 戻すのは最初の読み込みより前（後だと既定の並びを 1 回余分に引く）
        assert js.index("urlStateApplySelect('mor-preset'") < js.rindex("loadMorning();")


class TestCompany:
    def test_tab_round_trip(self):
        js = _js("company.js")
        assert "urlStateSet({ tab: t.dataset.tab })" in js
        assert "urlStateGet('tab')" in js
        # 一覧に無い値は無視する（data-tab の一致で探す）
        assert re.search(r"tabs\.find\(x => x\.dataset\.tab === urlTab\)", js)

    def test_peer_tab_waits_for_the_company(self):
        """URL から同業比較で開いた直後は企業が未読込。そこで引くと「業種データが無い」と誤って出る。"""
        js = _js("company.js")
        assert re.search(r"'peer' && !peersLoaded && curCompany\.code\) loadPeers\(\)", js)
        load = js[js.index("async function loadCompany("):]
        assert re.search(r"dataset\.tab === 'peer' && !peersLoaded\) loadPeers\(\)", load)

    def test_login_redirect_keeps_the_query(self):
        js = _js("company.js")
        assert "encodeURIComponent(location.pathname + location.search)" in js


class TestCollection:
    def test_tab_round_trip_and_unknown_tab_falls_back(self):
        js = _js("collection.js")
        assert "urlStateSet({tab: t})" in js
        assert re.search(r"showTab\(_TABS\.includes\(urlTab\) \? urlTab : 'collect', true\)", js)
        # 起動時の表示では URL に書かない
        assert "if(initial !== true) urlStateSet({tab: t});" in js

    def test_screen_conditions_cover_every_input(self):
        """条件の欄を足したら URL の表にも足す（足し忘れると、その欄だけ再読込で消える）。"""
        js = _js("collection.js")
        html = (TEMPLATES / "collection.html").read_text(encoding="utf-8")
        tab = html[html.index('<div id="tab-screen"'):]
        number_inputs = set(re.findall(r'<input type="number" id="(sc-[a-z-]+)"', tab))
        table = re.search(r"const SCREEN_NUM_FIELDS = \[(.*?)\];", js, flags=re.S)
        assert table, "SCREEN_NUM_FIELDS が無い"
        ids = set(re.findall(r"\['(sc-[a-z-]+)','[a-z_]+'\]", table.group(1)))
        assert ids == number_inputs, f"表と欄が食い違う: {ids ^ number_inputs}"
        keys = re.findall(r"\['sc-[a-z-]+','([a-z_]+)'\]", table.group(1))
        assert len(keys) == len(set(keys)), "URL のキーが重複している"
        assert not {"tab", "industry", "market", "delisted"} & set(keys)

    def test_numbers_are_validated_before_restore(self):
        fn = re.search(r"async function restoreScreenFromUrl\(\)\{(.*?)\n\}", _js("collection.js"), flags=re.S)
        assert fn, "restoreScreenFromUrl が無い"
        body = fn.group(1)
        assert "Number.isFinite(Number(v))" in body, "数として読めない値は欄へ入れない"
        # 業種は選択肢が届いてから戻す
        assert body.index("await loadIndustries()") < body.index("urlStateApplySelect('sc-industry','industry')")
        assert "urlStateGet('tab') === 'screen'" in body

    def test_conditions_are_written_on_run_not_on_input(self):
        js = _js("collection.js")
        run = re.search(r"async function runScreen\(\)\{(.*?)\n\}", js, flags=re.S).group(1)
        assert "writeScreenUrl();" in run
        assert js.count("writeScreenUrl();") == 1, "条件を書くのは「実行」のときだけ"

    def test_industry_reload_keeps_the_selection(self):
        fn = re.search(r"async function loadIndustries\(\)\{(.*?)\n\}", _js("collection.js"), flags=re.S).group(1)
        assert "const cur = sel.value;" in fn and "sel.value = cur" in fn


class TestCompanyLinksOpenTheSameWay:
    def test_every_company_link_opens_in_a_new_tab(self):
        bad = []
        for p in sorted(STATIC_JS.glob("*.js")):
            for m in re.finditer(r'<a href="/company/[^>]*>', p.read_text(encoding="utf-8")):
                tag = m.group(0)
                if 'target="_blank"' not in tag or 'rel="noopener"' not in tag:
                    bad.append(f"{p.name}: {tag[:80]}")
        assert not bad, "企業リンクは新しいタブで開く（rel=noopener 付き）:\n" + "\n".join(bad)
