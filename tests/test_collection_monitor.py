"""収集画面の 1 画面目は「監視」が主役（#1212）。

収集は夜間バッチへ移っており、この画面の主な用途は「手で収集する」から「収集を監視し、
失敗したときだけ手を入れる」へ変わった。ホームの「自動収集」と同じ表を先頭に置く。

ここが縛るのは、**破っても画面が壊れず失敗として現れないもの**だけ:

- 表を描く関数を画面ごとに写すと、片方だけ古くなる（以前ホームは停止済みの GitHub Actions を
  緑ドットで宣伝し続けた・#563）。関数は common.js の 1 か所に置く。
- 置き場（id）や CSS を片方の画面だけ落とすと、関数は黙って何も描かない（`if (!dot …) return`）。
- 「最終実行はダッシュボードで確認」の案内が残ると、目の前に表があるのに往復させる。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
STATIC_JS = ROOT / "static" / "js"

SCHED_PAGES = ("dashboard.html", "collection.html")
SCHED_IDS = ("sched-dot", "sched-headline", "sched-grid")
# renderSchedule が付けるクラス。CSS は画面ごとに持つ決まり（DESIGN.md）なので両方に要る
SCHED_CSS = (".scheduler-card", ".sched-grid", ".sched-item", ".sched-item-label",
             ".sched-item-value", ".dot-green", ".dot-amber", ".dot-red")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _style(html: str) -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.S))


class TestScheduleTableIsShared:
    def test_render_function_lives_only_in_common_js(self):
        owners = [p.name for p in sorted(STATIC_JS.glob("*.js"))
                  if re.search(r"function\s+renderSchedule\b", _read(p))]
        assert owners == ["common.js"], (
            f"renderSchedule は common.js だけに置く（複製は片方だけ直って食い違う）: {owners}")

    def test_both_pages_call_it(self):
        for js in ("dashboard.js", "collection.js"):
            assert re.search(r"\brenderSchedule\(", _read(STATIC_JS / js)), (
                f"{js} が renderSchedule を呼んでいない")

    def test_both_pages_have_the_slots_and_load_common_js(self):
        for name in SCHED_PAGES:
            html = _read(TEMPLATES / name)
            for id_ in SCHED_IDS:
                assert f'id="{id_}"' in html, f"{name}: #{id_} が無い（表が黙って描かれない）"
            assert "/static/js/common.js" in html, f"{name}: common.js を読み込んでいない"

    def test_both_pages_carry_the_css(self):
        for name in SCHED_PAGES:
            css = _style(_read(TEMPLATES / name))
            for sel in SCHED_CSS:
                assert re.search(re.escape(sel) + r"\s*[{,]", css), (
                    f"{name}: `{sel}` の CSS が無い（renderSchedule が付けるクラス）")

    def test_the_table_folds_to_fewer_columns_on_narrow_screens(self):
        """375px で 3 列のままだと、タスク名が折り返して 1 画面目に収まらない。"""
        for name in SCHED_PAGES:
            css = _style(_read(TEMPLATES / name))
            media = "\n".join(re.findall(r"@media[^{]*max-width[^{]*\{(.*?\})\s*\}", css, flags=re.S))
            assert ".sched-grid" in media, f"{name}: 狭い幅で .sched-grid の列を減らしていない"


class TestCollectionFirstScreen:
    def test_schedule_table_comes_first_in_the_collect_tab(self):
        html = _read(TEMPLATES / "collection.html")
        tab = html.index('<div id="tab-collect">')
        assert tab < html.index('id="sched-grid"') < html.index('id="s-companies"'), (
            "「自動収集」の表は件数カードより前（375px では件数カードだけで 1 画面目が埋まる）")

    def test_smart_collection_is_foldable_and_closed_by_default(self):
        html = _read(TEMPLATES / "collection.html")
        m = re.search(r'<details id="smart-details"([^>]*)>(.*?)</details><!-- /スマート収集 -->',
                      html, flags=re.S)
        assert m, "スマート収集が <details id=\"smart-details\"> に包まれていない"
        attrs, body = m.groups()
        assert not re.search(r"\bopen\b", attrs), (
            "既定は閉じておく（開いておいて後から畳むと、普段の表示で毎回ちらつく）")
        for id_ in ("btn-smart", "btn-smart-stop", "smart-progress"):
            assert f'id="{id_}"' in body, f"#{id_} が囲みの外にある"

    def test_js_opens_the_fold_when_empty_and_when_running(self):
        js = _read(STATIC_JS / "collection.js")
        fn = re.search(r"function applyCollectState\(.*?\n\}", js, flags=re.S)
        assert fn, "applyCollectState が無い"
        assert "smart-details" in fn.group(0) and ".open = true" in fn.group(0)
        start = re.search(r"async function startSmartCollection\(.*?\n\}", js, flags=re.S).group(0)
        assert "smart-details" in start, (
            "実行を始めたら囲みを開く（進捗と停止ボタンは囲みの中）")

    def test_one_primary_button_on_the_first_screen(self):
        """既定の HTML（財務データがある普段の状態）で、畳まれていない主ボタンは 1 つ。"""
        html = _read(TEMPLATES / "collection.html")
        tab = html[html.index('<div id="tab-collect">'):html.index('<!-- 差分収集 / 全件収集（詳細設定） -->')]
        visible = re.sub(r"<details.*?</details>", "", tab, flags=re.S)
        primaries = re.findall(r'<button[^>]*class="[^"]*\bbtn-primary\b[^"]*"[^>]*id="([^"]+)"', visible)
        assert primaries == ["btn-run-now"], primaries
        smart = re.search(r'<button[^>]*id="btn-smart"[^>]*>', html).group(0)
        assert "btn-secondary" in smart and "style=" not in smart, (
            "スマート収集のボタンは既定で副。inline の背景色があるとクラスの切り替えが効かない")

    def test_no_stale_pointer_to_the_dashboard(self):
        html = _read(TEMPLATES / "collection.html")
        assert not re.search(r"ダッシュボード(</a>)?の「自動収集」", html), (
            "表はこの画面にある。「ダッシュボードの『自動収集』で確認」の案内を残さない")
