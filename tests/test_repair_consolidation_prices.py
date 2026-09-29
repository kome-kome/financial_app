"""`scripts/repair_consolidation_prices.py`（#765 の一回性の修復）。

台帳の値は Issue #765 で観測した実際の異常値で、実際の終値と Yahoo の split 比率から
**ビット単位まで再現できる**ことをここで縛る（書き写しの誤りをテストの失敗として現す）。
DB の手順は SQLite の上で、日付を今日からの相対で置いて確かめる（日次の保持窓の内側に置く）。
"""
from datetime import date, timedelta

import pytest

import scripts.repair_consolidation_prices as R
from database import StockPriceDaily, StockPriceWeekly, iso_week_start

SQUEEZE = 16278046720.0          # 3,700 ÷ round(1/4,400,000, 10) の float32
SQUEEZE_FWD = 16280000512.0      # 3,700 × 4,400,000 の float32（split 当日＝廃止日）
SBI = 55319998464.0              # 2,766 × 20,000,000 の float32


class TestManifest:
    def test_counts(self):
        assert sum(f.table == "daily" for f in R.FIXES) == 18   # 表 D の16＋9/29 の幽霊2
        assert sum(f.table == "weekly" for f in R.FIXES) == 4
        assert {f.ec for f in R.FIXES} == {"E25282", "E21381", "E02798", "E35289",
                                           "E02305", "E03530"}

    @pytest.mark.parametrize("fix", [f for f in R.FIXES if f.form != R.GHOST],
                             ids=lambda f: f"{f.ec}-{f.table}-{f.date}")
    def test_observed_value_reproduces_bit_exactly(self, fix):
        assert R.reproduce(fix) == fix.observed

    def test_ghost_is_normal_scale(self):
        ghost = [f for f in R.FIXES if f.form == R.GHOST]
        assert [(f.ec, f.date, f.observed) for f in ghost] == [("E02305", "2026-06-12", 7580.0)]


class TestStatus:
    RESTORE = R.Fix("daily", "E1", "2026-09-09", R.RESTORE, 3700.0, SQUEEZE, 4_400_000, R.RETRO)
    DELETE = R.Fix("daily", "E1", "2026-09-14", R.DELETE, 3700.0, SQUEEZE_FWD, 4_400_000, R.FORWARD)
    GHOST = R.Fix("daily", "E1", "2026-06-12", R.DELETE, 7580.0, 7580.0, None, R.GHOST)

    def test_restore(self):
        assert R.status_of(self.RESTORE, (SQUEEZE, 0.0)) == R.PENDING
        # 同じ単位のまま別の値に書き直されていても未処理（完全一致で判定しない）
        assert R.status_of(self.RESTORE, (SQUEEZE_FWD, 0.0)) == R.PENDING
        assert R.status_of(self.RESTORE, (3700.0, None)) == R.DONE
        assert R.status_of(self.RESTORE, None) == R.MISMATCH

    def test_delete(self):
        assert R.status_of(self.DELETE, (SQUEEZE_FWD, 0.0)) == R.PENDING
        assert R.status_of(self.DELETE, None) == R.DONE
        assert R.status_of(self.DELETE, (3690.0, 100.0)) == R.MISMATCH   # 正常な行は消さない

    def test_ghost(self):
        assert R.status_of(self.GHOST, (7580.0, 0.0)) == R.PENDING
        assert R.status_of(self.GHOST, (7580.0, 1200.0)) == R.MISMATCH   # 取引があった行
        assert R.status_of(self.GHOST, None) == R.DONE


class TestRun:
    """直す→走査→（ドライランなら）巻き戻す、の手順。"""

    @pytest.fixture(autouse=True)
    def _local_and_side_effects(self, monkeypatch):
        monkeypatch.setattr(R.D, "DB_TARGET", "local")
        monkeypatch.setattr(R.D, "_is_local", True)
        self.market_calls, self.bumps = [], []
        monkeypatch.setattr(R, "update_market_data_from_history",
                            lambda db, **kw: self.market_calls.append(kw) or 3)
        monkeypatch.setattr(R.weekly_price_cache, "bump_generation_safely",
                            lambda db, reason: self.bumps.append(reason) or "stamp")

    def _world(self, db, make_price, make_weekly):
        """E25282 型（日次）と E03530 型（週次のみ）の小さな世界。日付は今日から相対。"""
        base = date.today() - timedelta(days=28)
        mon = base - timedelta(days=base.weekday())            # 4週ほど前の月曜
        d = {k: (mon + timedelta(days=k)).isoformat() for k in range(12)}
        self.d = d
        db.add_all([
            make_price(edinet_code="E25282", trade_date=d[1], close=3700.0, volume=100.0),
            make_price(edinet_code="E25282", trade_date=d[2], close=SQUEEZE, volume=0.0),
            make_price(edinet_code="E25282", trade_date=d[7], close=SQUEEZE_FWD, volume=0.0),
            make_weekly(edinet_code="E25282", trade_date=d[2], close_last=SQUEEZE,
                        volume_sum=100.0, turnover_sum=370000.0, n_days=2),
            make_weekly(edinet_code="E25282", trade_date=d[7], close_last=SQUEEZE_FWD,
                        volume_sum=0.0, turnover_sum=0.0, n_days=1),
            make_weekly(edinet_code="E03530", trade_date="2023-09-22", close_last=2796.0),
            make_weekly(edinet_code="E03530", trade_date="2023-09-27", close_last=SBI),
            make_weekly(edinet_code="E03530", trade_date="2025-11-21", close_last=SBI,
                        volume_sum=0.0),
            make_weekly(edinet_code="E03530", trade_date="2025-12-19", close_last=1731.0),
        ])
        db.commit()
        return (
            R.Fix("daily", "E25282", d[2], R.RESTORE, 3700.0, SQUEEZE, 4_400_000, R.RETRO),
            R.Fix("daily", "E25282", d[7], R.DELETE, 3700.0, SQUEEZE_FWD, 4_400_000, R.FORWARD),
            R.Fix("weekly", "E03530", "2023-09-25", R.RESTORE, 2766.0, SBI, 20_000_000, R.FORWARD),
            R.Fix("weekly", "E03530", "2025-11-17", R.DELETE, 2766.0, SBI, 20_000_000, R.FORWARD),
        )

    @staticmethod
    def _snapshot(db):
        daily = sorted((r.edinet_code, r.trade_date, r.close, r.volume)
                       for r in db.query(StockPriceDaily.edinet_code, StockPriceDaily.trade_date,
                                         StockPriceDaily.close, StockPriceDaily.volume))
        weekly = sorted((r.edinet_code, r.week_start, r.close_last, r.volume_sum, r.n_days)
                        for r in db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.week_start,
                                          StockPriceWeekly.close_last, StockPriceWeekly.volume_sum,
                                          StockPriceWeekly.n_days))
        return daily, weekly

    def test_dry_run_rehearses_then_rolls_back(self, db, make_price, make_weekly):
        fixes = self._world(db, make_price, make_weekly)
        before = self._snapshot(db)
        rep = R.run(db, fixes, apply=False)
        assert rep["refused"] is None and rep["applied"] is False
        assert [p["status"] for p in rep["plan"]] == [R.PENDING] * 4
        assert len(rep["before"]["companies"]) == 2
        assert rep["after"]["companies"] == []                  # 直せば段差は0になる見込み
        assert self._snapshot(db) == before                    # DB は元のまま
        assert self.market_calls == [] and self.bumps == []

    def test_apply_restores_deletes_and_rebuilds_weeks(self, db, make_price, make_weekly):
        fixes = self._world(db, make_price, make_weekly)
        d = self.d
        rep = R.run(db, fixes, apply=True)
        assert rep["applied"] is True and rep["refused"] is None
        daily, weekly = self._snapshot(db)
        assert daily == [("E25282", d[1], 3700.0, 100.0), ("E25282", d[2], 3700.0, None)]
        w = {(ec, ws): (c, v, n) for ec, ws, c, v, n in weekly}
        # 日次から作り直した週: 出来高は NULL にした日を数えない
        assert w[("E25282", iso_week_start(d[2]))] == (3700.0, 100.0, 2)
        # 日次が1日も残らなくなった週は週次も消す
        assert ("E25282", iso_week_start(d[7])) not in w
        assert w[("E03530", "2023-09-25")][0] == 2766.0
        assert ("E03530", "2025-11-17") not in w
        assert rep["after"]["companies"] == []
        assert self.bumps and self.market_calls == [
            {"point_in_time": True, "only": ["E03530", "E25282"]}]

        again = R.run(db, fixes, apply=True)                   # 2回目は何もしない
        assert again.get("already_applied") is True and len(self.bumps) == 1

    def test_refuses_when_a_scaled_row_is_not_in_the_manifest(self, db, make_price, make_weekly):
        """台帳に無い比率倍の行が増えていたら（ガードの無い晩が足した等）、直しても段差が残る
        ＝巻き戻して拒否する。"""
        fixes = self._world(db, make_price, make_weekly)
        db.add(make_price(edinet_code="E25282", trade_date=self.d[3], close=SQUEEZE, volume=0.0))
        db.commit()
        before = self._snapshot(db)
        rep = R.run(db, fixes, apply=True)
        assert rep["refused"] and rep["applied"] is False
        assert rep["after"]["companies"] == ["E25282"]
        assert self._snapshot(db) == before
        assert self.bumps == [] and self.market_calls == []

    def test_refuses_on_mismatch(self, db, make_price, make_weekly):
        fixes = self._world(db, make_price, make_weekly)
        gone = R.Fix("daily", "E25282", self.d[4], R.RESTORE, 3700.0, SQUEEZE, 4_400_000, R.RETRO)
        rep = R.run(db, fixes + (gone,), apply=True)
        assert rep["refused"] and rep["applied"] is False
        assert "executed" not in rep

    def test_refuses_non_local_target(self, db, monkeypatch):
        monkeypatch.setattr(R.D, "DB_TARGET", "prod")
        with pytest.raises(SystemExit):
            R.run(db, (), apply=False)
