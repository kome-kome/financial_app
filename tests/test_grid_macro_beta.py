"""格子ドライバ `scripts/grid_macro_beta.py` の不変条件（Issue #540）。

このドライバの存在理由は「測定を残るコードにする」こと（ADR-0041 の教訓＝#509/#517 の昇格
ゲートは2回ともアドホックなスクリプトで実体が残らなかった）。したがって縛るのは
**条件の写し間違いが結果から見分けられない箇所**に絞る:

1. セル指定（`"8,10"` ＝ warmup だけ 8）の解釈
2. コスト見積りと**安い順**の並べ替え（窓が足りないとき失うのが高いセルだけで済む）
3. bench へ渡す引数（`--probe-draws 0` / 全セル同一の `--panel-stamp` / tune・draws の転記）
4. レポートが**生値**を出すこと（丸めた表示で判断しない・#466）

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
