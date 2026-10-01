"""jax_import_guard（SAC が jaxlib の GPU/TPU 専用拡張を遮断しても CPU の jax を立ち上げる・#782）。

守る境界は2つ。**遮断のときだけ**代替する（DLL が無い等の別の失敗は従来どおり落とす）ことと、
**一覧のモジュールだけ**を触ることだ。どちらかが緩むと、CPU で本当に使う部品の故障まで
空モジュールで覆い隠し、属性参照の段まで失敗が遅れる。

本物の遮断は OS の判定次第で再現できない（2026-10-02 は 3時間弱で自然に解けた）ので、遮断の
文言と本物の loader を差し替えて、import の仕組みそのものを通して確かめる。
"""
import importlib
import importlib.abc
import importlib.machinery
import sys

import pytest

import jax_import_guard as guard
from scripts import check_heavy_imports

MESSAGE = "アプリケーション制御ポリシーによってこのファイルがブロックされました。"
FAKE = "fake_gpu_ext_782"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """meta_path・代替の記録・偽モジュールを各テストの前の状態へ戻す。

    収集の段で `macro_beta_inference` を import するテストがあると、本物のガードが既に
    挿さっている。各テストはそれを外した状態から始める。
    """
    meta_path = list(sys.meta_path)
    sys.meta_path[:] = [f for f in meta_path if not isinstance(f, guard._Finder)]
    substituted = list(guard._substituted)
    guard._substituted.clear()
    monkeypatch.setattr(guard, "block_message", lambda: MESSAGE)
    monkeypatch.setattr(guard, "GPU_ONLY_EXTENSIONS", frozenset({FAKE}))
    yield
    sys.meta_path[:] = meta_path
    guard._substituted[:] = substituted
    sys.modules.pop(FAKE, None)


class _RealLoader(importlib.abc.Loader):
    """拡張モジュールの loader の代役。遮断は本物と同じく `create_module` で起きる。"""

    def __init__(self, error=None):
        self.error = error
        self.executed = False

    def create_module(self, spec):
        if self.error is not None:
            raise self.error
        return None

    def exec_module(self, module):
        self.executed = True
        module.loaded_for_real = True


@pytest.fixture
def real_loader(monkeypatch):
    """PathFinder が FAKE にだけ偽の spec を返すようにする（他の import は素通し）。"""
    loader = _RealLoader()
    original = importlib.machinery.PathFinder.find_spec

    def find_spec(fullname, path=None, target=None):
        if fullname == FAKE:
            return importlib.machinery.ModuleSpec(FAKE, loader)
        return original(fullname, path, target)

    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", find_spec)
    return loader


def _finders():
    return [f for f in sys.meta_path if isinstance(f, guard._Finder)]


class TestInstall:
    def test_not_windows_installs_nothing(self, monkeypatch):
        monkeypatch.setattr(guard, "block_message", lambda: None)
        guard.install()
        assert _finders() == []

    def test_is_idempotent_and_goes_first(self):
        guard.install()
        guard.install()
        assert len(_finders()) == 1
        assert isinstance(sys.meta_path[0], guard._Finder)

    def test_block_message_is_none_off_windows(self, monkeypatch):
        monkeypatch.undo()                     # 差し替え前の本物を呼ぶ
        if sys.platform == "win32":
            assert guard.block_message()
        else:
            assert guard.block_message() is None


class TestFinder:
    def test_ignores_names_outside_the_list(self):
        assert guard._Finder(MESSAGE).find_spec("json", None) is None

    def test_default_list_is_gpu_or_tpu_only(self, monkeypatch):
        monkeypatch.undo()
        for name in guard.GPU_ONLY_EXTENSIONS:
            assert name.startswith("jaxlib.mlir._mlir_libs.")
            assert any(k in name for k in ("gpu", "GPU", "tpu")), name


class TestImportThroughTheGuard:
    def test_blocked_load_is_substituted(self, real_loader):
        real_loader.error = ImportError(f"DLL load failed while importing {FAKE}: {MESSAGE}")
        guard.install()
        module = importlib.import_module(FAKE)
        assert module.__substituted_by__ == guard.__name__
        assert guard.substituted() == (FAKE,)
        assert real_loader.executed is False   # 遮断された本物は一度も実行しない
        with pytest.raises(AttributeError):    # 使えば静かにではなく例外で分かる
            module.register_dialect  # noqa: B018

    def test_other_import_errors_still_raise(self, real_loader):
        real_loader.error = ImportError(f"DLL load failed while importing {FAKE}: "
                                        "指定されたモジュールが見つかりません。")
        guard.install()
        with pytest.raises(ImportError, match="見つかりません"):
            importlib.import_module(FAKE)
        assert guard.substituted() == ()

    def test_successful_load_is_delegated(self, real_loader):
        guard.install()
        module = importlib.import_module(FAKE)
        assert module.loaded_for_real is True
        assert real_loader.executed is True
        assert guard.substituted() == ()

    def test_without_the_guard_the_block_propagates(self, real_loader):
        real_loader.error = ImportError(MESSAGE)
        with pytest.raises(ImportError):
            importlib.import_module(FAKE)


class TestDepsSmokeReportsSubstitution:
    @pytest.fixture(autouse=True)
    def _all_imports_ok(self, monkeypatch):
        monkeypatch.setattr(guard, "install", lambda: None)
        monkeypatch.setattr(check_heavy_imports, "probe", lambda name: ("ok", "1.0"))
        monkeypatch.setattr(check_heavy_imports, "warm_jax", lambda: None)

    def test_substitution_is_a_warning_not_a_failure(self, monkeypatch, capsys):
        name = "jaxlib.mlir._mlir_libs._mosaic_gpu_ext"
        monkeypatch.setattr(guard, "substituted", lambda: (name,))
        assert check_heavy_imports.main() == 0
        out = capsys.readouterr().out
        assert f"[warn ] {name}" in out

    def test_nothing_substituted_prints_no_warning(self, capsys):
        assert check_heavy_imports.main() == 0
        assert "[warn ]" not in capsys.readouterr().out

    def test_real_failures_still_fail(self, monkeypatch):
        monkeypatch.setattr(check_heavy_imports, "probe",
                            lambda name: ("error", "ImportError: x") if name == "jax" else ("ok", "1.0"))
        assert check_heavy_imports.main() == 1
