"""売買の記録の画面の配線（#1222）。

分析画面の「保有を見直す」に置いた欄と、`static/js/analysis.js` の関数・API の対応を照合する。
片方だけ名前を変えても画面は壊れず、押しても何も起きないだけ＝失敗として現れない。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "templates" / "analysis.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "analysis.js").read_text(encoding="utf-8")

# 売り候補のタブの中身（次のタブの手前まで）
TAB = HTML[HTML.index('<div id="tab-sell_ranking"'):HTML.index("<!-- /tab-sell_ranking -->")]
# 売買の記録の JS の節（次の節の手前まで）
BLOCK = JS[JS.index("// ── 売買の記録と運用成績"):JS.index("const SELL_ACTION_STYLE")]


def test_every_handler_in_the_tab_is_defined():
    names = set(re.findall(r'data-(?:click|change)="(\w+)"', TAB)) | set(
        re.findall(r'data-click="(\w+)"', BLOCK))
    assert {"addLedgerEntry", "onLedgerSideChange", "fillHoldingsFromLedger", "voidLedgerEntry"} <= names
    missing = [n for n in sorted(names) if not re.search(rf"\bfunction {n}\b", JS)]
    assert not missing, f"analysis.js に無い関数を呼んでいる: {missing}"


def test_every_element_the_script_reads_exists_in_the_tab():
    ids = set(re.findall(r"getElementById\('([\w-]+)'\)", BLOCK))
    ids |= set(re.findall(r"'((?:ledger|pf)-[\w-]+)'", BLOCK))
    ids -= {"confirm-dialog"}
    missing = [i for i in sorted(ids) if f'id="{i}"' not in TAB]
    assert ids and not missing, f"テンプレートに無い id を読んでいる: {missing}"


def test_split_toggle_classes_exist_on_both_sides():
    for cls in ("ledger-trade-field", "ledger-split-field"):
        assert f".{cls}" in BLOCK and cls in TAB
    # 分割の欄は最初は隠れている（買いが既定）
    assert re.search(r'class="form-group ledger-split-field hidden"', TAB)


def test_script_calls_the_portfolio_endpoints():
    for path in ("/api/portfolio/ledger", "/api/portfolio/summary", "/void"):
        assert path in BLOCK


def test_portfolio_loads_when_the_tab_opens():
    show_tab = JS[JS.index("function showTab(t)"):JS.index("// ── 保存済みの結果")]
    assert "loadPortfolioOnce()" in show_tab


def test_void_asks_before_it_writes():
    """取消は元に戻せないので、送る前に画面内の確認を通す（ブラウザ標準の confirm は使わない）。"""
    void = BLOCK[BLOCK.index("async function voidLedgerEntry"):BLOCK.index("async function fillHoldingsFromLedger")]
    assert void.index("confirmDialog(") < void.index("apiFetch(")
    assert not re.search(r"(?<![\w.])confirm\(", BLOCK)
