"""collector.update_market_data_from_history のユニットテスト。

同期関数で db Session を直接受け取るため、conftest の in-memory SQLite で完結する。
point_in_time=False（最新株価で最新レコードを更新）と
point_in_time=True（全レコードを period_end 近傍の週次株価で更新）の2分岐をカバー。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import update_market_data_from_history


class TestUpdateMarketDataDefault:
    """point_in_time=False（デフォルト）: 最新株価で最新レコードを更新"""

    def test_empty_db_returns_zero(self, db):
        assert update_market_data_from_history(db) == 0

    def test_financial_records_without_prices_returns_zero(self, db, make_fin):
        db.add(make_fin())
        db.commit()
        assert update_market_data_from_history(db) == 0

    def test_prices_without_financial_records_returns_zero(self, db, make_price):
        db.add(make_price(close=1000.0))
        db.commit()
        assert update_market_data_from_history(db) == 0

    def test_updates_stock_price(self, db, make_fin, make_price):
        rec = make_fin()
        db.add(rec)
        db.add(make_price(close=1500.0))
        db.commit()
        count = update_market_data_from_history(db)
        assert count == 1
        db.refresh(rec)
        assert rec.stock_price == 1500.0

    def test_calculates_pbr_from_bps(self, db, make_fin, make_price):
        rec = make_fin(bs_bps=500.0)
        db.add(rec)
        db.add(make_price(close=1000.0))
        db.commit()
        update_market_data_from_history(db)
        db.refresh(rec)
        assert rec.pbr == pytest.approx(2.0)

    def test_market_cap_prefers_issued_shares(self, db, make_fin, make_price):
        # issued_shares 直接利用: 1.0e6株 × 2000円 / 1e6 = 2000 百万円
        # 旧実装（bs_total_equity/bs_bps）なら: 1.5e9/500 = 3.0e6株 → 6000 百万円
        rec = make_fin(issued_shares=1.0e6, bs_total_equity=1.5e9, bs_bps=500.0)
        db.add(rec)
        db.add(make_price(close=2000.0))
        db.commit()
        update_market_data_from_history(db)
        db.refresh(rec)
        assert rec.market_cap == pytest.approx(2000.0)  # issued_shares 優先

    def test_market_cap_falls_back_to_derived_when_no_issued_shares(self, db, make_fin, make_price):
        # issued_shares なし: bs_total_equity/bs_bps フォールバック
        # 1.0e9/500 = 2.0e6株 × 1000円 / 1e6 = 2000 百万円
        rec = make_fin(bs_total_equity=1.0e9, bs_bps=500.0)
        db.add(rec)
        db.add(make_price(close=1000.0))
        db.commit()
        update_market_data_from_history(db)
        db.refresh(rec)
        assert rec.market_cap == pytest.approx(2000.0)  # フォールバック

    def test_calculates_per_from_eps(self, db, make_fin, make_price):
        rec = make_fin(pl_eps=50.0)
        db.add(rec)
        db.add(make_price(close=1000.0))
        db.commit()
        update_market_data_from_history(db)
        db.refresh(rec)
        assert rec.per == pytest.approx(20.0)

    def test_skips_zero_price(self, db, make_fin, make_price):
        rec = make_fin()
        db.add(rec)
        db.add(make_price(close=0.0))
        db.commit()
        assert update_market_data_from_history(db) == 0
        db.refresh(rec)
        assert rec.stock_price is None

    def test_skips_negative_price(self, db, make_fin, make_price):
        rec = make_fin()
        db.add(rec)
        db.add(make_price(close=-10.0))
        db.commit()
        assert update_market_data_from_history(db) == 0

    def test_updates_only_latest_record_per_company(self, db, make_fin, make_price):
        old_rec = make_fin(year=2021, period_end="2021-03-31")
        new_rec = make_fin(year=2023, period_end="2023-03-31")
        db.add(old_rec); db.add(new_rec)
        db.add(make_price(close=2000.0))
        db.commit()
        count = update_market_data_from_history(db)
        assert count == 1
        db.refresh(old_rec)
        db.refresh(new_rec)
        # 最新レコード（2023年）が更新される
        assert new_rec.stock_price == 2000.0
        assert old_rec.stock_price is None

    def test_two_companies_updated_independently(self, db, make_fin, make_price):
        rec1 = make_fin(edinet_code="E00001")
        rec2 = make_fin(edinet_code="E00002", year=2022, period_end="2022-03-31")
        db.add(rec1); db.add(rec2)
        db.add(make_price(edinet_code="E00001", close=1000.0))
        db.add(make_price(edinet_code="E00002", close=2000.0, trade_date="2023-01-05"))
        db.commit()
        count = update_market_data_from_history(db)
        assert count == 2
        db.refresh(rec1); db.refresh(rec2)
        assert rec1.stock_price == 1000.0
        assert rec2.stock_price == 2000.0


class TestUpdateMarketDataPointInTime:
    """point_in_time=True: 全財務レコードを period_end 近傍の週次株価で更新"""

    def test_empty_weekly_returns_zero(self, db, make_fin):
        db.add(make_fin())
        db.commit()
        assert update_market_data_from_history(db, point_in_time=True) == 0

    def test_empty_financial_records_returns_zero(self, db, make_weekly):
        db.add(make_weekly(close_last=1000.0))
        db.commit()
        assert update_market_data_from_history(db, point_in_time=True) == 0

    def test_updates_by_nearest_period_end(self, db, make_fin, make_weekly):
        rec = make_fin(period_end="2023-03-31", bs_bps=1000.0)
        db.add(rec)
        db.add(make_weekly(trade_date="2023-03-31", close_last=3000.0))
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec)
        assert rec.stock_price == 3000.0
        assert rec.pbr == pytest.approx(3.0)

    def test_period_end_none_skips_bisect_but_gets_latest_price(self, db, make_fin, make_weekly):
        # period_end なしは二分探索をスキップするが、最終ステップ（latest_prices）で
        # 最新週次株価が適用される（スクリーニング用の最新株価上書き）。
        rec = make_fin(period_end=None)
        db.add(rec)
        db.add(make_weekly(trade_date="2023-03-31", close_last=1000.0))
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec)
        assert rec.stock_price == 1000.0

    def test_skips_zero_weekly_price(self, db, make_fin, make_weekly):
        rec = make_fin(period_end="2023-03-31")
        db.add(rec)
        db.add(make_weekly(trade_date="2023-03-31", close_last=0.0))
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec)
        assert rec.stock_price is None


class TestUpdateMarketDataPointInTimeNearest:
    """point_in_time=True の最近傍探索・日付範囲フィルタ・latest_by_ec 整合の深掘り。

    最新レコードは最終ステップで latest_prices に上書きされるため、近傍探索の挙動は
    「最新でない（year が小さい）レコード」で観測する。
    """

    def test_bisect_selects_nearest_weekly(self, db, make_fin, make_weekly):
        # old_rec(2022) は最新でないため近傍探索の結果がそのまま残る
        old_rec = make_fin(year=2022, period_end="2022-03-31")
        new_rec = make_fin(year=2023, period_end="2023-03-31")  # latest
        db.add_all([old_rec, new_rec])
        # 2022-03-31 近傍: 03-28(差3日) を 03-07(差24日) より優先（bisect で前後2候補比較）
        db.add(make_weekly(trade_date="2022-03-07", close_last=2100.0))
        db.add(make_weekly(trade_date="2022-03-28", close_last=2200.0))
        db.add(make_weekly(trade_date="2023-03-27", close_last=5000.0))  # 最新側
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(old_rec); db.refresh(new_rec)
        assert old_rec.stock_price == 2200.0   # period_end 最近傍
        assert new_rec.stock_price == 5000.0   # latest 上書き

    def test_weekly_beyond_max_gap_not_matched(self, db, make_fin, make_weekly):
        # MAX_GAP_DAYS=30。old_rec の period_end から 30日超離れた weekly は不採用
        old_rec = make_fin(year=2022, period_end="2022-03-31")
        new_rec = make_fin(year=2023, period_end="2023-03-31")  # latest
        db.add_all([old_rec, new_rec])
        db.add(make_weekly(trade_date="2022-05-09", close_last=2200.0))  # 39日差 → 範囲外
        db.add(make_weekly(trade_date="2023-03-27", close_last=5000.0))  # 最新側
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(old_rec); db.refresh(new_rec)
        assert old_rec.stock_price is None     # gap > MAX_GAP_DAYS で不採用・既存値保持
        assert new_rec.stock_price == 5000.0

    def test_latest_record_overwritten_with_latest_price(self, db, make_fin, make_weekly):
        # 単独=最新レコード。近傍(3000)で一旦更新後、最終ステップで最新株価(4000)に上書き。
        # 最新週次(2023-06-26)は period_end±MAX_GAP の weekly 取得範囲外だが、
        # latest_prices は別クエリのため最新終値として引かれる（latest_by_ec 整合）。
        rec = make_fin(year=2023, period_end="2023-03-31", bs_bps=1000.0)
        db.add(rec)
        db.add(make_weekly(trade_date="2023-03-27", close_last=3000.0))
        db.add(make_weekly(trade_date="2023-06-26", close_last=4000.0))
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec)
        assert rec.stock_price == 4000.0       # 近傍3000ではなく最新4000で上書き
        assert rec.pbr == pytest.approx(4.0)

    def test_two_companies_matched_by_own_period_end(self, db, make_fin, make_weekly):
        rec1 = make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31")
        rec2 = make_fin(edinet_code="E00002", sec_code="1002",
                        year=2023, period_end="2023-09-30")
        db.add_all([rec1, rec2])
        db.add(make_weekly(edinet_code="E00001", trade_date="2023-03-27", close_last=1500.0))
        db.add(make_weekly(edinet_code="E00002", trade_date="2023-09-25", close_last=2500.0))
        db.commit()
        n = update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec1); db.refresh(rec2)
        assert rec1.stock_price == 1500.0      # 各社が自社 period_end 近傍で独立にマッチ
        assert rec2.stock_price == 2500.0
        assert n == 2


class TestPeriodTypeIsolation:
    """annual と H1 が同居する社で、株価・per/pbr の更新が annual へ入ること（#421）。

    `financial_metrics` VIEW は `period_type='annual'` 限定なので、更新が H1 行へ
    吸われると画面上は「annual の per/pbr が凍結」に見える。H1 の period_end が
    annual を追い越すケースは #424 子1（H1 の定期収集）で増える。
    """

    def test_latest_price_goes_to_annual_not_h1(self, db, make_fin, make_price):
        annual = make_fin(year=2026, period_end="2026-03-31", bs_bps=1000.0)
        h1 = make_fin(year=2026, period_end="2026-09-30", period_type="H1", bs_bps=1000.0)
        db.add_all([annual, h1])
        db.add(make_price(trade_date="2026-08-03", close=2000.0))
        db.commit()

        assert update_market_data_from_history(db) == 1
        db.refresh(annual); db.refresh(h1)
        assert annual.stock_price == 2000.0
        assert h1.stock_price is None          # H1 は日次の上書き対象外

    def test_point_in_time_overwrite_targets_annual(self, db, make_fin, make_weekly):
        """point_in_time でも「最新レコードへの現在株価上書き」は annual を選ぶ。"""
        annual = make_fin(year=2026, period_end="2026-03-31", bs_bps=1000.0)
        h1 = make_fin(year=2026, period_end="2026-09-30", period_type="H1", bs_bps=1000.0)
        db.add_all([annual, h1])
        db.add(make_weekly(trade_date="2026-03-30", close_last=3000.0))
        db.add(make_weekly(trade_date="2026-09-25", close_last=4000.0))
        db.add(make_weekly(trade_date="2026-10-30", close_last=5000.0))
        db.commit()

        update_market_data_from_history(db, point_in_time=True)
        db.refresh(annual); db.refresh(h1)
        assert annual.stock_price == 5000.0    # 最新株価で上書きされる
        assert h1.stock_price == 4000.0        # H1 は自身の period_end 近傍のまま


class TestBulkMarketValueUpdate:
    """株価・バリュエーション列の一括更新（#464）。

    旧実装は ORM 属性代入 + commit で**1件1 UPDATE**を発行しており、往復レイテンシに比例して
    遅くなっていた（GHA↔Supabase で 42,289件が143.1分）。`bulk_update_mappings` では解決せず
    （psycopg2 が束ねるのは INSERT だけ）、`UPDATE ... FROM (VALUES ...)` で 50秒になった。
    """

    def test_compute_market_values_matches_apply_to_record(self, db, make_fin):
        """純関数版と ORM 版が同じ値を出す（ロジックを二重持ちしていないこと）。"""
        from collector_prices import _compute_market_values, _apply_price_to_record

        rec = make_fin(pl_eps=100.0, bs_bps=500.0, issued_shares=1.0e6,
                       bs_total_equity=5.0e8, dps=20.0)
        db.add(rec)
        db.commit()

        vals = _compute_market_values(2000.0, rec.pl_eps, rec.bs_bps, rec.issued_shares,
                                      rec.bs_total_equity, rec.dps)
        _apply_price_to_record(rec, 2000.0)

        assert vals == {"stock_price": 2000.0, "per": 20.0, "pbr": 4.0,
                        "market_cap": 2000.0, "div_yield": 1.0}
        assert (rec.stock_price, rec.per, rec.pbr, rec.market_cap, rec.div_yield) == \
               (2000.0, 20.0, 4.0, 2000.0, 1.0)

    def test_uncomputable_columns_are_omitted(self):
        """計算できない指標はキーごと落とす（None で既存値を潰さないため）。"""
        from collector_prices import _compute_market_values

        vals = _compute_market_values(1000.0, pl_eps=0, bs_bps=None,
                                      issued_shares=None, bs_total_equity=None, dps=None)
        assert vals == {"stock_price": 1000.0}

    def test_bulk_apply_writes_values(self, db, make_fin):
        from collector_prices import _bulk_apply_market_values
        from database import FinancialRecord

        r1 = make_fin(edinet_code="E00001", pl_eps=100.0, bs_bps=500.0)
        r2 = make_fin(edinet_code="E00002", pl_eps=50.0, bs_bps=250.0)
        db.add_all([r1, r2])
        db.commit()

        _bulk_apply_market_values(db, {
            r1.id: {"stock_price": 2000.0, "per": 20.0},
            r2.id: {"stock_price": 500.0, "per": 10.0},
        })
        db.expire_all()

        assert db.query(FinancialRecord).filter_by(id=r1.id).one().stock_price == 2000.0
        assert db.query(FinancialRecord).filter_by(id=r2.id).one().per == 10.0

    def test_bulk_apply_does_not_null_out_missing_columns(self, db, make_fin):
        """渡されなかった列は現在値のまま（列集合を揃える実装が既存値を潰さないこと）。"""
        from collector_prices import _bulk_apply_market_values
        from database import FinancialRecord

        rec = make_fin(edinet_code="E00001", pl_eps=100.0, bs_bps=500.0)
        rec.per = 33.3
        rec.market_cap = 777.0
        db.add(rec)
        db.commit()
        rid = rec.id

        # stock_price だけ渡す＝per/market_cap は計算できなかったケース
        _bulk_apply_market_values(db, {rid: {"stock_price": 1234.0}})
        db.expire_all()

        got = db.query(FinancialRecord).filter_by(id=rid).one()
        assert got.stock_price == 1234.0
        assert got.per == 33.3          # 潰されない
        assert got.market_cap == 777.0  # 潰されない

    def test_bulk_apply_is_idempotent(self, db, make_fin):
        from collector_prices import _bulk_apply_market_values
        from database import FinancialRecord

        rec = make_fin(edinet_code="E00001", pl_eps=100.0, bs_bps=500.0)
        db.add(rec)
        db.commit()
        rid = rec.id

        for _ in range(2):
            _bulk_apply_market_values(db, {rid: {"stock_price": 1500.0, "pbr": 3.0}})
        db.expire_all()

        got = db.query(FinancialRecord).filter_by(id=rid).one()
        assert (got.stock_price, got.pbr) == (1500.0, 3.0)

    def test_bulk_apply_empty_is_noop(self, db):
        from collector_prices import _bulk_apply_market_values
        _bulk_apply_market_values(db, {})   # 例外を出さない

    def test_chunk_with_all_null_column_does_not_break_types(self, db, make_fin):
        """1列が**チャンク内で全行 NULL** でも型不一致で落ちない（#464）。

        psycopg2 はパラメータをクライアント側で literal 展開するため、CAST を付けないと
        Postgres がその列を text と推論し
        `column "pbr" is of type double precision but expression is of type text` で落ちる。
        値が1つでも混ざれば通るので**データ内容とチャンク境界次第で落ちる**不安定な失敗になる。
        """
        from collector_prices import _bulk_apply_market_values
        from database import FinancialRecord

        # bs_bps を持たない＝pbr が計算できない社だけを集めたチャンク
        r1 = make_fin(edinet_code="E00001", pl_eps=100.0, bs_bps=None)
        r2 = make_fin(edinet_code="E00002", pl_eps=50.0, bs_bps=None)
        db.add_all([r1, r2])
        db.commit()

        _bulk_apply_market_values(db, {
            r1.id: {"stock_price": 2000.0, "per": 20.0},
            r2.id: {"stock_price": 500.0, "per": 10.0},
        })
        db.expire_all()

        assert db.query(FinancialRecord).filter_by(id=r1.id).one().pbr is None
        assert db.query(FinancialRecord).filter_by(id=r2.id).one().stock_price == 500.0


class TestLatestRecordTieBreak:
    """同一 year の annual が複数ある社で、上書き対象が行順に依存しないこと（#464）。

    本番実測で 58組の (edinet_code, year) 重複があり、うち12社は最大 year で同点だった。
    year だけで比較すると勝者が `db.query(...).all()` の行順（ORDER BY 無しでは無保証）に
    依存し、同一データで2回走らせるだけで「最新株価で上書きされる行」が入れ替わる。
    `_fetch_latest_fin_by_ec` は ORDER BY year DESC, period_end DESC を使っており、
    point_in_time 経路だけ規則が違っていた。
    """

    def test_tie_on_year_resolved_by_period_end(self, db, make_company, make_fin, make_weekly):
        from collector_prices import update_market_data_from_history
        from database import FinancialRecord

        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        # 同一 year=2025 の annual が2件。period_end が新しい方が勝つべき
        old = make_fin(edinet_code="E00001", year=2025, period_end="2025-03-31",
                       pl_eps=100.0, bs_bps=500.0)
        new = make_fin(edinet_code="E00001", year=2025, period_end="2025-09-30",
                       pl_eps=100.0, bs_bps=500.0)
        db.add_all([old, new])
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-10-03", close_last=3000.0))
        db.commit()
        old_id, new_id = old.id, new.id

        update_market_data_from_history(db, point_in_time=True)
        db.expire_all()

        # 最新株価 3000 は period_end が新しい方へ入る
        assert db.query(FinancialRecord).filter_by(id=new_id).one().stock_price == 3000.0
        got_old = db.query(FinancialRecord).filter_by(id=old_id).one().stock_price
        assert got_old != 3000.0 or got_old is None

    def test_repeated_runs_are_stable(self, db, make_company, make_fin, make_weekly):
        """同点があっても連続実行で結果が動かない（非決定性の回帰テスト）。"""
        from collector_prices import update_market_data_from_history
        from database import FinancialRecord

        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.add_all([
            make_fin(edinet_code="E00001", year=2025, period_end="2025-03-31",
                     pl_eps=100.0, bs_bps=500.0),
            make_fin(edinet_code="E00001", year=2025, period_end="2025-09-30",
                     pl_eps=100.0, bs_bps=500.0),
        ])
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-10-03", close_last=3000.0))
        db.commit()

        snapshots = []
        for _ in range(3):
            update_market_data_from_history(db, point_in_time=True)
            db.expire_all()
            snapshots.append(sorted(
                (r.id, r.stock_price, r.per, r.pbr)
                for r in db.query(FinancialRecord).all()))

        assert snapshots[0] == snapshots[1] == snapshots[2]
