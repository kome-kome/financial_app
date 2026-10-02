"""Smart App Control が jaxlib の GPU/TPU 専用拡張を遮断しても、CPU の jax を立ち上げる。

なぜ要るか
----------
2026-10-02 01:00 の月次 `macro_beta` が `deps_smoke` ごと exit=1 で落ちた（#782）:

    ImportError: DLL load failed while importing _mosaic_gpu_ext:
    アプリケーション制御ポリシーによってこのファイルがブロックされました。

`_mosaic_gpu_ext.pyd` は NVIDIA GPU 専用の拡張で、CPU の推論では一度も使わない。それでも
jax は起動時にこれを無条件に import する（`jax/_src/lib/__init__.py`）ので、1ファイルの遮断で
jax・numpyro 全体が立ち上がらなくなる。

9/1 の障害（未評価 DLL を初回ロードで1度だけ弾く）とは型が違う。同じファイル（8/21 導入・中身
不変）が 10/1 01:00 の月次では読めていて、10/2 には対話セッションで import しても遮断された＝
**既に読めていたファイルの判定が持続的に反転した**。逆向きもあり、lightgbm は 9/28 に遮断
されていたのが 10/2 には読めた。SAC の判定は時間とともにどちらへも動くので、「対話で一度
import して評価を通す」では直らない。SAC は OFF にしない（不可逆・docs/GOTCHAS.md）。

何をするか
----------
`GPU_ONLY_EXTENSIONS` に挙げたモジュールの読み込みが**SAC の遮断で**失敗したときだけ、その
モジュールを空のモジュールで代替する。遮断された DLL は一度も実行されない——SAC をすり抜けるの
ではなく、使わない部品を読まないだけである。

- 遮断以外の理由（DLL が無い・依存 DLL が読めない等）の失敗はそのまま送出する
- 一覧に無いモジュールは触らない。CPU で本当に使う部品が遮断されたら、従来どおり失敗する
- 空モジュールの属性を誰かが読めば `AttributeError` になる＝静かに誤った値は出ない
- 代替した事実は `substituted()` で取れる。呼ぶ側がログへ残す（`deps_smoke` の `[warn ]` 行）
- Windows 以外（CI の Linux・Render）では何も挿さない

遮断かどうかは、例外の文面に Windows のエラー 4551 の文言が含まれるかで判定する。`ImportError`
は winerror を持たないので番号では見られない。文言は `ctypes.FormatError` で OS の表示言語の
まま取るので、日本語の文面をここへ書き写さない。

使い方: jax を import する前に `install()` を1回呼ぶ（冪等）。`macro_beta_inference` は
モジュール冒頭で呼ぶので、それを先に import する bench / experiment / grid も覆われる。
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types

# jax が CPU でも起動時に import しうる、GPU / TPU 専用の拡張モジュール。**名前から GPU/TPU
# 専用と分かるものだけ**を置く。5つ全部を空にしても CPU の jax 起動と NUTS が動くことを
# 2026-10-02 に確かめた（jax 0.10.2）。jaxlib を上げたら同じ確認をやり直すこと。
GPU_ONLY_EXTENSIONS: frozenset[str] = frozenset({
    "jaxlib.mlir._mlir_libs._mosaic_gpu_ext",
    "jaxlib.mlir._mlir_libs._mlirDialectsNVGPU",
    "jaxlib.mlir._mlir_libs._mlirDialectsGPU",
    "jaxlib.mlir._mlir_libs._mlirGPUPasses",
    "jaxlib.mlir._mlir_libs._tpu_ext",
})

# ERROR_APP_CONTROL_BLOCKED（「アプリケーション制御ポリシーによってこのファイルがブロックされました」）。
ERROR_APP_CONTROL_BLOCKED = 4551

_substituted: list[str] = []


def block_message() -> str | None:
    """SAC の遮断で LoadLibrary が返す文言。Windows 以外は None（ガードを挿さない）。"""
    if sys.platform != "win32":
        return None
    import ctypes
    return ctypes.FormatError(ERROR_APP_CONTROL_BLOCKED).strip() or None


def substituted() -> tuple[str, ...]:
    """このプロセスで空モジュールに代替したモジュール名（代替した順）。"""
    return tuple(_substituted)


class _Loader(importlib.abc.Loader):
    """本物の loader に読ませ、SAC の遮断で落ちたときだけ空モジュールを返す。"""

    def __init__(self, real: importlib.abc.Loader, message: str) -> None:
        self._real = real
        self._message = message

    def create_module(self, spec):
        try:
            return self._real.create_module(spec)
        except ImportError as exc:
            if self._message not in str(exc):
                raise
        module = types.ModuleType(spec.name)
        module.__substituted_by__ = __name__
        _substituted.append(spec.name)
        return module

    def exec_module(self, module) -> None:
        if getattr(module, "__substituted_by__", None) == __name__:
            return
        self._real.exec_module(module)


class _Finder(importlib.abc.MetaPathFinder):
    """`GPU_ONLY_EXTENSIONS` のときだけ本物の spec を取り、loader を包む。"""

    def __init__(self, message: str) -> None:
        self._message = message

    def find_spec(self, fullname, path, target=None):
        if fullname not in GPU_ONLY_EXTENSIONS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _Loader(spec.loader, self._message)
        return spec


def install() -> None:
    """jax を import する前に呼ぶ。冪等。Windows 以外では何もしない。"""
    message = block_message()
    if message is None:
        return
    if any(isinstance(f, _Finder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _Finder(message))
