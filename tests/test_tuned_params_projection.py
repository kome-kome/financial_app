"""保存済み調整値を現在の探索空間へ射影する層のテスト（#604）。

`plugin_tuned_params` は「**そのとき探索した空間**」の記録である。軸を外すと古い値が
残り続けるのに、画面はそれを「🔧 自動調整済み」としてフォームへプリフィルする——
つまり **探索をやめた設定が推奨値として出続ける**。

実際に起きていた形（2026-09-04）:

  - M-1 の `params_schema()` の既定は `use_momentum=False`
  - 探索は `use_momentum=True, momentum_window=18` を選んで保存（2026-09-02）
  - #604 で2軸を探索空間から外したが、保存値はそのまま
  - 画面はページ読込時に保存値をプリフィルし、**ボタンのラベルは「初期値にリセット」**
    ＝押すと ON へ戻る。既定へ戻す手段が画面に無かった

**この食い違いは例外にならない。** 値は schema の bounds を満たすので `coerce_params` も
通る。だから射影の規則そのものをここで縛る。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")

import api  # noqa: E402,F401  （routers を直接 import すると循環するので先に読む）
from plugins.tuning import SearchDim  # noqa: E402
from routers.analysis import project_tuned_params  # noqa: E402


class _Space:
    """`tuning_search_space()` だけを持つ最小のプラグイン代役。"""

    def __init__(self, base, dims):
        self._space = (base, dims)

    def tuning_search_space(self):
        return self._space


class _Broken:
    def tuning_search_space(self):
        raise RuntimeError("探索空間の構築に失敗")


class TestProjection:
    def test_dim_values_are_kept(self):
        """探索中の軸は保存値をそのまま使う（探索が選んだ値そのものだから）。"""
        p = _Space({}, [SearchDim("max_features", [5, 10])])
        out, changed = project_tuned_params(p, {"max_features": 10})
        assert out == {"max_features": 10}
        assert changed == []

    def test_base_params_override_the_stored_value(self):
        """**探索が固定した軸は固定値で上書きする。**

        `base_params` は「その値で測った」という測定条件そのもの。保存値が違うなら
        それは古い空間の遺物なので、現在の条件を優先する。
        """
        p = _Space({"use_momentum": False}, [SearchDim("max_features", [5, 10])])
        out, changed = project_tuned_params(p, {"use_momentum": True, "max_features": 5})
        assert out["use_momentum"] is False
        assert changed == ["use_momentum"]

    def test_base_params_matching_the_stored_value_is_not_flagged(self):
        """同じ値なら「変わった」とは言わない（無意味な警告を出さない）。"""
        p = _Space({"use_momentum": False}, [])
        out, changed = project_tuned_params(p, {"use_momentum": False})
        assert out == {"use_momentum": False}
        assert changed == []

    def test_key_outside_the_space_is_dropped(self):
        """空間から消えた軸は落とす＝`coerce_params` が `params_schema` の既定を補完する。"""
        p = _Space({}, [SearchDim("max_features", [5, 10])])
        out, changed = project_tuned_params(p, {"max_features": 5, "momentum_window": 18})
        assert "momentum_window" not in out
        assert changed == ["momentum_window"]

    def test_base_params_absent_from_stored_are_added(self):
        """保存値に無い固定値も足す（探索条件を完全に再現する）。"""
        p = _Space({"use_momentum": False}, [SearchDim("max_features", [5])])
        out, _changed = project_tuned_params(p, {"max_features": 5})
        assert out["use_momentum"] is False

    def test_changed_keys_are_sorted(self):
        """表示に使うので順序を安定させる（並びが揺れると差分がノイズになる）。"""
        p = _Space({}, [])
        _out, changed = project_tuned_params(p, {"z": 1, "a": 2, "m": 3})
        assert changed == ["a", "m", "z"]

    def test_plugin_without_search_space_passes_through(self):
        """探索空間を持たないプラグインは射影しない（射影の根拠が無い）。"""
        out, changed = project_tuned_params(object(), {"anything": 1})
        assert out == {"anything": 1}
        assert changed == []

    def test_broken_search_space_does_not_kill_the_response(self):
        """探索空間の取得が失敗しても表示は殺さず、生値をそのまま返す。"""
        out, changed = project_tuned_params(_Broken(), {"x": 1})
        assert out == {"x": 1}
        assert changed == []

    def test_empty_params_is_safe(self):
        p = _Space({"use_momentum": False}, [])
        out, changed = project_tuned_params(p, {})
        assert out == {"use_momentum": False}
        assert changed == []

    def test_none_params_is_safe(self):
        p = _Space({}, [SearchDim("max_features", [5])])
        out, changed = project_tuned_params(p, None)
        assert out == {}
        assert changed == []

    def test_projection_does_not_mutate_the_input(self):
        """呼び出し元は生値を `params_as_tuned` として返す。壊すと監査用の値が消える。"""
        p = _Space({"use_momentum": False}, [])
        raw = {"use_momentum": True}
        project_tuned_params(p, raw)
        assert raw == {"use_momentum": True}


class TestRealM1Space:
    """実物の M-1 で、この Issue が起きた組み合わせが正しく射影されること。"""

    @staticmethod
    def _m1():
        from plugins import get_plugin
        return get_plugin("macro_risk_return")

    def test_stored_momentum_on_becomes_off(self):
        """2026-09-02 に保存された `use_momentum=True` が OFF へ射影される。"""
        stored = {"use_macro": True, "use_momentum": True,
                  "momentum_window": 18, "max_features": 5, "min_coverage": 0.3}
        out, changed = project_tuned_params(self._m1(), stored)
        assert out["use_momentum"] is False
        assert "momentum_window" not in out
        assert "use_momentum" in changed and "momentum_window" in changed

    def test_searched_axes_survive(self):
        """いま探索している軸（`use_macro` / `max_features`）は保存値のまま残る。"""
        stored = {"use_macro": False, "max_features": 30}
        out, _changed = project_tuned_params(self._m1(), stored)
        assert out["use_macro"] is False
        assert out["max_features"] == 30

    def test_min_coverage_also_drops(self):
        """#596 で外した軸も同じ規則で落ちる（この射影は特定の軸を知らない）。"""
        out, changed = project_tuned_params(self._m1(), {"min_coverage": 0.9})
        assert "min_coverage" not in out
        assert "min_coverage" in changed


class TestFrontendWiring:
    """画面側の配線（`static/js/analysis.js`）。

    **ラベルが誤解の直接の原因だった。** ボタンは「初期値にリセット」と書いてあるのに
    押すとチューナの選んだ値へ戻る＝「初期値＝製品の既定」と読んだ人は、探索が選んだ
    設定を既定だと理解する。しかも `params_schema` の既定へ戻す手段が画面に無かった。

    JS のテスト基盤は無いので、ここではソースを読んで配線と文言だけを縛る
    （`tests/test_templates_nav.py` が `.gnav` の貼り忘れを照合するのと同じ手）。
    """

    @staticmethod
    def _js() -> str:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "static", "js", "analysis.js"), encoding="utf-8") as f:
            return f.read()

    def test_reset_label_does_not_claim_to_be_the_default(self):
        """「初期値にリセット」を**ボタンの文字として**使わない。

        押すと調整済みの値へ戻るので、この文言は嘘になる。

        判定は `>ラベル<`（要素のテキスト位置）で行う。素の部分一致にすると、
        改名の経緯を説明したコメント本文に当たって落ちる——最初の実装がまさにそれで
        落ちた（#596 で `macro_nan_ok=True` の文字列一致が docstring に誤爆したのと同型）。
        """
        assert ">初期値にリセット<" not in self._js()

    def test_default_button_exists(self):
        """既定値へ戻す導線が画面にあること（無いと調整済みの値から抜けられない）。"""
        js = self._js()
        assert 'data-click="applyDefaultParams"' in js
        assert "function applyDefaultParams(" in js

    def test_tuned_button_still_exists(self):
        """調整済みの値へ戻す導線も残すこと（#294 の用途）。"""
        js = self._js()
        assert 'data-click="applyTunedParams"' in js
        assert "function applyTunedParams(" in js

    def test_stale_params_is_surfaced(self):
        """射影で値を変えたことを画面が出すこと（黙って変えない）。"""
        assert "stale_params" in self._js()


class TestM2KeepsItsAxes:
    """M-2 は2軸を探索し続けているので、射影しても値が変わらないこと。

    「射影＝常に既定へ倒す」ではない。**探索中の軸は触らない**——ここを取り違えると
    自動調整の意味そのものが消える。
    """

    def test_momentum_is_untouched_for_m2(self):
        from plugins import get_plugin
        stored = {"use_momentum": True, "momentum_window": 18}
        out, changed = project_tuned_params(get_plugin("macro_gbdt"), stored)
        assert out["use_momentum"] is True
        assert out["momentum_window"] == 18
        assert changed == []
