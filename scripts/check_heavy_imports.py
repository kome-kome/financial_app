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

## 既に読めていたファイルも遮断されうる（#782）

2026-10-02 01:00 は、10/1 まで読めていた `_mosaic_gpu_ext.pyd`（中身不変）が遮断され、対話で
import しても通らなかった（同日 03:15 までに自然に解けた）。SAC の判定は時間とともにどちらへも
動くので、上の「一度 import して評価を通す」は初回ロードにしか効かない。遮断されたのが CPU の
NUTS で使わない jaxlib 拡張（GPU/TPU 専用・#789 の `cpu/_sparse`）なら `jax_import_guard` が
代替し、ここでは `[warn ]` 行として残す（失敗にしない）。それ以外の遮断は従来どおり失敗にする。

## バッチが使う依存だけを確かめる（#789）

2026-10-03 01:00 の月次 M-1 は、SAC が `jaxlib/cpu/_sparse.pyd` を遮断して exit=1 になった。
だが M-1（`macro_risk_return`）も月次本体（factor_premia / macro_dlm / macro_gbdt）も jax・
numpyro・pymc を一切 import しない——使わない部品の遮断で失敗扱いになった誤報である。
そこで `--profile` で確かめる範囲を選ぶ:

- `base`: 基盤（numpy / scipy / pandas / sklearn / statsmodels）だけ。M-1 と月次本体
- `inference`（既定）: base ＋ pymc / pytensor / arviz / jax / numpyro ＋ `jax.devices()`。
  `macro_beta_inference` を回すバッチ（月次 beta・日中枠の beta / bench）

既定を厳しい側に置くのは、指定を忘れたバッチが確認を失わないため。`_sparse` 自体は
`jax_import_guard` がスタブで代替する（`[warn ]` 行で残す）。

## 未導入と import 失敗を区別する

未導入（`ModuleNotFoundError`）は **skip** として報告するだけで失敗にしない——`jax` 系は
`requirements-inference.txt` 側で、本番 Render ランタイムには載らない。一方**導入済みなのに
import できない**のは環境の異常なので失敗にする。この2つを同一視すると、SAC のブロックが
「入っていないだけ」に見えて黙って通る。

実行（必ず -m 形式）:
    python -m scripts.check_heavy_imports                  # inference（全部）
    python -m scripts.check_heavy_imports --profile base   # 基盤だけ
"""
from __future__ import annotations

import argparse
import importlib
import sys
from typing import Optional, Sequence

import jax_import_guard

# (import 名, 何のために要るか)。**pip のパッケージ名ではなく import 名**を書く。
# 本番も使う native 拡張。どのバッチも使う。
BASE_IMPORTS: tuple[tuple[str, str], ...] = (
    ("numpy", "全モデルの土台"),
    ("scipy", "統計・最適化"),
    ("pandas", "パネル整形"),
    ("sklearn", "M-2 / 前処理"),
    ("statsmodels", "OLS / Fama-MacBeth"),
)

# `macro_beta_inference` だけが使う推論系。M-1（macro_risk_return）はこれが作った
# `macro_beta_loadings` を DB から読むだけで、ここは import しない（#789）。
INFERENCE_IMPORTS: tuple[tuple[str, str], ...] = (
    ("pymc", "M-1 macro_beta の階層ベイズ（pytensor / arviz もここで解決される）"),
    ("pytensor", "pymc の計算グラフ"),
    ("arviz", "事後診断（r_hat / ESS）"),
    ("jax", "numpyro NUTS の実行基盤。**SAC がブロックしたのはここが読む jaxlib の DLL**"),
    ("numpyro", "NUTS サンプラ本体"),
)

# プロファイル → 確かめる import。`inference` は `jax.devices()` まで踏み込む。
PROFILES: dict[str, tuple[tuple[str, str], ...]] = {
    "base": BASE_IMPORTS,
    "inference": BASE_IMPORTS + INFERENCE_IMPORTS,
}
DEFAULT_PROFILE = "inference"


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`argv=None` は引数なし扱い（pytest から呼んだとき pytest の sys.argv を読まない）。"""
    parser = argparse.ArgumentParser(description="重い依存が import できるかを確かめる")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE,
                        help="base＝基盤だけ（M-1・月次本体）／inference＝推論系と jax.devices() まで"
                             f"（既定 {DEFAULT_PROFILE}）")
    args = parser.parse_args([] if argv is None else list(argv))

    # 本番の推論（`macro_beta_inference`）と同じ条件で確かめる。ガード無しで測ると、本番では
    # 通る遮断をここだけが失敗として報告する。
    jax_import_guard.install()
    print(f"[profile] {args.profile}")
    failures: list[str] = []
    for name, why in PROFILES[args.profile]:
        state, detail = probe(name)
        print(f"[{state:5s}] {name:12s} {detail}  <- {why}")
        if state == "error":
            failures.append(f"{name}: {detail}")

    warmed = warm_jax() if args.profile == "inference" else None
    if warmed is not None:
        state, detail = warmed
        print(f"[{state:5s}] {'jax.devices':12s} {detail}")
        if state == "error":
            failures.append(f"jax.devices: {detail}")

    # 代替は失敗に数えないが、黙らせもしない。**判定の反転は CodeIntegrity ログ（約4時間で
    # 上書き）に残らず、このログが唯一の時系列になる**（#782）。
    for name in jax_import_guard.substituted():
        print(f"[warn ] {name}  Smart App Control が遮断 → 代替"
              "（CPU の NUTS では使わない部品・#782・#789）")

    if failures:
        print("")
        print("重い依存を import できない。**この先のステップは同じ理由で落ちる**:")
        for line in failures:
            print(f"  - {line}")
        print("")
        print("Windows で 'アプリケーション制御ポリシーによってこのファイルがブロックされました' "
              "と出ている場合は Smart App Control が DLL を弾いている。"
              "未評価の DLL なら対話セッションで一度 import して評価を通す。"
              "既に読めていた DLL の判定が反転した場合は対話でも通らず、時間を置くと戻る"
              "ことがある（#782・docs/GOTCHAS.md）。"
              "Smart App Control は OFF にしない（不可逆）。")
        return 1

    print("")
    print("重い依存はすべて import できた（未評価 DLL の初回ロードはここで消化済み）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
