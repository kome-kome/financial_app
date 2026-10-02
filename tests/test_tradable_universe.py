"""tests/test_tradable_universe.py — 投資可能な母集団の生存条件（Issue #605）。

`is_active` だけでは足りない。`/equities/master` の as-of は「今日−84日」（J-Quants の
エンバーゴ）なので廃止の反映が最大12週遅れ、実測（2026-09-04）では値の付かない29社が
`is_active=True` のまま推奨母集団に残り、`macro_enet_scores` の μ̂ 付きで候補に並んでいた。

ここが縛るのは3つ:

1. `stale_price_codes` の境界（cutoff ちょうどは残す・それより古いものだけ落とす）
2. 価格行を1本も持たない銘柄を落とさないこと（#555 と同型の静かな欠測を作らない）
3. **4経路が同じ判定を共有していること**。1箇所だけ直すと「推奨には出ないが売却候補には
   出る」状態になり、しかもそれは失敗として現れない（ADR-0049 で `min_coverage` を
   AST で縛ったのと同じ理由でここも構文で縛る）
4. **計算の母集団も同じ判定を共有していること**（#780）。sector_ols の当日回帰と、producer が
   保存する代表 as-of。表示から消えても計算に残ると、現役社の gap_ratio や鮮度の数字が
   静かに動く
"""
import ast
import logging
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (  # noqa: E402
    Company, FinancialMetric, FinancialRecord, PRICE_STALE_ALERT_BDAYS, PRICE_STALE_WARN_BDAYS,
    non_tradable_codes, stale_cutoff_date, stale_price_codes, tradable_filters,
)
from plugins.macro_snapshots import tradable_snapshot_asof  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# tradable_filters を共有しなければならない経路。表示母集団の4経路（#605）と、
# 計算の母集団である sector_ols の当日回帰（#780）。
TRADABLE_CALLERS = (
    "plugins/recommend.py",
    "plugins/gap_analysis.py",
    "plugins/net_cash_analysis.py",
    "plugins/sell_ranking.py",
    "plugins/sector_ols.py",
)

# 代表 as-of を保存する producer（#417）。as-of は今買える社だけで作る（#780）。
ASOF_PRODUCERS = (
    "plugins/macro_enet.py",
    "plugins/macro_gbdt.py",
    "plugins/macro_dlm.py",
    "plugins/macro_ensemble.py",
)


def _cutoff() -> date:
    return date.fromisoformat(stale_cutoff_date(date.today(), PRICE_STALE_ALERT_BDAYS))


class TestStalePriceCodes:
    def test_boundary_cutoff_day_is_not_stale(self, db, make_price):
        """cutoff 当日は残し、その前日から落とす（`d < cutoff` の境界）。"""
        cutoff = _cutoff()
        db.add(make_price(edinet_code="E_ON",  trade_date=cutoff.isoformat()))
        db.add(make_price(edinet_code="E_OLD", trade_date=(cutoff - timedelta(days=1)).isoformat()))
        db.commit()
        assert stale_price_codes(db) == {"E_OLD"}

    def test_uses_alert_threshold_not_warn(self, db, make_price):
        """閾値は ALERT(=10営業日)。WARN(=5) を使うと生きた銘柄を落とす（#605 の設計判断）。"""
        assert PRICE_STALE_ALERT_BDAYS > PRICE_STALE_WARN_BDAYS
        warn_cutoff = date.fromisoformat(stale_cutoff_date(date.today(), PRICE_STALE_WARN_BDAYS))
        # WARN では stale だが ALERT ではまだ生きている日（連休・一時的な取得失敗の帯）。
        between = warn_cutoff - timedelta(days=1)
        assert between >= _cutoff(), "WARN と ALERT の間に日が無い（閾値定義の変更を疑う）"
        db.add(make_price(edinet_code="E_BETWEEN", trade_date=between.isoformat()))
        db.commit()
        assert stale_price_codes(db) == set()

    def test_code_without_any_price_row_is_not_stale(self, db, make_price):
        """価格行が1本も無い銘柄は落とさない。

        上場したてで daily がまだ無い社を落とすと #555 と同型の静かな欠測になる。
        判定できるのは「価格が止まった」であって「価格が無い」ではない。
        """
        db.add(make_price(edinet_code="E_HAS", trade_date=date.today().isoformat()))
        db.commit()
        assert "E_NOPRICE" not in stale_price_codes(db)

    def test_latest_bar_wins_over_old_bars(self, db, make_price):
        """銘柄ごとの MAX(trade_date) で見る（古いバーが残っていても最新が生きていれば残す）。"""
        db.add(make_price(edinet_code="E_MIX", trade_date=(_cutoff() - timedelta(days=90)).isoformat()))
        db.add(make_price(edinet_code="E_MIX", trade_date=date.today().isoformat(), close=1100.0))
        db.commit()
        assert stale_price_codes(db) == set()


class TestTradableFilters:
    def _universe(self, db):
        return {ec for (ec,) in db.query(FinancialMetric.edinet_code).filter(*tradable_filters(db)).all()}

    def test_excludes_delisted_and_stale_priced(self, db, make_metric, make_price):
        """上場廃止（#315）と価格停止（#605）の両方が落ちる。"""
        old = (_cutoff() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        db.add(make_metric(edinet_code="E_OK",      is_active=True))
        db.add(make_metric(edinet_code="E_DELIST",  is_active=False))
        db.add(make_metric(edinet_code="E_ZOMBIE",  is_active=True))   # 廃止の追随待ち（本 Issue）
        db.add(make_metric(edinet_code="E_LEGACY",  is_active=None))   # 旧データは残す
        db.add(make_price(edinet_code="E_OK",     trade_date=today))
        db.add(make_price(edinet_code="E_ZOMBIE", trade_date=old))
        db.add(make_price(edinet_code="E_LEGACY", trade_date=today))
        db.commit()
        assert self._universe(db) == {"E_OK", "E_LEGACY"}

    def test_no_stale_codes_means_no_extra_condition(self, db, make_metric):
        """除外対象ゼロのとき `notin_([])` を積まない（空 IN は方言依存で挙動が割れる）。"""
        db.add(make_metric(edinet_code="E_OK", is_active=True))
        db.commit()
        assert len(tradable_filters(db)) == 1
        assert self._universe(db) == {"E_OK"}


class TestEveryConsumerSharesTheFilter:
    """4経路が `tradable_filters` を呼び、`is_active` を直接書いていないこと。

    「1箇所だけ直す」は失敗として現れない——推奨からは消えるのに売却ランキングには
    残る、という形で静かに食い違う。だから構文で縛る。
    """

    @pytest.mark.parametrize("relpath", TRADABLE_CALLERS)
    def test_calls_tradable_filters(self, relpath):
        tree = ast.parse(open(os.path.join(ROOT, relpath), encoding="utf-8").read())
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "tradable_filters" in called, f"{relpath} が tradable_filters を呼んでいない"

    @pytest.mark.parametrize("relpath", TRADABLE_CALLERS)
    def test_does_not_reimplement_is_active_filter(self, relpath):
        """`FinancialMetric.is_active.isnot(...)` を直接書かない（条件の源は1つ）。"""
        tree = ast.parse(open(os.path.join(ROOT, relpath), encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "isnot":
                continue
            owner = node.func.value
            if isinstance(owner, ast.Attribute) and owner.attr == "is_active":
                raise AssertionError(
                    f"{relpath} が is_active フィルタを再実装している。tradable_filters を使うこと")


# ── 計算の母集団（#780）───────────────────────────────────────────────────────

def _seed_companies(db, make_company, make_price):
    """廃止・停止・現役・価格行なしの4社（companies 基準）。"""
    old = (_cutoff() - timedelta(days=1)).isoformat()
    today = date.today().isoformat()
    db.add(make_company(edinet_code="E_OK",      sec_code="1001", is_active=True))
    db.add(make_company(edinet_code="E_DELIST",  sec_code="1002", is_active=False))
    db.add(make_company(edinet_code="E_ZOMBIE",  sec_code="1003", is_active=True))
    db.add(make_company(edinet_code="E_NOPRICE", sec_code="1004", is_active=True))
    db.add(make_price(edinet_code="E_OK",     trade_date=today))
    db.add(make_price(edinet_code="E_ZOMBIE", trade_date=old))
    # companies に行が無いまま価格だけ止まった社（判定の外＝「買えない」に数えない）
    db.add(make_price(edinet_code="E_ORPHAN", trade_date=old))
    db.commit()


class TestTradableFiltersOnOtherColumns:
    """列を渡すと VIEW 以外の JOIN にも同じ条件が掛かる（sector_ols の当日回帰・#780）。"""

    def test_financial_records_join_drops_the_same_codes(self, db, make_company, make_price,
                                                         make_fin):
        _seed_companies(db, make_company, make_price)
        for ec in ("E_OK", "E_DELIST", "E_ZOMBIE", "E_NOPRICE", "E_NOCOMPANY"):
            db.add(make_fin(edinet_code=ec))
        db.commit()
        got = {ec for (ec,) in (
            db.query(FinancialRecord.edinet_code)
              .outerjoin(Company, FinancialRecord.edinet_code == Company.edinet_code)
              .filter(*tradable_filters(db, is_active=Company.is_active,
                                        edinet_code=FinancialRecord.edinet_code))
              .all())}
        # companies に行が無い社は外部結合で is_active が NULL になり、VIEW と同じく残る
        assert got == {"E_OK", "E_NOPRICE", "E_NOCOMPANY"}


class TestNonTradableCodes:
    def test_returns_delisted_and_stale_only(self, db, make_company, make_price):
        _seed_companies(db, make_company, make_price)
        assert non_tradable_codes(db) == {"E_DELIST", "E_ZOMBIE"}

    def test_empty_when_everyone_is_tradable(self, db, make_company, make_price):
        db.add(make_company(edinet_code="E_OK", sec_code="1001", is_active=True))
        db.add(make_price(edinet_code="E_OK", trade_date=date.today().isoformat()))
        db.commit()
        assert non_tradable_codes(db) == set()


class TestTradableSnapshotAsof:
    """producer の代表 as-of は今買える社だけで作る（#780）。

    M-6 の `n_stale=113` の大半は廃止・停止の社だった＝朝の鮮度カードの数字が鮮度の問題を
    表していなかった。社ごとのスナップ日は保存しないので、絞れるのは保存時だけ。
    """

    def test_excludes_non_tradable_from_every_field(self, db, make_company, make_price):
        _seed_companies(db, make_company, make_price)
        asof = tradable_snapshot_asof(db, [
            ("E_OK",      "2026-09-25"),
            ("E_NOPRICE", "2026-09-18"),
            ("E_DELIST",  "2025-06-16"),   # 廃止社の最終バー（最古を名乗っていた）
            ("E_ZOMBIE",  "2026-08-28"),
        ])
        assert asof["snapshot_date_min"] == "2026-09-18"
        assert asof["snapshot_date"] == "2026-09-18"   # 2社の lower median
        assert asof["snapshot_date_max"] == "2026-09-25"
        assert asof["n_stale"] == 0

    def test_falls_back_to_everyone_when_nothing_is_tradable(self, db, make_company,
                                                              make_price, caplog):
        """全社が落ちる＝価格収集そのものの停止。None（画面は「未蓄積」）ではなく古さを見せる。"""
        _seed_companies(db, make_company, make_price)
        with caplog.at_level(logging.WARNING, logger="plugins.macro_snapshots"):
            asof = tradable_snapshot_asof(db, [("E_DELIST", "2025-06-16"),
                                               ("E_ZOMBIE", "2026-08-28")])
        assert asof["snapshot_date_min"] == "2025-06-16"
        assert asof["snapshot_date_max"] == "2026-08-28"
        assert "全社で代表させる" in caplog.text

    def test_no_dates_at_all_stays_empty_without_warning(self, db, make_company, make_price,
                                                         caplog):
        _seed_companies(db, make_company, make_price)
        with caplog.at_level(logging.WARNING, logger="plugins.macro_snapshots"):
            asof = tradable_snapshot_asof(db, [("E_OK", None)])
        assert asof["snapshot_date"] is None
        assert caplog.text == ""


class TestProducersUseTradableAsof:
    """producer 4本が as-of を `tradable_snapshot_asof` で作り、素の代表値関数を直接呼ばない。

    1本だけ全行のまま残っても失敗としては現れない（その producer を選んだときだけ、朝の
    鮮度カードが廃止社を数える）。だから構文で縛る。
    """

    @staticmethod
    def _called(relpath):
        tree = ast.parse(open(os.path.join(ROOT, relpath), encoding="utf-8").read())
        return {n.func.id for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    @pytest.mark.parametrize("relpath", ASOF_PRODUCERS)
    def test_calls_tradable_snapshot_asof(self, relpath):
        assert "tradable_snapshot_asof" in self._called(relpath)

    @pytest.mark.parametrize("relpath", ASOF_PRODUCERS)
    def test_does_not_call_representative_snapshot_date_directly(self, relpath):
        assert "representative_snapshot_date" not in self._called(relpath), (
            f"{relpath} が全行の as-of を作っている。tradable_snapshot_asof を使うこと")
