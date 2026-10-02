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
import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import jax_import_guard as guard
from scripts import check_heavy_imports

MESSAGE = "アプリケーション制御ポリシーによってこのファイルがブロックされました。"
FAKE = "fake_gpu_ext_782"
FAKE_STUB = "fake_cpu_ext_789"
REPO_ROOT = Path(__file__).resolve().parent.parent
# 結合テストの子が「SAC に遮断された拡張」を名指しする行の印（#792）。ASCII なので文字コードに依らない。
SAC_BLOCKED_MARK = "SAC_BLOCKED"


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
    monkeypatch.setattr(guard, "STUBBED_EXTENSIONS",
                        {FAKE_STUB: {"registrations": guard._no_registrations}})
    yield
    sys.meta_path[:] = meta_path
    guard._substituted[:] = substituted
    sys.modules.pop(FAKE, None)
    sys.modules.pop(FAKE_STUB, None)


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
    """PathFinder が FAKE / FAKE_STUB にだけ偽の spec を返すようにする（他の import は素通し）。"""
    loader = _RealLoader()
    original = importlib.machinery.PathFinder.find_spec

    def find_spec(fullname, path=None, target=None):
        if fullname in (FAKE, FAKE_STUB):
            return importlib.machinery.ModuleSpec(fullname, loader)
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


class TestStubbedExtension:
    """空モジュールでは足りない部品（#789）。import 時に読まれる属性だけを持たせる。

    `jaxlib.cpu._sparse` は numpyro の import が `jax.experimental.sparse` 経由で
    `registrations()` を呼ぶので、空モジュールでは import の段で落ちる。
    """

    def test_blocked_load_gets_only_the_stub_attributes(self, real_loader):
        real_loader.error = ImportError(f"DLL load failed while importing {FAKE_STUB}: {MESSAGE}")
        guard.install()
        module = importlib.import_module(FAKE_STUB)
        assert module.__substituted_by__ == guard.__name__
        assert module.registrations() == {}    # 登録するカーネルが無い＝使えば XLA が止める
        assert guard.substituted() == (FAKE_STUB,)
        assert real_loader.executed is False
        with pytest.raises(AttributeError):    # スタブに無い属性は従来どおり例外
            module.batch_partitionable_targets  # noqa: B018

    def test_other_import_errors_still_raise(self, real_loader):
        real_loader.error = ImportError("DLL load failed: 指定されたモジュールが見つかりません。")
        guard.install()
        with pytest.raises(ImportError, match="見つかりません"):
            importlib.import_module(FAKE_STUB)
        assert guard.substituted() == ()

    def test_successful_load_is_delegated(self, real_loader):
        guard.install()
        module = importlib.import_module(FAKE_STUB)
        assert module.loaded_for_real is True
        assert not hasattr(module, "__substituted_by__")

    def test_default_stubs_are_listed_with_their_reason(self, monkeypatch):
        monkeypatch.undo()
        assert "jaxlib.cpu._sparse" in guard.STUBBED_EXTENSIONS
        # GPU 一覧と重ねない（どちらの扱いか曖昧にしない）。
        assert not set(guard.STUBBED_EXTENSIONS) & guard.GPU_ONLY_EXTENSIONS
        for name, attrs in guard.STUBBED_EXTENSIONS.items():
            assert attrs, f"{name}: 属性が無いなら GPU_ONLY_EXTENSIONS 側（空モジュール）でよい"


_SPARSE_BLOCKED_IMPORT = textwrap.dedent("""
    import importlib.abc
    import importlib.machinery
    import jax_import_guard as guard

    NAME = "jaxlib.cpu._sparse"
    message = guard.block_message()

    class Blocked(importlib.abc.Loader):
        def create_module(self, spec):
            raise ImportError(f"DLL load failed while importing _sparse: {message}")

        def exec_module(self, module):
            raise AssertionError("遮断された本物を実行した")

    original = importlib.machinery.PathFinder.find_spec

    def find_spec(fullname, path=None, target=None):
        spec = original(fullname, path, target)
        if fullname == NAME and spec is not None:
            spec.loader = Blocked()
        return spec

    importlib.machinery.PathFinder.find_spec = find_spec
    guard.install()

    try:
        import jax
        import numpyro  # noqa: F401  (jax.experimental.sparse の冒頭が registrations() を呼ぶ)
        import jaxlib.cpu_sparse
    except ImportError as exc:
        # 本物の LoadLibrary の失敗だけが .pyd のパスを持つ（上の模擬は持たない＝ガードが
        # 壊れて模擬の遮断が漏れても印は付かず、SAC のせいにしない・#792）。
        if exc.path and message and message in str(exc):
            print("__SAC_BLOCKED_MARK__", exc.path)
        raise

    assert guard.substituted() == (NAME,), guard.substituted()
    assert jaxlib.cpu_sparse.registrations() == {"cpu": []}
    print("devices", jax.devices(), float(jax.jit(lambda x: x * 2.0)(1.5)))
""").replace("__SAC_BLOCKED_MARK__", SAC_BLOCKED_MARK)


def _sac_blocked_extension(stdout: str) -> str | None:
    """子が印を付けた「SAC に遮断された拡張」のパス。印が無ければ None。"""
    for line in stdout.splitlines():
        if line.startswith(SAC_BLOCKED_MARK + " "):
            return line[len(SAC_BLOCKED_MARK) + 1:].strip()
    return None


@pytest.mark.skipif(sys.platform != "win32", reason="ガードは Windows でしか挿さない")
def test_real_jax_starts_with_cpu_sparse_blocked():
    """本物の jax / numpyro が `_sparse` の遮断をスタブで越えて立ち上がる（#789）。

    jax を既に読んだ pytest のプロセスでは import の段を再現できないのでサブプロセスで測る。
    jaxlib を上げて import 時に読まれる属性が増えたら、ここが AttributeError で落ちる。

    導入の有無は import せずに確かめる（#792）。`importorskip` は ImportError 全般を skip に
    変えるので、一覧外の部品（10/3 は `_chlo`）が SAC に遮断されたまさにその状況で黙って
    通っていた。しかも `_isolate` がガードを外しているので、このプロセスで import すると
    ガードで救える遮断まで落ちる。遮断の判定はガード付きの子に任せ、一覧外なら失敗させる。
    """
    if not all(importlib.util.find_spec(name) for name in ("jax", "numpyro")):
        pytest.skip("jax / numpyro 未導入")
    result = subprocess.run(
        [sys.executable, "-c", _SPARSE_BLOCKED_IMPORT],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=120,
    )
    blocked = _sac_blocked_extension(result.stdout)
    if blocked:
        pytest.fail(
            f"SAC が {blocked} を遮断している（jax_import_guard の一覧外）。今 jax を使うバッチ"
            "（月次 beta・日中枠の beta / bench）を回すと落ちる。時間を置いて"
            " `python -m scripts.check_heavy_imports` で戻ったかを確かめる（docs/GOTCHAS.md・#792）",
            pytrace=False,
        )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith(" 3.0"), result.stdout[-500:]


class TestSacBlockedMarker:
    """結合テストが一覧外の遮断を名指しする経路（#792）。CI でも走る純粋な部分だけを縛る。"""

    def test_reads_the_blocked_path(self):
        path = r"C:\venv\Lib\site-packages\jaxlib\mlir\_mlir_libs\_chlo.pyd"
        out = f"noise\n{SAC_BLOCKED_MARK} {path}\n"
        assert _sac_blocked_extension(out) == path

    def test_normal_output_has_no_mark(self):
        assert _sac_blocked_extension("devices [CpuDevice(id=0)] 3.0\n") is None

    def test_mark_is_wired_into_the_child(self):
        assert f'print("{SAC_BLOCKED_MARK}", exc.path)' in _SPARSE_BLOCKED_IMPORT
        assert "__SAC_BLOCKED_MARK__" not in _SPARSE_BLOCKED_IMPORT


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
