"""plugins/sell_ranking.py のユニットテスト。

純粋: parse_holdings / _compute_trend / _apply_timing / PRESETS 整合。
execute(): ユニバース標準化による売りスコアの符号・ランキング・アクションラベル・
not_found / invalid 集約（in-memory SQLite）。依存ゲート（sector_ols）は execute を
直接呼んでバイパスし、execute_plugin 経由のゲート挙動は別途検証する。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import execute_plugin
from plugins.base import DependencyError
from plugins.utils import coerce_params
from plugins.sell_ranking import (
    SELL_METRICS, PRESETS, plugin,
    parse_holdings, _compute_trend, _apply_timing, _base_action,
)


def _run(raw, db):
    """依存ゲートをバイパスして execute を直接実行（coerce 済み params を渡す）。"""
    typed = coerce_params(plugin.params_schema(), raw)
    return asyncio.run(plugin.execute(typed, db))


def _wk(week_start, close):
    return SimpleNamespace(week_start=week_start, close_last=close)


# ── 純粋: 定数 ───────────────────────────────────────────────────────────────

class TestConstants:
    def test_presets_reference_valid_metrics(self):
        for name, weights in PRESETS.items():
            for metric in weights:
                assert metric in SELL_METRICS, f"{name} の {metric} が SELL_METRICS に無い"

    def test_presets_weights_nonnegative(self):
        # 売り重視度は非負（execute で負はクリップするが、定義段階で守る）
        for weights in PRESETS.values():
            for w in weights.values():
                assert w >= 0

    def test_macro_preset_exists_and_macro_only(self):
        # マクロ予測型は μ・−Rᴹ の2軸のみ（両キー存在・他観点を含まない）
        assert "マクロ予測型" in PRESETS
        keys = set(PRESETS["マクロ予測型"])
        assert keys == {"mu", "neg_r_macro"}

    def test_default_preset_is_macro(self):
        # 既定プリセットはマクロ予測型
        assert plugin.params_schema()["preset"]["default"] == "マクロ予測型"


# ── 純粋: parse_holdings ─────────────────────────────────────────────────────

class TestParseHoldings:
    def test_code_only(self):
        parsed, invalid = parse_holdings("7203\n9984")
        assert [p["sec_code"] for p in parsed] == ["7203", "9984"]
        assert all(p["avg_cost"] is None and p["buy_date"] is None for p in parsed)
        assert invalid == []

    def test_cost_and_date(self):
        parsed, invalid = parse_holdings("7203, @1500, 2024-03-10")
        assert parsed[0] == {"sec_code": "7203", "avg_cost": 1500.0, "buy_date": "2024-03-10"}
        assert invalid == []

    def test_cost_without_at_and_whitespace_sep(self):
        parsed, _ = parse_holdings("6758 12000")
        assert parsed[0]["sec_code"] == "6758"
        assert parsed[0]["avg_cost"] == 12000.0

    def test_invalid_line_collected(self):
        parsed, invalid = parse_holdings("7203\nAAPL\nへんな行")
        assert [p["sec_code"] for p in parsed] == ["7203"]
        assert invalid == ["AAPL", "へんな行"]

    def test_duplicate_skipped(self):
        parsed, _ = parse_holdings("7203, @1000\n7203, @2000")
        assert len(parsed) == 1
        assert parsed[0]["avg_cost"] == 1000.0   # 先勝ち

    def test_blank_and_comment_ignored(self):
        parsed, invalid = parse_holdings("\n# メモ\n7203\n")
        assert [p["sec_code"] for p in parsed] == ["7203"]
        assert invalid == []


# ── 純粋: _compute_trend ─────────────────────────────────────────────────────

class TestComputeTrend:
    def _series(self, prices):
        return [_wk(f"2023-{(i // 4) + 1:02d}-{(i % 4) * 7 + 1:02d}", p)
                for i, p in enumerate(prices)]

    def test_uptrend(self):
        rows = self._series([1000 + i * 25 for i in range(14)])   # 単調増 +>10%
        res = _compute_trend(rows)
        assert res["trend"] == "上昇"
        assert res["ret_13w"] > 10

    def test_downtrend(self):
        rows = self._series([1000 - i * 25 for i in range(14)])   # 単調減 -<-10%
        res = _compute_trend(rows)
        assert res["trend"] == "下落"
        assert res["ret_13w"] < -10

    def test_flat(self):
        rows = self._series([1000 + (i % 2) for i in range(14)])  # ほぼ横ばい
        res = _compute_trend(rows)
        assert res["trend"] == "横ばい"

    def test_insufficient_weeks(self):
        rows = self._series([1000, 1100, 1200])   # < 8 週 → 不明
        res = _compute_trend(rows)
        assert res["trend"] == "不明"
        assert res["ret_13w"] is None
        assert res["last_close"] == 1200


# ── 純粋: タイミング補正 / 閾値ラベル ─────────────────────────────────────────

class TestActionLabels:
    def test_base_action_thresholds(self):
        assert _base_action(1.0, 0.8, 0.3) == "SELL"
        assert _base_action(0.5, 0.8, 0.3) == "REDUCE"
        assert _base_action(0.0, 0.8, 0.3) == "HOLD"
        assert _base_action(None, 0.8, 0.3) == "データ不足"

    def test_downtrend_escalates(self):
        assert _apply_timing("HOLD", "下落") == "REDUCE"
        assert _apply_timing("REDUCE", "下落") == "SELL"
        assert _apply_timing("SELL", "下落") == "SELL"   # 上限

    def test_uptrend_softens_sell_only(self):
        assert _apply_timing("SELL", "上昇") == "REDUCE"
        assert _apply_timing("REDUCE", "上昇") == "REDUCE"
        assert _apply_timing("HOLD", "上昇") == "HOLD"

    def test_flat_and_unknown_no_change(self):
        assert _apply_timing("SELL", "横ばい") == "SELL"
        assert _apply_timing("REDUCE", "不明") == "REDUCE"


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

def _universe(make_metric, roes):
    """roe を変えた最新年度ユニバースを構築（sec_code=1001.., edinet=E0001..）。"""
    rows = []
    for i, roe in enumerate(roes, 1):
        rows.append(make_metric(edinet_code=f"E{i:04d}", sec_code=f"{1000 + i}",
                                company_name=f"会社{i}", year=2023, roe=roe))
    return rows


class TestExecute:
    def test_weak_stock_scores_higher_than_strong(self, db, make_metric):
        # roe が低い（割高/業績不振側）ほど売りスコアが高くなる
        db.add_all(_universe(make_metric, [-5.0, 0.0, 5.0, 10.0, 15.0]))
        db.commit()
        res = _run({"holdings": "1001\n1005", "weights": {"roe": 1.0},
                    "min_coverage": 0.0}, db)
        assert res["count"] == 2
        # ランキング先頭は roe 最低の 1001
        assert res["results"][0]["sec_code"] == "1001"
        assert res["results"][1]["sec_code"] == "1005"
        assert res["results"][0]["score"] > res["results"][1]["score"]
        assert res["results"][0]["score"] > 0    # 平均より下＝売り側
        assert res["results"][1]["score"] < 0    # 平均より上＝売らない側

    def test_action_labels_from_score(self, db, make_metric):
        db.add_all(_universe(make_metric, [-5.0, 0.0, 5.0, 10.0, 15.0]))
        db.commit()
        res = _run({"holdings": "1001\n1005", "weights": {"roe": 1.0},
                    "min_coverage": 0.0, "sell_threshold": 0.8,
                    "reduce_threshold": 0.3}, db)
        by_code = {r["sec_code"]: r for r in res["results"]}
        assert by_code["1001"]["action"] == "SELL"   # zstd≈-1.27 → score≈+1.27
        assert by_code["1005"]["action"] == "HOLD"   # zstd≈+1.27 → score≈-1.27

    def test_not_found_collected(self, db, make_metric):
        db.add_all(_universe(make_metric, [1.0, 2.0, 3.0, 4.0]))
        db.commit()
        res = _run({"holdings": "1001\n9999", "weights": {"roe": 1.0},
                    "min_coverage": 0.0}, db)
        assert res["not_found"] == ["9999"]
        assert res["count"] == 1

    def test_invalid_passthrough(self, db, make_metric):
        db.add_all(_universe(make_metric, [1.0, 2.0, 3.0, 4.0]))
        db.commit()
        res = _run({"holdings": "1001\nNOPE", "weights": {"roe": 1.0},
                    "min_coverage": 0.0}, db)
        assert res["invalid"] == ["NOPE"]

    def test_pnl_from_avg_cost(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E0001", sec_code="1001", roe=1.0, stock_price=1200.0),
            make_metric(edinet_code="E0002", sec_code="1002", roe=2.0),
            make_metric(edinet_code="E0003", sec_code="1003", roe=3.0),
            make_metric(edinet_code="E0004", sec_code="1004", roe=4.0),
            make_metric(edinet_code="E0005", sec_code="1005", roe=5.0),
        ])
        db.commit()
        res = _run({"holdings": "1001, @1000", "weights": {"roe": 1.0},
                    "min_coverage": 0.0}, db)
        row = res["results"][0]
        # last_close は週次が無いので stock_price(1200) にフォールバック → +20%
        assert row["pnl_pct"] == 20.0

    def test_timing_escalates_with_downtrend(self, db, make_metric, make_weekly):
        # roe=平均並みで base=HOLD/REDUCE 付近の銘柄が、下落トレンドで一段上がる
        db.add_all(_universe(make_metric, [0.0, 5.0, 10.0, 15.0, 20.0]))
        # sec_code 1003 (roe=10, ほぼ平均) に下落週次を付与
        for i in range(14):
            db.add(make_weekly(edinet_code="E0003",
                               week_start=f"2023-{(i//4)+1:02d}-{(i%4)*7+1:02d}",
                               close_last=1000 - i * 30))
        db.commit()
        res = _run({"holdings": "1003", "weights": {"roe": 1.0}, "min_coverage": 0.0,
                    "sell_threshold": 0.8, "reduce_threshold": 0.0,
                    "timing_adjust": True}, db)
        row = res["results"][0]
        assert row["trend"] == "下落"
        # roe=10 はユニバース平均 → score≈0 → base REDUCE（reduce_th=0.0）→ 下落補正で SELL
        assert row["action"] == "SELL"

    def test_empty_holdings_returns_empty(self, db, make_metric):
        db.add_all(_universe(make_metric, [1.0, 2.0, 3.0, 4.0]))
        db.commit()
        res = _run({"holdings": "", "weights": {"roe": 1.0}}, db)
        assert res["count"] == 0
        assert res["results"] == []

    def test_delisted_holding_surfaces_flag_but_not_excluded(self, db, make_metric):
        # 保有銘柄はユーザー入力のため is_active=False でも not_found にしない。
        # 上場廃止済みであることは情報表示（is_active/delisted_date）で気付けるようにする（Issue #315）。
        from datetime import date
        db.add_all([
            make_metric(edinet_code="E9001", sec_code="9001", roe=1.0,
                       is_active=False, delisted_date=date(2024, 6, 1)),
            *_universe(make_metric, [2.0, 3.0, 4.0, 5.0]),
        ])
        db.commit()
        res = _run({"holdings": "9001", "weights": {"roe": 1.0}, "min_coverage": 0.0}, db)
        assert res["not_found"] == []
        row = res["results"][0]
        assert row["is_active"] is False
        assert row["delisted_date"] == "2024-06-01"

    def test_active_holding_reports_is_active_true(self, db, make_metric):
        db.add_all(_universe(make_metric, [1.0, 2.0, 3.0, 4.0]))
        db.commit()
        res = _run({"holdings": "1001", "weights": {"roe": 1.0}, "min_coverage": 0.0}, db)
        row = res["results"][0]
        assert row["is_active"] is True
        assert row["delisted_date"] is None

    def test_universe_excludes_delisted_from_standardization(self, db, make_metric):
        # 標準化ユニバース（uni_q）に delisted 銘柄が混ざると極端値が mean/sd を歪める。
        # is_active フィルタが効いていれば、追加後もスコアは変わらないはず（Issue #315）。
        db.add_all(_universe(make_metric, [-5.0, 0.0, 5.0, 10.0, 15.0]))
        db.commit()
        baseline = _run({"holdings": "1001", "weights": {"roe": 1.0}, "min_coverage": 0.0}, db)

        db.add(make_metric(edinet_code="E9999", sec_code="9999", company_name="廃止企業",
                           year=2023, roe=1000.0, is_active=False))
        db.commit()
        after = _run({"holdings": "1001", "weights": {"roe": 1.0}, "min_coverage": 0.0}, db)

        assert after["results"][0]["score"] == pytest.approx(baseline["results"][0]["score"])

    def test_net_cash_cushion_loss_scores_higher(self, db, make_metric):
        # ネットキャッシュ余力（nc_ratio）の毀損＝クッション消失ほど売りスコアが高い（#208）。
        # market_cap=1000(百万円)=1e9円。nc = 流動資産 − 総負債（投資有価証券=0）。
        specs = [
            ("1001", 2.0e8),   # nc=-3e8 → ratio -0.3（最も毀損）
            ("1002", 5.0e8),   # 0
            ("1003", 1.0e9),   # 0.5
            ("1004", 1.5e9),   # 1.0
            ("1005", 2.0e9),   # 1.5（最も厚い）
        ]
        for i, (sec, ca) in enumerate(specs, 1):
            db.add(make_metric(edinet_code=f"E{i:04d}", sec_code=sec, company_name=f"会社{i}",
                               year=2023, market_cap=1000.0,
                               bs_current_assets=ca, bs_investment_securities=0.0,
                               bs_total_liabilities=5.0e8))
        db.commit()
        res = _run({"holdings": "1001\n1005", "weights": {"nc_ratio": 1.0},
                    "min_coverage": 0.0}, db)
        assert res["count"] == 2
        assert res["results"][0]["sec_code"] == "1001"   # クッション毀損 → 売り上位
        assert res["results"][1]["sec_code"] == "1005"
        assert res["results"][0]["score"] > 0
        assert res["results"][1]["score"] < 0


# ── 依存ゲート（sector_ols）─────────────────────────────────────────────────

class TestDependencyGate:
    def test_blocks_without_regression(self, db, make_metric):
        db.add_all(_universe(make_metric, [1.0, 2.0, 3.0, 4.0]))
        db.commit()
        with pytest.raises(DependencyError):
            asyncio.run(execute_plugin(plugin, {"holdings": "1001"}, db))

    def test_runs_with_regression(self, db, make_metric):
        from database import RegressionResult
        from datetime import date
        db.add_all(_universe(make_metric, [1.0, 2.0, 3.0, 4.0]))
        db.add(RegressionResult(edinet_code="E0001", year=2023,
                                period_end=date(2023, 3, 31),
                                predicted_market_cap=12000.0, gap_ratio=-15.0,
                                model="ols", sector="情報・通信業"))
        db.commit()
        res = asyncio.run(execute_plugin(plugin,
            {"holdings": "1001", "weights": {"roe": 1.0}, "min_coverage": 0.0}, db))
        assert res["count"] == 1


# ── μ 出所トグル（M-1/M-2・ADR-0004）─────────────────────────────────────────

class TestMuSource:
    def _seed_universe(self, db, make_metric):
        db.add_all(_universe(make_metric, [1.0, 2.0, 3.0, 4.0, 5.0]))
        db.commit()

    def _seed_m2(self, db, mus):
        from database import replace_macro_gbdt_scores
        replace_macro_gbdt_scores(
            db, [{"edinet_code": ec, "mu": v} for ec, v in mus.items()], "2026-06-26")

    def test_macro_gbdt_mu_used_when_available(self, db, make_metric):
        self._seed_universe(db, make_metric)
        # 低μ=保有理由小=売る理由大 → 売りスコア高
        self._seed_m2(db, {"E0001": -0.10, "E0002": -0.05, "E0003": 0.0,
                           "E0004": 0.05, "E0005": 0.10})
        res = _run({"holdings": "1001\n1005", "weights": {"mu": 1.0},
                    "min_coverage": 0.0, "mu_source": "macro_gbdt",
                    "timing_adjust": False}, db)
        assert res["mu_available"] is True
        assert res["mu_source"] == "macro_gbdt"
        by = {r["sec_code"]: r for r in res["results"]}
        assert by["1001"]["score"] > by["1005"]["score"]   # 低μ → 売り上位
        assert by["1001"]["score"] > 0
        assert by["1005"]["score"] < 0

    def test_macro_gbdt_graceful_when_not_run(self, db, make_metric):
        self._seed_universe(db, make_metric)
        # macro_gbdt_scores 空 → graceful（mu 除外・roe で判定継続）
        res = _run({"holdings": "1001", "weights": {"mu": 1.0, "roe": 0.5},
                    "min_coverage": 0.0, "mu_source": "macro_gbdt"}, db)
        assert res["mu_available"] is False
        assert res["mu_source"] == "macro_gbdt"
        assert res["count"] == 1

    def test_default_mu_source_is_m2(self, db, make_metric):
        self._seed_universe(db, make_metric)
        # mu_source 未指定 → coerce が default=macro_gbdt（M-2）補完
        res = _run({"holdings": "1001", "weights": {"roe": 1.0},
                    "min_coverage": 0.0}, db)
        assert res["mu_source"] == "macro_gbdt"

    def test_r3_gate_noop_under_macro_gbdt(self, db, make_metric):
        self._seed_universe(db, make_metric)
        self._seed_m2(db, {"E0001": -0.20, "E0002": -0.05, "E0003": 0.0,
                           "E0004": 0.05, "E0005": 0.10})
        # r3_gate>0 でも M-2 は r1_prime 無 → SELL を REDUCE に格下げしない（no-op）
        res = _run({"holdings": "1001", "weights": {"mu": 1.0}, "min_coverage": 0.0,
                    "sell_threshold": 0.8, "reduce_threshold": 0.3,
                    "r3_gate": 0.1, "mu_source": "macro_gbdt", "timing_adjust": False}, db)
        row = res["results"][0]
        assert row["sec_code"] == "1001"
        assert row["action"] == "SELL"
