"""`scripts/momentum_gate.py` の窓モードと M-1 対応のユニットテスト（#592）。

既定の昇格ゲート（ADR-0045・2条件 ON/OFF）は `tests/test_momentum_gate.py` が縛っている。
こちらが縛るのは**それを壊さずに軸を広げた**部分:

  - `build_conditions()` … 窓未指定なら既定と完全に同じ2条件へ落ちること
  - `bonferroni_alpha()` … alpha を検定数から**導出**していること（定数を書き写していない）
  - `MODEL_SPECS`      … M-1 が M-2/M-6 とパネルを共有しないこと
  - CV 設定の一致       … `run_one` が M-1 の本番と同じ窓・同じ purge で回ること

最後の1つがこのファイルの主眼である。M-1 を測る手続きは `run_one`（`candidate_bakeoff`）へ
委ねているが、**その定数が `macro_risk_return` の CV 呼び出しとずれても例外は出ず、
それらしい rank-IC が返る**（ADR-0041 が「書き直すと本番と別物を測る」と言っているのは
まさにこの形の失敗）。よって定数を目で合わせるのではなく AST で照合する。

パネル構築（`build_snapshots`）と実測本体は DB フルロードが要るのでここでは触らない。
"""
import ast
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import candidate_bakeoff, momentum_gate
from scripts.momentum_gate import (
    ALPHA, BASE_COND, CONDS, METRICS, MODEL_LABELS, MODEL_SPECS, MODELS, MOM_WINDOW,
    N_TESTS, _select_bic, bonferroni_alpha, build_conditions,
)


class TestBuildConditions:
    def test_default_reproduces_the_adr0045_gate(self):
        """**窓未指定なら既定のゲートと完全に同一。** ここが崩れると過去の判定と繋がらない。"""
        conds = build_conditions()
        assert set(conds) == set(CONDS)
        for name, use_mom in CONDS.items():
            assert conds[name] == (use_mom, MOM_WINDOW)

    def test_none_and_empty_list_are_the_same(self):
        assert build_conditions(None) == build_conditions([])

    def test_windows_mode_adds_the_no_momentum_baseline(self):
        """基準（モメンタム無し）が必ず入る。無いと「何と比べたのか」が消える。"""
        conds = build_conditions([6, 12])
        assert BASE_COND in conds
        assert conds[BASE_COND][0] is False
        assert conds["mw6"] == (True, 6)
        assert conds["mw12"] == (True, 12)

    def test_windows_are_sorted_and_deduplicated(self):
        """重複した窓を測っても情報は増えず、検定数だけ増えて alpha が不当に締まる。"""
        conds = build_conditions([18, 6, 6, 12, 18])
        assert list(conds) == [BASE_COND, "mw6", "mw12", "mw18"]

    def test_string_windows_are_accepted(self):
        """CLI は文字列で来る。int 化はここが一手に引き受ける。"""
        assert build_conditions(["6", "12"]) == build_conditions([6, 12])

    @pytest.mark.parametrize("bad", [[0], [-1], [3, 0]])
    def test_non_positive_window_is_rejected(self, bad):
        """窓 0 は `build_snapshots` 側で静かに別解釈されうるので入口で弾く。"""
        with pytest.raises(ValueError):
            build_conditions(bad)


class TestBonferroniAlpha:
    def test_default_matches_the_frozen_constant(self):
        """既定（2モデル・2条件）の導出値が定数 `ALPHA` と一致すること。

        一致していれば、窓モードで alpha を導出へ切り替えても既定の判定は動かない。
        """
        assert bonferroni_alpha(len(MODELS), len(CONDS)) == pytest.approx(ALPHA)
        assert len(MODELS) * len(METRICS) * (len(CONDS) - 1) == N_TESTS

    def test_more_windows_tighten_alpha(self):
        """窓を5本振れば検定は5倍。**締まらないなら多重比較を補正し損ねている。**"""
        one = bonferroni_alpha(1, 2)          # 基準 + 1条件
        five = bonferroni_alpha(1, 6)         # 基準 + 5条件
        assert five == pytest.approx(one / 5)

    def test_more_models_tighten_alpha(self):
        assert bonferroni_alpha(2, 2) == pytest.approx(bonferroni_alpha(1, 2) / 2)

    def test_single_condition_does_not_divide_by_zero(self):
        """条件が基準1つだけでも落ちない（検定は0本だが alpha は定義しておく）。"""
        assert bonferroni_alpha(1, 1) > 0


class TestModelSpecs:
    def test_default_models_are_all_known(self):
        assert set(MODELS) <= set(MODEL_SPECS)

    def test_every_spec_has_a_label(self):
        """ラベル欠けは KeyError で落ちるだけだが、実測の最中に落ちると数時間が消える。"""
        assert set(MODEL_SPECS) <= set(MODEL_LABELS)

    def test_m1_does_not_share_a_panel_with_m2(self):
        """**M-1 は strict（`macro_nan_ok=False`）で母集団が別物。**

        種別を共有させると M-1 を M-2 の母集団で測ることになり、#592 が分離しようと
        している当の交絡を測定側に持ち込む。
        """
        assert MODEL_SPECS["risk_return"][2] != MODEL_SPECS["xgb_m2"][2]

    def test_m2_and_m6_still_share_one_panel(self):
        """M-6 は M-2 と同設定（`macro_enet.py` の宣言）。共有は従来どおり保つ。"""
        assert MODEL_SPECS["elasticnet"][2] == MODEL_SPECS["xgb_m2"][2]

    def test_m1_is_estimated_as_plain_ols(self):
        """M-1 の CV は `fit_predict` を渡さない素の OLS（`macro_risk_return` の呼び出し）。"""
        assert MODEL_SPECS["risk_return"][1] == "ols"


class TestCvSettingsMatchProduction:
    """`run_one` の CV 設定が M-1 の本番と一致していることを AST で照合する。

    文字列一致だと自分のコメント本文にも当たるので（#596 で実際に踏んだ）、
    `tests/test_column_scoping.py` と同じく構文木を読む。
    """

    @staticmethod
    def _m1_cv_kwargs() -> dict:
        import plugins.macro_risk_return as m1
        tree = ast.parse(inspect.getsource(m1))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", None))
                 == "walk_forward_cv_monthly"]
        assert len(calls) == 1, f"M-1 の CV 呼び出しが {len(calls)} 箇所ある（1 を想定）"
        return {k.arg: k.value for k in calls[0].keywords}

    def test_min_train_months_matches(self):
        kw = self._m1_cv_kwargs()
        assert isinstance(kw["min_train_months"], ast.Constant)
        assert kw["min_train_months"].value == candidate_bakeoff.MIN_TRAIN_MONTHS

    def test_step_months_matches(self):
        kw = self._m1_cv_kwargs()
        assert isinstance(kw["step_months"], ast.Constant)
        assert kw["step_months"].value == candidate_bakeoff.STEP_MONTHS

    def test_embargo_comes_from_the_same_constant(self):
        """purge は両者とも `LABEL_HORIZON_MONTHS`（52週ラベルの月換算・ADR-0014）。

        片方だけ数値へ書き換えられると、purge の非対称が**静かに**生まれる。
        """
        kw = self._m1_cv_kwargs()
        assert isinstance(kw["embargo_months"], ast.Name)
        assert kw["embargo_months"].id == "LABEL_HORIZON_MONTHS"
        from plugins.macro_snapshots import LABEL_HORIZON_MONTHS
        assert candidate_bakeoff.LABEL_HORIZON_MONTHS == LABEL_HORIZON_MONTHS

    def test_m1_returns_residuals(self):
        """残差が返らないと共通 (ym,ec) 域の制限そのものができない。"""
        kw = self._m1_cv_kwargs()
        assert kw["return_residuals"].value is True


class TestSelectBic:
    """BIC 選択が**サンプル順を保存する**こと。

    `_restrict` / `_align` / `build_oof_meta` は `samples_by_ym[ym]` と `ids_by_ym[ym]` の
    index 1:1 対応に依拠している。ここで順序が変わると、共通域の突合が**別の銘柄同士を
    突き合わせたまま、それらしい数値を返す**。
    """

    @staticmethod
    def _panel() -> dict:
        return {
            "2024-01": [([1.0, 2.0, 3.0], 0.1), ([4.0, 5.0, 6.0], 0.2)],
            "2024-02": [([7.0, 8.0, 9.0], 0.3)],
        }

    def test_keeps_only_the_selected_columns(self, monkeypatch):
        monkeypatch.setattr(momentum_gate, "get_plugin",
                            lambda _n: type("P", (), {
                                "_select_macro_features": staticmethod(
                                    lambda *a, **k: ["c", "a"])})())
        sel, names = _select_bic(self._panel(), ["a", "b", "c"], max_features=2)
        assert names == ["c", "a"]
        # 列は selected の順に並ぶ（feat_names の順ではない）
        assert sel["2024-01"][0][0] == [3.0, 1.0]

    def test_row_order_and_targets_are_preserved(self, monkeypatch):
        monkeypatch.setattr(momentum_gate, "get_plugin",
                            lambda _n: type("P", (), {
                                "_select_macro_features": staticmethod(
                                    lambda *a, **k: ["a"])})())
        sel, _ = _select_bic(self._panel(), ["a", "b", "c"], max_features=1)
        assert [t for _, t in sel["2024-01"]] == [0.1, 0.2]
        assert [t for _, t in sel["2024-02"]] == [0.3]
        assert list(sel) == ["2024-01", "2024-02"]

    def test_empty_selection_stops_instead_of_measuring_nothing(self, monkeypatch):
        """0本で続行すると「特徴量ゼロの OLS」を測って rank-IC 0 付近を返す。"""
        monkeypatch.setattr(momentum_gate, "get_plugin",
                            lambda _n: type("P", (), {
                                "_select_macro_features": staticmethod(
                                    lambda *a, **k: [])})())
        with pytest.raises(SystemExit):
            _select_bic(self._panel(), ["a", "b"], max_features=2)
