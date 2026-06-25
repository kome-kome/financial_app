"""tests/test_macro_risk_return.py — M-1 Phase B: MacroRiskReturnPlugin"""
import asyncio
import math
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from collections import defaultdict

import pytest

from plugins.macro_risk_return import (
    MacroRiskReturnPlugin,
    _pareto_frontier,
    _realized_vol,
    _find_applicable_fin,
    _macro_from_cache,
)
from plugins.utils import coerce_params


# ── フィクスチャ ──────────────────────────────────────────────────────────────

def _make_price(trade_date: str, close_last: float):
    return SimpleNamespace(trade_date=trade_date, close_last=close_last)


def _make_fin(period_end_str: str, **kwargs):
    defaults = dict(
        edinet_code="E01234",
        sec_code="1234",
        company_name="テスト株式会社",
        industry="製造業",
        market="東証プライム",
        period_end=date.fromisoformat(period_end_str),
        per=15.0, pbr=1.2, roe=8.0, roa=4.0, equity_ratio=55.0,
        de_ratio=0.4, cf_ratio=9.0, eps_growth=5.0, op_growth=6.0,
        rd_intensity=2.0, da_intensity=3.0,
        z_op_margin=0.5, z_roe=0.3, z_cf_ratio=0.1,
        op_margin=8.0, net_margin=5.0, asset_turnover=0.8,
        div_yield=2.0, rev_growth=4.0, nc_ratio=0.1,
        bs_total_assets=1.0e5,  # R3 サイズ代理（総資産）
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _weekly_prices(ref: date, n_weeks: int = 60, base: float = 1000.0, drift: float = 0.001):
    rows = []
    for i in range(n_weeks, 0, -1):
        d = (ref - timedelta(days=i * 7)).isoformat()
        rows.append(_make_price(d, base * math.exp(drift * (n_weeks - i))))
    return rows


# ── _pareto_frontier ─────────────────────────────────────────────────────────

class TestParetoFrontier:

    def test_simple_domination(self):
        items = [
            {"edinet_code": "A", "r2": 0.1, "mu_raw": 0.2},  # 非劣
            {"edinet_code": "B", "r2": 0.3, "mu_raw": 0.1},  # A に劣後
        ]
        pareto = _pareto_frontier(items)
        assert "A" in pareto
        assert "B" not in pareto

    def test_no_domination(self):
        items = [
            {"edinet_code": "A", "r2": 0.1, "mu_raw": 0.1},
            {"edinet_code": "B", "r2": 0.3, "mu_raw": 0.3},
        ]
        pareto = _pareto_frontier(items)
        assert pareto == {"A", "B"}

    def test_single_item(self):
        items = [{"edinet_code": "A", "r2": 0.2, "mu_raw": 0.1}]
        assert _pareto_frontier(items) == {"A"}

    def test_three_items(self):
        # A(低リスク・中リターン)、B(中・高)、C(高リスク・低リターン)
        # A と B は互いを支配しない → 両方非劣。C は A に劣後。
        items = [
            {"edinet_code": "A", "r2": 0.1, "mu_raw": 0.2},  # 非劣
            {"edinet_code": "B", "r2": 0.3, "mu_raw": 0.4},  # 非劣
            {"edinet_code": "C", "r2": 0.3, "mu_raw": 0.2},  # B に劣後（同リスク・低リターン）
        ]
        pareto = _pareto_frontier(items)
        assert "A" in pareto
        assert "B" in pareto
        assert "C" not in pareto


# ── _realized_vol ─────────────────────────────────────────────────────────────

class TestRealizedVol:

    def test_basic(self):
        ref = date(2025, 6, 1)
        rows = _weekly_prices(ref, n_weeks=60, base=1000.0, drift=0.0)
        vol = _realized_vol(rows, ref.isoformat())
        assert vol is not None
        assert vol >= 0.0

    def test_no_future_leak(self):
        """ref_date より未来のデータを含めても結果が変わらない。"""
        ref = date(2025, 6, 1)
        rows_past = _weekly_prices(ref, n_weeks=60)
        future = _make_price("2026-01-01", 9999.0)
        rows_with_future = rows_past + [future]
        vol_past = _realized_vol(rows_past, ref.isoformat())
        vol_mixed = _realized_vol(rows_with_future, ref.isoformat())
        assert vol_past == vol_mixed

    def test_insufficient_data_returns_none(self):
        rows = [_make_price("2025-01-01", 1000.0)]
        assert _realized_vol(rows, "2025-06-01") is None


# ── _macro_from_cache ────────────────────────────────────────────────────────

class TestMacroFromCache:

    def _build_cache(self, ref: date) -> dict:
        cache: dict[str, dict[str, float]] = {}
        for i in range(400, 0, -1):
            d = (ref - timedelta(days=i)).isoformat()
            cache.setdefault("USDJPY", {})[d] = 130.0 if i > 330 else 150.0
        return cache

    def test_yoy_calculation(self):
        ref = date(2025, 6, 1)
        cache = self._build_cache(ref)
        result = _macro_from_cache(cache, ref.isoformat(), ["macro_usdjpy_yoy"])
        val = result.get("macro_usdjpy_yoy")
        assert val is not None
        assert abs(val - (150 - 130) / 130) < 0.01

    def test_empty_cache_returns_none(self):
        result = _macro_from_cache({}, "2025-06-01", ["macro_usdjpy_yoy"])
        assert result["macro_usdjpy_yoy"] is None

    def test_monthly_series_forward_fill(self):
        """月次系列で30日窓内に観測がなくても前月の値を forward-fill して None にならない。
        JP10Y_FRED シナリオ: 最終観測2025-05-01、ref=2025-06-15（5/1 は30日窓外）。"""
        ref = date(2025, 6, 15)
        cache: dict[str, dict[str, float]] = {}
        # 月次データを2020-01〜2025-05 で用意（zscore に必要な20件超・値に変動あり）
        d = date(2020, 1, 1)
        i = 0
        while d <= date(2025, 5, 1):
            cache.setdefault("JP10Y_FRED", {})[d.isoformat()] = 0.3 + i * 0.02
            m, y = d.month + 1, d.year
            if m > 12:
                m, y = 1, y + 1
            d = date(y, m, 1)
            i += 1

        result = _macro_from_cache(cache, ref.isoformat(), ["macro_jp10y_fred_zscore"])
        # 30日窓（5/16〜6/15）に観測なし → 5/1 を forward-fill → None にならないこと
        assert result["macro_jp10y_fred_zscore"] is not None


# ── params_schema + coerce_params ────────────────────────────────────────────

class TestParamsSchema:

    def setup_method(self):
        self.plugin = MacroRiskReturnPlugin()
        self.schema = self.plugin.params_schema()

    def test_schema_keys(self):
        for key in ["lambda_risk", "risk_axis", "fin_features", "top_n"]:
            assert key in self.schema

    def test_coerce_defaults(self):
        result = coerce_params(self.schema, {})
        assert result["lambda_risk"] == 1.0
        assert result["risk_axis"] == "r2"
        assert isinstance(result["fin_features"], list)
        assert result["top_n"] == 30

    def test_coerce_lambda_bounds(self):
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"lambda_risk": -0.1})
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"lambda_risk": 5.1})  # max=5.0 (#215 拡張)

    def test_coerce_invalid_risk_axis(self):
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"risk_axis": "r99"})

    def test_coerce_r1_r3_axis_invalid(self):
        """R1/R3 は効用軸から除外（#215 リスク軸再編）：選択肢として拒否される。"""
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"risk_axis": "r1"})
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"risk_axis": "r3"})

    def test_coerce_r_macro_axis_valid(self):
        """r_macro が有効な risk_axis 値として受理される（#215）。"""
        result = coerce_params(self.schema, {"risk_axis": "r_macro"})
        assert result["risk_axis"] == "r_macro"

    def test_default_fin_features_include_price_free(self):
        """既定の財務特徴量に価格フリーの roa・eps_growth が混合されている。"""
        result = coerce_params(self.schema, {})
        assert set(result["fin_features"]) == {
            "per", "pbr", "roe", "equity_ratio", "roa", "eps_growth"
        }

    def test_coerce_new_fin_features(self):
        """追加した価格フリー特徴量が fin_features 選択肢として受理される。"""
        result = coerce_params(
            self.schema,
            {"fin_features": ["roa", "cf_ratio", "de_ratio", "eps_growth", "op_growth"]},
        )
        assert result["fin_features"] == ["roa", "cf_ratio", "de_ratio", "eps_growth", "op_growth"]

    def test_coerce_invalid_fin_feature_rejected(self):
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"fin_features": ["not_a_feature"]})

    def test_macro_features_default(self):
        """macro_features の既定は現状の3系列。"""
        result = coerce_params(self.schema, {})
        assert result["macro_features"] == [
            "macro_usdjpy_yoy", "macro_sp500_yoy", "macro_us10y_zscore"
        ]

    def test_macro_features_accepts_nikkei(self):
        result = coerce_params(
            self.schema, {"macro_features": ["macro_nikkei225_yoy"]}
        )
        assert result["macro_features"] == ["macro_nikkei225_yoy"]

    def test_macro_features_invalid_rejected(self):
        # macro_data 蓄積の無い系列（JP10Y は収集失敗で未公開）は選択肢外として reject される。
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"macro_features": ["macro_jp10y_zscore"]})

    def test_macro_features_accepts_exposed_commodities_and_eurjpy(self):
        """#218 フェーズ1: 既収集の EURJPY・WTI・GOLD が選択可能・正しく結線される。"""
        from plugins.macro_risk_return import _MACRO_MAP
        assert _MACRO_MAP["macro_eurjpy_yoy"] == ("EURJPY", "yoy")
        assert _MACRO_MAP["macro_wti_yoy"] == ("WTI", "yoy")
        assert _MACRO_MAP["macro_gold_yoy"] == ("GOLD", "yoy")
        result = coerce_params(
            self.schema,
            {"macro_features": ["macro_eurjpy_yoy", "macro_wti_yoy", "macro_gold_yoy"]},
        )
        assert result["macro_features"] == [
            "macro_eurjpy_yoy", "macro_wti_yoy", "macro_gold_yoy"
        ]

    def test_macro_features_topix_excluded(self):
        """TOPIX は本番 macro_data に蓄積がない（収集失敗）ため選択肢から除外。"""
        from plugins.macro_risk_return import _MACRO_MAP
        assert "macro_topix_yoy" not in _MACRO_MAP
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"macro_features": ["macro_topix_yoy"]})

    def test_macro_map_has_nikkei(self):
        """_MACRO_MAP に NIKKEI225(YoY) が結線されている。"""
        from plugins.macro_risk_return import _MACRO_MAP
        assert _MACRO_MAP["macro_nikkei225_yoy"] == ("NIKKEI225", "yoy")

    def test_use_momentum_default_off(self):
        """use_momentum の既定は OFF（マクロ ON のままで walk-forward CV を成立させるため）。"""
        result = coerce_params(self.schema, {})
        assert result["use_momentum"] is False

    def test_use_momentum_can_enable(self):
        result = coerce_params(self.schema, {"use_momentum": True})
        assert result["use_momentum"] is True

    def test_new_price_free_fin_features_accepted(self):
        """追加した価格フリー特徴量（収益性・成長・財務健全性）が受理される。"""
        feats = ["op_margin", "net_margin", "asset_turnover", "rev_growth", "nc_ratio"]
        result = coerce_params(self.schema, {"fin_features": feats})
        assert result["fin_features"] == feats

    def test_div_yield_fin_feature_accepted(self):
        """配当利回り（バリュー因子）が fin_features 選択肢として受理される。"""
        result = coerce_params(self.schema, {"fin_features": ["div_yield"]})
        assert result["fin_features"] == ["div_yield"]


# ── _select_macro_features (BIC が有効特徴量を選ぶ) ──────────────────────────

class TestSelectMacroFeatures:

    def _make_samples(self, n: int = 200) -> dict[str, list]:
        """y = 2*x0 - x1 + noise の合成データ。BIC が x0, x1 を選びノイズ列を落とすことを確認。"""
        import random
        rng = random.Random(42)
        samples: dict[str, list] = defaultdict(list)
        for i in range(n):
            x0 = rng.gauss(0, 1)
            x1 = rng.gauss(0, 1)
            x_noise = rng.gauss(0, 1)
            y = 2.0 * x0 - 1.0 * x1 + rng.gauss(0, 0.3)
            ym = f"2024-{(i % 12) + 1:02d}"
            samples[ym].append(([x0, x1, x_noise], y))
        return dict(samples)

    def test_bic_selects_informative_features(self):
        plugin = MacroRiskReturnPlugin()
        samples = self._make_samples(300)
        selected = plugin._select_macro_features(
            samples, ["x0", "x1", "x_noise"], max_features=3
        )
        # x0, x1 は選ばれるはず（ノイズ列は BIC で落とされる可能性大）
        assert "x0" in selected
        assert "x1" in selected

    def test_bic_respects_max_features(self):
        plugin = MacroRiskReturnPlugin()
        samples = self._make_samples(300)
        feat_names = [f"f{i}" for i in range(10)]
        # 全列をランダムノイズにして max_features=2 で上限が守られることを確認
        import random
        rng = random.Random(0)
        fixed_samples: dict[str, list] = {"2024-01": [
            ([rng.gauss(0, 1) for _ in range(10)], rng.gauss(0, 1))
            for _ in range(50)
        ]}
        selected = plugin._select_macro_features(fixed_samples, feat_names, max_features=2)
        assert len(selected) <= 2

    def test_bic_empty_samples_returns_empty(self):
        plugin = MacroRiskReturnPlugin()
        result = plugin._select_macro_features({}, ["f0", "f1"])
        assert result == []


# ── R3: セクター×サイズ・バケット CV-RMSE ─────────────────────────────────────

class TestR3Buckets:

    def setup_method(self):
        self.plugin = MacroRiskReturnPlugin()

    def test_size_bucket(self):
        th = (50.0, 100.0)
        assert self.plugin._size_bucket(10, th) == "S"
        assert self.plugin._size_bucket(70, th) == "M"
        assert self.plugin._size_bucket(150, th) == "L"
        # 欠損・閾値なし・非正は None
        assert self.plugin._size_bucket(None, th) is None
        assert self.plugin._size_bucket(10, None) is None
        assert self.plugin._size_bucket(0, th) is None

    def test_bucket_rmse_by_sector_size(self):
        """セクター×サイズ三分位ごとに残差 RMSE が分離して算出される。"""
        residuals, metas = [], []
        for size, err in [(1.0, 0.1), (50.0, 0.2), (100.0, 0.3)]:
            for _ in range(6):
                residuals.append((0.0, err))   # (yhat, ytrue) → 残差 = err
                metas.append(("A", size))
        r3 = self.plugin._compute_r3_buckets({"2024-01": residuals}, {"2024-01": metas})

        assert r3["thresholds"] == (50.0, 100.0)  # size 1→S / 50→M / 100→L
        assert self.plugin._r3_for("A", 1.0, r3) == pytest.approx(0.1)
        assert self.plugin._r3_for("A", 50.0, r3) == pytest.approx(0.2)
        assert self.plugin._r3_for("A", 100.0, r3) == pytest.approx(0.3)

    def test_fallback_hierarchy(self):
        """小標本バケットは sector→global へフォールバックする。"""
        residuals, metas = [], []
        for _ in range(6):  # sector A: 十分な標本
            residuals.append((0.0, 0.1)); metas.append(("A", 10.0))
        for _ in range(2):  # sector Z: バケットもセクターも標本不足（<5）
            residuals.append((0.0, 0.5)); metas.append(("Z", 10.0))
        r3 = self.plugin._compute_r3_buckets({"2024-01": residuals}, {"2024-01": metas})

        assert self.plugin._r3_for("A", 10.0, r3) == pytest.approx(0.1)
        expected_global = math.sqrt((6 * 0.1 ** 2 + 2 * 0.5 ** 2) / 8)
        assert self.plugin._r3_for("Z", 10.0, r3) == pytest.approx(expected_global)

    def test_no_residuals_returns_none(self):
        """残差ゼロ件なら R3 は None（global も空）。"""
        r3 = self.plugin._compute_r3_buckets({}, {})
        assert r3["thresholds"] is None
        assert self.plugin._r3_for("A", 10.0, r3) is None


# ── execute（モック DB で統合確認）────────────────────────────────────────────

class TestExecuteIntegration:

    def _build_mock_db(self, ref: date = date(2025, 6, 1), n_weeks: int = 120):
        """合成データ（3社・既定120週・6決算）で execute が通ることを確認。
        n_weeks はモメンタム（過去履歴要件）テスト用に延伸できる。"""
        codes = ["E01234", "E02345", "E03456"]
        bases  = [1000.0,   2000.0,   500.0]

        # (edinet_code, trade_date, close_last) タプル列（3社分）
        price_tuples = [
            (ec,
             (ref - timedelta(days=(n_weeks - i) * 7)).isoformat(),
             base * math.exp(0.001 * i * (1 + j * 0.2)))
            for j, (ec, base) in enumerate(zip(codes, bases))
            for i in range(n_weeks)
        ]

        # 各社 6期分の財務データ
        fin_list = [
            _make_fin((ref - timedelta(days=365 * (5 - y))).isoformat(),
                      edinet_code=ec,
                      year=2020 + y,
                      per=15.0 + j * 2,
                      pbr=1.2 + j * 0.3,
                      roa=4.0 + j + y * 0.5,           # 価格フリー特徴量に横断/時系列の変動を持たせる
                      eps_growth=5.0 + j * 2 + y,
                      bs_total_assets=(j + 1) * 1.0e5)  # 社ごとに S/M/L サイズを変える
            for j, ec in enumerate(codes)
            for y in range(6)
        ]

        companies = [
            SimpleNamespace(edinet_code=ec, sec_code=f"{1234 + j * 1000}",
                            name=f"テスト株式会社{j+1}", industry="製造業")
            for j, ec in enumerate(codes)
        ]

        # 呼び出し順ベースのモック（column-level query は位置で識別できないため）
        call_count = [0]

        def _query_side_effect(*args):
            mock_q = MagicMock()
            mock_q.filter.return_value = mock_q
            mock_q.filter_by.return_value = mock_q
            mock_q.order_by.return_value = mock_q
            mock_q.first.return_value = None
            call_count[0] += 1
            n = call_count[0]
            if n == 1:      # StockPriceWeekly 列クエリ
                mock_q.all.return_value = price_tuples
            elif n == 2:    # FinancialMetric
                mock_q.all.return_value = fin_list
            else:           # Company
                mock_q.all.return_value = companies
            return mock_q

        db = MagicMock()
        db.query.side_effect = _query_side_effect
        return db

    def test_execute_minimal(self):
        plugin = MacroRiskReturnPlugin()
        schema = plugin.params_schema()
        params = coerce_params(schema, {"use_macro": False})

        db = self._build_mock_db()
        result = asyncio.run(plugin.execute(params, db))

        assert "results" in result
        assert "cv_metrics" in result
        assert "selected_features" in result
        assert isinstance(result["results"], list)

    def test_execute_returns_raw_risk_return_fields(self):
        """サーバーは全社の raw 値（mu_raw/r1/r2/r3）を返す。
        効用 U・パレート・top_n スライスはクライアント側の後処理へ移譲したため返さない。"""
        plugin = MacroRiskReturnPlugin()
        schema = plugin.params_schema()
        params = coerce_params(schema, {"use_macro": False, "top_n": 5})

        db = self._build_mock_db()
        result = asyncio.run(plugin.execute(params, db))

        assert result["risk_axis"] == "r2"
        # λ・top_n はクライアント初期表示シードとしてエコーされる
        assert result["lambda_risk"] == params["lambda_risk"]
        assert result["top_n"] == 5
        for item in result["results"]:
            assert "edinet_code" in item
            assert "mu_raw" in item
            assert "r1" in item
            assert "r2" in item
            assert "r3" in item  # R3 リスク指標（float or None）
            # 後処理（U・パレート）はサーバーから除去された
            assert "utility" not in item
            assert "is_pareto" not in item

    def test_execute_returns_all_companies_not_sliced(self):
        """top_n はサーバーでスライスしない（クライアントが U で並べ替え・上位抽出する）。"""
        plugin = MacroRiskReturnPlugin()
        schema = plugin.params_schema()
        res_small = asyncio.run(plugin.execute(
            coerce_params(schema, {"use_macro": False, "top_n": 5}), self._build_mock_db()))
        res_large = asyncio.run(plugin.execute(
            coerce_params(schema, {"use_macro": False, "top_n": 100}), self._build_mock_db()))
        # top_n を変えても返却件数は同じ（= 全社）
        assert len(res_small["results"]) == len(res_large["results"])
        assert len(res_small["results"]) == res_small["n_companies"]

    def test_execute_r3_in_results(self):
        """R3 は各銘柄の raw 値（足切りゲート用）として結果に付与される（#215 ゲート降格）。"""
        plugin = MacroRiskReturnPlugin()
        schema = plugin.params_schema()
        params = coerce_params(schema, {"use_macro": False, "top_n": 5})

        db = self._build_mock_db()
        result = asyncio.run(plugin.execute(params, db))

        for item in result["results"]:
            assert "r3" in item
            assert "mu_raw" in item

    def test_execute_r_macro_in_results(self):
        """全社 results に r_macro キーが含まれる（#215）。macro_beta 未蓄積なら None。"""
        plugin = MacroRiskReturnPlugin()
        schema = plugin.params_schema()
        params = coerce_params(schema, {"use_macro": False, "top_n": 5, "risk_axis": "r_macro"})

        db = self._build_mock_db()
        result = asyncio.run(plugin.execute(params, db))

        assert result["risk_axis"] == "r_macro"
        for item in result["results"]:
            assert "r_macro" in item  # macro_beta 未蓄積 → None だが key は必ず存在

    def test_execute_returns_feature_coefs(self):
        """execute は selected_features と整合する標準化係数 feature_coefs を返す。"""
        plugin = MacroRiskReturnPlugin()
        schema = plugin.params_schema()
        params = coerce_params(schema, {"use_macro": False, "top_n": 5})

        result = asyncio.run(plugin.execute(params, self._build_mock_db()))

        assert "feature_coefs" in result
        coefs = result["feature_coefs"]
        assert isinstance(coefs, dict)
        # 係数のキー集合は選択特徴量と一致し、値は float
        assert set(coefs.keys()) == set(result["selected_features"])
        assert len(coefs) == len(result["selected_features"])
        assert all(isinstance(v, float) for v in coefs.values())

    def test_execute_with_momentum(self):
        """use_momentum=True で momentum_12m1 が候補に入り execute が通る。
        モメンタムは過去履歴を要するため週次株価を延伸（n_weeks=200）して検証する。"""
        plugin = MacroRiskReturnPlugin()
        schema = plugin.params_schema()
        params = coerce_params(schema, {"use_macro": False, "use_momentum": True})

        db = self._build_mock_db(n_weeks=200)
        result = asyncio.run(plugin.execute(params, db))

        assert "results" in result
        assert "cv_metrics" in result
        assert isinstance(result["results"], list)

    def test_momentum_independent_of_macro(self):
        """momentum は use_macro と独立。use_macro=False・use_momentum=True で
        momentum 特徴量が候補生成され、サンプルが組める（従来は use_macro 連動だった）。"""
        plugin = MacroRiskReturnPlugin()
        db = self._build_mock_db(n_weeks=200)
        prices_by_co, fin_by_co, companies = plugin._load_data(db)
        # macro_names=[]（use_macro 相当 OFF）でも use_momentum=True なら momentum が候補に入る
        _samples, _meta, _snaps, all_feat_names = plugin._build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            ["per", "pbr", "roa"], [], True, 12, 0.5,
        )
        assert "momentum_12m1" in all_feat_names

    def test_no_momentum_when_disabled(self):
        """use_momentum=False なら macro 有無に関わらず momentum 特徴量は候補に入らない。"""
        plugin = MacroRiskReturnPlugin()
        db = self._build_mock_db(n_weeks=200)
        prices_by_co, fin_by_co, companies = plugin._load_data(db)
        _samples, _meta, _snaps, all_feat_names = plugin._build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            ["per", "pbr", "roa"], [], False, 12, 0.5,
        )
        assert "momentum_12m1" not in all_feat_names
