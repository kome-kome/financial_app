"""収集画面（collection.js）の単位付き表示（% ・億・円・倍）の静的検証（#906・#911）。

比率（営業利益率・ROE・自己資本比率 等）・1株あたりの値（EPS・BPS）・倍率（PER・PBR）は
VIEW が空欄（null）を返すことがある。文字列連結で単位を付けると、空欄が「null%」、
`||'-'` で逃がすと空欄と 0 がどちらも「-%」「-円」「-倍」になる。
金額は `null/1e6` が 0 になるので、空欄が「0億」と出る。
どれも画面は壊れず、失敗として現れない（#896 で営業利益が空欄の行が増えて目に付いた）。

表示は `fmtPct`・`fmtOku`・`fmtYen`・`fmtX`（空欄・NaN は「—」）へ集める。
億への換算は `common.js` の `toOku`（円→億）・`mnToOku`（百万円→億＝時価総額）だけに置く。
収集画面は両方を /1e6 していたため、円の列が 100 倍、時価総額が 1 万分の 1 で出ていた（#911）。
JS を実行する仕組みが無いので、ファイルを文字列として読んで照合する。
"""
import os
import re

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE_DIR, "static", "js")
COLLECTION_JS = os.path.join(JS_DIR, "collection.js")

# 空欄を単位付きの文字列にしてしまう書き方。進捗バーの `pct+'%'` は値が必ず入るので当たらない。
# キーはテスト ID（英字）、値は（説明, 型）。
_BROKEN_UNIT_PATTERNS = {
    "field-plus-pct": ("r.<区分>.<項目>+'%'（空欄が「null%」）",
                       re.compile(r"\br\.\w+\.\w+\s*\+\s*'%'")),
    "or-dash-plus-pct": ("(x||'-')+'%'（空欄も 0 も「-%」）",
                         re.compile(r"\|\|\s*'-'\s*\)\s*\+\s*'%'")),
    "or-dash-template-pct": ("${x||'-'}%（空欄も 0 も「-%」）",
                             re.compile(r"\|\|\s*'-'\s*\}%")),
    "or-dash-plus-unit": ("(x||'-')+'円'|'倍'|'億'（空欄も 0 も「-円」「-倍」）",
                          re.compile(r"\|\|\s*'-'\s*\)\s*\+\s*'(?:円|倍|億)'")),
    "or-zero-div": ("(x||0)/1e<n>（空欄が「0億」）",
                    re.compile(r"\|\|\s*0\s*\)\s*/\s*1e\d")),
}

# 空欄を「—」で出す表示関数と、値があるときに付ける単位。
_UNIT_FORMATTERS = {
    "fmtPct": "'%'",
    "fmtOku": "'億'",
    "fmtYen": "'円'",
    "fmtX": "'倍'",
}

# 億への換算。common.js にだけ置き、ページ側では再宣言しない。
# `const OKU` を二重に宣言すると、後から読んだページの JS 全体が SyntaxError で止まる。
_OKU_DEFINITIONS = {
    "OKU": re.compile(r"\bconst\s+OKU\b"),
    "toOku": re.compile(r"\bfunction\s+toOku\s*\("),
    "mnToOku": re.compile(r"\bfunction\s+mnToOku\s*\("),
}


def _read(name):
    with open(os.path.join(JS_DIR, name), encoding="utf-8") as f:
        return f.read()


def _hits(pattern, source):
    return [
        f"L{source.count(chr(10), 0, m.start()) + 1}: {m.group(0)}"
        for m in pattern.finditer(source)
    ]


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


@pytest.mark.parametrize("name", list(_UNIT_FORMATTERS))
def test_unit_formatters_show_blank_as_dash(source, name):
    m = re.search(rf"function {name}\(v\)\{{(.*?)\}}\n", source)
    assert m, f"collection.js に {name}(v) が無い"
    body = m.group(1)
    assert "v==null" in body and "isNaN(v)" in body, f"{name} が空欄・NaN を判定していない"
    assert "'—'" in body, f"{name} が空欄を「—」で出していない"
    assert _UNIT_FORMATTERS[name] in body, f"{name} が単位 {_UNIT_FORMATTERS[name]} を付けていない"


@pytest.mark.parametrize("key", list(_BROKEN_UNIT_PATTERNS))
def test_no_unit_concatenation_of_nullable_values(source, key):
    label, pattern = _BROKEN_UNIT_PATTERNS[key]
    hits = _hits(pattern, source)
    assert not hits, f"{label} が残っている（fmtPct・fmtOku・fmtYen・fmtX を使う）: {hits}"


def test_no_hand_written_oku_divisor(source):
    """億への換算を書き写さない。円の列と百万円の列で割る数が違い、収集画面は両方を誤った。"""
    hits = _hits(re.compile(r"/\s*1e\d+"), source)
    assert not hits, f"割り算で単位を換算している（common.js の toOku / mnToOku を使う）: {hits}"


def test_market_cap_goes_through_mn_to_oku(source):
    """時価総額だけは百万円で届く。toOku（円→億）へ通すと 1 万分の 1 になる。"""
    bad = [
        f"L{source.count(chr(10), 0, m.start()) + 1}"
        for m in re.finditer(r"\.market_cap\b", source)
        if not re.search(r"mnToOku\(\s*[\w?.]*$", source[max(0, m.start() - 40):m.start()])
    ]
    assert not bad, f"market_cap を mnToOku を通さずに使っている: {bad}"


def test_bs_row_amounts_are_converted_to_oku(source):
    """bsRow の金額は億の値を受け取る。生の値（円・百万円）を渡すと桁がずれる。"""
    # 先読みの前の `\s*` が 0 文字へ戻ると空白で先読みを素通りするので、空白も先読みで弾く。
    hits = _hits(re.compile(r"bsRow\('[^']*',\s*(?!\s|toOku\(|mnToOku\(|null\b)[^,)]+"), source)
    assert not hits, f"bsRow に換算していない金額を渡している（toOku / mnToOku を通す）: {hits}"


def test_oku_conversion_lives_in_common_js():
    common = _read("common.js")
    assert re.search(r"\bconst\s+OKU\s*=\s*1e8\s*;", common), "common.js に OKU = 1e8 が無い"
    m = re.search(r"function toOku\(v\)\{(.*?)\}", common)
    assert m and "v==null" in m.group(1) and "/OKU" in m.group(1), "toOku が空欄を保ったまま円を億にしていない"
    m = re.search(r"function mnToOku\(v\)\{(.*?)\}", common)
    assert m and "v==null" in m.group(1) and "/100" in m.group(1), "mnToOku が空欄を保ったまま百万円を億にしていない"


@pytest.mark.parametrize("page", ["collection.js", "company.js"])
def test_pages_do_not_redeclare_oku_conversion(page):
    src = _read(page)
    redeclared = [name for name, pat in _OKU_DEFINITIONS.items() if pat.search(src)]
    assert not redeclared, f"{page} が common.js の換算を再宣言している: {redeclared}"
