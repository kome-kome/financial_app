"""M-2 / M-6 の本番で「無い」と確かめた財務の空欄を 0 で埋める（#983・#950）。

書類の実物で「無い」と確かめた空欄（無配の配当利回り・R&D の無い社の R&D 集約度）を 0 で埋めてから
`build_snapshots` へ渡す。財務特徴量が 1 つでも空欄の行は捨てる規則なので、埋めないと無配・R&D の無い社が
学習と予測から丸ごと外れる。危ないのは次の形で、**どれも例外が出ない**:

  - execute が `load_data` の財務をそのまま `build_snapshots` へ渡す（本番は 1,820 社のまま・差なし）
  - M-1 まで埋まる（M-1 は母集団が別物で、#983 の範囲外）
  - 探索・夜間の共有キャッシュの中で写しを毎回作り、id で鍵を作る `build_snapshots` のキャッシュが外れる
  - 埋めた写しが `load_data` のキャッシュの財務を書き換える
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest

import plugins.macro_snapshots as ms
from plugins import get_plugin
from plugins.utils import coerce_params

PE = datetime.date(2025, 3, 31)
# FIN_FILL_SOURCE_FIELDS の順（dps, stock_price, pl_rd_expenses, pl_revenue）: 無配・R&D なし・株価と売上あり
SOURCES = {("E1", "2025-03-31"): (None, 1000.0, None, 1.0e9)}


def _fin():
    vals = dict.fromkeys(ms.FIN_LOAD_FIELDS)
    vals.update(edinet_code="E1", period_end=PE, per=12.0)
    return {"E1": [ms._FinRow(**vals)]}


class _Stop(Exception):
    """`build_snapshots` に届いたところで execute を止める（届いた財務だけを見る）。"""


def _run_until_build(name: str, fin_by_co: dict, sources=SOURCES) -> dict:
    """プラグインの execute を `build_snapshots` の手前まで回し、渡った財務を返す。"""
    module = f"plugins.{name}"
    plugin = get_plugin(name)
    params = coerce_params(plugin.params_schema(), {})
    seen: dict = {}

    def fake_build(prices_by_co, fin, *a, **k):
        seen["fin"] = fin
        raise _Stop

    with patch(f"{module}.load_data", return_value=({"E1": [object()]}, fin_by_co, {})), \
         patch(f"{module}.preload_macro", return_value={}), \
         patch(f"{module}.build_snapshots", side_effect=fake_build), \
         patch("plugins.macro_snapshots.load_fill_sources", return_value=sources) as lfs:
        with pytest.raises(_Stop):
            plugin.execute(params, MagicMock())
    seen["sources_read"] = lfs.call_count
    return seen


class TestProductionConstant:
    def test_production_fills(self):
        """#950 の決定（2026-10-17・ユーザー判断）: M-2 / M-6 は埋める。"""
        assert ms.FIN_FILL is True

    def test_filled_columns_are_model_features(self):
        """埋める列は M-2 / M-6 が読む財務の行の列（VIEW は変えず、読み込んだ行の写しだけを埋める）。"""
        assert set(ms.FIN_FILL_COLUMNS) <= {o["value"] for o in ms.FIN_BASE_OPTIONS}


class TestExecutePassesTheFilledFinancials:
    @pytest.mark.parametrize("name", ["macro_gbdt", "macro_enet"])
    def test_m2_and_m6_get_the_filled_financials(self, name):
        fin = _fin()
        seen = _run_until_build(name, fin)
        rec = seen["fin"]["E1"][0]
        assert rec.rd_intensity == 0.0, f"{name} が埋めた財務を build_snapshots へ渡していない"
        assert rec.div_yield == 0.0
        assert fin["E1"][0].rd_intensity is None, "load_data の財務を書き換えた"
        assert seen["sources_read"] == 1

    def test_m1_gets_the_financials_as_loaded(self):
        """M-1 の本番は埋めない（母集団が別物・#983）。判定の生の値も読まない。"""
        fin = _fin()
        seen = _run_until_build("macro_risk_return", fin)
        assert seen["fin"] is fin
        assert seen["sources_read"] == 0

    @pytest.mark.parametrize("name", ["macro_gbdt", "macro_enet"])
    def test_switching_off_passes_the_financials_as_loaded(self, name, monkeypatch):
        """本番の値は定数1つ（`macro_snapshots.FIN_FILL`）で切り替わる。M-2 / M-6 で共有する。"""
        monkeypatch.setattr(ms, "FIN_FILL", False)
        fin = _fin()
        seen = _run_until_build(name, fin)
        assert seen["fin"] is fin
        assert seen["sources_read"] == 0


class TestApplyFinFill:
    def test_outside_the_shared_cache_it_fills_every_call(self):
        fin = _fin()
        with patch("plugins.macro_snapshots.load_fill_sources", return_value=SOURCES) as lfs:
            out1, n1 = ms.apply_fin_fill(MagicMock(), fin)
            out2, _n2 = ms.apply_fin_fill(MagicMock(), fin)
        assert n1 == {"rd_intensity": 1, "div_yield": 1}
        assert out1["E1"][0].rd_intensity == 0.0 and out2["E1"][0].rd_intensity == 0.0
        assert lfs.call_count == 2

    def test_inside_the_shared_cache_the_same_copy_is_returned(self):
        """**同じ写しを返す**——毎回作ると `build_snapshots` のキャッシュ（鍵は `id(fin_by_co)`）が M-2 と M-6・
        探索の候補ごとに外れ、夜間と探索で断面を作り直す。"""
        db, fin = MagicMock(), _fin()
        with patch("plugins.macro_snapshots.load_fill_sources", return_value=SOURCES) as lfs, \
                ms.shared_snapshot_cache():
            out1, n1 = ms.apply_fin_fill(db, fin)
            out2, n2 = ms.apply_fin_fill(db, fin)
        assert out1 is out2
        assert n1 == n2 == {"rd_intensity": 1, "div_yield": 1}
        assert lfs.call_count == 1

    def test_the_cache_keeps_the_source_alive(self):
        """キャッシュの値は元の財務も持つ（解放されると id が別の dict に再利用され、別の財務の写しが返りうる）。"""
        db, fin = MagicMock(), _fin()
        with patch("plugins.macro_snapshots.load_fill_sources", return_value=SOURCES), \
                ms.shared_snapshot_cache():
            ms.apply_fin_fill(db, fin)
            cache = ms._shared_cache.get()["fin_fill"]
            (_key, value), = cache._data.items()
        assert value[0] is fin

    def test_switched_off_returns_the_input(self, monkeypatch):
        monkeypatch.setattr(ms, "FIN_FILL", False)
        fin = _fin()
        with patch("plugins.macro_snapshots.load_fill_sources") as lfs:
            out, n = ms.apply_fin_fill(MagicMock(), fin)
        assert out is fin
        assert n == {c: 0 for c in ms.FIN_FILL_COLUMNS}
        lfs.assert_not_called()


class TestFillFinBlanksOnProductionRows:
    """本番の行（`macro_snapshots._FinRow`・VIEW の列）で埋める条件を縛る。計測の道具側の行での縛りは
    `tests/test_momentum_gate_fin_fill.py::TestFillFinBlanks`（同じ関数）。"""

    def test_blank_price_is_not_filled(self):
        out, n = ms.fill_fin_blanks(_fin(), {("E1", "2025-03-31"): (None, None, None, 1.0e9)})
        assert out["E1"][0].div_yield is None and n["div_yield"] == 0
        assert out["E1"][0].rd_intensity == 0.0

    def test_blank_or_zero_revenue_is_not_filled(self):
        for revenue in (None, 0.0):
            out, n = ms.fill_fin_blanks(_fin(), {("E1", "2025-03-31"): (None, 1000.0, None, revenue)})
            assert out["E1"][0].rd_intensity is None and n["rd_intensity"] == 0

    def test_ttm_rows_do_not_match_an_annual_source(self):
        """TTM 行（period_end が半期末）は通期の生の値と突き合わず、埋まらない（今と同じく外れる）。"""
        vals = dict.fromkeys(ms.FIN_LOAD_FIELDS)
        vals.update(edinet_code="E1", period_end=datetime.date(2025, 9, 30))
        out, n = ms.fill_fin_blanks({"E1": [ms._FinRow(**vals)]}, SOURCES)
        assert out["E1"][0].rd_intensity is None and sum(n.values()) == 0
