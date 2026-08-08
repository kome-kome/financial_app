"""plugins/recommend.py のユニットテスト。

純粋: METRICS / PRESETS の整合性。
execute(): 重み付きスコアのランキング・カバレッジフィルタ・空DB・top_n（in-memory SQLite）。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import execute_plugin
from plugins.recommend import (
    METRICS, MU_SOURCE_OPTIONS, PRESETS, RUNTIME_METRICS, SELECT_COLS,
    STATISTICAL_PRESET_NAME, compute_momentum_z, compute_mu_z,
    get_dynamic_preset, plugin, resolve_weights,
)


# ── 純粋: 定数の整合性 ───────────────────────────────────────────────────────

class TestConstants:
    def test_metrics_unique_and_nonempty(self):
        assert len(METRICS) == 10
        assert len(set(METRICS)) == len(METRICS)

    def test_presets_reference_valid_metrics(self):
        # 各プリセットのウェイトキーは必ず METRICS に存在すること
        for name, weights in PRESETS.items():
            for metric in weights:
                assert metric in METRICS, f"{name} の {metric} が METRICS に無い"

    def test_presets_weights_are_numeric(self):
        for weights in PRESETS.values():
            for w in weights.values():
                assert isinstance(w, (int, float))

    def test_no_preset_carries_mu(self):
        """μ̂ の既定は OFF（Issue #423 子4・ADR-0030）。

        買い側 rank-IC と売り側 spread は順位が逆転しうる（ADR-0022）ため、売り側の
        既定 mu_source をそのまま買いの既定重みへ持ち込まない。既定へ入れるときは
        ADR-0028 の昇格ゲート（補正後αを通す実測）が必要＝このテストを外す前に ADR。
        """
        for name, weights in PRESETS.items():
            assert "mu" not in weights, f"{name} が既定で μ̂ を使っている"


# ── 転送列の契約（Issue #441）────────────────────────────────────────────────
#
# financial_metrics VIEW は97列あり、全列×全社（実測 4,430 行）の転送が
# /api/recommend・/api/morning の主コストだった（本番実測 37.9秒＝Render 30秒上限超）。
# SELECT_COLS は METRICS から導出するので、指標を足せば転送列も自動で追従する。

class TestSelectCols:
    def test_covers_every_view_backed_metric(self):
        # RUNTIME_METRICS（z_momentum=compute_momentum_z / mu=producer スコア）は
        # VIEW 列ではなく実行時計算なので転送対象外
        for m in METRICS:
            if m in RUNTIME_METRICS:
                assert m not in SELECT_COLS
            else:
                assert m in SELECT_COLS, f"{m} が SELECT_COLS に無い＝黙って None になる"

    def test_all_columns_exist_on_the_view_model(self):
        from database import FinancialMetric
        for c in SELECT_COLS:
            assert hasattr(FinancialMetric, c), c

    def test_is_a_strict_subset_of_the_view(self):
        from database import FinancialMetric
        all_cols = [c.key for c in FinancialMetric.__table__.columns]
        assert set(SELECT_COLS) < set(all_cols)
        assert len(SELECT_COLS) < len(all_cols) / 2   # 97列 → 約20列

    def test_no_duplicates(self):
        assert len(SELECT_COLS) == len(set(SELECT_COLS))


# ── resolve_weights() / get_dynamic_preset(): 統計的最適化プリセット（Issue #271）────

class TestResolveWeights:
    def test_static_preset_passthrough(self, db):
        assert resolve_weights(db, "バランス型") == PRESETS["バランス型"]
        assert resolve_weights(db, "成長重視") == PRESETS["成長重視"]

    def test_unknown_preset_falls_back_to_balanced(self, db):
        assert resolve_weights(db, "存在しないプリセット") == PRESETS["バランス型"]

    def test_statistical_preset_falls_back_when_unset(self, db):
        assert get_dynamic_preset(db) is None
        assert resolve_weights(db, STATISTICAL_PRESET_NAME) == PRESETS["バランス型"]

    def test_statistical_preset_resolves_from_db(self, db):
        from database import upsert_recommend_factor_premia
        upsert_recommend_factor_premia(db, "rfp_1", [
            {"run_id": "rfp_1", "factor_name": "z_roe", "mean_b": 0.15,
             "newey_west_se": 0.04, "t_stat": 3.75, "p_value": 0.001, "n_periods": 30},
            {"run_id": "rfp_1", "factor_name": "z_momentum", "mean_b": 0.08,
             "newey_west_se": 0.03, "t_stat": 2.6, "p_value": 0.01, "n_periods": 30},
        ])
        dynamic = get_dynamic_preset(db)
        assert dynamic == {"z_roe": 0.15, "z_momentum": 0.08}
        assert resolve_weights(db, STATISTICAL_PRESET_NAME) == dynamic

    def test_dynamic_preset_drops_factors_outside_metrics(self, db):
        """METRICS 外の factor は重みに採らない（Issue #441）。

        この重みは coerce_params を通らない経路（execute 内の resolve_weights）で使われる
        一方、読み取り列は METRICS から導出するため、範囲外キーが残ると値が取れず黙って
        カバレッジだけが下がる。
        """
        from database import upsert_recommend_factor_premia
        upsert_recommend_factor_premia(db, "rfp_2", [
            {"run_id": "rfp_2", "factor_name": "z_roe", "mean_b": 0.2,
             "newey_west_se": None, "t_stat": None, "p_value": None, "n_periods": 12},
            {"run_id": "rfp_2", "factor_name": "z_nc_ratio", "mean_b": 0.1,
             "newey_west_se": None, "t_stat": None, "p_value": None, "n_periods": 12},
        ])
        assert get_dynamic_preset(db) == {"z_roe": 0.2}

    def test_dynamic_preset_all_out_of_range_falls_back(self, db):
        from database import upsert_recommend_factor_premia
        upsert_recommend_factor_premia(db, "rfp_3", [
            {"run_id": "rfp_3", "factor_name": "z_nc_ratio", "mean_b": 0.1,
             "newey_west_se": None, "t_stat": None, "p_value": None, "n_periods": 12},
        ])
        assert get_dynamic_preset(db) is None
        assert resolve_weights(db, STATISTICAL_PRESET_NAME) == PRESETS["バランス型"]

    def test_params_schema_includes_statistical_preset_option(self):
        options = plugin.params_schema()["preset"]["options"]
        values = [o["value"] for o in options]
        assert STATISTICAL_PRESET_NAME in values


# ── compute_momentum_z(): 12-1モメンタムのZスコア化 ───────────────────────────

class TestComputeMomentumZ:
    AS_OF = "2024-01-08"
    OLD = "2023-01-02"      # long leg（12ヶ月前側）。as_of の1年以上前。
    RECENT = "2023-12-04"   # short leg（1ヶ月前側）。as_of の1ヶ月強前。

    def _four_companies(self, make_weekly):
        # momentum = ln(recent/old)。E00001が最高・E00004が最低になるよう設定。
        pairs = [
            ("E00001", 1000.0, 4000.0),   # ln(4)
            ("E00002", 1000.0, 2000.0),   # ln(2)
            ("E00003", 1000.0, 1000.0),   # ln(1) = 0
            ("E00004", 2000.0,  500.0),   # ln(0.25)
        ]
        rows = []
        for ec, old_close, recent_close in pairs:
            rows.append(make_weekly(edinet_code=ec, trade_date=self.OLD, close_last=old_close))
            rows.append(make_weekly(edinet_code=ec, trade_date=self.RECENT, close_last=recent_close))
        return rows

    def test_higher_momentum_gets_higher_z(self, db, make_weekly):
        import math
        from plugins.utils import winsorize, normalize_transform

        db.add_all(self._four_companies(make_weekly))
        db.commit()
        z = compute_momentum_z(db, ["E00001", "E00002", "E00003", "E00004"], self.AS_OF)

        assert z["E00001"] > z["E00002"] > z["E00003"] > z["E00004"]

        # 期待値は winsorize/normalize_transform（独立にテスト済み）を使って別途算出し、
        # compute_momentum_z がこれらを正しく配線しているかを検証する。
        raw = {"E00001": math.log(4000 / 1000), "E00002": math.log(2000 / 1000),
               "E00003": math.log(1000 / 1000), "E00004": math.log(500 / 2000)}
        wv, _, _ = winsorize(list(raw.values()))
        mean_ = sum(wv) / len(wv)
        var = sum((v - mean_) ** 2 for v in wv) / (len(wv) - 1)
        sd = var ** 0.5 or 1.0
        for ec, v in raw.items():
            assert z[ec] == pytest.approx(normalize_transform(v, mean_, sd, "zscore"))

    def test_insufficient_history_excluded(self, db, make_weekly):
        # E00005 は recent 側の1行のみ＝12ヶ月前のデータが無く momentum 算出不能
        db.add_all(self._four_companies(make_weekly))
        db.add(make_weekly(edinet_code="E00005", trade_date=self.RECENT, close_last=1500.0))
        db.commit()
        z = compute_momentum_z(
            db, ["E00001", "E00002", "E00003", "E00004", "E00005"], self.AS_OF)
        assert "E00005" not in z
        assert len(z) == 4

    def test_leak_safe_future_prices_ignored(self, db, make_weekly):
        db.add_all(self._four_companies(make_weekly))
        db.commit()
        baseline = compute_momentum_z(
            db, ["E00001", "E00002", "E00003", "E00004"], self.AS_OF)

        # as_of より後の極端な価格変動を追加してもリークしないこと
        db.add(make_weekly(edinet_code="E00001", trade_date="2024-06-03", close_last=1.0))
        db.commit()
        after = compute_momentum_z(
            db, ["E00001", "E00002", "E00003", "E00004"], self.AS_OF)
        assert after == baseline

    def test_fewer_than_four_valid_returns_empty(self, db, make_weekly):
        db.add_all(self._four_companies(make_weekly)[:4])  # E00001・E00002 の2社分のみ
        db.commit()
        assert compute_momentum_z(db, ["E00001", "E00002"], self.AS_OF) == {}

    def test_no_codes_returns_empty(self, db):
        assert compute_momentum_z(db, [], self.AS_OF) == {}

    # ── ロード方式（下限日付＋社数チャンク・Issue #418）─────────────────────
    #
    # 下限なし全期間ロードは本番 stock_price_weekly（127万行）で pooler の
    # statement_timeout を踏むため、as_of - _MOMENTUM_LOOKBACK_DAYS の下限と
    # _MOMENTUM_CODE_BATCH 社チャンクを入れている。ここでは「分割しても結果が変わらない」
    # ことと「下限が get_momentum_return の参照範囲（12ヶ月）を削っていない」ことを固定する。

    def test_chunked_load_matches_unchunked(self, db, make_weekly, monkeypatch):
        import plugins.recommend as recommend_mod

        db.add_all(self._four_companies(make_weekly))
        db.commit()
        codes = ["E00001", "E00002", "E00003", "E00004"]
        baseline = compute_momentum_z(db, codes, self.AS_OF)

        # 1社ずつ・2社ずつに刻んでも同一結果（チャンク境界で行が落ちない）
        for batch in (1, 2, 3):
            monkeypatch.setattr(recommend_mod, "_MOMENTUM_CODE_BATCH", batch)
            assert compute_momentum_z(db, codes, self.AS_OF) == baseline

    def test_lookback_window_covers_twelve_month_leg(self, db, make_weekly):
        # OLD は as_of の371日前＝long leg（as_of-360日以前の最終バー）として必要。
        # 下限がこれを削っていないことを、OLD 由来の値が出ていることで確認する。
        from datetime import date
        from plugins.recommend import _MOMENTUM_LOOKBACK_DAYS

        # long leg（12ヶ月＝360日前以前の最終バー）が窓に収まる余裕があること
        assert (date.fromisoformat(self.AS_OF)
                - date.fromisoformat(self.OLD)).days < _MOMENTUM_LOOKBACK_DAYS
        assert _MOMENTUM_LOOKBACK_DAYS >= 12 * 30 + 30

        db.add_all(self._four_companies(make_weekly))
        db.commit()
        z = compute_momentum_z(db, ["E00001", "E00002", "E00003", "E00004"], self.AS_OF)
        # ln(4) が最大・ln(0.25) が最小＝OLD の終値が long leg に使われている
        assert len(z) == 4
        assert z["E00001"] > 0 > z["E00004"]

    def test_rows_older_than_lookback_are_not_loaded(self, db, make_weekly):
        # 下限より古い行しか持たない銘柄は算出対象外になる（唯一の挙動変化点）。
        # 下限なし実装では long/short 両脚ともその古い行を拾って momentum を返していた。
        from datetime import date, timedelta
        from plugins.recommend import _MOMENTUM_LOOKBACK_DAYS

        ref = date.fromisoformat(self.AS_OF)
        ancient_1 = (ref - timedelta(days=_MOMENTUM_LOOKBACK_DAYS + 200)).isoformat()
        ancient_2 = (ref - timedelta(days=_MOMENTUM_LOOKBACK_DAYS + 100)).isoformat()

        db.add_all(self._four_companies(make_weekly))
        db.add(make_weekly(edinet_code="E00005", trade_date=ancient_1, close_last=1000.0))
        db.add(make_weekly(edinet_code="E00005", trade_date=ancient_2, close_last=2000.0))
        db.commit()

        z = compute_momentum_z(
            db, ["E00001", "E00002", "E00003", "E00004", "E00005"], self.AS_OF)
        assert "E00005" not in z
        # 既存4社の値は E00005 の有無に影響されない（winsorize 母集団が同じ）
        assert z == compute_momentum_z(
            db, ["E00001", "E00002", "E00003", "E00004"], self.AS_OF)


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

class TestExecute:
    def test_ranking_orders_by_weighted_score(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E00001", z_roe=3.0),
            make_metric(edinet_code="E00002", z_roe=1.0),
            make_metric(edinet_code="E00003", z_roe=-1.0),
        ])
        db.commit()
        res = asyncio.run(execute_plugin(plugin,
            {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))
        assert res["count"] == 3
        codes = [r["edinet_code"] for r in res["results"]]
        assert codes == ["E00001", "E00002", "E00003"]
        scores = [r["score"] for r in res["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_min_coverage_skips_low_coverage(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E00001", z_roe=2.0, z_op_margin=1.0),  # coverage 1.0
            make_metric(edinet_code="E00002", z_roe=2.0, z_op_margin=None),  # coverage 0.5
        ])
        db.commit()
        res = asyncio.run(execute_plugin(plugin,
            {"weights": {"z_roe": 1.0, "z_op_margin": 1.0}, "min_coverage": 0.75}, db))
        assert res["count"] == 1
        assert res["skipped_low_coverage"] == 1
        assert res["results"][0]["edinet_code"] == "E00001"

    def test_zero_weights_returns_empty(self, db):
        res = asyncio.run(execute_plugin(plugin,{"weights": {"z_roe": 0.0}}, db))
        assert res["count"] == 0
        assert res["total_candidates"] == 0

    def test_empty_db_returns_empty(self, db):
        res = asyncio.run(execute_plugin(plugin,{"weights": {"z_roe": 1.0}}, db))
        assert res["count"] == 0
        assert res["total_candidates"] == 0

    def test_top_n_limits_results(self, db, make_metric):
        # top_n はスキーマ slider の min=10（パラメータ契約で reject 強制）。有効範囲で検証する。
        db.add_all([make_metric(edinet_code=f"E{i:05d}", z_roe=float(i)) for i in range(1, 13)])
        db.commit()
        res = asyncio.run(execute_plugin(
            plugin, {"weights": {"z_roe": 1.0}, "min_coverage": 0.0, "top_n": 10}, db))
        assert len(res["results"]) == 10
        assert res["total_candidates"] == 12

    def test_only_latest_year_per_company(self, db, make_metric):
        # 同一企業の複数年は最新年のみ対象（max-year subquery）
        db.add_all([
            make_metric(edinet_code="E00001", year=2021, period_end="2021-03-31", z_roe=9.0),
            make_metric(edinet_code="E00001", year=2023, period_end="2023-03-31", z_roe=1.0),
        ])
        db.commit()
        res = asyncio.run(execute_plugin(plugin,
            {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))
        assert res["count"] == 1
        assert res["results"][0]["year"] == 2023

    def test_z_momentum_drives_ranking(self, db, make_metric, make_weekly):
        # execute() は as_of に date.today() を使うため、テスト実行日からの相対日付で
        # 「12ヶ月超前」「1ヶ月強前」の2点を用意する（固定カレンダー日付は使えない）。
        from datetime import date, timedelta
        today = date.today()
        old_date    = (today - timedelta(days=400)).isoformat()
        recent_date = (today - timedelta(days=40)).isoformat()
        pairs = [
            ("E00001", 1000.0, 4000.0),
            ("E00002", 1000.0, 2000.0),
            ("E00003", 1000.0, 1000.0),
            ("E00004", 2000.0,  500.0),
        ]
        for ec, old_close, recent_close in pairs:
            db.add(make_metric(edinet_code=ec))
            db.add(make_weekly(edinet_code=ec, trade_date=old_date, close_last=old_close))
            db.add(make_weekly(edinet_code=ec, trade_date=recent_date, close_last=recent_close))
        db.commit()

        res = asyncio.run(execute_plugin(plugin,
            {"weights": {"z_momentum": 1.0}, "min_coverage": 0.0}, db))
        codes = [r["edinet_code"] for r in res["results"]]
        assert codes == ["E00001", "E00002", "E00003", "E00004"]

    def test_z_momentum_not_queried_when_unweighted(self, db, make_metric, monkeypatch):
        # weights に z_momentum が無ければ StockPriceWeekly への問い合わせ自体を行わない
        # （価格クエリはopt-in）。compute_momentum_z が一切呼ばれないことを確認する。
        import plugins.recommend as recommend_mod
        db.add(make_metric(edinet_code="E00001", z_roe=1.0))
        db.commit()

        called = []
        monkeypatch.setattr(recommend_mod, "compute_momentum_z",
                             lambda *a, **kw: called.append(1) or {})
        asyncio.run(execute_plugin(plugin, {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))
        assert called == []

    def test_delisted_company_excluded_from_ranking(self, db, make_metric):
        # 上場廃止銘柄（is_active=False）は買えないためランキング対象から除外する（Issue #315）。
        db.add_all([
            make_metric(edinet_code="E00001", z_roe=3.0, is_active=False),
            make_metric(edinet_code="E00002", z_roe=1.0),
        ])
        db.commit()
        res = asyncio.run(execute_plugin(plugin,
            {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))
        assert res["total_candidates"] == 1
        assert [r["edinet_code"] for r in res["results"]] == ["E00002"]

    def test_is_active_unset_still_included(self, db, make_metric):
        # 旧データ（is_active 未設定=NULL）は誤って除外しない（後方互換）。
        db.add(make_metric(edinet_code="E00001", z_roe=1.0))
        db.commit()
        res = asyncio.run(execute_plugin(plugin,
            {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))
        assert res["total_candidates"] == 1

    def test_weights_key_outside_metrics_rejected(self, db, make_metric):
        # 転送列は METRICS から導出するため、範囲外キーは黙って None にせず reject する
        # （Issue #441・パラメータ契約の membership 検証）。
        db.add(make_metric(edinet_code="E00001", z_roe=1.0))
        db.commit()
        with pytest.raises(ValueError, match="z_nc_ratio"):
            asyncio.run(execute_plugin(
                plugin, {"weights": {"z_roe": 1.0, "z_nc_ratio": 0.5}}, db))

    def test_price_asof_on_rows_and_freshness_over_population(self, db, make_metric):
        """行ごとの as-of は上位 top_n 社だけ引き、鮮度判定は母集団全体で行う（Issue #441）。

        絞ったのは「表示に要る as-of」だけで、鮮度の分位・stale 件数は DB 側集約が
        株価を持つ全銘柄を見る。ここが崩れると「上位10社が新しいから緑」という
        誤った安全側判定になる。
        """
        from datetime import date, timedelta
        from database import StockPriceDaily

        today = date.today()
        for i in range(1, 13):
            ec = f"E{i:05d}"
            db.add(make_metric(edinet_code=ec, z_roe=float(i)))
            db.add(StockPriceDaily(edinet_code=ec, trade_date=today.isoformat(), close=100.0))
        # ランキングには出ないが株価母集団には居る古い銘柄
        db.add(StockPriceDaily(edinet_code="E99999", close=1.0,
                               trade_date=(today - timedelta(days=60)).isoformat()))
        db.commit()

        res = asyncio.run(execute_plugin(
            plugin, {"weights": {"z_roe": 1.0}, "min_coverage": 0.0, "top_n": 10}, db))
        assert len(res["results"]) == 10
        assert all(r["price_asof"] == today.isoformat() for r in res["results"])
        assert res["price_freshness"]["n_codes"] == 13          # 表示10社ではなく母集団
        assert res["price_freshness"]["n_stale_over_5d"] == 1

    def test_presets_response_includes_statistical_preset_when_available(self, db, make_metric):
        from database import upsert_recommend_factor_premia
        db.add(make_metric(edinet_code="E00001", z_roe=1.0))
        upsert_recommend_factor_premia(db, "rfp_1", [
            {"run_id": "rfp_1", "factor_name": "z_roe", "mean_b": 0.2,
             "newey_west_se": None, "t_stat": None, "p_value": None, "n_periods": 12},
        ])
        db.commit()

        res = asyncio.run(execute_plugin(plugin,
            {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))
        assert STATISTICAL_PRESET_NAME in res["presets"]
        assert res["presets"][STATISTICAL_PRESET_NAME] == {"z_roe": 0.2}
        # 既存4プリセットは変更されず残っていること
        assert res["presets"]["バランス型"] == PRESETS["バランス型"]

    def test_dynamic_preset_never_carries_mu(self, db, make_metric):
        """Fama-MacBeth 側に mu が混ざっても統計的最適化プリセットへは入れない。

        入れてしまうと mu_source 未指定の実行が reject され、「統計的最適化」を
        選んだだけで 400 になる（Issue #423 子4）。
        """
        from database import upsert_recommend_factor_premia
        db.add(make_metric(edinet_code="E00001", z_roe=1.0))
        upsert_recommend_factor_premia(db, "rfp_mu", [
            {"run_id": "rfp_mu", "factor_name": "z_roe", "mean_b": 0.2,
             "newey_west_se": None, "t_stat": None, "p_value": None, "n_periods": 12},
            {"run_id": "rfp_mu", "factor_name": "mu", "mean_b": 0.9,
             "newey_west_se": None, "t_stat": None, "p_value": None, "n_periods": 12},
        ])
        db.commit()
        assert get_dynamic_preset(db) == {"z_roe": 0.2}
        res = asyncio.run(execute_plugin(plugin,
            {"preset": STATISTICAL_PRESET_NAME, "min_coverage": 0.0}, db))
        assert res["count"] == 1


# ── μ̂ の結線（Issue #423 子4）───────────────────────────────────────────────
#
# 買い推奨は既定では μ̂ を使わない（PRESETS に mu 重みが無い＝mu_source 既定 None）。
# ここで検証するのは「opt-in したときだけ効く」「重みを付けたのに効いていない状態を
# 黙って作らない」の2点。

class TestMuWiring:
    SNAP = "2026-06-26"

    def _seed(self, db, make_metric, mus: dict, persist: bool = True):
        db.add_all([make_metric(edinet_code=ec, z_roe=1.0) for ec in mus])
        db.commit()
        if persist:
            from database import replace_macro_enet_scores
            replace_macro_enet_scores(
                db, [{"edinet_code": ec, "mu": v, "r1_prime": None}
                     for ec, v in mus.items()], self.SNAP)

    def test_mu_source_default_is_off(self):
        schema = plugin.params_schema()
        assert schema["mu_source"]["default"] is None
        # 空文字オプション（=使わない）が先頭＝UI の select が既定で μ̂ 無しを指す
        assert MU_SOURCE_OPTIONS[0]["value"] == ""

    def test_mu_weight_without_source_is_rejected(self, db, make_metric):
        """重みだけ付けて出所未指定 → 黙って None にせず reject（fail fast）。"""
        self._seed(db, make_metric, {"E00001": 0.1, "E00002": 0.2,
                                     "E00003": 0.3, "E00004": 0.4})
        with pytest.raises(ValueError, match="mu_source"):
            asyncio.run(execute_plugin(
                plugin, {"weights": {"z_roe": 1.0, "mu": 1.0}, "min_coverage": 0.0}, db))

    def test_mu_drives_ranking_when_opted_in(self, db, make_metric):
        mus = {"E00001": 0.10, "E00002": 0.05, "E00003": 0.0,
               "E00004": -0.05, "E00005": -0.10}
        self._seed(db, make_metric, mus)
        res = asyncio.run(execute_plugin(plugin, {
            "weights": {"mu": 1.0}, "min_coverage": 0.0,
            "mu_source": "macro_enet", "top_n": 10}, db))
        assert res["mu_available"] is True
        assert res["mu_source"] == "macro_enet"
        # 高 μ̂ ＝買い候補上位（売り側の符号反転とは逆向き）
        assert [r["edinet_code"] for r in res["results"]] == [
            "E00001", "E00002", "E00003", "E00004", "E00005"]

    def test_mu_is_zscored_not_raw(self, db, make_metric):
        """μ̂ は週次リターン[小数]で z_* と2桁スケールが違う。生値のまま加重すると
        他指標が実質無効化されるため、候補集団内で winsorize→Z 化してから使う。"""
        mus = {"E00001": 0.10, "E00002": 0.05, "E00003": 0.0,
               "E00004": -0.05, "E00005": -0.10}
        self._seed(db, make_metric, mus)
        res = asyncio.run(execute_plugin(plugin, {
            "weights": {"mu": 1.0}, "min_coverage": 0.0,
            "mu_source": "macro_enet", "top_n": 10}, db))
        top = res["results"][0]
        assert top["detail"]["mu"] == pytest.approx(1.26, abs=0.2)   # Zスコア（|z|>1）
        assert abs(top["detail"]["mu"]) > 10 * abs(mus["E00001"])    # 生値ではない

    def test_graceful_degrade_when_producer_not_run(self, db, make_metric):
        """M-6 未実行なら μ̂ を外して継続し、mu_available=False で明示する（ADR-0004）。"""
        self._seed(db, make_metric, {"E00001": 0.1, "E00002": 0.2}, persist=False)
        res = asyncio.run(execute_plugin(plugin, {
            "weights": {"z_roe": 1.0, "mu": 1.0}, "min_coverage": 0.0,
            "mu_source": "macro_enet"}, db))
        assert res["mu_available"] is False
        assert res["count"] == 2          # μ̂ 抜きで z_roe だけで判定継続
        assert res["results"][0]["detail"]["mu"] is None

    def test_producer_not_read_when_mu_unweighted(self, db, make_metric, monkeypatch):
        """mu に重みが無ければ producer を読まない（既定経路のコストを増やさない）。"""
        import plugins.recommend as recommend_mod
        self._seed(db, make_metric, {"E00001": 0.1, "E00002": 0.2,
                                     "E00003": 0.3, "E00004": 0.4})
        called = []
        monkeypatch.setattr(recommend_mod, "compute_mu_z",
                            lambda *a, **kw: called.append(1) or {})
        res = asyncio.run(execute_plugin(plugin, {
            "weights": {"z_roe": 1.0}, "min_coverage": 0.0,
            "mu_source": "macro_enet"}, db))
        assert called == []
        assert res["mu_available"] is False

    def test_compute_mu_z_needs_four_samples(self, db, make_metric):
        """winsorize が機能しない小標本は空 dict（compute_momentum_z と同じ契約）。"""
        self._seed(db, make_metric, {"E00001": 0.1, "E00002": 0.2, "E00003": 0.3})
        assert compute_mu_z(db, "macro_enet",
                            ["E00001", "E00002", "E00003"]) == {}

    def test_compute_mu_z_population_is_the_candidate_set(self, db, make_metric):
        """標準化母集団は候補集団（フィルタ後）。producer 全体ではない。"""
        mus = {f"E{i:05d}": i / 100.0 for i in range(1, 9)}
        self._seed(db, make_metric, mus)
        subset = [f"E{i:05d}" for i in range(1, 5)]
        z_sub = compute_mu_z(db, "macro_enet", subset)
        z_all = compute_mu_z(db, "macro_enet", list(mus))
        assert set(z_sub) == set(subset)
        assert z_sub["E00001"] != pytest.approx(z_all["E00001"])
