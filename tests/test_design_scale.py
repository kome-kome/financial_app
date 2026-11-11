"""分析画面の見た目が DESIGN.md の段から外れないことの静的検証（#1177）。

どれも破っても画面は壊れず、少しずつ揃わなくなるだけなので失敗として現れない:

- 角の丸みが段（DESIGN.md「Shapes」）の外の値（7px・999px・2px）になっていた
- 進捗バーだけグラデーションを持っていた（DESIGN.md「飾りの色・グラデーション・影は持たない」）
- CSV のボタンが「CSV出力」と「CSVダウンロード」で混在、バリュエーション分析の実行ボタンだけ小さい（btn-sm）
- 撤去したロック演出の CSS（`.locked`）が残っていた
- おすすめタブの「★ 業種別OLS を先に実行すると…」が、夜間が計算済みのときも常に出ていた

テンプレートと静的 JS をファイルとして読むだけで、アプリも DB も起動しない。
"""
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYSIS_HTML = os.path.join(BASE_DIR, "templates", "analysis.html")
ANALYSIS_JS = os.path.join(BASE_DIR, "static", "js", "analysis.js")

# DESIGN.md「Shapes」の段: 3〜4px（札）・6px（ボタン・入力欄）・8px（囲み・トースト・吹き出し）・
# 10px（カード）・10〜12px（丸い札）・円（50%）。0 は片側だけ丸める棒グラフの角に使う
RADIUS_SCALE = {"0", "3px", "4px", "6px", "8px", "10px", "12px", "50%"}
_RADIUS_RE = re.compile(r"border-radius:\s*([^;\"'}]+)")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_radii_stay_on_the_design_scale():
    off = []
    for path in (ANALYSIS_HTML, ANALYSIS_JS):
        text = _read(path)
        found = _RADIUS_RE.findall(text)
        assert found, f"{os.path.basename(path)}: border-radius を拾えない（検出の正規表現が実体とずれた）"
        for value in found:
            parts = value.strip().split()
            if any(p not in RADIUS_SCALE for p in parts):
                off.append(f"{os.path.basename(path)}: border-radius:{value.strip()}")
    assert not off, "DESIGN.md「Shapes」の段から選ぶ（新しい丸みを足さない）:\n" + "\n".join(off)


def test_radius_detector_catches_the_old_values():
    # 測定器の較正: #1177 で直した値を必ず拾い、段の外と判定する
    for old in ("border-radius:7px", "border-radius:999px;", "border-radius:2px;"):
        value = _RADIUS_RE.search(old).group(1).strip()
        assert value not in RADIUS_SCALE


def test_analysis_page_has_no_gradients():
    assert "gradient(" not in _read(ANALYSIS_HTML)


def test_csv_buttons_share_one_label():
    labels = re.findall(r'data-click="exportCSV"[^>]*>([^<]+)</button>', _read(ANALYSIS_HTML))
    assert labels, "CSV のボタンを拾えない"
    assert set(labels) == {"CSV出力"}, labels


def test_gap_run_button_is_full_size():
    tag = re.search(r'<button[^>]*id="btn-gap-analysis"[^>]*>', _read(ANALYSIS_HTML)).group(0)
    assert "btn-sm" not in tag, "ほかの分析の実行ボタンと同じ大きさにする（375px でタップ領域が小さい）"


def test_no_leftover_lock_styles():
    assert ".locked" not in _read(ANALYSIS_HTML)
    assert "'locked'" not in _read(ANALYSIS_JS)


def test_gap_hint_shows_only_without_regression_results():
    html = _read(ANALYSIS_HTML)
    assert re.search(r'<span id="rec-gap-hint" class="hidden">', html), "既定で隠しておく（計算済みが普通）"
    js = _read(ANALYSIS_JS)
    start = js.index("function refreshGapAvailability(")
    body = js[start:js.index("\n}\n", start)]
    assert "_updateGapHint(available)" in body, "鮮度バーと同じ判定で出し分ける"
