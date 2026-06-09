"""plugins/utils.py の coerce_params（パラメータ契約の coerce seam）の純粋テスト。

interface = (schema, raw) → typed dict。db / HTTP に依存しないため直接テストできる。
型付け・default 補完・bounds/membership reject・dtype 推論・schema バグ検出を網羅する。
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.utils import coerce_params


# ── dtype 別 coerce ─────────────────────────────────────────────────────────

class TestScalarCoercion:
    def test_int_from_int_float_str(self):
        s = {"x": {"type": "number", "dtype": "int", "default": 0}}
        assert coerce_params(s, {"x": 5})["x"] == 5
        assert coerce_params(s, {"x": 5.0})["x"] == 5      # 整数値の float は許容
        assert coerce_params(s, {"x": "7"})["x"] == 7      # 数値文字列
        assert isinstance(coerce_params(s, {"x": "7"})["x"], int)

    def test_int_rejects_fractional_and_bool_and_garbage(self):
        s = {"x": {"type": "number", "dtype": "int", "default": 0}}
        with pytest.raises(ValueError):
            coerce_params(s, {"x": 5.5})
        with pytest.raises(ValueError):
            coerce_params(s, {"x": True})    # bool は int サブクラスだが弾く
        with pytest.raises(ValueError):
            coerce_params(s, {"x": "abc"})

    def test_float(self):
        s = {"x": {"type": "slider", "dtype": "float", "default": 0.0}}
        assert coerce_params(s, {"x": 1})["x"] == 1.0
        assert isinstance(coerce_params(s, {"x": 1})["x"], float)
        assert coerce_params(s, {"x": "0.5"})["x"] == 0.5
        with pytest.raises(ValueError):
            coerce_params(s, {"x": "abc"})

    def test_str(self):
        s = {"x": {"type": "text", "default": ""}}
        assert coerce_params(s, {"x": "hello"})["x"] == "hello"

    def test_bool(self):
        s = {"x": {"type": "checkbox", "default": False}}
        assert coerce_params(s, {"x": True})["x"] is True
        assert coerce_params(s, {"x": "true"})["x"] is True
        assert coerce_params(s, {"x": "false"})["x"] is False
        assert coerce_params(s, {"x": "on"})["x"] is True
        assert coerce_params(s, {"x": 1})["x"] is True
        assert coerce_params(s, {"x": 0})["x"] is False

    def test_list_from_comma_string_and_list(self):
        s = {"x": {"type": "multiselect", "default": []}}
        assert coerce_params(s, {"x": "a, b ,c"})["x"] == ["a", "b", "c"]
        assert coerce_params(s, {"x": ["a", "b"]})["x"] == ["a", "b"]
        assert coerce_params(s, {"x": [" a ", "", "b"]})["x"] == ["a", "b"]  # 空要素除去

    def test_dict_weights_values_to_float(self):
        s = {"w": {"type": "weights", "default": {}}}
        out = coerce_params(s, {"w": {"z_roe": 2, "z_eps": "1.5"}})["w"]
        assert out == {"z_roe": 2.0, "z_eps": 1.5}
        assert all(isinstance(v, float) for v in out.values())


# ── 欠損・default・optional ───────────────────────────────────────────────────

class TestMissingAndDefault:
    def test_missing_uses_default(self):
        s = {"x": {"type": "number", "dtype": "int", "default": 10}}
        assert coerce_params(s, {})["x"] == 10

    def test_blank_values_use_default(self):
        # None / 空文字 / NaN は欠損扱い（フロントの空数値入力は NaN→null 由来）
        s = {"x": {"type": "number", "dtype": "int", "default": 10}}
        assert coerce_params(s, {"x": None})["x"] == 10
        assert coerce_params(s, {"x": ""})["x"] == 10
        assert coerce_params(s, {"x": float("nan")})["x"] == 10

    def test_optional_without_default_is_none(self):
        s = {"x": {"type": "number", "dtype": "int", "optional": True}}
        assert coerce_params(s, {})["x"] is None
        assert coerce_params(s, {"x": None})["x"] is None

    def test_required_missing_raises(self):
        s = {"x": {"type": "number", "dtype": "int"}}  # default も optional も無い
        with pytest.raises(ValueError):
            coerce_params(s, {})

    def test_default_none_passes_through(self):
        s = {"x": {"type": "number", "dtype": "int", "default": None, "optional": True}}
        assert coerce_params(s, {})["x"] is None


# ── bounds（reject）─────────────────────────────────────────────────────────

class TestBounds:
    def test_within_bounds_ok(self):
        s = {"x": {"type": "number", "dtype": "int", "default": 10, "min": 5, "max": 100}}
        assert coerce_params(s, {"x": 50})["x"] == 50

    def test_below_min_rejected(self):
        s = {"x": {"type": "number", "dtype": "int", "default": 10, "min": 5}}
        with pytest.raises(ValueError):
            coerce_params(s, {"x": 3})

    def test_above_max_rejected(self):
        s = {"x": {"type": "number", "dtype": "int", "default": 10, "max": 100}}
        with pytest.raises(ValueError):
            coerce_params(s, {"x": 500})


# ── membership（reject）──────────────────────────────────────────────────────

class TestMembership:
    def test_select_valid(self):
        s = {"x": {"type": "select", "default": "a",
                   "options": [{"value": "a"}, {"value": "b"}]}}
        assert coerce_params(s, {"x": "b"})["x"] == "b"

    def test_select_invalid_rejected(self):
        s = {"x": {"type": "select", "default": "a",
                   "options": [{"value": "a"}, {"value": "b"}]}}
        with pytest.raises(ValueError):
            coerce_params(s, {"x": "zzz"})

    def test_multiselect_invalid_element_rejected(self):
        s = {"x": {"type": "multiselect", "default": [],
                   "options": [{"value": "a"}, {"value": "b"}]}}
        assert coerce_params(s, {"x": ["a", "b"]})["x"] == ["a", "b"]
        with pytest.raises(ValueError):
            coerce_params(s, {"x": ["a", "zzz"]})


# ── schema バグ検出・未知キー ────────────────────────────────────────────────

class TestSchemaContract:
    def test_numeric_without_dtype_raises(self):
        # number/slider は dtype 明示が必須（int/float 曖昧の排除）
        with pytest.raises(ValueError):
            coerce_params({"x": {"type": "number", "default": 1}}, {"x": 1})
        with pytest.raises(ValueError):
            coerce_params({"x": {"type": "slider", "default": 1}}, {"x": 1})

    def test_unknown_widget_raises(self):
        with pytest.raises(ValueError):
            coerce_params({"x": {"type": "frobnicate", "default": 1}}, {"x": 1})

    def test_unknown_raw_keys_ignored(self):
        s = {"x": {"type": "number", "dtype": "int", "default": 10}}
        out = coerce_params(s, {"x": 5, "unknown": "ignored"})
        assert out == {"x": 5}
        assert "unknown" not in out

    def test_output_has_all_declared_keys(self):
        s = {
            "a": {"type": "select", "default": "x", "options": [{"value": "x"}]},
            "b": {"type": "number", "dtype": "int", "optional": True},
            "c": {"type": "checkbox", "default": True},
        }
        out = coerce_params(s, {})
        assert set(out.keys()) == {"a", "b", "c"}
        assert out == {"a": "x", "b": None, "c": True}
