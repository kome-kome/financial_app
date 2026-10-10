"""収集画面（collection.js）の % 表示の静的検証（#906）。

比率（営業利益率・ROE・自己資本比率 等）は VIEW が空欄（null）を返すことがある。
文字列連結で `%` を付けると、空欄が「null%」、`||'-'` で逃がすと空欄と 0 がどちらも「-%」になる。
どちらも画面は壊れず、失敗として現れない（#896 で営業利益が空欄の行が増えて目に付いた）。

% 表示は `fmtPct`（空欄・NaN は「—」、それ以外は小数第 2 位＝VIEW の丸めと同じ桁）へ集める。
JS を実行する仕組みが無いので、ファイルを文字列として読んで照合する。
"""
import os
import re

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTION_JS = os.path.join(BASE_DIR, "static", "js", "collection.js")

# 空欄を % 付きの文字列にしてしまう書き方。進捗バーの `pct+'%'` は値が必ず入るので当たらない。
# キーはテスト ID（英字）、値は（説明, 型）。
_BROKEN_PCT_PATTERNS = {
    "field-plus-pct": ("r.<区分>.<項目>+'%'（空欄が「null%」）",
                       re.compile(r"\br\.\w+\.\w+\s*\+\s*'%'")),
    "or-dash-plus-pct": ("(x||'-')+'%'（空欄も 0 も「-%」）",
                         re.compile(r"\|\|\s*'-'\s*\)\s*\+\s*'%'")),
    "or-dash-template-pct": ("${x||'-'}%（空欄も 0 も「-%」）",
                             re.compile(r"\|\|\s*'-'\s*\}%")),
}


@pytest.fixture(scope="module")
def source():
    with open(COLLECTION_JS, encoding="utf-8") as f:
        return f.read()


def test_fmt_pct_shows_blank_as_dash_and_keeps_two_decimals(source):
    m = re.search(r"function fmtPct\(v\)\{(.*?)\}\n", source)
    assert m, "collection.js に fmtPct(v) が無い"
    body = m.group(1)
    assert "v==null" in body and "isNaN(v)" in body, "空欄・NaN を判定していない"
    assert "'—'" in body, "空欄を「—」で出していない"
    assert "toFixed(2)" in body, "VIEW の丸め（小数第 2 位）と桁がそろっていない"


@pytest.mark.parametrize("key", list(_BROKEN_PCT_PATTERNS))
def test_no_percent_concatenation_of_nullable_values(source, key):
    label, pattern = _BROKEN_PCT_PATTERNS[key]
    hits = [
        f"L{source.count(chr(10), 0, m.start()) + 1}: {m.group(0)}"
        for m in pattern.finditer(source)
    ]
    assert not hits, f"{label} が残っている（fmtPct を使う）: {hits}"
