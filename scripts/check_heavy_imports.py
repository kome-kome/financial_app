"""重い依存が実際に import できるかを最初に確かめる（月次バッチの先頭ステップ）。

## なぜ要るか

2026-09-01、月次バッチの初実走で `macro_beta` が **1.4分・exit=1** で落ちた:

    ImportError: DLL load failed while importing _ifrt_proxy:
    アプリケーション制御ポリシーによってこのファイルがブロックされました。

原因は **Smart App Control**（`VerifiedAndReputablePolicyState=1`＝Enforced）が、8/21 の
jaxlib 更新で入った未評価の `_ifrt_proxy.pyd` を**初回ロードでブロック**したこと。CodeIntegrity
ログ（3118 / 3077 / 3033）はその1回だけを記録しており、以後は同じ DLL の import が通る——
未評価バイナリを1度弾いてから評価を取得する、という一過性の挙動である。

問題は**それが月1回しか走らない本番実行に当たった**こと。月次は次が1か月後なので、初回
ロードの1回きりの失敗が `macro_beta_loadings` の1か月ぶんの固着になる。

## 何を保証するか

「バッチが本気で走り出す前に、重い依存を全部 import しておく」。これで:

- 未評価 DLL の初回ロードは**この軽いステップが引き受ける**（本番ステップの手前で消化する）
- それでも落ちるなら **1分以内に失敗として現れて起票される**。180分の予算を待たない
- 環境の実体（各パッケージの版）がログの先頭に残る

**Smart App Control は切らない**。一度 OFF にすると Windows の再インストール無しには再び ON に
できず、マルウェア防御が恒久に一段下がる。1回きりのブロックに対して不可逆な代償が大きすぎる。

pip でパッケージを更新した後は、**対話セッションで一度 import して評価を通しておく**こと
（`docs/GOTCHAS.md`）。バッチはセッション0（S4U）で走り、そこで初めて触るのが一番まずい。

## 未導入と import 失敗を区別する

未導入（`ModuleNotFoundError`）は **skip** として報告するだけで失敗にしない——`jax` 系は
`requirements-inference.txt` 側で、本番 Render ランタイムには載らない。一方**導入済みなのに
import できない**のは環境の異常なので失敗にする。この2つを同一視すると、SAC のブロックが
「入っていないだけ」に見えて黙って通る。

実行（必ず -m 形式）:
    python -m scripts.check_heavy_imports
"""
from __future__ import annotations

import importlib
import sys

# (import 名, 何のために要るか)。**pip のパッケージ名ではなく import 名**を書く。
# 並びは「本番も使う native 拡張」→「推論バッチ専用」の順。
HEAVY_IMPORTS: tuple[tuple[str, str], ...] = (
    ("numpy", "全モデルの土台"),
    ("scipy", "統計・最適化"),
    ("pandas", "パネル整形"),
    ("sklearn", "M-2 / 前処理"),
    ("statsmodels", "OLS / Fama-MacBeth"),
    ("pymc", "M-1 macro_beta の階層ベイズ（pytensor / arviz もここで解決される）"),
    ("pytensor", "pymc の計算グラフ"),
    ("arviz", "事後診断（r_hat / ESS）"),
    ("jax", "numpyro NUTS の実行基盤。**SAC がブロックしたのはここが読む jaxlib の DLL**"),
    ("numpyro", "NUTS サンプラ本体"),
)


def probe(name: str) -> tuple[str, str]:
    """(状態, 詳細) を返す。状態は "ok" / "skip" / "error"。

    `ModuleNotFoundError` は `ImportError` の**サブクラス**なので先に捕まえる。順序を逆にすると
    「未導入」と「DLL がブロックされた」が同じ枝に落ち、後者が skip に化ける。
    """
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError:
        return "skip", "未導入"
    except BaseException as exc:      # noqa: BLE001 — DLL ブロックは ImportError 以外でも来うる
        return "error", f"{type(exc).__name__}: {str(exc)[:200]}"
    return "ok", str(getattr(module, "__version__", "版不明"))


def warm_jax() -> tuple[str, str] | None:
    """jax のデバイス初期化まで踏み込む（**import だけでは読まれない DLL がある**）。

    `jax.devices()` は XLA バックエンドを実際に立ち上げるので、未評価バイナリのロードを
    より深いところまで先に済ませられる。jax が未導入なら何もしない。
    """
    try:
        import jax
    except ModuleNotFoundError:
        return None
    except BaseException as exc:      # noqa: BLE001
        return "error", f"{type(exc).__name__}: {str(exc)[:200]}"
    try:
        return "ok", f"devices={[str(d) for d in jax.devices()]}"
    except BaseException as exc:      # noqa: BLE001
        return "error", f"{type(exc).__name__}: {str(exc)[:200]}"


def main() -> int:
    failures: list[str] = []
    for name, why in HEAVY_IMPORTS:
        state, detail = probe(name)
        print(f"[{state:5s}] {name:12s} {detail}  <- {why}")
        if state == "error":
            failures.append(f"{name}: {detail}")

    warmed = warm_jax()
    if warmed is not None:
        state, detail = warmed
        print(f"[{state:5s}] {'jax.devices':12s} {detail}")
        if state == "error":
            failures.append(f"jax.devices: {detail}")

    if failures:
        print("")
        print("重い依存を import できない。**この先のステップは同じ理由で落ちる**:")
        for line in failures:
            print(f"  - {line}")
        print("")
        print("Windows で 'アプリケーション制御ポリシーによってこのファイルがブロックされました' "
              "と出ている場合は Smart App Control が未評価の DLL を弾いている。"
              "対話セッションで一度 import して評価を通す（docs/GOTCHAS.md）。"
              "Smart App Control は OFF にしない（不可逆）。")
        return 1

    print("")
    print("重い依存はすべて import できた（未評価 DLL の初回ロードはここで消化済み）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
