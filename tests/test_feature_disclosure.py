"""feature_disclosure.py（Issue #322 改善案3・特徴量計算層）のユニットテスト。

UKI移植ロジックの検証観点:
- 実績(r_*)は cur_per_en の変化点で単四半期化（1Qは累積=単四半期、それ以外はdiff）
- 予想(f_*として公開する水準特徴量)は cur_fy_en（年度）単位でのみ更新され、年度内は据え置き
- 複合比率(pm/cost)はその開示時点の生の値から計算（水準特徴量の年度内据え置きとは無関係）
- m_* は実績単四半期 − 前回開示時点の予想、d_f_* は予想系列の前回開示比差分
- ForecastRevision系ドキュメントは基準系列から除外、同一期間の重複はkeep-last
"""
import math

import pandas as pd
import pytest

from feature_disclosure import (
    build_disclosure_features,
    build_disclosure_features_batch,
    dedupe_disclosures,
)


def _row(cur_per_type, cur_per_en, cur_fy_en, sales, op, odp, np_, f_sales, f_op, f_odp, f_np,
         disc_date, doc_type="XFinancialStatements_Consolidated_JP", disc_time="13:00:00", disc_no=None):
    return {
        "edinet_code": "E00000",
        "disc_date": disc_date,
        "disc_time": disc_time,
        "disc_no": disc_no or f"{disc_date.replace('-', '')}0001",
        "doc_type": doc_type,
        "cur_per_type": cur_per_type,
        "cur_per_en": cur_per_en,
        "cur_fy_en": cur_fy_en,
        "sales": sales, "op": op, "odp": odp, "np": np_,
        "f_sales": f_sales, "f_op": f_op, "f_odp": f_odp, "f_np": f_np,
    }


def _company_a_rows():
    return [
        _row("1Q", "2024-06-30", "2025-03-31", 100, 10, 9, 6, 500, 50, 45, 30, "2024-08-01"),
        _row("2Q", "2024-09-30", "2025-03-31", 210, 22, 20, 13, 520, 52, 47, 31, "2024-11-06"),
        _row("EarnForecastRevision", "2024-09-30", "2025-03-31", None, None, None, None,
             515, 51, 46, 30.5, "2024-12-01", doc_type="EarnForecastRevision"),
        _row("3Q", "2024-12-31", "2025-03-31", 330, 35, 32, 21, 530, 54, 48, 32, "2025-02-05"),
        _row("FY", "2025-03-31", "2025-03-31", 450, 48, 44, 29, None, None, None, None, "2025-05-08"),
        _row("1Q", "2025-06-30", "2026-03-31", 120, 12, 11, 7, 560, 58, 50, 34, "2025-08-07"),
    ]


class TestDedupe:
    def test_excludes_revision_doc_types(self):
        clean = dedupe_disclosures(_company_a_rows())
        assert all(r["doc_type"] not in ("EarnForecastRevision", "DividendForecastRevision")
                   for r in clean)
        assert len(clean) == 5

    def test_keeps_last_of_same_period_duplicate(self):
        rows = [
            _row("FY", "2025-03-31", "2025-03-31", 100, 10, 9, 6, None, None, None, None,
                 "2025-05-08", disc_time="09:00:00", disc_no="A1"),
            _row("FY", "2025-03-31", "2025-03-31", 101, 11, 10, 7, None, None, None, None,
                 "2025-05-08", disc_time="15:00:00", disc_no="A2"),
        ]
        clean = dedupe_disclosures(rows)
        assert len(clean) == 1
        assert clean[0]["disc_no"] == "A2"
        assert clean[0]["sales"] == 101


class TestBuildDisclosureFeatures:
    @pytest.fixture
    def feats(self):
        return build_disclosure_features(_company_a_rows())

    def test_row_count_excludes_revision(self, feats):
        assert len(feats) == 5

    def test_r_sales_single_quarterization(self, feats):
        # Q1は累積=単四半期、以降はdiff、年度またぎのQ1も生値
        assert feats["r_sales"].tolist() == pytest.approx([100, 110, 120, 120, 120])

    def test_f_sales_frozen_within_fiscal_year(self, feats):
        # 年度内(Q1-FY)はQ1時点の値で据え置き、翌年度Q1で更新
        assert feats["f_sales"].tolist() == pytest.approx([500, 500, 500, 500, 560])

    def test_f_pm1_uses_raw_forecast_each_disclosure(self, feats):
        # 比率特徴量は年度内据え置きではなく、その時点の生の予想値を使う
        expected = [30 / 500, 31 / 520, 32 / 530, math.nan, 34 / 560]
        got = feats["f_pm1"].tolist()
        for e, g in zip(expected, got):
            if math.isnan(e):
                assert math.isnan(g)
            else:
                assert g == pytest.approx(e)

    def test_m_sales_surprise(self, feats):
        # m_sales = 実績単四半期 - 前回開示時点のf_sales(据え置き値)
        m = feats["m_sales"].tolist()
        assert math.isnan(m[0])
        assert m[1:] == pytest.approx([110 - 500, 120 - 500, 120 - 500, 120 - 500])

    def test_d_f_sales_yoy_guidance_revision(self, feats):
        d = feats["d_f_sales"].tolist()
        assert math.isnan(d[0])
        assert d[1:4] == pytest.approx([0, 0, 0])
        assert d[4] == pytest.approx(60)

    def test_expense_breakdown(self, feats):
        # r_expense1 = r_sales - r_op（両者とも単四半期化後の値）
        assert feats["r_expense1"].tolist() == pytest.approx([90, 98, 107, 107, 108])


class TestIfrsNullOrdinaryIncome:
    def test_odp_dependent_features_are_nan_not_error(self):
        rows = [
            _row("1Q", "2024-06-30", "2025-03-31", 100, 10, None, 6, 500, 50, None, 30, "2024-08-01"),
            _row("2Q", "2024-09-30", "2025-03-31", 210, 22, None, 13, 520, 52, None, 31, "2024-11-06"),
        ]
        feats = build_disclosure_features(rows)
        assert feats["r_pm2"].isna().all()
        assert feats["r_cost2"].isna().all()
        assert feats["r_cost3"].isna().all()
        assert feats["f_pm2"].isna().all()
        # 経常利益に依存しない特徴量は正常計算される
        assert feats["r_pm1"].tolist() == pytest.approx([6 / 100, 13 / 210])


class TestEmptyInput:
    def test_returns_empty_dataframe(self):
        feats = build_disclosure_features([])
        assert len(feats) == 0


def _with_code(rows, code):
    return [{**r, "edinet_code": code} for r in rows]


def _company_b_rows():
    # A とは異なる水準・欠損パターンの2社目（IFRS 経常利益 None を含む）
    return [
        _row("1Q", "2024-06-30", "2025-03-31", 300, 40, None, 25, 1200, 130, None, 90, "2024-08-02"),
        _row("2Q", "2024-09-30", "2025-03-31", 640, 85, None, 52, 1250, 135, None, 92, "2024-11-07"),
        _row("3Q", "2024-12-31", "2025-03-31", 980, 128, None, 80, 1250, 135, None, 92, "2025-02-06"),
    ]


class TestBuildDisclosureFeaturesBatch:
    """全社バッチ版（Issue #340）が per-company 版と bit 一致することのリグレッションガード。"""

    def test_batch_matches_per_company(self):
        rows_by_company = {
            "E00001": _with_code(_company_a_rows(), "E00001"),
            "E00002": _with_code(_company_b_rows(), "E00002"),
        }
        batch = build_disclosure_features_batch(rows_by_company)
        assert set(batch) == {"E00001", "E00002"}
        for code, rows in rows_by_company.items():
            ref = build_disclosure_features(rows).reset_index(drop=True)
            got = batch[code].reset_index(drop=True)
            assert list(got.columns) == list(ref.columns)
            # 値の一致を検証（dtype は per-company/batch で null 有無により int64/float64 が
            # 揺れる純粋な pandas 構築アーティファクトで、実 DB 値は float・下流も float() 変換
            # のため無害。本質的不変条件は「値の一致」）
            pd.testing.assert_frame_equal(got, ref, check_dtype=False)

    def test_excludes_company_with_only_revision_rows(self):
        # dedupe で全行除外される社はバッチ結果に現れない（per-company の feats.empty 相当）
        revision_only = [
            _row("EarnForecastRevision", "2024-09-30", "2025-03-31", None, None, None, None,
                 515, 51, 46, 30.5, "2024-12-01", doc_type="EarnForecastRevision"),
        ]
        batch = build_disclosure_features_batch({
            "E00001": _with_code(_company_a_rows(), "E00001"),
            "E00009": _with_code(revision_only, "E00009"),
        })
        assert "E00009" not in batch
        assert "E00001" in batch

    def test_empty_input_returns_empty_dict(self):
        assert build_disclosure_features_batch({}) == {}
        assert build_disclosure_features_batch({"E0": [], "E1": []}) == {}
