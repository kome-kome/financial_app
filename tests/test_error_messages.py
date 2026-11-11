"""画面の失敗の通知に、サーバの説明文だけを出すことの静的検証（#1175）。

どれも破っても画面は壊れず、失敗の通知の文面がずれるだけなので失敗として現れない:

- `apiFetch` が 404 を 'NOT_FOUND' に置き換えて理由の文を捨てていた
  （バリュエーション分析の「先に「業種別OLS」を実行してください」が消えていた）
- 404 以外の失敗は `{"detail": "…"}` の生の JSON が通知に出ていた
- バリュエーション分析は 404・429・500 のどれでも「先に業種別OLS分析を実行してください」を付け足していた
  （404 は入力の検証エラーにも使うので、原因と違う案内になる）
- バリュエーション分析の実行ボタンは押しても反応が見えず、二重に押しやすかった

テンプレートと静的 JS をファイルとして読むだけで、アプリも DB も起動しない。
"""
import glob
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE_DIR, "static", "js")
STATIC_JS = sorted(glob.glob(os.path.join(JS_DIR, "*.js")))


def _read(name):
    with open(os.path.join(JS_DIR, name), encoding="utf-8") as f:
        return f.read()


def _function_body(text, name):
    start = text.index(f"function {name}(")
    return text[start:text.index("\n}\n", start)]


def test_api_fetch_keeps_the_detail_and_the_status():
    body = _function_body(_read("common.js"), "apiFetch")
    assert "new Error('NOT_FOUND')" not in body, "404 を 'NOT_FOUND' に置き換えると理由の文が消える"
    assert "_detailText(await r.text())" in body, "失敗の本文から detail を取り出していない"
    assert "err.status = r.status" in body, "呼び出し側が status で見分けられない"


def test_error_text_helpers_live_only_in_common_js():
    """`_detailText` と `_errDetail` は common.js にだけ置く（複製は片方だけ直って食い違う）。"""
    common = _read("common.js")
    for fn in ("_detailText", "_errDetail"):
        assert f"function {fn}(" in common, f"common.js に {fn} が無い"
        dup = [os.path.basename(p) for p in STATIC_JS
               if os.path.basename(p) != "common.js" and f"function {fn}(" in _read(os.path.basename(p))]
        assert not dup, f"{fn} が common.js 以外にもある: {dup}"


def test_detail_text_handles_validation_error_lists():
    # FastAPI の入力検証（422）は detail が配列。msg だけを並べ、生の JSON を出さない
    body = _function_body(_read("common.js"), "_detailText")
    assert "Array.isArray(d)" in body and "x.msg" in body


def test_nobody_matches_the_old_not_found_sentinel():
    hits = [os.path.basename(p) for p in STATIC_JS if "=== 'NOT_FOUND'" in _read(os.path.basename(p))]
    assert not hits, f"apiFetch は 'NOT_FOUND' を投げなくなった。404 は e.status で見分ける: {hits}"


def test_analysis_notifications_show_the_detail_not_the_raw_message():
    js = _read("analysis.js")
    raw = [f"analysis.js:{js.count(chr(10), 0, m.start()) + 1}"
           for m in re.finditer(r"showNotif\([^;]*\be\.message\b", js)]
    assert not raw, f"e.message は生の JSON のことがある。_errDetail(e) を通す: {raw}"


def test_gap_analysis_adds_no_hint_of_its_own_and_shows_progress():
    body = _function_body(_read("analysis.js"), "runGapAnalysis")
    assert "先に業種別OLS" not in body, "案内はサーバの detail に任せる（404 は検証エラーにも使う）"
    assert "_errDetail(e)" in body
    assert "btn.disabled = true" in body and "spinner" in body, "実行中の表示が無い（二重に押しやすい）"
    finally_part = body[body.index("finally"):]
    assert "btn.disabled = false" in finally_part, "失敗してもボタンを戻す（finally で戻す）"
