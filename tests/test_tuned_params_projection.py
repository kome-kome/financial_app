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
from routers.analysis import panel_changed, project_tuned_params  # noqa: E402


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
        """いま探索している軸（`use_macro` / `max_features`）は保存値のまま残る。

        **射影は「常に既定へ倒す」ではない。** 探索中の軸まで既定へ倒すと、自動調整の
        意味そのものが消える。実物のプラグインでこの側を縛るのはここ
        （かつては M-2 で縛っていたが、#604 で M-2 の2軸も外したため M-1 の探索軸へ移した）。
        """
        stored = {"use_macro": False, "max_features": 30}
        out, changed = project_tuned_params(self._m1(), stored)
        assert out["use_macro"] is False
        assert out["max_features"] == 30
        assert changed == []

    def test_min_coverage_also_drops(self):
        """#596 で外した軸も同じ規則で落ちる（この射影は特定の軸を知らない）。"""
        out, changed = project_tuned_params(self._m1(), {"min_coverage": 0.9})
        assert "min_coverage" not in out
        assert "min_coverage" in changed


class TestPanelFreshness:
    """測ったパネルの照合（#711・ADR-0047）。

    射影は**探索空間の形しか見ない**ので、いま探索中の軸に残った古い値は素通りする。
    実際 M-1 の `max_features=5`（2026-09-02・分割補正前のパネル）は #615 で `use_macro` が
    base へ落ちた後も画面へプリフィルされ続けた——9/20 の実測で rank-IC +0.0052・
    fold 間 std 0（予測値が月内で全銘柄同じ）という最悪の条件だったのに、である。

    **判定は「同じ」と積極的に言えたときだけ False。** 指紋を持たない古い行と、指紋を
    読めなかった回を「一致した」と同じ扱いにすると、判定できない状況が自動適用として
    現れる＝失敗として見えない。
    """

    def test_same_fingerprint_is_not_changed(self):
        assert panel_changed("abc123", "abc123") is False

    def test_different_fingerprint_is_changed(self):
        assert panel_changed("e3e3b334da49e8f4", "abc123") is True

    def test_missing_stored_fingerprint_is_undecidable(self):
        """指紋を持たない世代の行（列が無かった頃・テストの `None` 保存）。"""
        assert panel_changed(None, "abc123") is None

    def test_missing_current_fingerprint_is_undecidable(self):
        """現在の指紋が読めなかった回（DB エラー等）。"""
        assert panel_changed("abc123", None) is None

    def test_both_missing_is_undecidable(self):
        assert panel_changed(None, None) is None

    def test_empty_string_is_undecidable(self):
        """空文字は「指紋がある」に数えない（`!=` で比較すると誤って True になる）。"""
        assert panel_changed("", "abc123") is None
        assert panel_changed("abc123", "") is None


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

    def test_auto_apply_requires_a_positive_panel_match(self):
        """自動適用は `panel_changed === false` のときだけ（#711）。

        **肯定でしか適用しない**のが要点。`!== true` や `!tuned.panel_changed` で書くと、
        判定不能（`null`）が自動適用として通る——指紋が読めなかっただけの回に、測った
        パネルの分からない値が「自動調整済み」の顔で推奨される。
        """
        js = self._js()
        assert "tuned.panel_changed === false" in js
        assert "tuned.panel_changed !== true" not in js

    def test_auto_apply_is_not_unconditional(self):
        """ページ読込時の無条件プリフィルが残っていないこと。

        `_loadTunedBadge` の末尾にあった裸の `applyTunedParams(pluginName);` がこの
        Issue の実害そのものだった。ボタン（`data-click`）からの呼び出しは残るので、
        **行頭の裸呼び出しだけ**を見る。
        """
        js = self._js()
        assert "\n  applyTunedParams(pluginName);" not in js
        assert "if (autoApply) applyTunedParams(pluginName);" in js

    def test_lead_label_does_not_claim_auto_applied_when_it_did_not(self):
        """自動適用しなかったときに「自動調整済み」と名乗らないこと。

        #604 のボタン改名（「初期値にリセット」）と同型の嘘になる——画面の値が調整済み
        でないのに調整済みと書けば、読んだ人は実行前の値を確かめない。
        """
        assert "'🔧 前回の探索結果'" in self._js()


class TestRealM2Space:
    """M-2 も #604 で2軸を外した。保存値（2026-07-19・`use_momentum=True`）が射影されること。

    M-1 と違い M-2 は木モデルで特徴量選択が無く、**外すと μ̂ が変わる**（M-1 は BIC が
    モメンタム列を選ばないのでモデルが変わらなかった）。変わる向きは共通域の実測では
    改善側——全窓が基準以下で、窓24 は −0.0264（p=0.001）で有意に悪化していた。
    """

    def test_stored_momentum_on_becomes_off(self):
        from plugins import get_plugin
        stored = {"use_momentum": True, "momentum_window": 18, "max_depth": 8}
        out, changed = project_tuned_params(get_plugin("macro_gbdt"), stored)
        assert out["use_momentum"] is False
        assert "momentum_window" not in out
        assert out["max_depth"] == 8          # 探索中の軸は保存値のまま
        assert set(changed) == {"use_momentum", "momentum_window"}

    def test_m5_inherits_the_same_space(self):
        """M-5（`macro_gbdt_rank`）は M-2 を継承するので同じ射影が効く。

        #570 で退役済み（hidden=True）だが、継承で同じ穴を持つ以上ここも塞がっている
        ことを確かめる——**波及に気づかないまま片方だけ直す**のがこの種の事故の形。
        """
        from plugins import get_plugin
        p = get_plugin("macro_gbdt_rank")
        if p is None:
            pytest.skip("macro_gbdt_rank は登録されていない")
        base, dims = p.tuning_search_space()
        assert base.get("use_momentum") is False
        assert "use_momentum" not in {d.name for d in dims}
