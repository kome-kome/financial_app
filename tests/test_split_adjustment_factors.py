"""分割補正係数（バリュエーション基準の不一致・#655・ADR-0055）のテスト。

ここで守るのは4つ。

1. **検出アルゴリズムを写していない**——`rebuild_split_adjustment_factors` が書く factor は
   台帳（`corporate_actions`）の検出器の純関数が出す値と厳密に一致する。
2. **寄与イベントの集合と factor が整合する**——`kinds` / `n_events` は `Ledger.factor_rows` で
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

import corporate_actions as C  # noqa: E402
from corporate_actions import (  # noqa: E402
    build_ledger, load_jquants_adj_factor_coverage, load_jquants_adj_factor_events,
    load_price_series, rebuild_split_adjustment_factors,
)
from database import (  # noqa: E402
    FinancialRecord, JQuantsAdjFactorCoverage, JQuantsAdjFactorEvent, SplitAdjustmentFactor,
    StockPriceWeekly, replace_split_adjustment_factors, upsert_jquants_adj_factor_coverage,
    upsert_jquants_adj_factor_events,
)

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

        rows = [C.AnnualRow(
            r.edinet_code, r.year, r.period_end, r.issued_shares, r.bs_bps, r.pl_eps,
            r.dps, r.stock_price, r.per, r.pbr, r.div_yield, r.market_cap, r.bs_total_equity,
        ) for r in db.query(FinancialRecord).filter_by(period_type="annual").all()]
        events, _ = C.detect_events(rows)
        expected = {k: v for k, v in C.cumulative_factors(rows, events).items() if v != 1.0}

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

        `kinds` / `n_events` は `Ledger.factor_rows` で寄与集合を引き直して作るため、
        `e.year > row.year` の述語が正本（`cumulative_factors`）から乖離しうる。
        積で突き合わせることでその乖離をここで捕まえる。
        """
        _seed_split_company(db, make_fin)
        rebuild_split_adjustment_factors(db)

        rows = [C.AnnualRow(
            r.edinet_code, r.year, r.period_end, r.issued_shares, r.bs_bps, r.pl_eps,
            r.dps, r.stock_price, r.per, r.pbr, r.div_yield, r.market_cap, r.bs_total_equity,
        ) for r in db.query(FinancialRecord).filter_by(period_type="annual").all()]
        events, _ = C.detect_events(rows)

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

        _, _, kind = C.snap_to_canonical(7.0, tol=C.DEFAULT_SNAP_TOL)
        assert kind == "unsnapped", "前提が崩れている（7.0 が定番比へ寄るようになった）"

        with pytest.raises(RuntimeError, match="1件も作れなかった"):
            rebuild_split_adjustment_factors(db)


class TestLedgerIsWhatRebuildWrites:
    """`build_ledger` は書き込まずに、係数表と同じ F を返す（#685・#746）。

    リークの測定（`scripts/measure_split_leak.py`）と TTM 合成はこの台帳で F とイベントを作る。
    係数表の入力を読み手ごとに揃え直すと、読み忘れがあっても値はもっともらしいまま出る。
    """

    def test_empty_db_is_none(self, db):
        assert build_ledger(db) is None

    def test_does_not_write(self, db, make_fin):
        _seed_split_company(db, make_fin)
        build_ledger(db)
        assert db.query(SplitAdjustmentFactor).count() == 0

    def test_nontrivial_factors_equal_the_table(self, db, make_fin):
        _seed_split_company(db, make_fin)
        _seed_quiet_company(db, make_fin)
        ledger = build_ledger(db)
        assert ledger.events and "n_official_companies" in ledger.stats
        assert {r.edinet_code for r in ledger.rows} == {"E00001", "E00002"}

        rebuild_split_adjustment_factors(db)

        table = {(r.edinet_code, r.year): r.factor
                 for r in db.query(SplitAdjustmentFactor).all()}
        assert table == {k: v for k, v in ledger.factors.items() if v != 1.0}

    def test_a_given_ledger_is_written_without_detecting_again(self, db, make_fin, monkeypatch):
        """パイプラインが作った台帳を渡すと、ここでは検出し直さない＝係数表と TTM が同じ検出を使う。"""
        _seed_split_company(db, make_fin)
        ledger = build_ledger(db)
        calls: list = []
        real = C.detect_events

        def spy(rows, **kw):
            calls.append(1)
            return real(rows, **kw)

        monkeypatch.setattr(C, "detect_events", spy)
        assert rebuild_split_adjustment_factors(db, ledger=ledger) == 2
        assert calls == []

    def test_ledger_and_bps_path_are_not_given_together(self, db, make_fin):
        """台帳は作った時点の bps_path で固まっている。両方渡すと片方が黙って無視される。"""
        _seed_split_company(db, make_fin)
        with pytest.raises(ValueError, match="同時に渡さない"):
            rebuild_split_adjustment_factors(db, ledger=build_ledger(db), bps_path=True)


def _seed_bps_path_company(db, make_fin, ec="E00004"):
    """株数が分割に追随しない社（#656）。E03137 しまむらの形。

    2022 に 1:2 分割したが `issued_shares` は据え置きで、`bs_bps` と `pl_eps` だけが半分に
    なる。第1経路は候補にすら上げられない＝第2経路だけが拾える。

    **株数は翌年（2023）に x2 される。** これが倍率を決める第3の信号で、無ければ
    第2経路はイベントを採らない（#659）。第1経路は 2023 のペアを交差検証で落とす
    （bps は下がらず上がっている）ので、同じ動きが2件に数えられることもない。
    """
    spec = [
        (2020, 1000.0, 2000.0, 200.0),
        (2021, 1000.0, 2100.0, 210.0),
        (2022, 1000.0, 1050.0, 105.0),   # bps_ratio=2.0 / eps_ratio=2.0 → split
        (2023, 2000.0, 1100.0, 110.0),   # 株数が1年遅れて追随＝倍率の出どころ
    ]
    for year, shares, bps, eps in spec:
        db.add(make_fin(edinet_code=ec, year=year, period_end=date(year, 3, 31),
                        issued_shares=shares, bs_bps=bps, pl_eps=eps, dps=20.0,
                        stock_price=1000.0, per=10.0, pbr=0.5,
                        div_yield=2.0, market_cap=5000.0))
    db.commit()


class TestBpsPathReachesTheTable:
    """第2経路（#656）が係数表まで届くこと。既定の ON/OFF は検出器側が唯一の源。"""

    def test_default_follows_the_detector(self, db, make_fin):
        """呼び出し側は既定を**書き写さない**。写すと2箇所が乖離する。"""
        _seed_bps_path_company(db, make_fin)
        _seed_split_company(db, make_fin)      # 検出0件で失敗しないよう第1経路の社も置く

        rebuild_split_adjustment_factors(db)

        got = db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00004").count()
        assert (got > 0) is C.DEFAULT_BPS_PATH

    def test_enabled_writes_the_factor_and_kinds(self, db, make_fin):
        _seed_bps_path_company(db, make_fin)
        rebuild_split_adjustment_factors(db, bps_path=True)

        got = {(r.year, r.factor, r.n_events, r.kinds)
               for r in db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00004").all()}
        assert got == {(2020, 2.0, 1, "split"), (2021, 2.0, 1, "split")}

    def test_disabled_leaves_the_company_alone(self, db, make_fin):
        _seed_bps_path_company(db, make_fin)
        _seed_split_company(db, make_fin)

        rebuild_split_adjustment_factors(db, bps_path=False)

        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00004").count() == 0
        # 第1経路の社は影響を受けない
        assert {(r.year, r.factor) for r in db.query(SplitAdjustmentFactor)
                .filter_by(edinet_code="E00001").all()} == {(2019, 2.0), (2020, 2.0)}

    def test_both_paths_do_not_square_the_factor(self, db, make_fin):
        """株数も bps も動く普通の分割は 1 件へ畳む。畳み損ねると F が 2.0 -> 4.0 になる。"""
        _seed_split_company(db, make_fin)
        rebuild_split_adjustment_factors(db, bps_path=True)

        got = {(r.year, r.factor, r.n_events)
               for r in db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00001").all()}
        assert got == {(2019, 2.0, 1), (2020, 2.0, 1)}


def _seed_latest_year_bps_company(db, make_fin, ec="E00006"):
    """最新年（2022）に bps / eps だけが半分になり、翌年の行がまだ無い社（#661）。

    翌年の株数という第3の信号が無いので #659 までは採らない。公式 `AdjFactor` が
    イベント窓 (2021-03-31 - 45日, 2022-03-31 + 45日] の中にあれば、そこから倍率を取る。
    """
    for year, shares, bps, eps in ((2020, 1000.0, 2000.0, 200.0),
                                   (2021, 1000.0, 2100.0, 210.0),
                                   (2022, 1000.0, 1050.0, 105.0)):
        db.add(make_fin(edinet_code=ec, year=year, period_end=date(year, 3, 31),
                        issued_shares=shares, bs_bps=bps, pl_eps=eps, dps=20.0,
                        stock_price=1000.0, per=10.0, pbr=0.5,
                        div_yield=2.0, market_cap=5000.0))
    db.commit()


class TestOfficialEventsReachTheTable:
    """catchup が残した公式 AdjFactor（#661）が係数表まで届くこと。rebuild は DB だけを読む。"""

    def _official(self, db, rows):
        upsert_jquants_adj_factor_events(db, rows)
        db.commit()

    def test_empty_table_leaves_the_latest_year_alone(self, db, make_fin):
        """表が空の夜（取り込み前・catchup が書けなかった）は #659 と同じく採らない。"""
        _seed_latest_year_bps_company(db, make_fin)
        _seed_split_company(db, make_fin)      # 検出0件で失敗しないよう第1経路の社も置く

        rebuild_split_adjustment_factors(db, bps_path=True)

        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00006").count() == 0

    def test_official_event_fills_the_latest_year(self, db, make_fin):
        _seed_latest_year_bps_company(db, make_fin)
        self._official(db, [{"edinet_code": "E00006", "event_date": "2021-10-01",
                             "adj_factor": 0.5, "jq_code": "99990"}])

        rebuild_split_adjustment_factors(db, bps_path=True)

        got = {(r.year, r.factor, r.n_events, r.kinds)
               for r in db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00006").all()}
        assert got == {(2020, 2.0, 1, "split"), (2021, 2.0, 1, "split")}

    def test_factor_matches_the_pure_functions_given_the_same_official_events(self, db, make_fin):
        """表の値は、同じ公式イベントを純関数に渡した結果と厳密に一致する（写していない証拠）。"""
        _seed_latest_year_bps_company(db, make_fin)
        _seed_bps_path_company(db, make_fin)
        _seed_split_company(db, make_fin)
        self._official(db, [
            {"edinet_code": "E00006", "event_date": "2021-10-01", "adj_factor": 0.5, "jq_code": None},
            # 翌年の株数がある社の公式イベント（食い違い）。倍率は翌年の株数のまま動かない
            {"edinet_code": "E00004", "event_date": "2021-10-01", "adj_factor": 0.2, "jq_code": None},
        ])

        rebuild_split_adjustment_factors(db, bps_path=True)

        rows = [C.AnnualRow(
            r.edinet_code, r.year, r.period_end, r.issued_shares, r.bs_bps, r.pl_eps,
            r.dps, r.stock_price, r.per, r.pbr, r.div_yield, r.market_cap, r.bs_total_equity,
        ) for r in db.query(FinancialRecord).filter_by(period_type="annual")
            .order_by(FinancialRecord.edinet_code, FinancialRecord.year).all()]
        events, _ = C.detect_events(rows, bps_path=True,
                                    official_events=load_jquants_adj_factor_events(db))
        expected = {k: v for k, v in C.cumulative_factors(rows, events).items() if v != 1.0}
        stored = {(r.edinet_code, r.year): r.factor
                  for r in db.query(SplitAdjustmentFactor).all()}
        assert stored == expected
        # 食い違った公式値は E00004 の係数を動かさない（翌年の株数 x2 のまま）
        assert stored[("E00004", 2020)] == pytest.approx(2.0)

    def test_rebuild_passes_what_the_table_holds(self, db, make_fin, monkeypatch):
        """読み込みを書き忘れると、公式の値は表にあるのに黙って使われない。"""
        _seed_split_company(db, make_fin)
        self._official(db, [{"edinet_code": "E00001", "event_date": "2020-10-01",
                             "adj_factor": 0.5, "jq_code": "12340"}])
        seen: dict = {}
        real = C.detect_events

        def spy(rows, **kw):
            seen.update(kw)
            return real(rows, **kw)

        monkeypatch.setattr(C, "detect_events", spy)
        rebuild_split_adjustment_factors(db)
        assert seen["official_events"] == {"E00001": [("2020-10-01", 0.5)]}


def _seed_relisted_company(db, make_fin, ec="E00007"):
    """E05714 型: 上場廃止した旧社の行と、再上場した新社の行が欠損年をまたいで隣り合う社（#672）。

    株数比 15.56・bps 逆比 16.91 は E05714 の実値。週次は再上場の週から始まる。
    """
    for year, shares, bps, equity in ((2020, 435087405.0, 1584.9, 691978000000.0),
                                      (2026, 6770358214.0, 93.74, 629284000000.0)):
        db.add(make_fin(edinet_code=ec, year=year, period_end=date(year, 3, 31),
                        issued_shares=shares, bs_bps=bps, pl_eps=100.0, dps=20.0,
                        stock_price=1000.0, per=10.0, pbr=0.5,
                        div_yield=2.0, market_cap=5000.0, bs_total_equity=equity))
    _seed_weekly(db, {ec: ["2025-09-29", "2025-10-06"], "E00001": ["2019-07-29"]})


def _seed_weekly(db, weeks_by_ec):
    for ec, weeks in weeks_by_ec.items():
        for ws in weeks:
            db.add(StockPriceWeekly(edinet_code=ec, week_start=ws, trade_date=ws, close_last=100.0))
    db.commit()


class TestListingGapReachesTheTable:
    """上場廃止をまたぐペアの判定（#672）の入力が係数表まで届くこと。"""

    def test_load_price_series_returns_the_first_week_and_the_holes(self, db):
        """空白は隣り合う週の間隔が閾値以上のものだけ。E03530 型（途中で途切れる）を拾う入力。"""
        _seed_weekly(db, {
            "E00001": ["2020-01-06", "2019-07-29", "2019-08-05"],
            "E00002": ["2025-09-29"],
            # 2023-09-25 の次が 2025-11-17（784 日）。364 日の間隔は空白に数えない
            "E00003": ["2022-09-26", "2023-09-25", "2025-11-17", "2025-11-24"],
        })
        assert load_price_series(db, min_hole_days=365) == {
            "E00001": ("2019-07-29", ()),
            "E00002": ("2025-09-29", ()),
            "E00003": ("2022-09-26", (("2023-09-25", "2025-11-17"),)),
        }

    def test_rebuild_passes_the_series_starts(self, db, make_fin, monkeypatch):
        """読み込みを書き忘れると判定は黙って無効になり、15 が E05714 型を補正へ入れる。"""
        _seed_split_company(db, make_fin)
        _seed_relisted_company(db, make_fin)
        seen: dict = {}
        real = C.detect_events

        def spy(rows, **kw):
            seen.update(kw)
            return real(rows, **kw)

        monkeypatch.setattr(C, "detect_events", spy)
        rebuild_split_adjustment_factors(db)
        assert seen["price_series"] == {"E00007": ("2025-09-29", ()), "E00001": ("2019-07-29", ())}

    def test_relisted_company_gets_no_factor(self, db, make_fin):
        _seed_split_company(db, make_fin)          # 検出0件で失敗しないよう普通の分割も置く
        _seed_relisted_company(db, make_fin)

        rebuild_split_adjustment_factors(db)

        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00007").count() == 0
        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00001").count() == 2


class TestAdjFactorEventTable:
    def test_upsert_updates_the_value_and_keeps_first_seen(self, db):
        upsert_jquants_adj_factor_events(db, [{"edinet_code": "E00001", "event_date": "2020-10-01",
                                               "adj_factor": 0.5, "jq_code": "12340"}])
        db.commit()
        first = db.query(JQuantsAdjFactorEvent).one().first_seen_at
        upsert_jquants_adj_factor_events(db, [{"edinet_code": "E00001", "event_date": "2020-10-01",
                                               "adj_factor": 0.25, "jq_code": "12340"},
                                              {"edinet_code": "E00001", "event_date": "2019-04-01",
                                               "adj_factor": 0.5, "jq_code": "12340"}])
        db.commit()
        db.expire_all()
        got = db.query(JQuantsAdjFactorEvent).filter_by(event_date="2020-10-01").one()
        assert got.adj_factor == 0.25
        assert got.first_seen_at == first
        # 読み出しは社ごとに日付順
        assert load_jquants_adj_factor_events(db) == {
            "E00001": [("2019-04-01", 0.5), ("2020-10-01", 0.25)]}

    def test_empty_upsert_writes_nothing(self, db):
        assert upsert_jquants_adj_factor_events(db, []) == 0
        assert load_jquants_adj_factor_events(db) == {}


def _seed_false_positive_company(db, make_fin, ec="E00008"):
    """E01121 日本板硝子の実値を写した偽陽性の社（#668）。株数 x1.5545・bps の交差検証も
    純資産比（x1.3027）も通るので、公式の不在を確かめない限り composite 1.5 で補正される。

    2024 年の行は 2025 と同じ株数で置き、F=1.5 の行が 2024 / 2025 の2本になるようにした。
    """
    for year, shares, bps, eps, equity in (
            (2024, 91431499.0, 3111.09, 100.0, 153838000000.0),
            (2025, 91568599.0, 3182.04, -173.2, 142411000000.0),
            (2026, 142341906.0, 2230.45, 44.51, 185519000000.0)):
        db.add(make_fin(edinet_code=ec, year=year, period_end=date(year, 3, 31),
                        issued_shares=shares, bs_bps=bps, pl_eps=eps, dps=20.0,
                        stock_price=1000.0, per=10.0, pbr=0.5,
                        div_yield=2.0, market_cap=5000.0, bs_total_equity=equity))
    db.commit()


class TestOfficialAbsenceReachesTheTable:
    """公式の取得区間（#668）が係数表まで届くこと。rebuild は DB だけを読み J-Quants を叩かない。"""

    # E01121 に J-Quants が実際に返した区間（2026-09-14・486 本）
    SPAN = {"edinet_code": "E00008", "first_bar_date": "2024-06-24",
            "last_bar_date": "2026-06-22", "n_bars": 486, "jq_code": "52020"}

    def test_no_record_keeps_the_correction(self, db, make_fin):
        """記録表が空の夜は今日までどおり（公式イベントが無いだけでは外さない・決定4-5）。"""
        _seed_false_positive_company(db, make_fin)
        _seed_split_company(db, make_fin)

        rebuild_split_adjustment_factors(db)

        got = {(r.year, r.factor, r.kinds)
               for r in db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00008").all()}
        assert got == {(2024, 1.5, "composite"), (2025, 1.5, "composite")}

    def test_confirmed_absence_removes_the_correction(self, db, make_fin):
        _seed_false_positive_company(db, make_fin)
        _seed_split_company(db, make_fin)          # 本物の分割（記録なし）は残る
        upsert_jquants_adj_factor_coverage(db, [self.SPAN])
        db.commit()

        rebuild_split_adjustment_factors(db)

        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00008").count() == 0
        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00001").count() == 2

    def test_real_split_with_an_official_event_is_kept(self, db, make_fin):
        """本物の分割は、区間があっても窓の中に公式イベントがあるので外れない。"""
        _seed_split_company(db, make_fin)          # 2021 の 1:2・窓 (2020-02-15, 2021-05-15]
        upsert_jquants_adj_factor_coverage(db, [dict(self.SPAN, edinet_code="E00001",
                                                     first_bar_date="2019-06-24",
                                                     last_bar_date="2021-06-22")])
        upsert_jquants_adj_factor_events(db, [{"edinet_code": "E00001", "event_date": "2020-09-29",
                                               "adj_factor": 0.5, "jq_code": "12340"}])
        db.commit()

        rebuild_split_adjustment_factors(db)

        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00001").count() == 2

    def test_rebuild_merges_the_spans_before_passing(self, db, make_fin, monkeypatch):
        """区間は取り込みのたびに追記される。併合せずに渡すと、2本にまたがる窓が確かめられない。"""
        _seed_split_company(db, make_fin)
        upsert_jquants_adj_factor_coverage(db, [
            dict(self.SPAN, first_bar_date="2025-01-06", last_bar_date="2026-06-22"),
            dict(self.SPAN, first_bar_date="2024-06-24", last_bar_date="2025-06-30"),
        ])
        db.commit()
        seen: dict = {}
        real = C.detect_events

        def spy(rows, **kw):
            seen.update(kw)
            return real(rows, **kw)

        monkeypatch.setattr(C, "detect_events", spy)
        rebuild_split_adjustment_factors(db)
        assert seen["official_coverage"] == {"E00008": [("2024-06-24", "2026-06-22")]}


class TestAdjFactorCoverageTable:
    SPAN = {"edinet_code": "E00001", "first_bar_date": "2024-06-24",
            "last_bar_date": "2026-06-22", "n_bars": 486, "jq_code": "12340"}

    def test_same_span_is_updated_not_duplicated(self, db):
        upsert_jquants_adj_factor_coverage(db, [self.SPAN])
        db.commit()
        upsert_jquants_adj_factor_coverage(db, [dict(self.SPAN, n_bars=487)])
        db.commit()
        db.expire_all()
        got = db.query(JQuantsAdjFactorCoverage).one()
        assert got.n_bars == 487

    def test_other_spans_are_kept_and_returned_raw(self, db):
        """**追記であって上書きではない**。古い期間の「確かめた」が消えると外した補正が黙って戻る。"""
        upsert_jquants_adj_factor_coverage(db, [self.SPAN])
        db.commit()
        upsert_jquants_adj_factor_coverage(db, [dict(self.SPAN, first_bar_date="2024-09-02",
                                                     last_bar_date="2026-09-01", n_bars=480)])
        db.commit()
        assert load_jquants_adj_factor_coverage(db) == {
            "E00001": [("2024-06-24", "2026-06-22"), ("2024-09-02", "2026-09-01")]}

    def test_empty_upsert_writes_nothing(self, db):
        assert upsert_jquants_adj_factor_coverage(db, []) == 0
        assert load_jquants_adj_factor_coverage(db) == {}


def _seed_issuance_company(db, make_fin, ec="E00005"):
    """株数 x2・bps 半減で第1経路の交差検証は通るが、純資産総額が x2.16 になっている社（#657）。

    `bs_bps` と純資産総額が食い違う形で、純資産比は E05716 地域新聞社の実測（x2.1611）に揃えた。
    既定の許容（1.0＝2倍超）の外側なので、純資産比チェックが有効なら増資と読んで採らない。
    """
    for year, shares, bps, equity in ((2020, 1000.0, 2000.0, 2.0e6),
                                      (2021, 2000.0, 1000.0, 4.32e6)):
        db.add(make_fin(edinet_code=ec, year=year, period_end=date(year, 3, 31),
                        issued_shares=shares, bs_bps=bps, pl_eps=100.0, dps=20.0,
                        stock_price=1000.0, per=10.0, pbr=0.5,
                        div_yield=2.0, market_cap=5000.0, bs_total_equity=equity))
    db.commit()


class TestEquityCheckReachesTheTable:
    """純資産比チェック（#657）の入力と既定が係数表まで届くこと。既定は検出器側が唯一の源。"""

    def test_rebuild_passes_the_total_equity(self, db, make_fin, monkeypatch):
        """SELECT に列を足し忘れると、チェックは全社で「判定不能」になり黙って効かない。"""
        _seed_issuance_company(db, make_fin)
        _seed_split_company(db, make_fin)      # 既定で増資型が落ちても検出0件で失敗しないように
        seen: list = []
        real = C.detect_events

        def spy(rows, **kw):
            seen.extend(rows)
            return real(rows, **kw)

        monkeypatch.setattr(C, "detect_events", spy)
        rebuild_split_adjustment_factors(db)
        assert sorted((r.year, r.bs_total_equity) for r in seen
                      if r.edinet_code == "E00005") == [(2020, 2.0e6), (2021, 4.32e6)]

    def test_default_follows_the_detector(self, db, make_fin):
        """呼び出し側は既定を**書き写さない**。写すと2箇所が乖離する。"""
        _seed_issuance_company(db, make_fin)
        _seed_split_company(db, make_fin)      # 検出0件で失敗しないよう普通の分割も置く

        rebuild_split_adjustment_factors(db)

        got = db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00005").count()
        assert (got > 0) is (C.DEFAULT_EQUITY_TOL is None)
        assert db.query(SplitAdjustmentFactor).filter_by(edinet_code="E00001").count() == 2


class TestViewAppliesTheDirections:
    """VIEW 定義 SQL の補正の向きを照合する。

    実値の突合は Postgres でしか出来ないが、**向きの取り違えは符号が逆の歪みを新しく作る**
    ので、せめて演算子だけはソースで縛る。唯一の源は
    `corporate_actions.COLUMN_DIRECTION`。
    """

    def test_every_distorted_column_is_corrected_in_the_right_direction(self):
        for col, sign in C.COLUMN_DIRECTION.items():
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
        assert "stock_price" not in C.COLUMN_DIRECTION
        line = next(l for l in VIEW_SQL.splitlines() if "fr.stock_price" in l)
        assert "saf.factor" not in line
