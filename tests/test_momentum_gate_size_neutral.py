"""`scripts/momentum_gate.py` の規模で中立化した順位を比べるモード（`--size-neutral`・#970）。

#957 で、M-2 / M-6 は毎期、小型株を実際より高く・大型株を低く置いていた（M-6 の最大の十分位で −0.132）。
このモードは予測そのまま（`raw`・本番）と、月ごとの断面で予測を log(時価総額) へ OLS で回帰した残差
（`size_neutral`）を、同じ CV・同じ共通域で比べる。このファイルが縛るのは:

  - 中立化した予測は log 時価総額と（数値誤差を除いて）無相関になり、規模と無関係な予測の順位は
    ほとんど動かないこと
  - 時価総額の無い行は**両条件から同じ集合で**落ち、落とした行数が出力に出ること
  - 時価総額は、その月末までに使えた通期の行（決算日 + FINANCIAL_LAG_DAYS ≦ 月末）からしか取らないこと
  - モードの条件・分母・対象モデル・配線

パネル構築と CV の本体は DB フルロードが要るのでここでは触らない（他のモードのテストと同じ）。
"""
import inspect
import math
import os
import random
import sys
from collections import namedtuple

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.macro_snapshots import FINANCIAL_LAG_DAYS, _spearman  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    MOM_WINDOW, SIZE_NEUTRAL_BASE_COND, SIZE_NEUTRAL_CONDS, SIZE_NEUTRAL_MODELS, Cond,
    aligned_from, base_of, bonferroni_alpha, build_conditions, log_market_cap_by_ym,
    size_common_keys, size_decile_labels, size_neutralize,
)

YM = "2024-06"


def _pearson(a: list, b: list) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    return sab / math.sqrt(saa * sbb)


def _month(n: int, slope: float, seed: int = 970) -> tuple[dict, dict, dict]:
    """1か月の断面: (resid_by_ym, meta_by_ym, logcap)。予測 = slope × log 時価総額 ＋ ノイズ。"""
    rng = random.Random(seed)
    xs = [rng.gauss(10.0, 1.5) for _ in range(n)]
    ecs = [f"E{k:05d}" for k in range(n)]
    preds = [slope * x + rng.gauss(0.0, 0.3) for x in xs]
    reals = [rng.gauss(0.0, 1.0) for _ in range(n)]
    resid = {YM: list(zip(preds, reals))}
    meta = {YM: [(ec, "業種") for ec in ecs]}
    logcap = {(YM, ec): x for ec, x in zip(ecs, xs)}
    return resid, meta, logcap


# ── 中立化（受け入れ条件 1・2）──────────────────────────────────────────────

class TestSizeNeutralize:
    def test_prediction_correlated_with_size_becomes_uncorrelated(self):
        """規模と線形に相関させた予測は、通すと log 時価総額との相関が数値誤差の範囲で 0 になる。"""
        resid, meta, logcap = _month(200, slope=0.8)
        xs = [logcap[(YM, ec)] for ec, _ in meta[YM]]
        before = [yh for yh, _y in resid[YM]]
        assert _pearson(before, xs) > 0.9                  # 検体は実際に規模と強く相関している
        out, out_meta, dropped = size_neutralize(resid, meta, logcap)
        after = [yh for yh, _y in out[YM]]
        assert dropped == 0 and out_meta == meta
        assert abs(_pearson(after, xs)) < 1e-12
        assert [y for _yh, y in out[YM]] == [y for _yh, y in resid[YM]]   # 実現値は触らない

    def test_size_free_prediction_keeps_almost_the_same_order(self):
        """規模と無相関な予測は、通しても順位がほとんど変わらない。"""
        resid, meta, logcap = _month(200, slope=0.0)
        before = [yh for yh, _y in resid[YM]]
        after = [yh for yh, _y in size_neutralize(resid, meta, logcap)[0][YM]]
        assert _spearman(before, after) > 0.99
        assert after != before                             # 平均を引くので値は動く（空振りしていない）

    @pytest.mark.parametrize("n, same_size", [(2, False), (5, True)])
    def test_months_without_a_usable_slope_only_shift(self, n, same_size):
        """3社未満・時価総額が全社同じ月は傾きを推定できないので、平均を引くだけ（順位は不変）。"""
        resid, meta, logcap = _month(n, slope=0.8)
        if same_size:
            logcap = {k: 9.0 for k in logcap}
        before = [yh for yh, _y in resid[YM]]
        after = [yh for yh, _y in size_neutralize(resid, meta, logcap)[0][YM]]
        mean = sum(before) / n
        assert after == pytest.approx([b - mean for b in before], abs=1e-12)

    def test_rows_without_market_cap_are_dropped_and_counted(self):
        resid, meta, logcap = _month(30, slope=0.5)
        missing = {(YM, meta[YM][k][0]) for k in (0, 7, 19)}
        logcap = {k: v for k, v in logcap.items() if k not in missing}
        out, out_meta, dropped = size_neutralize(resid, meta, logcap)
        assert dropped == 3
        assert len(out[YM]) == len(out_meta[YM]) == 27
        assert not ({(YM, m[0]) for m in out_meta[YM]} & missing)


# ── 時価総額の無い行は両条件から同じだけ落ちる（受け入れ条件 3）──────────────────

class TestCommonDomain:
    def test_rows_without_market_cap_drop_from_both_sides_equally(self):
        keys = {(YM, f"E{k}") for k in range(10)} | {("2024-07", "E1")}
        logcap = {k: 10.0 for k in keys if k[1] not in ("E3", "E8")}
        kept = size_common_keys(keys, logcap)
        assert kept == {k for k in keys if k[1] not in ("E3", "E8")}
        assert len(keys) - len(kept) == 2

    def test_main_drops_before_the_transform_and_reports_the_count(self):
        """落とすのは変換の前（共通域を取る段）で、同じ `keys` で両条件を制限する。行数は出力と JSON に出る。"""
        src = inspect.getsource(momentum_gate.main)
        drop = src.index("keys = size_common_keys(keys, logcap)")
        restrict = src.index("r2, m2 = _restrict(resid, meta, cpanels[(cond, kind)][2], keys)")
        transform = src.index("r2, m2, _d = size_neutralize(r2, m2, logcap)")
        assert drop < restrict < transform
        assert "時価総額の無い行を両条件から除外" in src
        assert '"dropped_rows": size_dropped' in src
        assert "without: {n_ids - len(logcap)}" in src


# ── 先を読まない（受け入れ条件 4）──────────────────────────────────────────

_Fin = namedtuple("_Fin", ("period_end",))


class TestMarketCapIsPointInTime:
    FINS = {"E1": [_Fin("2023-03-31"), _Fin("2024-03-31")]}
    CAPS = {("E1", "2023-03-31"): 1000.0, ("E1", "2024-03-31"): 5000.0}

    def test_lag_is_the_one_the_panel_uses(self):
        assert FINANCIAL_LAG_DAYS == 45   # 2024-03-31 + 45日 = 2024-05-15

    def test_a_row_is_used_only_after_its_lag_has_passed(self):
        got = log_market_cap_by_ym({"2024-04": ["E1"], "2024-05": ["E1"]}, self.FINS, self.CAPS)
        # 4月末は 2024-03-31 の行がまだ使えない（5/15 以降）→ 前の年の行
        assert got[("2024-04", "E1")] == pytest.approx(math.log(1000.0))
        # 5月末なら使える
        assert got[("2024-05", "E1")] == pytest.approx(math.log(5000.0))

    def test_no_usable_row_or_no_positive_cap_is_left_out(self):
        caps = {("E1", "2023-03-31"): 0.0, ("E1", "2024-03-31"): 5000.0}
        got = log_market_cap_by_ym({"2023-04": ["E1"], "2024-04": ["E1"], "2024-06": ["E2"]},
                                   self.FINS, caps)
        assert ("2023-04", "E1") not in got       # 2023-03-31 の行も 5/15 まで使えない
        assert ("2024-04", "E1") not in got       # 使える行の時価総額が 0
        assert ("2024-06", "E2") not in got       # 財務の行が無い
        assert got == {}

    def test_period_end_as_date_is_matched_too(self):
        """キャッシュ由来（文字列）でも VIEW 由来（date）でも同じ行に当たる。"""
        import datetime as dt
        fins = {"E1": [_Fin(dt.date(2024, 3, 31))]}
        got = log_market_cap_by_ym({"2024-05": ["E1"]}, fins, self.CAPS)
        assert got[("2024-05", "E1")] == pytest.approx(math.log(5000.0))


# ── 規模の十分位と並べるための組み直し ────────────────────────────────────

class TestDecilesAndAlignment:
    def test_each_month_is_split_into_ten_bins_from_the_smallest(self):
        logcap = {(YM, f"E{k:02d}"): float(k) for k in range(20)}
        labels = size_decile_labels(logcap, set(logcap))
        assert labels[(YM, "E00")] == ["size_d0"] and labels[(YM, "E01")] == ["size_d0"]
        assert labels[(YM, "E19")] == ["size_d9"]
        assert sorted({lab[0] for lab in labels.values()}) == [f"size_d{k}" for k in range(10)]

    def test_only_the_common_domain_is_labelled(self):
        logcap = {(YM, "E1"): 1.0, (YM, "E2"): 2.0, (YM, "E3"): 3.0}
        assert set(size_decile_labels(logcap, {(YM, "E1"), (YM, "E3")})) == {(YM, "E1"), (YM, "E3")}

    def test_ties_are_broken_by_the_company_code(self):
        logcap = {(YM, "E2"): 1.0, (YM, "E1"): 1.0}
        assert size_decile_labels(logcap, set(logcap), n_bins=2) == {
            (YM, "E1"): ["size_d0"], (YM, "E2"): ["size_d1"]}

    def test_aligned_from_keys_rows_by_company(self):
        resid = {YM: [(0.1, 1.0), (0.2, 2.0)]}
        meta = {YM: [("E1", "x"), ("E2", "y")]}
        assert aligned_from(resid, meta) == {(YM, "E1"): (0.1, 1.0), (YM, "E2"): (0.2, 2.0)}

    def test_breakdown_reads_the_size_bias(self):
        """予測が規模と相関すると大きい分位の bias が正に、中立化すると 0 近くへ寄る（並べる表の向き）。"""
        resid, meta, logcap = _month(200, slope=0.8)
        keys = set(logcap)
        labels = size_decile_labels(logcap, keys)
        raw = momentum_gate.blank_group_breakdown(aligned_from(resid, meta), labels)
        out, out_meta, _d = size_neutralize(resid, meta, logcap)
        neutral = momentum_gate.blank_group_breakdown(aligned_from(out, out_meta), labels)
        assert raw["size_d9"]["placement_bias"] > 0.2
        assert abs(neutral["size_d9"]["placement_bias"]) < abs(raw["size_d9"]["placement_bias"]) / 2


# ── モードの条件・分母・対象モデル・配線 ──────────────────────────────────────

class TestSizeNeutralMode:
    def test_both_sides_share_the_production_condition(self):
        """`Cond` は2条件で同一＝パネルも CV も1回（差は CV の後の予測の変換だけ）。"""
        conds = build_conditions(size_neutral=True)
        assert list(conds) == list(SIZE_NEUTRAL_CONDS) == ["raw", "size_neutral"]
        assert set(conds.values()) == {Cond(False, MOM_WINDOW)}

    def test_baseline_is_the_raw_prediction(self):
        conds = build_conditions(size_neutral=True)
        assert base_of(conds) == SIZE_NEUTRAL_BASE_COND == "raw"

    def test_measures_m2_and_m6(self):
        assert SIZE_NEUTRAL_MODELS == ["xgb_m2", "elasticnet"]
        build_conditions(size_neutral=True, models=["elasticnet"])
        with pytest.raises(ValueError, match="M-2 / M-6"):
            build_conditions(size_neutral=True, models=["xgb_m2", "risk_return"])

    def test_alpha_is_four_tests(self):
        conds = build_conditions(size_neutral=True)
        assert bonferroni_alpha(len(SIZE_NEUTRAL_MODELS), len(conds)) == pytest.approx(0.0125)

    def test_main_wiring(self):
        """**渡し忘れても例外は出ない**（両条件が同じ予測になり、差なしだけが残る）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("size_neutral=args.size_neutral") == 2      # build_conditions / mode_of
        assert "SIZE_NEUTRAL_MODELS" in src
        assert 'market_caps = load_market_caps(db) if (mode == "size_neutral" or m6_post)' in src
        assert "log_market_cap_by_ym(ids0, fins[conds[base].fin_rows], market_caps)" in src
        # 条件の変換は分母以外にだけ当てる（全月の段と共通月の段の両方）
        assert src.count("size_neutralize(") == 3
        assert 'lambda r, mt: size_neutralize(r, mt, logcap)[:2], "size_neutral"' in src
        assert "args.size_neutral and len(set(conds.values())) != 1" in src


# ── M-6 は本番の規模の中立化を当てて測る（#995）──────────────────────────────────

class TestM6ProductionPost:
    def test_only_m6_outside_the_size_neutral_axis(self):
        """M-6 だけ・`--size-neutral` 軸以外だけで当てる（その軸は raw / size_neutral を自前で作る）。"""
        from scripts.momentum_gate import m6_production_post
        assert momentum_gate._M6_SIZE_NEUTRAL is True      # 本番は中立化（macro_enet._SIZE_NEUTRAL）
        for mode in (None, "fin_fill", "risk_axis", "inner_cv_purge", "ttm"):
            assert m6_production_post("elasticnet", mode)
            assert not m6_production_post("xgb_m2", mode)
            assert not m6_production_post("risk_return", mode)
        assert not m6_production_post("elasticnet", "size_neutral")

    def test_follows_the_production_constant(self, monkeypatch):
        """本番の定数を読む（書き写さない）。本番で外したら、ゲートも当てない。"""
        from scripts.momentum_gate import m6_production_post
        monkeypatch.setattr(momentum_gate, "_M6_SIZE_NEUTRAL", False)
        assert not m6_production_post("elasticnet", None)

    def test_constant_is_imported_from_the_plugin(self):
        import plugins.macro_enet as me
        assert momentum_gate._M6_SIZE_NEUTRAL is me._SIZE_NEUTRAL

    def test_same_order_as_the_measured_residual_and_keeps_the_level(self):
        """本番の形（月平均を足し戻す）は、測った残差（`size_neutralize`）と月内の順位が同一で、平均だけが違う。"""
        from plugins.macro_snapshots import neutralize_size_by_ym
        resid, meta, logcap = _month(200, slope=0.8)
        measured = [yh for yh, _y in size_neutralize(resid, meta, logcap)[0][YM]]
        prod_out, missing = neutralize_size_by_ym(resid, meta, logcap)
        prod = [yh for yh, _y in prod_out[YM]]
        before = [yh for yh, _y in resid[YM]]
        assert missing == 0
        assert _spearman(measured, prod) == pytest.approx(1.0, abs=1e-12)
        shift = sum(before) / len(before)
        assert prod == pytest.approx([m + shift for m in measured], abs=1e-9)
        assert sum(prod) / len(prod) == pytest.approx(shift, abs=1e-12)

    def test_main_applies_it_in_both_stages_before_the_downstream(self):
        """全月の段（リスク軸の変換の前）と共通月の段（`cparts` へ入れる前）の2か所で当てる。
        **当て忘れても例外は出ない**（M-6 を本番と違う予測で測り、もっともらしい数字だけが残る・#815）。"""
        src = inspect.getsource(momentum_gate.main)
        call = "neutralize_size_by_ym(part[0], part[1], prod_logcap[cond])"
        assert src.count(call) == 2
        first = src.index(call)
        assert src.index("out, part, seed_parts = ran[memo]") < first < src.index(
            'if mode == "risk_axis":\n                    # 同じ CV の残差へ条件ごとの U 変換')
        second = src.index(call, first + 1)
        assert src.index("out, part = cran[memo]") < second < src.index('cparts[f"{cond}|{model}"] = part')
        assert src.count("if m6_production_post(model, mode):") == 2
        # 全月の段の内訳と OOF の CSV（`--fin-nan`）も後処理の後の残差を読む（memo の `ran` を直接読まない）
        keep = src.index('full_parts[f"{cond}|{model}"] = part')
        assert first < keep < src.index('if mode == "risk_axis":\n                    # 同じ CV の残差へ条件ごとの U 変換')
        assert 'part = full_parts[f"{cond}|{model}"]' in src
        assert "ran[(conds[cond], kind, model)]" not in src
        assert "prod_logcap[cond] = log_market_cap_by_ym(" in src
        assert "M-6 is scored after the production size neutralization (#995)" in src
        assert '"m6_production_size_neutral": {"applied": m6_post' in src
