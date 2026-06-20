"""スライダー（type='slider'）の params_schema 契約を全プラグイン横断で検証する。

背景: HTML の range 入力は step 未指定だと step='any'（連続値）になり、int dtype の
スライダーが `25.0912…` のような端数を吐いて coerce_params に reject される事故が起きた
（M-1 max_features）。これを構造的に防ぐため、スライダーは以下を必ず満たすことを契約化する:

  1. すべてのスライダーは `step` を明示宣言する（粒度の源は schema）。
  2. int dtype のスライダーの `step` は整数（グリッドが必ず整数に乗る）。
  3. `default` は min を起点に step グリッド上へ乗る（初期表示が妥当）。

JS 側（static/js/analysis.js）も step 未宣言時に dtype から安全側へ導出する二重防御を
持つが、本テストはサーバ側 schema が最初から正しいことを保証する。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import list_plugins


def _slider_fields():
    """全プラグインの (plugin_name, key, field) でスライダーのみを列挙する。"""
    for p in list_plugins():
        for key, field in p.params_schema().items():
            if field.get("type") == "slider":
                yield p.name, key, field


def test_every_slider_declares_step():
    missing = [(n, k) for n, k, f in _slider_fields() if "step" not in f]
    assert not missing, f"step 未宣言のスライダー: {missing}"


def test_int_slider_step_is_integral():
    bad = []
    for n, k, f in _slider_fields():
        if f.get("dtype") == "int":
            step = f.get("step")
            if step is None or float(step) != int(step):
                bad.append((n, k, step))
    assert not bad, f"int スライダーの step が非整数: {bad}"


def test_slider_default_lands_on_step_grid():
    bad = []
    for n, k, f in _slider_fields():
        step = f.get("step")
        if step is None or step <= 0:
            continue
        lo, default = f.get("min"), f.get("default")
        if lo is None or default is None:
            continue
        steps = (default - lo) / step
        if abs(steps - round(steps)) > 1e-6:
            bad.append((n, k, default, lo, step))
    assert not bad, f"default が step グリッドに乗らないスライダー: {bad}"


def test_slider_default_within_bounds():
    bad = []
    for n, k, f in _slider_fields():
        lo, hi, default = f.get("min"), f.get("max"), f.get("default")
        if None in (lo, hi, default):
            continue
        if not (lo <= default <= hi):
            bad.append((n, k, default, lo, hi))
    assert not bad, f"default が [min,max] 外のスライダー: {bad}"
