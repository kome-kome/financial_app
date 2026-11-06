"""`momentum_gate --fin-drop`（#1145）: 財務特徴量から営業利益系と減価償却の列を外すモード。

本番（25 列・`keep`）を分母に、`da_intensity` を外す条件と、営業利益系 5 列と `da_intensity` を外す条件を
共通域で比べる。DB と CV は回さず、条件・列の外し方・届いたかの検査・日中キューの登録を確かめる。
"""
from types import SimpleNamespace

import pytest

import scripts.momentum_gate as G
import scripts.run_daytime as rd

OP = ("op_margin", "op_growth", "z_op_margin", "delta_op_margin", "z_op_margin_sec")


class TestConditions:
    def test_three_preregistered_conditions_with_production_as_base(self):
        conds = G.build_conditions(fin_drop=True)
        assert list(conds) == ["keep", "drop_da", "drop_op_da"]
        assert conds["keep"].fin_drop == ()
        assert conds["drop_da"].fin_drop == ("da_intensity",)
        assert set(conds["drop_op_da"].fin_drop) == set(OP) | {"da_intensity"}
        assert G.base_of(conds) == G.FIN_DROP_BASE_COND == "keep"
        # 他の軸は本番構成のまま（差は外す列だけ）
        assert conds["keep"]._replace(fin_drop=("da_intensity",)) == conds["drop_da"]
        assert conds["keep"] == G.Cond(False, G.MOM_WINDOW)

    def test_dropped_columns_are_real_features(self):
        names = {o["value"] for o in G.FIN_BASE_OPTIONS}
        assert {c for cols in G.FIN_DROP_CONDS.values() for c in cols} <= names

    def test_unknown_column_stops(self, monkeypatch):
        """外す列が特徴量に無いと、何も外さずに「差なし」だけが残る。"""
        monkeypatch.setitem(G.FIN_DROP_CONDS, "drop_da", ("no_such_col",))
        with pytest.raises(ValueError, match="no_such_col"):
            G.build_conditions(fin_drop=True)

    def test_m1_is_rejected(self):
        with pytest.raises(ValueError, match="BIC"):
            G.build_conditions(fin_drop=True, models=["risk_return"])

    def test_eight_tests(self):
        assert G.bonferroni_alpha(len(G.FIN_DROP_MODELS), len(G.FIN_DROP_CONDS)) == pytest.approx(0.05 / 8)

    def test_mode_name_and_suffix(self):
        assert G.mode_of(fin_drop=True) == "fin_drop"
        assert G.MODE_SUFFIX["fin_drop"] == "_fin_drop"
        assert "fin_drop" in G._AXIS_VERDICTS

    def test_cannot_be_combined_with_other_modes(self):
        with pytest.raises(ValueError, match="--fin-drop"):
            G.build_conditions(fin_drop=True, fin_nan=True)

    def test_panels_are_not_shared_between_conditions(self):
        conds = G.build_conditions(fin_drop=True)
        assert len({G.panel_cond(c) for c in conds.values()}) == 3


class TestBuild:
    """`_build` が外す列を `build_snapshots` へ渡す列から抜くこと（M-1 には効かせない）。"""

    def _capture(self, monkeypatch):
        seen = {}

        def fake_build_snapshots(prices, fins, companies, macro, fin_features, *a, **kw):
            seen["fin_features"] = list(fin_features)
            return {}, {}, {}, list(fin_features), {}

        monkeypatch.setattr(G, "build_snapshots", fake_build_snapshots)
        monkeypatch.setattr(G, "_thin", lambda s, m, i, stride: (s, m, i))
        monkeypatch.setattr(G, "_select_bic", lambda s, feats, n: (s, feats))
        monkeypatch.setattr(G, "macro_names_for", lambda kind: [])
        return seen

    def test_gbdt_drops_the_columns(self, monkeypatch):
        seen = self._capture(monkeypatch)
        cols = G.FIN_DROP_CONDS["drop_op_da"]
        G._build("gbdt", SimpleNamespace(stride=1), {}, {}, {}, {}, False, G.MOM_WINDOW, fin_drop=cols)
        assert not set(cols) & set(seen["fin_features"])
        assert len(seen["fin_features"]) == len(G.FIN_BASE_OPTIONS) - len(cols)

    def test_keep_passes_all_25(self, monkeypatch):
        seen = self._capture(monkeypatch)
        G._build("gbdt", SimpleNamespace(stride=1), {}, {}, {}, {}, False, G.MOM_WINDOW)
        assert len(seen["fin_features"]) == len(G.FIN_BASE_OPTIONS)

    def test_m1_ignores_the_drop(self, monkeypatch):
        seen = self._capture(monkeypatch)
        m1_default = G.coerce_params(G.get_plugin("macro_risk_return").params_schema(), {})["fin_features"]
        G._build("m1", SimpleNamespace(stride=1), {}, {}, {}, {}, False, G.MOM_WINDOW,
                 fin_drop=tuple(m1_default[:1]))
        assert seen["fin_features"] == list(m1_default)


class TestReach:
    KINDS = ["gbdt"]

    def _feats(self, cond):
        return [o["value"] for o in G.FIN_BASE_OPTIONS if o["value"] not in G.FIN_DROP_CONDS[cond]]

    def test_healthy(self):
        samples = {"keep|gbdt": 100, "drop_da|gbdt": 110, "drop_op_da|gbdt": 130}
        feats = {f"{c}|gbdt": self._feats(c) for c in G.FIN_DROP_CONDS}
        assert G.fin_drop_reach_problems(samples, feats, self.KINDS) == []

    def test_no_new_rows_is_a_problem(self):
        samples = {"keep|gbdt": 100, "drop_da|gbdt": 100, "drop_op_da|gbdt": 130}
        feats = {f"{c}|gbdt": self._feats(c) for c in G.FIN_DROP_CONDS}
        problems = G.fin_drop_reach_problems(samples, feats, self.KINDS)
        assert len(problems) == 1 and "drop_da|gbdt" in problems[0]

    def test_column_left_in_features_is_a_problem(self):
        samples = {"keep|gbdt": 100, "drop_da|gbdt": 110, "drop_op_da|gbdt": 130}
        feats = {f"{c}|gbdt": self._feats("keep") for c in G.FIN_DROP_CONDS}
        problems = G.fin_drop_reach_problems(samples, feats, self.KINDS)
        assert any("残っている" in p for p in problems)


class TestDaytimeJob:
    def test_job_runs_the_mode_alone(self):
        job = rd.JOBS["gate:fin-drop"]
        assert "--fin-drop" in job.argv and job.parallel_sensitive is True
        assert "--smoke" not in job.argv and job.argv[job.argv.index("--stride") + 1] == "1"
        assert "--models" not in job.argv          # 既定のモデル（M-2 / M-6）
