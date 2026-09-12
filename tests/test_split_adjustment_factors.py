"""分割補正係数（バリュエーション基準の不一致・#655・ADR-0055）のテスト。

ここで守るのは4つ。

1. **検出アルゴリズムを写していない**——`rebuild_split_adjustment_factors` が書く factor は
   `scripts/measure_split_valuation_bias` の純関数が出す値と厳密に一致する。
2. **寄与イベントの集合と factor が整合する**——`kinds` / `n_events` は `collector_prices` 側で
   引き直しているので、`e.year > row.year` の述語が正本から乖離したらここで落ちる。
3. **F=1.0 の行は書かない**（VIEW が `COALESCE(...,1.0)` で埋める前提）。
4. **検出0件では既存の表に触らず失敗する**——全置換の順序で「消してから失敗」にすると、
   補正が静かに全部外れた VIEW が残る（どの値も妥当な株価指標なのでエラーは出ない）。

補正の**適用**（VIEW の `× F` / `÷ F`）は SQLite では検証できない。テストの
`financial_metrics` は VIEW ではなく ORM 列から生成した実テーブルで、STDDEV/WINDOW を
持たない SQLite に同等の VIEW を作れないため（`tests/conftest.py` の `db` fixture 参照）。
代わりに VIEW 定義 SQL の**向き**をソース照合し、実値の突合は Postgres 側で行う。
"""
from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector_prices import rebuild_split_adjustment_factors  # noqa: E402
from database import (  # noqa: E402
    FinancialRecord, SplitAdjustmentFactor, replace_split_adjustment_factors,
)
from scripts import measure_split_valuation_bias as M  # noqa: E402

VIEW_SQL = (ROOT / "sql" / "financial_metrics_view.sql").read_text(encoding="utf-8")


def _seed_split_company(db, make_fin, ec="E00001"):
    """2021年に 1:2 分割した社。株数が倍・BPS が半分で交差検証を通る。

    F は「その行の年より**後**」のイベントの積なので、2019 と 2020 の行だけが F=2.0、
    2021（分割当年・既に新株数基準の提出値）と 2022 は F=1.0 になる。
    """
    spec = [
        (2019, 1000.0, 2000.0),
        (2020, 1000.0, 2100.0),
        (2021, 2000.0, 1050.0),   # sh_ratio=2.0 / bps_ratio=2100/1050=2.0 → split
        (2022, 2000.0, 1100.0),
    ]
    for year, shares, bps in spec:
        db.add(make_fin(edinet_code=ec, year=year, period_end=date(year, 3, 31),
                        issued_shares=shares, bs_bps=bps, pl_eps=100.0, dps=20.0,
                        stock_price=1000.0, per=10.0, pbr=0.5,
                        div_yield=2.0, market_cap=5000.0))
    db.commit()


def _seed_quiet_company(db, make_fin, ec="E00002"):
    """株数が一切動かない社。イベントは検出されない＝1行も書かれない。"""
    for year in (2019, 2020, 2021, 2022):
        db.add(make_fin(edinet_code=ec, year=year, period_end=date(year, 3, 31),
                        issued_shares=500.0, bs_bps=800.0, pl_eps=50.0, dps=10.0,
                        stock_price=400.0, per=8.0, pbr=0.5,
                        div_yield=2.5, market_cap=200.0))
    db.commit()


class TestRebuild:
    def test_factor_matches_the_pure_functions(self, db, make_fin):
        """書いた factor が測定器の純関数の出力と厳密に一致する（写していない証拠）。"""
        _seed_split_company(db, make_fin)
        _seed_quiet_company(db, make_fin)

        n = rebuild_split_adjustment_factors(db)

        rows = [M.AnnualRow(
            r.edinet_code, r.year, r.period_end, r.issued_shares, r.bs_bps, r.pl_eps,
            r.dps, r.stock_price, r.per, r.pbr, r.div_yield, r.market_cap,
        ) for r in db.query(FinancialRecord).filter_by(period_type="annual").all()]
        events, _ = M.detect_events(rows)
        expected = {k: v for k, v in M.cumulative_factors(rows, events).items() if v != 1.0}

        stored = {(r.edinet_code, r.year): r.factor
                  for r in db.query(SplitAdjustmentFactor).all()}
        assert stored == expected
        assert n == len(expected)

    def test_only_rows_before_the_event_are_written(self, db, make_fin):
        """分割当年とそれ以降は F=1.0 なので書かれない。"""
        _seed_split_company(db, make_fin)
        rebuild_split_adjustment_factors(db)

        got = {(r.year, r.factor) for r in db.query(SplitAdjustmentFactor).all()}
        assert got == {(2019, 2.0), (2020, 2.0)}

    def test_quiet_company_gets_no_row(self, db, make_fin):
        """株数が動かない社は1行も持たない（VIEW 側が 1.0 を埋める）。"""
        _seed_split_company(db, make_fin)
        _seed_quiet_company(db, make_fin)
        rebuild_split_adjustment_factors(db)

        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00002").count() == 0

    def test_kinds_and_n_events_multiply_back_to_the_factor(self, db, make_fin):
        """寄与イベントの積が factor に一致する。

        `kinds` / `n_events` は `collector_prices` 側で寄与集合を引き直して作るため、
        `e.year > row.year` の述語が正本（`cumulative_factors`）から乖離しうる。
        積で突き合わせることでその乖離をここで捕まえる。
        """
        _seed_split_company(db, make_fin)
        rebuild_split_adjustment_factors(db)

        rows = [M.AnnualRow(
            r.edinet_code, r.year, r.period_end, r.issued_shares, r.bs_bps, r.pl_eps,
            r.dps, r.stock_price, r.per, r.pbr, r.div_yield, r.market_cap,
        ) for r in db.query(FinancialRecord).filter_by(period_type="annual").all()]
        events, _ = M.detect_events(rows)

        for saf in db.query(SplitAdjustmentFactor).all():
            contrib = [e for e in events
                       if e.edinet_code == saf.edinet_code and e.canonical is not None
                       and e.year > saf.year]
            prod = math.prod(e.canonical for e in contrib)
            assert saf.n_events == len(contrib)
            assert saf.factor == pytest.approx(prod)
            assert saf.kinds == ",".join(sorted({e.kind for e in contrib}))
        assert {r.kinds for r in db.query(SplitAdjustmentFactor).all()} == {"split"}

    def test_no_events_preserves_the_existing_table(self, db, make_fin):
        """検出0件では既存の係数を消さずに失敗する。

        消してから失敗すると、補正が全部外れた VIEW が残り、しかも値はどれも
        妥当な株価指標なのでエラーとしては現れない（#508 と同型の沈黙する壊れ方）。
        """
        _seed_quiet_company(db, make_fin)
        replace_split_adjustment_factors(
            db, [{"edinet_code": "E09999", "year": 2020, "factor": 3.0,
                  "n_events": 1, "kinds": "split"}])
        db.commit()

        with pytest.raises(RuntimeError, match="1件も作れなかった"):
            rebuild_split_adjustment_factors(db)

        db.rollback()
        survived = db.query(SplitAdjustmentFactor).all()
        assert [(r.edinet_code, r.year, r.factor) for r in survived] == [("E09999", 2020, 3.0)]

    def test_empty_db_skips_instead_of_failing(self, db):
        """annual 行が0件ならスキップする（失敗にしない）。

        初回ブートストラップ前やスタブ DB でここを失敗にすると、空の DB からの立ち上げと
        パイプラインのテストが通らない。「走らなかった」の検知は収集本体が担う——annual 行が
        消えていれば前段がとうに失敗している。**「入力が無い」と「入力はあるのに作れない」を
        取り違えないこと**が要点で、後者だけが検出の壊れを意味する。
        """
        assert rebuild_split_adjustment_factors(db) == 0
        assert db.query(SplitAdjustmentFactor).count() == 0

    def test_rebuild_is_a_full_replacement(self, db, make_fin):
        """前回の係数が残らない。F が減る（訂正で検出が消える）ケースで二重補正を防ぐ。"""
        _seed_split_company(db, make_fin)
        replace_split_adjustment_factors(
            db, [{"edinet_code": "E09999", "year": 2020, "factor": 9.0,
                  "n_events": 1, "kinds": "split"}])
        db.commit()

        rebuild_split_adjustment_factors(db)

        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E09999").count() == 0

    def test_unsnapped_events_are_excluded(self, db, make_fin):
        """定番比へ寄せられない比（`unsnapped`）は積に入らない＝行が書かれない。

        `cumulative_factors(use_canonical=True)` が canonical=None を飛ばすため。
        観測比 7.0 は `CANONICAL_RATIOS` に無く、残差も composite の帯の外。
        """
        ec = "E00003"
        for year, shares, bps in ((2020, 1000.0, 7000.0), (2021, 7000.0, 1000.0)):
            db.add(make_fin(edinet_code=ec, year=year, period_end=date(year, 3, 31),
                            issued_shares=shares, bs_bps=bps, pl_eps=100.0, dps=20.0,
                            stock_price=1000.0, per=10.0, pbr=0.5,
                            div_yield=2.0, market_cap=5000.0))
        db.commit()

        _, _, kind = M.snap_to_canonical(7.0, tol=M.DEFAULT_SNAP_TOL)
        assert kind == "unsnapped", "前提が崩れている（7.0 が定番比へ寄るようになった）"

        with pytest.raises(RuntimeError, match="1件も作れなかった"):
            rebuild_split_adjustment_factors(db)


class TestViewAppliesTheDirections:
    """VIEW 定義 SQL の補正の向きを照合する。

    実値の突合は Postgres でしか出来ないが、**向きの取り違えは符号が逆の歪みを新しく作る**
    ので、せめて演算子だけはソースで縛る。唯一の源は
    `measure_split_valuation_bias.COLUMN_DIRECTION`。
    """

    def test_every_distorted_column_is_corrected_in_the_right_direction(self):
        for col, sign in M.COLUMN_DIRECTION.items():
            if col == "nc_ratio":
                # VIEW 内で補正後 market_cap から計算されるので、この列を直接は触らない。
                continue
            op = "*" if sign > 0 else "/"
            assert f"fr.{col}" in VIEW_SQL, f"{col} が VIEW から消えている"
            line = next(l for l in VIEW_SQL.splitlines()
                        if f"fr.{col}" in l and "saf.factor" in l)
            assert op in line.split(f"fr.{col}", 1)[1].split("saf.factor", 1)[0], (
                f"{col} の補正の向きが COLUMN_DIRECTION（{sign:+d}）と違う: {line.strip()}")

    def test_nc_ratio_denominator_reads_the_corrected_market_cap(self):
        """`nc_ratio` は CTE d の補正後 market_cap を参照する＝別途 ÷F しない。"""
        assert "d.market_cap * 1000000" in VIEW_SQL
        assert "saf.factor" not in VIEW_SQL.split("AS nc_ratio")[0].rsplit("net_cash", 1)[-1]

    def test_split_factor_is_exposed(self):
        """適用した F が列として見える（補正が効いた行を SQL / API から追える）。"""
        assert "AS split_factor" in VIEW_SQL

    def test_stock_price_is_left_alone(self):
        """`stock_price` は補正しない（COLUMN_DIRECTION に無い・意図した非対称）。"""
        assert "stock_price" not in M.COLUMN_DIRECTION
        line = next(l for l in VIEW_SQL.splitlines() if "fr.stock_price" in l)
        assert "saf.factor" not in line
