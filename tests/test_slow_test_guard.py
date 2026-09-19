"""tests/conftest.py の所要上限ガード（#703）の検証。

内側の pytest セッション（pytester）へ conftest のフックだけを持ち込み、上限を ini で
0.05秒まで下げて走らせる。外側のセッションの上限は変えない（変えるとこのテスト自身が
上限を超えて落ちる）。
"""
import pytest

pytest_plugins = ["pytester"]

_HOOKS = (
    "from tests.conftest import (  # noqa: F401\n"
    "    pytest_addoption, pytest_configure, pytest_collection_modifyitems,\n"
    "    pytest_runtest_makereport,\n"
    ")\n"
)


@pytest.fixture
def inner(pytester):
    pytester.makeconftest(_HOOKS)
    pytester.makeini("[pytest]\nslow_test_budget = 0.05\n")
    return pytester


def test_slow_pass_without_marker_becomes_failure(inner):
    inner.makepyfile(test_x="import time\n\ndef test_slow():\n    time.sleep(0.3)\n")
    res = inner.runpytest()
    res.assert_outcomes(failed=1)
    res.stdout.fnmatch_lines(["*上限*#703*"])


def test_slow_with_reason_is_exempt(inner):
    inner.makepyfile(test_x=(
        "import time, pytest\n\n"
        "@pytest.mark.slow(reason='重いことが検証対象')\n"
        "def test_slow():\n    time.sleep(0.3)\n"
    ))
    inner.runpytest().assert_outcomes(passed=1)


def test_fast_test_is_untouched(inner):
    inner.makepyfile(test_x="def test_fast():\n    pass\n")
    inner.runpytest().assert_outcomes(passed=1)


def test_own_failure_is_not_overwritten(inner):
    # 遅くて自分でも落ちたテストは、元の失敗理由のまま見せる（上限の話にすり替えない）。
    inner.makepyfile(test_x="import time\n\ndef test_slow():\n    time.sleep(0.3)\n    assert 1 == 2\n")
    res = inner.runpytest()
    res.assert_outcomes(failed=1)
    res.stdout.fnmatch_lines(["*assert 1 == 2*"])
    res.stdout.no_fnmatch_line("*#703*")


@pytest.mark.parametrize("mark", ["@pytest.mark.slow", "@pytest.mark.slow(reason='  ')"])
def test_slow_marker_without_reason_is_usage_error(inner, mark):
    # 速いテストでも収集時に落とす（遅くなってから素通しになるのを防ぐ）。
    inner.makepyfile(test_x=f"import pytest\n\n{mark}\ndef test_fast():\n    pass\n")
    res = inner.runpytest()
    assert res.ret == pytest.ExitCode.USAGE_ERROR
