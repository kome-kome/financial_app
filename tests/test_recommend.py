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
    METRICS, PRESETS, STATISTICAL_PRESET_NAME, compute_momentum_z,
    get_dynamic_preset, plugin, resolve_weights,
)


# ── 純粋: 定数の整合性 ───────────────────────────────────────────────────────

class TestConstants:
    def test_metrics_unique_and_nonempty(self):
        assert len(METRICS) == 9
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
