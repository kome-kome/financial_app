"""parse_xbrl_csv / parse_raw_rows の棚卸資産サブ項目フォールバックテスト。

JGAAP ファイルの ~92% は aggregate Inventories 要素を出さず、
MerchandiseAndFinishedGoods / WorkInProcess / RawMaterialsAndSupplies 等の
サブ項目のみ出力する。_inventory_fallback がこれらを合計して bs["inventory"] に設定する
ことを検証する。
"""
import pandas as pd
import pytest

from collector import parse_xbrl_csv, parse_raw_rows


# ── ヘルパー ──────────────────────────────────────────────────────────────

def _make_df(rows: list[dict]) -> pd.DataFrame:
    """要素ID / コンテキストID / 値 の最小 CSV DataFrame を生成する（日本語列名）。"""
    return pd.DataFrame(
        [(r["element"], r.get("label", ""), r["context"], "0", "", "", "", "", r["value"])
         for r in rows],
        columns=["要素ID", "項目名", "コンテキストID", "相対年度", "アキュム・起点",
                 "時点・期間", "ユニットID", "単位", "値"],
    )


# ── parse_xbrl_csv テスト ─────────────────────────────────────────────────

def test_fallback_sums_sub_items_when_no_aggregate():
    """aggregate Inventories なし → サブ項目合計が bs["inventory"] に入る。"""
    df = _make_df([
        {"element": "jppfs_cor:MerchandiseAndFinishedGoods", "context": "CurrentYearInstant", "value": "30000"},
        {"element": "jppfs_cor:WorkInProcess",               "context": "CurrentYearInstant", "value": "4000"},
        {"element": "jppfs_cor:RawMaterialsAndSupplies",     "context": "CurrentYearInstant", "value": "5000"},
        {"element": "jppfs_cor:CurrentAssets",               "context": "CurrentYearInstant", "value": "100000"},
    ])
    result = parse_xbrl_csv(df, "E99999", "2025-03-31")
    assert result["bs"]["inventory"] == pytest.approx(39000)
    assert result["bs"]["current_assets"] == pytest.approx(100000)


def test_aggregate_takes_precedence_over_sub_items():
    """aggregate Inventories あり → サブ項目合計を無視して aggregate 値を使う。"""
    df = _make_df([
        {"element": "jppfs_cor:Inventories",                 "context": "CurrentYearInstant", "value": "6905"},
        {"element": "jppfs_cor:MerchandiseAndFinishedGoods", "context": "CurrentYearInstant", "value": "3083"},
        {"element": "jppfs_cor:WorkInProcess",               "context": "CurrentYearInstant", "value": "142"},
        {"element": "jppfs_cor:RawMaterials",                "context": "CurrentYearInstant", "value": "3679"},
    ])
    result = parse_xbrl_csv(df, "E99999", "2025-12-31")
    assert result["bs"]["inventory"] == pytest.approx(6905)


def test_no_double_count_combined_over_individual():
    """MerchandiseAndFinishedGoods と Merchandise の両方がある場合、前者のみ使う。"""
    df = _make_df([
        {"element": "jppfs_cor:MerchandiseAndFinishedGoods", "context": "CurrentYearInstant", "value": "5000"},
        {"element": "jppfs_cor:Merchandise",                 "context": "CurrentYearInstant", "value": "3000"},
        {"element": "jppfs_cor:FinishedGoods",               "context": "CurrentYearInstant", "value": "2000"},
        {"element": "jppfs_cor:WorkInProcess",               "context": "CurrentYearInstant", "value": "1000"},
    ])
    result = parse_xbrl_csv(df, "E99999", "2025-03-31")
    # MerchandiseAndFinishedGoods(5000) + WorkInProcess(1000) = 6000 (Merchandise/FinishedGoods は無視)
    assert result["bs"]["inventory"] == pytest.approx(6000)


def test_consolidated_preferred_over_nonconsolidated():
    """連結コンテキストのサブ項目が非連結より優先される。"""
    df = _make_df([
        {"element": "jppfs_cor:MerchandiseAndFinishedGoods",
         "context": "CurrentYearInstant_NonConsolidatedMember", "value": "1000"},
        {"element": "jppfs_cor:MerchandiseAndFinishedGoods",
         "context": "ConsolidatedCurrentYearInstant", "value": "9000"},
        {"element": "jppfs_cor:WorkInProcess",
         "context": "ConsolidatedCurrentYearInstant", "value": "500"},
    ])
    result = parse_xbrl_csv(df, "E99999", "2025-03-31")
    assert result["bs"]["inventory"] == pytest.approx(9500)


def test_prior_year_context_ignored():
    """Prior コンテキストはスキップ → 当期サブ項目がなければ inventory は None。"""
    df = _make_df([
        {"element": "jppfs_cor:MerchandiseAndFinishedGoods",
         "context": "Prior1YearInstant", "value": "9999"},
    ])
    result = parse_xbrl_csv(df, "E99999", "2025-03-31")
    assert result["bs"].get("inventory") is None


def test_empty_df_returns_none():
    result = parse_xbrl_csv(None, "E99999", "2025-03-31")
    assert result["bs"].get("inventory") is None


# ── parse_raw_rows テスト ─────────────────────────────────────────────────

def test_parse_raw_rows_fallback():
    """parse_raw_rows でもサブ項目フォールバックが機能する。"""
    rows = [
        {"element": "MerchandiseAndFinishedGoods", "context": "CurrentYearInstant", "value": "20000"},
        {"element": "WorkInProcess",               "context": "CurrentYearInstant", "value": "3000"},
        {"element": "RawMaterialsAndSupplies",     "context": "CurrentYearInstant", "value": "7000"},
    ]
    result = parse_raw_rows(rows)
    assert result["bs"]["inventory"] == pytest.approx(30000)
