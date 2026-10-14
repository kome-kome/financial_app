"""`scripts/momentum_gate.py` の財務の空欄モード（`--fin-nan`・#938/#916）。

M-2/M-6 で、財務特徴量が空欄の行を捨てずに NaN で学習へ通すかを共通域で測る道具。危ないのは
**黙って同じものを比べる**形で、どれも例外が出ずに「差なし」という結論だけが残る:

  - `fin_nan_ok` がパネル構築まで渡らない（`_build` の呼び出しで落とす）
  - キャッシュのキーに入らず、drop と nan が同じパネルを共有する（`tests/test_tuning.py` が縛る）
  - 空欄の財務を持つ行が1つも無く、両条件が同じパネルになる

このファイルは条件・分母・検定数・出力と、3つ目を止める純関数を縛る。1つ目は
`tests/test_momentum_gate_axes.py::TestBuildUsesTheConditionAxes` が縛る。
"""
import csv
import datetime
import inspect
import os
import sys
from collections import namedtuple

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.macro_snapshots import FIN_BASE_OPTIONS  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    FIN_BLANK_GROUPS, FIN_NAN_BASE_COND, FIN_NAN_CONDS, FIN_NAN_MODELS, MODE_SUFFIX, MODEL_SPECS,
    MOM_WINDOW, N_TESTS, OOF_CSV_COLUMNS, STANDARD_UNKNOWN, base_of, blank_group_breakdown,
    bonferroni_alpha, build_conditions, fin_blank_labels, fin_nan_reach_problems, mode_of,
    oof_rows, standard_labels, verdict_text, write_oof_csv,
)

NAN = float("nan")
_Fin = namedtuple("_Fin", ["period_end"])


class TestConditions:
    def test_conditions_are_drop_and_nan(self):
        conds = build_conditions(fin_nan=True)
        assert list(conds) == list(FIN_NAN_CONDS) == ["drop", "nan"]
        assert conds["drop"].fin_nan_ok is False
        assert conds["nan"].fin_nan_ok is True

    def test_other_axes_stay_at_the_production_shape(self):
        """問うのは「本番の M-2/M-6 で空欄の社を外さずに通したら」。他の軸は本番構成に固定する。"""
        for c in build_conditions(fin_nan=True).values():
            assert c.use_momentum is False
            assert c.momentum_window == MOM_WINDOW
            assert c.use_macro is True
            assert c.build_interactions is True
            assert c.max_features is None
            assert c.fin_rows == "annual"
            assert c.demean_target is False

    def test_baseline_is_the_production_shape(self):
        conds = build_conditions(fin_nan=True)
        assert base_of(conds) == FIN_NAN_BASE_COND == "drop"

    def test_measures_m2_and_m6_on_one_shared_panel(self):
        """M-2 と M-6 は同じ gbdt のパネルを共有する＝パネルに効く軸は両方を測る。"""
        assert FIN_NAN_MODELS == ["xgb_m2", "elasticnet"]
        assert {MODEL_SPECS[m][2] for m in FIN_NAN_MODELS} == {"gbdt"}

    def test_alpha_is_the_promotion_gate_alpha(self):
        """2モデル × 2指標 × 1条件 = 4検定＝既定の昇格ゲート（ADR-0045）と同じ alpha。"""
        conds = build_conditions(fin_nan=True)
        assert bonferroni_alpha(len(FIN_NAN_MODELS), len(conds)) == pytest.approx(0.05 / N_TESTS)


class TestOutput:
    def test_mode_and_file(self):
        assert mode_of(fin_nan=True) == "fin_nan"
        assert MODE_SUFFIX["fin_nan"] == "_fin_nan"

    def test_verdict_is_neutral_and_says_what_the_common_domain_covers(self):
        """本番を変えるかは人が決める（#916）。共通域が財務の揃った行だけであることを見出しに書く。"""
        none = verdict_text("fin_nan", 2, [], [])
        assert none.startswith("FIN NAN AXIS [common domain = complete-financials rows only; "
                               "read raw for blank rows]: ")
        assert "passing blank financials as NaN did not beat dropping them" in none
        hit = verdict_text("fin_nan", 2, ["M-2(XGBoost)/nan/rank_ic"], [])
        assert "PROMOTE" not in hit and "use_momentum" not in hit

    def test_output_is_ascii(self):
        """出力は ASCII のみ（Windows cp932 リダイレクト対策）。"""
        verdict_text("fin_nan", 2, ["x"], ["y"]).encode("ascii")


class TestMainWiring:
    def test_each_condition_builds_with_its_own_flag(self):
        """**渡し忘れても例外は出ない**（両条件が同じパネルで組まれ、差なしになる）。"""
        src = inspect.getsource(momentum_gate.main)
        assert "fin_nan_ok=c.fin_nan_ok" in src
        assert "fin_nan=args.fin_nan" in src
        assert "fin_nan_reach_problems(" in src

    def test_json_records_the_axis(self):
        src = inspect.getsource(momentum_gate.main)
        assert '"fin_nan_ok": c.fin_nan_ok' in src
        assert '"fin_nan_samples": fin_nan_samples' in src

    def test_breakdown_is_wired_into_the_fin_nan_mode(self):
        """内訳（#916）は共通域の外を読む唯一の手段。出し忘れても判定は同じく出るので気づけない。"""
        src = inspect.getsource(momentum_gate.main)
        assert 'load_standards(db) if mode == "fin_nan"' in src
        assert "aligned_run = _align(part[0], ids)" in src
        assert "blank_group_breakdown(aligned_run, group_labels[(cond, kind)])" in src
        assert '"fin_blank_breakdown": fin_blank_breakdown' in src

    def test_row_level_oof_is_written_from_the_same_inputs(self):
        """行ごとの OOF（#951）は内訳と同じ入力から書く。書き忘れても判定と内訳は同じく出るので気づけない。"""
        src = inspect.getsource(momentum_gate.main)
        assert "oof_rows(aligned_run, group_labels[(cond, kind)])" in src
        assert "write_oof_csv(fin_oof_path, fin_oof)" in src
        assert 'out_path.with_name(out_path.stem + "_oof.csv")' in src
        assert '"fin_oof_path": str(fin_oof_path) if fin_oof_path else None' in src
        assert '"fin_oof_rows": fin_oof_rows' in src


class TestBlankGroups:
    def test_every_group_column_is_a_production_feature(self):
        """群の列が `FIN_BASE_OPTIONS` に無いと、その群は黙って1行も付かない（列名の打ち間違い等）。"""
        production = {o["value"] for o in FIN_BASE_OPTIONS}
        for g, cols in FIN_BLANK_GROUPS.items():
            assert set(cols) <= production, g


class TestFinBlankLabels:
    FEATS = ["per", "rd_intensity", "div_yield", "macro_x"]
    FIN = ["per", "rd_intensity", "div_yield"]

    def _labels(self, rows):
        samples = {"2024-03": [(x, 0.0) for x in rows]}
        ids = {"2024-03": [f"E{i}" for i in range(len(rows))]}
        return fin_blank_labels(samples, ids, self.FEATS, self.FIN)

    def test_complete_row(self):
        assert self._labels([[1.0, 2.0, 3.0, 4.0]])[("2024-03", "E0")] == ["complete"]

    def test_only_one_group_blank(self):
        got = self._labels([[1.0, NAN, 3.0, 4.0]])[("2024-03", "E0")]
        assert got == ["any_blank", "blank:rd_intensity", "only:rd_intensity"]

    def test_two_groups_blank_is_neither_only(self):
        got = self._labels([[NAN, NAN, 3.0, 4.0]])[("2024-03", "E0")]
        assert got == ["any_blank", "blank:rd_intensity", "blank:per"]

    def test_blank_macro_is_not_a_blank_financial(self):
        """マクロ・交差項の NaN は財務の空欄ではない（M-2 はマクロの欠損を元から NaN で通す）。"""
        assert self._labels([[1.0, 2.0, 3.0, NAN]])[("2024-03", "E0")] == ["complete"]

    def test_growth_group_covers_any_of_its_columns(self):
        feats = ["eps_growth", "rev_growth"]
        samples = {"2024-03": [([1.0, NAN], 0.0)]}
        got = fin_blank_labels(samples, {"2024-03": ["E0"]}, feats, feats)
        assert got[("2024-03", "E0")] == ["any_blank", "blank:growth", "only:growth"]


class TestStandardLabels:
    def test_uses_the_row_the_snapshot_would_use(self):
        """決算日＋45日が断面日以前の最新の行（`build_snapshots` と同じ選び方）の基準を付ける。"""
        fins = {"E1": [_Fin(datetime.date(2023, 3, 31)), _Fin(datetime.date(2024, 3, 31))]}
        stds = {("E1", "2023-03-31"): "JGAAP", ("E1", "2024-03-31"): "IFRS"}
        ids = {"2024-04": ["E1"], "2024-05": ["E1"]}
        got = standard_labels(ids, fins, stds)
        # 2024-03-31 + 45日 = 2024-05-15 なので、4月はまだ前期の行
        assert got[("2024-04", "E1")] == "std:JGAAP"
        assert got[("2024-05", "E1")] == "std:IFRS"

    def test_cached_string_period_end_matches(self):
        """キャッシュ由来の行は period_end が文字列のことがある。date と同じキーで引けること。"""
        fins = {"E1": [_Fin("2023-03-31")]}
        got = standard_labels({"2024-01": ["E1"]}, fins, {("E1", "2023-03-31"): "US-GAAP"})
        assert got[("2024-01", "E1")] == "std:US-GAAP"

    def test_missing_standard_or_row_is_unknown(self):
        fins = {"E1": [_Fin(datetime.date(2023, 3, 31))]}
        got = standard_labels({"2024-01": ["E1", "E2"]}, fins, {})
        assert got[("2024-01", "E1")] == f"std:{STANDARD_UNKNOWN}"
        assert got[("2024-01", "E2")] == f"std:{STANDARD_UNKNOWN}"


class TestBlankGroupBreakdown:
    @staticmethod
    def _period(ym, preds, reals):
        return {(ym, f"E{i}"): (p, r) for i, (p, r) in enumerate(zip(preds, reals))}

    def test_placement_bias_is_the_mean_rank_gap(self):
        """予測は上位・実現は下位に置かれた群は正（実際より買い側に置いている）。"""
        aligned = self._period("2024-01", [4, 3, 2, 1], [1, 2, 3, 4])
        labels = {("2024-01", "E0"): ["g"], ("2024-01", "E1"): ["g"]}
        row = blank_group_breakdown(aligned, labels)["g"]
        # 予測の順位 4,3・実現の順位 1,2 → ((4-1)+(3-2)) / (2 * 4) = 0.5
        assert row["placement_bias"] == pytest.approx(0.5)
        assert row["samples"] == 2 and row["bias_periods"] == 1
        assert row["placement_bias_t"] is None    # 1期だけでは標準誤差が無い

    def test_rank_ic_is_within_the_group_and_bias_catches_the_shift(self):
        """群の中の順位が完全に合っていれば rank-IC は 1。群ごと上に置かれたずれは bias にだけ出る。"""
        aligned = self._period("2024-01", [10, 11, 12, 1, 2, 3], [1, 2, 3, 6, 5, 4])
        labels = {("2024-01", f"E{i}"): ["g"] for i in range(3)}
        row = blank_group_breakdown(aligned, labels)["g"]
        assert row["rank_ic"] == pytest.approx(1.0)
        # 予測の順位 4,5,6・実現の順位 1,2,3 → (3+3+3) / (3 * 6) = 0.5
        assert row["placement_bias"] == pytest.approx(0.5)

    def test_group_smaller_than_three_has_no_rank_ic_but_has_bias(self):
        aligned = self._period("2024-01", [4, 3, 2, 1], [1, 2, 3, 4])
        labels = {("2024-01", "E0"): ["g"]}
        row = blank_group_breakdown(aligned, labels)["g"]
        assert row["rank_ic"] is None and row["ic_periods"] == 0
        assert row["placement_bias"] == pytest.approx((4 - 1) / 4)

    def test_periods_average_and_t(self):
        aligned = {**self._period("2024-01", [4, 3, 2, 1], [1, 2, 3, 4]),
                   **self._period("2024-02", [1, 2, 3, 4], [1, 2, 3, 4])}
        labels = {(ym, "E0"): ["g"] for ym in ("2024-01", "2024-02")}
        row = blank_group_breakdown(aligned, labels)["g"]
        assert row["bias_periods"] == 2
        assert row["placement_bias"] == pytest.approx((0.75 + 0.0) / 2)
        assert row["placement_bias_t"] == pytest.approx(1.0)   # 平均 0.375 ÷ (標準偏差 0.53033 / √2)

    def test_tiny_period_is_skipped(self):
        aligned = self._period("2024-01", [1, 2], [2, 1])
        assert blank_group_breakdown(aligned, {("2024-01", "E0"): ["g"]}) == {}


class TestOofRows:
    def test_standard_and_groups_are_split_and_rows_are_sorted(self):
        aligned = {("2024-02", "E1"): (0.3, -0.1), ("2024-01", "E2"): (0.2, 0.4),
                   ("2024-01", "E1"): (0.1, 0.5)}
        labels = {("2024-01", "E1"): ["complete", "std:JGAAP"],
                  ("2024-01", "E2"): ["any_blank", "blank:per", "only:per", "std:IFRS"],
                  ("2024-02", "E1"): ["complete", "std:JGAAP"]}
        assert oof_rows(aligned, labels) == [
            ("2024-01", "E1", 0.1, 0.5, "JGAAP", "complete"),
            ("2024-01", "E2", 0.2, 0.4, "IFRS", "any_blank;blank:per;only:per"),
            ("2024-02", "E1", 0.3, -0.1, "JGAAP", "complete"),
        ]

    def test_row_without_a_standard_label_is_unknown(self):
        """`standard_labels` は全行に付けるが、ラベルが欠けても行は落とさず unknown にする。"""
        got = oof_rows({("2024-01", "E1"): (0.1, 0.2)}, {})
        assert got == [("2024-01", "E1", 0.1, 0.2, STANDARD_UNKNOWN, "")]


class TestOofCsv:
    @staticmethod
    def _read(path):
        with path.open(encoding="utf-8", newline="") as f:
            return list(csv.reader(f))

    def test_header_and_run_columns(self, tmp_path):
        path = tmp_path / "sub" / "x_oof.csv"
        n = write_oof_csv(path, {"drop|xgb_m2": [("2024-01", "E1", 0.1, 0.5, "JGAAP", "complete")],
                                 "nan|elasticnet": [("2024-01", "E2", 1, 2, "IFRS", "any_blank")]})
        rows = self._read(path)
        assert n == 2
        assert tuple(rows[0]) == OOF_CSV_COLUMNS
        assert rows[1][:4] == ["drop", "xgb_m2", "2024-01", "E1"]
        assert rows[2][:2] == ["nan", "elasticnet"]
        assert rows[2][4:6] == ["1.0", "2.0"]    # int も float として書く

    def test_breakdown_is_reproducible_from_the_csv(self, tmp_path):
        """CSV だけから内訳を計算し直して一致すること＝列が足り、浮動小数を丸めていないこと。

        予測は 2 社ずつ 1e-12 だけ違う。丸めて書くと同点に化け、順位と bias が変わる。
        """
        aligned, labels = {}, {}
        for ym in ("2024-01", "2024-02"):
            for i in range(6):
                base = 0.1 * (i // 2) + (0.37 if ym == "2024-02" else 0.0)
                aligned[(ym, f"E{i}")] = (base + i * 1e-12, (-1) ** i * 0.01 * (i + 1))
                labels[(ym, f"E{i}")] = (["any_blank", "blank:per"] if i % 2 else ["complete"]) \
                    + [f"std:{'IFRS' if i < 3 else 'JGAAP'}"]
        want = blank_group_breakdown(aligned, labels)

        path = tmp_path / "oof.csv"
        write_oof_csv(path, {"nan|elasticnet": oof_rows(aligned, labels)})
        back_aligned, back_labels = {}, {}
        for r in self._read(path)[1:]:
            rec = dict(zip(OOF_CSV_COLUMNS, r))
            key = (rec["ym"], rec["edinet_code"])
            back_aligned[key] = (float(rec["yhat"]), float(rec["y"]))
            back_labels[key] = rec["blank_groups"].split(";") + [f"std:{rec['standard']}"]
        assert back_aligned == aligned
        assert blank_group_breakdown(back_aligned, back_labels) == want


class TestReachProblems:
    def test_more_rows_on_the_nan_side_is_healthy(self):
        samples = {"drop|gbdt": 100, "nan|gbdt": 130}
        assert fin_nan_reach_problems(samples, ["gbdt"]) == []

    def test_equal_rows_mean_the_switch_did_not_reach(self):
        """空欄の財務を持つ行が無い（または引数が渡っていない）＝同じパネルを比べることになる。"""
        problems = fin_nan_reach_problems({"drop|gbdt": 100, "nan|gbdt": 100}, ["gbdt"])
        assert len(problems) == 1 and "nan|gbdt" in problems[0]

    def test_fewer_rows_on_the_nan_side_is_also_a_problem(self):
        """nan 側は drop 側を含む。減っていたら包含関係そのものが崩れている。"""
        assert fin_nan_reach_problems({"drop|gbdt": 100, "nan|gbdt": 90}, ["gbdt"])

    def test_a_missing_panel_counts_as_zero(self):
        assert fin_nan_reach_problems({"drop|gbdt": 100}, ["gbdt"])
