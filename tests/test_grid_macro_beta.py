"""格子ドライバ `scripts/grid_macro_beta.py` の不変条件（Issue #540）。

このドライバの存在理由は「測定を残るコードにする」こと（ADR-0041 の教訓＝#509/#517 の昇格
ゲートは2回ともアドホックなスクリプトで実体が残らなかった）。したがって縛るのは
**条件の写し間違いが結果から見分けられない箇所**に絞る:

1. セル指定（`"8,10"` ＝ warmup だけ 8）の解釈
2. コスト見積りと**安い順**の並べ替え（窓が足りないとき失うのが高いセルだけで済む）
3. bench へ渡す引数（`--probe-draws 0` / 全セル同一の `--panel-stamp` / tune・draws の転記）
4. レポートが**生値**を出すこと（丸めた表示で判断しない・#466）
5. 規模の軸（#664）: 直積とラベル、`--resume` の照合（条件で照合し、違う条件・ESS の無い
   record を済みにしない）、締切判定、所要見積りの較正、規模の表の判定規則

NUTS は CI で回せないので、サンプリングを含まない純粋部分だけを見る。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import grid_macro_beta as gmb  # noqa: E402


class _Args:
    """`bench_command` が読む属性だけを持つ最小の argparse 代役。"""

    def __init__(self, **kw):
        defaults = dict(mode="real", n_stock=250, chains=2, tune=800, draws=400, repeat=1,
                        seed=0, nuts_sampler="numpyro", init="adapt_diag",
                        out=".logs/bench_540.jsonl", panel_stamp="20260825")
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


class TestParseDepthSpec:
    def test_single_depth_applies_to_both_phases(self):
        assert gmb.parse_depth_spec("8") == (8, 8)

    def test_comma_form_is_warmup_then_sampling(self):
        # "8,10" は **warmup だけ 8**（draws 側の軌道長は変えない）の意。
        assert gmb.parse_depth_spec("8,10") == (8, 10)

    def test_empty_means_sampler_default_ten(self):
        # 見積り用に 10 とみなすだけ。bench へ渡す文字列に既定値を埋めたりはしない。
        assert gmb.parse_depth_spec("") == (10, 10)
        assert gmb.parse_depth_spec(None) == (10, 10)


class TestCellTotalSteps:
    def test_counts_warmup_and_draws_over_all_chains(self):
        # chains=2, tune=10, draws=5, depth 3 => 2 * (10*7 + 5*7) = 210
        assert gmb.cell_total_steps("3", tune=10, draws=5, chains=2) == 210

    def test_warmup_only_cap_is_cheaper_than_flat_cap(self):
        flat = gmb.cell_total_steps("10", tune=800, draws=400, chains=2)
        warm = gmb.cell_total_steps("8,10", tune=800, draws=400, chains=2)
        # warmup が全 iter の 2/3 を占める設定なので、warmup だけ切っても半分になる。
        assert warm < flat
        assert warm == 2 * (800 * 255 + 400 * 1023)


class TestBuildCells:
    def _cells(self):
        return gmb.build_cells(["10", "7", "8,10", "8", "9"], [0.95],
                               tune=800, draws=400, chains=2, us_per_step=2084.9)

    def test_sorted_cheapest_first(self):
        steps = [c["est_total_steps"] for c in self._cells()]
        assert steps == sorted(steps)
        # 現行設定（md=10）が最後＝窓が切れたとき失うのは「既に分かっている量に最も近い」セル。
        assert self._cells()[-1]["max_tree_depth"] == "10"

    def test_labels_are_unique_and_filename_safe(self):
        cells = gmb.build_cells(["8", "8,10"], [0.9, 0.95], tune=800, draws=400,
                                chains=2, us_per_step=2084.9)
        labels = [c["label"] for c in cells]
        assert len(set(labels)) == len(labels)
        # カンマはラベルに残さない（JSONL の label が CSV 的に読まれても壊れない）。
        assert all("," not in lab for lab in labels)
        assert "md8w10-ta095" in labels

    def test_estimate_scales_with_steps(self):
        cells = self._cells()
        cheap, dear = cells[0], cells[-1]
        ratio_steps = dear["est_total_steps"] / cheap["est_total_steps"]
        ratio_min = dear["est_minutes"] / cheap["est_minutes"]
        assert ratio_min == pytest.approx(ratio_steps)


class TestBenchCommand:
    def _cmd(self, **kw):
        cell = {"label": "md8-ta095", "max_tree_depth": "8", "target_accept": 0.95}
        return gmb.bench_command("py", cell, _Args(**kw))

    def test_probe_is_disabled(self):
        cmd = self._cmd()
        # probe は2点回帰のための道具。1点しか測らない格子では tune=800 の warmup を
        # 1本余計に払うだけの純損になる。
        assert "--probe-draws" in cmd and cmd[cmd.index("--probe-draws") + 1] == "0"

    def test_forwards_the_cell_and_the_shared_conditions(self):
        cmd = self._cmd()
        for flag, want in (("--max-tree-depth", "8"), ("--target-accept", "0.95"),
                           ("--tune", "800"), ("--draws", "400"), ("--chains", "2"),
                           ("--n-stock", "250"), ("--label", "md8-ta095")):
            assert cmd[cmd.index(flag) + 1] == want

    def test_panel_stamp_pins_the_generation_in_real_mode(self):
        # 格子は数時間＝日付を跨ぐ。stamp を固定しないと途中のセルだけ別パネルを見る
        # （比較の前提が壊れているのに出力は何事も無く並ぶ・#454/#456 と同型）。
        assert self._cmd()[self._cmd().index("--panel-stamp") + 1] == "20260825"

    def test_panel_stamp_absent_in_synth_mode(self):
        # synth は DB を触らずキャッシュも使わない＝日付印の概念が無い。
        assert "--panel-stamp" not in self._cmd(mode="synth")

    def test_runs_bench_as_a_module(self):
        # `python scripts/bench_macro_beta.py` 直接実行は ModuleNotFoundError になる
        # （feedback_scripts_dir_needs_module_invocation）。
        cmd = self._cmd()
        assert cmd[1:3] == ["-m", "scripts.bench_macro_beta"]


def _jsonl(tmp_path, records):
    path = os.path.join(str(tmp_path), "bench_540.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + chr(10))
    return path


def _record(label="md8-ta095", md="8", ess=True):
    run = {"draws": 400, "seconds": 1276.4, "n_divergences": 0, "total_steps": 612000,
           "steps": {"mean": 255.0, "max_treedepth_rate": 1.0, "cap_steps": 255},
           "ess_bulk_median_per_1e6step": 837.7, "ess_bulk_median_per_sec": 0.4017,
           "ess": {"r_hat_max": 1.0231, "ess_bulk_min": 101.7, "ess_bulk_p10": 203.4,
                   "ess_bulk_median": 512.9, "ess_tail_min": 190.2, "n_params": 3012}}
    if not ess:
        run["ess"] = None
        run["ess_bulk_median_per_1e6step"] = None
        run["ess_bulk_median_per_sec"] = None
    return {"label": label, "mode": "real",
            "panel": {"n_stock": 250, "n_sector": 33, "n_factor": 12, "n_obs": 6190},
            "config": {"chains": 2, "tune": 800, "draws_list": [400], "target_accept": 0.95,
                       "max_tree_depth": md, "panel_stamp": "20260825"},
            "runs": [run]}


class TestReport:
    def test_prints_raw_values(self, tmp_path):
        text = gmb.report(_jsonl(tmp_path, [_record()]))
        # 生値のまま（有効数字を落とすと格子の差が消える・#466）。
        assert "101.7" in text and "512.9" in text and "1.0231" in text
        assert "837.7" in text
        text.encode("cp932")   # cp932 コンソールへリダイレクトしても落ちないこと

    def test_missing_ess_is_na_not_zero(self, tmp_path):
        # 「測れなかった」を 0 と書くと「効率ゼロ」という別の主張になる。
        text = gmb.report(_jsonl(tmp_path, [_record(ess=False)]))
        assert "n/a" in text

    def test_one_line_per_run(self, tmp_path):
        path = _jsonl(tmp_path, [_record("md7-ta095", "7"), _record("md8-ta095", "8")])
        text = gmb.report(path)
        assert "md7-ta095" in text and "md8-ta095" in text

    def test_missing_file_is_reported_not_raised(self, tmp_path):
        assert "まだ無い" in gmb.report(os.path.join(str(tmp_path), "nope.jsonl"))

    def test_names_the_primary_metric(self, tmp_path):
        # wall time で比べさせないための注記。表だけ切り出して貼られても意図が残る。
        text = gmb.report(_jsonl(tmp_path, [_record()]))
        assert "ESS/1e6step" in text
        assert "primary metric" in text


class TestGateColumn:
    """格子の表は**本番の合否**を出す（#613）。

    #611 で本番のゲートが「変数ごとの `r_hat` の p99」になったのに表は `r_hat_max` を
    出したままだった。人は表の値を 1.05 と見比べて `max_tree_depth` 等を選ぶので、
    **その基準が本番と違えば格子を回す意味が消える**。ずれる向きは「通る側」だけ＝
    良い設定を誤って捨てる。
    """

    def _by_param(self, mu_p99):
        return {"beta":        {"n": 3000, "r_hat_p99": 1.0129, "r_hat_max": 1.0411},
                "alpha":       {"n": 250,  "r_hat_p99": 1.0370, "r_hat_max": 1.0659},
                "mu_universe": {"n": 12,   "r_hat_p99": mu_p99, "r_hat_max": mu_p99}}

    def _rec(self, label, mu_p99, r_hat_max):
        rec = _record(label)
        rec["runs"][0]["ess"]["by_param"] = self._by_param(mu_p99)
        rec["runs"][0]["ess"]["r_hat_max"] = r_hat_max
        return rec

    def test_collapsed_cell_is_marked_fail(self, tmp_path):
        # .logs/bench_609_gate.jsonl の md=8（mu_universe が固着）。新規計測は要らない。
        text = gmb.report(_jsonl(tmp_path, [self._rec("md8-ta095", 1.6791, 1.7157)]))
        assert "FAIL" in text
        assert "mu_universe" in text

    def test_healthy_cell_is_marked_pass_even_with_high_r_hat_max(self, tmp_path):
        """`r_hat_max` が閾値超えでも新ゲートでは通る＝**旧基準なら誤って捨てていた**行。"""
        text = gmb.report(_jsonl(tmp_path, [self._rec("md8w10", 1.0213, 1.1242)]))
        assert "PASS" in text
        assert "FAIL" not in text

    def test_says_r_hat_max_is_not_the_gate(self, tmp_path):
        # 表だけ切り出して貼られても「max を閾値と見比べない」意図が残ること。
        text = gmb.report(_jsonl(tmp_path, [self._rec("md8w10", 1.0213, 1.1242)]))
        assert "r_hat_max is NOT the gate quantity" in text

    def test_gate_verdict_is_not_reimplemented_in_the_report(self):
        """判定と閾値を report 側へ書き写していないこと（本番と格子で基準がずれる）。"""
        import ast

        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "bench_macro_beta_report.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "gate_verdict" not in defined, "判定は macro_beta_inference が唯一の源"
        # 閾値のリテラルを持たない（MONTHLY_RHAT_THRESHOLD 経由で参照する）。
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, float)}
        assert 1.05 not in literals, "閾値を書き写している（MONTHLY_RHAT_THRESHOLD を使うこと）"


class TestRegimeNote:
    """**測った regime が #540 の対象かどうか**の警告（`bench_macro_beta_report.regime_note`）。

    表の一部なのでここで縛る。2026-08-25 に実際に踏んだ罠——実データを 250銘柄へ間引くと
    `select_shared_factors` が 5因子しか選ばず、md=9/10 の `td_rate` が 0.000（軌道長 255 で
    自然停止）になった。**#540 の前提「1023 に張り付く」が成り立っていない**のに、表は何事も
    無かったように並ぶ。警告が無ければ間違った regime の数字をそのまま ADR へ書いていた。
    """

    def _run(self, cap, rate):
        return {"draws": 400, "seconds": 1.0, "n_divergences": 0, "total_steps": 1000,
                "steps": {"mean": 255.0, "max_treedepth_rate": rate, "cap_steps": cap},
                "ess": {"r_hat_max": 1.02, "ess_bulk_min": 100.0, "ess_bulk_p10": 200.0,
                        "ess_bulk_median": 500.0, "ess_tail_min": 150.0, "n_params": 10}}

    def _rec(self, label, cap, rate):
        return {"label": label, "config": {"max_tree_depth": cap}, "panel": {},
                "runs": [self._run(cap, rate)]}

    def test_silent_when_the_loosest_cap_is_pinned(self):
        from scripts.bench_macro_beta_report import regime_note
        records = [self._rec("md8", 255, 1.0), self._rec("md10", 1023, 1.0)]
        assert regime_note(records) == ""

    def test_warns_when_the_loosest_cap_is_not_binding(self):
        from scripts.bench_macro_beta_report import regime_note
        # 緩い方（cap=1023）が張り付いていない＝上限は律速ではない＝#540 の regime ではない。
        records = [self._rec("md8", 255, 1.0), self._rec("md10", 1023, 0.0)]
        note = regime_note(records)
        assert "regime に居ない" in note
        assert "md10" in note

    def test_judges_by_the_loosest_cap_not_the_tightest(self):
        from scripts.bench_macro_beta_report import regime_note
        # 厳しい方が張り付くのは当たり前（cap が小さいのだから）。判定に使ってはいけない。
        records = [self._rec("md7", 127, 1.0), self._rec("md10", 1023, 0.0)]
        assert regime_note(records) != ""
        records = [self._rec("md7", 127, 0.0), self._rec("md10", 1023, 1.0)]
        assert regime_note(records) == ""

    def test_missing_measurements_do_not_produce_a_false_all_clear(self):
        from scripts.bench_macro_beta_report import regime_note
        # 測れていないものを「問題なし」と読ませない（空文字＝判定不能も同じ扱いだが、
        # 表には td_rate が n/a として並ぶので人が気づける）。
        rec = self._rec("x", None, None)
        rec["runs"][0]["steps"] = {"mean": None, "max_treedepth_rate": None, "cap_steps": None}
        assert regime_note([rec]) == ""

    def test_report_embeds_the_warning(self, tmp_path):
        rec = _record()
        rec["runs"][0]["steps"] = {"mean": 255.0, "max_treedepth_rate": 0.0, "cap_steps": 1023}
        assert "regime に居ない" in gmb.report(_jsonl(tmp_path, [rec]))


# ---- 規模の軸（#664）--------------------------------------------------------------------

class TestScaleAxis:
    """銘柄数 × seed の直積。**振った軸だけ**をラベルに付ける（1値なら従来ラベルのまま）。"""

    def _cells(self, n_stocks=(250, 500), seeds=(0, 1)):
        return gmb.build_cells(["8,10"], [0.95], tune=800, draws=800, chains=2,
                               us_per_step=190.2, n_stocks=n_stocks, seeds=seeds)

    def test_product_of_the_axes(self):
        cells = self._cells()
        assert {(c["n_stock"], c["seed"]) for c in cells} == {
            (250, 0), (250, 1), (500, 0), (500, 1)}

    def test_labels_name_only_the_varied_axes(self):
        labels = {c["label"] for c in self._cells()}
        assert "md8w10-ta095-n0250-s0" in labels
        only_seed = {c["label"] for c in self._cells(n_stocks=(250,))}
        assert only_seed == {"md8w10-ta095-s0", "md8w10-ta095-s1"}

    def test_single_values_keep_the_old_label(self):
        # 既存の JSONL・表の見え方を変えない（#540 の格子はこれまでどおり）。
        cells = self._cells(n_stocks=(250,), seeds=(0,))
        assert [c["label"] for c in cells] == ["md8w10-ta095"]

    def test_cheapest_first_across_stock_counts(self):
        cells = self._cells(n_stocks=(2000, 250, 1000), seeds=(1, 0))
        assert [c["n_stock"] for c in cells] == [250, 250, 1000, 1000, 2000, 2000]
        # 同じ費用の中は seed の昇順（並びが実行ごとに揺れない）。
        assert [c["seed"] for c in cells[:2]] == [0, 1]

    def test_estimate_scales_with_stock_count(self):
        small, big = self._cells(n_stocks=(250, 1000), seeds=(0,))
        assert big["est_minutes"] / small["est_minutes"] == pytest.approx(4 ** gmb.SCALE_EXP)
        # 基準銘柄数では係数が効かない（#540 の見積りを変えない）。
        steps = gmb.cell_total_steps("8,10", 800, 800, 2)
        assert small["est_minutes"] == pytest.approx(steps * 190.2 / 1e6 / 60.0)


class TestBenchCommandScaleAxis:
    def test_cell_values_win_over_args(self):
        cell = {"label": "x", "max_tree_depth": "8,10", "target_accept": 0.95,
                "n_stock": 1000, "seed": 2}
        cmd = gmb.bench_command("py", cell, _Args(mode="synth", n_stock=[250], seed=[0]))
        assert cmd[cmd.index("--n-stock") + 1] == "1000"
        assert cmd[cmd.index("--seed") + 1] == "2"

    def test_panel_seed_is_forwarded_only_when_given(self):
        cell = {"label": "x", "max_tree_depth": "8", "target_accept": 0.95}
        assert "--panel-seed" not in gmb.bench_command("py", cell, _Args(mode="synth"))
        cmd = gmb.bench_command("py", cell, _Args(mode="synth", panel_seed=0))
        assert cmd[cmd.index("--panel-seed") + 1] == "0"


def _scale_record(n_stock, seed, alpha, beta=1.01, mu=1.02, n_div=0, panel_seed=0,
                  md=(8, 10), draws=800, ess=True, seconds=600.0):
    run = {"draws": draws, "seconds": seconds, "n_divergences": n_div, "diag_sec": 30.0,
           "steps": {"mean": 1023.0, "max_treedepth_rate": 1.0, "cap_steps": 1023},
           "ess": {"r_hat_max": alpha + 0.02, "ess_bulk_min": 100.0, "ess_bulk_p10": 200.0,
                   "ess_bulk_median": 500.0, "ess_tail_min": 150.0, "n_params": 10,
                   "by_param": {
                       "alpha": {"n": n_stock, "r_hat_p99": alpha, "r_hat_max": alpha + 0.02},
                       "beta": {"n": n_stock * 12, "r_hat_p99": beta, "r_hat_max": beta + 0.02},
                       "mu_universe": {"n": 12, "r_hat_p99": mu, "r_hat_max": mu}}}}
    if not ess:
        run["ess"] = None
    return {"label": "md8w10-ta095-n{0:04d}-s{1}".format(n_stock, seed), "mode": "synth",
            "panel": {"n_stock": n_stock, "n_sector": 34, "n_factor": 12, "n_obs": n_stock * 24},
            "config": {"chains": 2, "tune": 800, "draws_list": [draws], "target_accept": 0.95,
                       "max_tree_depth": list(md), "seed": seed, "panel_seed": panel_seed,
                       "panel_stamp": None},
            "stage_sec": {"panel": 0.1, "model_build": 3.0, "sample_total": seconds},
            "runs": [run]}


class _GridArgs:
    mode, chains, tune, draws, panel_stamp, panel_seed = "synth", 2, 800, 800, "20260919", 0


class TestResume:
    """`--resume` の照合は**条件**で行う（ラベルではない）。違う条件の record を済みにしない。"""

    def _cell(self, n=250, seed=0, md="8,10"):
        return {"label": "whatever", "max_tree_depth": md, "target_accept": 0.95,
                "n_stock": n, "seed": seed}

    def test_matching_record_marks_the_cell_done(self):
        done = gmb.done_keys([_scale_record(250, 0, 1.02)])
        assert gmb.key_of(self._cell(), _GridArgs) in done

    def test_other_seed_or_stock_count_is_not_done(self):
        done = gmb.done_keys([_scale_record(250, 0, 1.02)])
        assert gmb.key_of(self._cell(seed=1), _GridArgs) not in done
        assert gmb.key_of(self._cell(n=500), _GridArgs) not in done

    def test_different_sampling_config_is_not_done(self):
        # draws 400 で測った record は、draws 800 のセルの代わりにならない。
        done = gmb.done_keys([_scale_record(250, 0, 1.02, draws=400)])
        assert gmb.key_of(self._cell(), _GridArgs) not in done
        done = gmb.done_keys([_scale_record(250, 0, 1.02, md=(10, 10))])
        assert gmb.key_of(self._cell(), _GridArgs) not in done

    def test_other_panel_seed_is_not_done(self):
        done = gmb.done_keys([_scale_record(250, 0, 1.02, panel_seed=7)])
        assert gmb.key_of(self._cell(), _GridArgs) not in done

    def test_record_without_ess_is_not_done(self):
        # 規模の表に点を作れない＝測ったことにならない。
        done = gmb.done_keys([_scale_record(250, 0, 1.02, ess=False)])
        assert gmb.key_of(self._cell(), _GridArgs) not in done

    def test_old_record_without_panel_seed_means_panel_seed_equals_seed(self):
        rec = _scale_record(250, 3, 1.02)
        del rec["config"]["panel_seed"]
        key = gmb.record_key(rec)
        assert key[7] == 3 and key[9] == 3

    def test_depth_forms_are_equivalent(self):
        assert gmb.depth_key("8,10") == gmb.depth_key([8, 10]) == (8, 10)
        assert gmb.depth_key(None) == gmb.depth_key("10") == gmb.depth_key(10) == (10, 10)

    def test_broken_lines_are_skipped(self, tmp_path):
        # 殺されたセルの書きかけ1行で再開ごと止めない。
        path = os.path.join(str(tmp_path), "x.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_scale_record(250, 0, 1.02)) + chr(10))
            f.write('{"label": "trunc')
        assert len(gmb.load_records(path)) == 1
        assert gmb.load_records(os.path.join(str(tmp_path), "nope.jsonl")) == []


class TestEstimate:
    def _cell(self, n):
        return gmb.build_cells(["8,10"], [0.95], tune=800, draws=800, chains=2,
                               us_per_step=190.2, n_stocks=(n,), seeds=(0,))[0]

    def test_no_measurement_uses_the_model_plus_overhead(self):
        c = self._cell(500)
        want = c["est_minutes"] + gmb.CELL_OVERHEAD_MIN
        assert gmb.estimate_minutes(c, []) == pytest.approx(want)

    def test_same_stock_count_uses_the_slowest_measurement(self):
        recs = [_scale_record(500, 0, 1.02, seconds=600.0),
                _scale_record(500, 1, 1.02, seconds=900.0)]
        got = gmb.estimate_minutes(self._cell(500), recs)
        assert got == pytest.approx(gmb.record_minutes(recs[1]))

    def test_other_stock_counts_calibrate_the_model(self):
        # 250銘柄の実測が見積りの2倍なら、1000銘柄の見積りも2倍にする。
        c250, c1000 = self._cell(250), self._cell(1000)
        model_250 = c250["est_minutes"] + gmb.CELL_OVERHEAD_MIN
        rec = _scale_record(250, 0, 1.02)
        # record_minutes = (stage 合計 + diag) / 60 + overhead を model の2倍へ合わせる。
        rec["stage_sec"]["sample_total"] = (2 * model_250 - gmb.CELL_OVERHEAD_MIN) * 60.0 - 33.1
        assert gmb.record_minutes(rec) == pytest.approx(2 * model_250)
        got = gmb.estimate_minutes(c1000, [rec])
        assert got == pytest.approx(2 * (c1000["est_minutes"] + gmb.CELL_OVERHEAD_MIN))


class TestFitsBefore:
    def test_no_deadline_always_fits(self):
        from datetime import datetime, timezone
        assert gmb.fits_before(None, datetime.now(timezone.utc), 1e9)

    def test_safety_and_margin_are_applied(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
        est = 100.0
        need = est * gmb.DEADLINE_SAFETY + gmb.DEADLINE_MARGIN_MIN
        assert gmb.fits_before(now + timedelta(minutes=need), now, est)
        assert not gmb.fits_before(now + timedelta(minutes=need - 0.5), now, est)
        # 見積りそのものより長い残りがあっても、安全率ぶん足りなければ始めない。
        assert not gmb.fits_before(now + timedelta(minutes=est + 1), now, est)


class TestScaleView:
    """#664 の規模の表（`bench_macro_beta_report.scale_table`）。"""

    def _records(self, slope_alpha, n_values=(250, 500, 1000, 2000), seeds=(0, 1, 2)):
        import math
        recs = []
        for n in n_values:
            for s in seeds:
                jitter = (s - 1) * 0.001
                recs.append(_scale_record(n, s, 1.02 + slope_alpha * math.log(n / 250) + jitter,
                                          mu=1.02 + jitter))
        return recs

    def _alpha(self, recs, var="alpha"):
        from scripts.bench_macro_beta_report import scale_points
        return [p for p in scale_points(recs) if p["var"] == var]

    def test_increasing_slope_is_detected(self):
        from scripts.bench_macro_beta_report import scale_fit
        fit = scale_fit(self._alpha(self._records(0.01)))
        assert fit["slope"] == pytest.approx(0.01, abs=1e-6)
        assert fit["verdict"] == "INCREASING"

    def test_flat_control_is_not_detected(self):
        from scripts.bench_macro_beta_report import scale_fit
        fit = scale_fit(self._alpha(self._records(0.01), "mu_universe"))
        assert fit["verdict"] == "NOT DETECTED"

    def test_divergent_runs_are_excluded_from_the_fit(self):
        from scripts.bench_macro_beta_report import scale_fit
        recs = self._records(0.0)
        # 発散した run が大きな p99 を出しても傾きを作らない（並走の汚染を規模と読まない）。
        recs.append(_scale_record(2000, 9, 1.30, n_div=344))
        fit = scale_fit(self._alpha(recs))
        assert fit["verdict"] == "NOT DETECTED"
        assert fit["k"] == 12

    def test_too_few_points_is_na_not_a_verdict(self):
        from scripts.bench_macro_beta_report import scale_fit
        fit = scale_fit(self._alpha(self._records(0.01, n_values=(250,))))
        assert fit["verdict"] == "n/a"

    def test_table_shows_spread_verdict_and_gate(self, tmp_path):
        recs = self._records(0.01)
        recs.append(_scale_record(2000, 9, 1.30, n_div=344))
        text = gmb.report(_jsonl(tmp_path, recs), "scale")
        assert "INCREASING" in text
        assert "spread(max-min)" in text
        assert "excluded 1 run(s) with divergences" in text
        assert "PASS" in text and "FAIL" in text      # 本番と同じ合否（1.30 の alpha は落ちる）
        assert "0.0146" in text                        # 本番の run 間差と並べて読む
        text.encode("cp932")

    def test_different_configs_are_not_mixed(self, tmp_path):
        recs = self._records(0.0) + [_scale_record(250, 0, 1.5, draws=400)]
        text = gmb.report(_jsonl(tmp_path, recs), "scale")
        # draws 400 の record は別の節になる（1本の傾きへ混ぜない）。
        assert text.count("r_hat p99 vs n_stock") == 2

    def test_no_by_param_is_reported_not_raised(self, tmp_path):
        text = gmb.report(_jsonl(tmp_path, [_record()]), "scale")
        assert "by_param" in text
