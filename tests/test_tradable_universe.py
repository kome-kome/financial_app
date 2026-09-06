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
"""
import ast
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (  # noqa: E402
    FinancialMetric, PRICE_STALE_ALERT_BDAYS, PRICE_STALE_WARN_BDAYS,
    stale_cutoff_date, stale_price_codes, tradable_filters,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# tradable_filters を共有しなければならない4経路（#605）。
TRADABLE_CALLERS = (
    "plugins/recommend.py",
    "plugins/gap_analysis.py",
    "plugins/net_cash_analysis.py",
    "plugins/sell_ranking.py",
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
