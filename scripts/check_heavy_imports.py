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

## 版の照合（`--pins`・#1324）

WSL2 の venv（ADR-0080）は Windows の venv とは別に入れてある。`requirements*.txt` の版を上げても、WSL 側は
古い版のまま import に成功し、M-7 の計測が古い版で走る——**import できるかを見るだけでは、ずれが失敗として
現れない**。`--pins requirements-inference.txt` は、入っている版（`importlib.metadata`）を `==` の固定値と
突き合わせ、ずれ・未導入があれば失敗にする。日中枠の `deps_smoke_wsl` と `check:wsl` が使う（Windows の
`deps_smoke` と月次には付けない＝Windows の venv の運用は変えない）。

- `-r` の入れ子をたどる（書いたファイルからの相対パス）。`==` 以外の行と推移的な依存は見ない
- 名前は PEP 503 で正規化して比べる（`PyYAML` と `pyyaml`、`typing_extensions` と `typing-extensions`）
- 版は文字列の完全一致（固定値はすべて `==`。`1.0` と `1.0.0` を同じとみなす正規化はしない）
- 固定値が 1 件も取れなければ失敗にする（ファイルを取り違えたまま「ずれ 0 件」で通さない）

実行（必ず -m 形式）:
    python -m scripts.check_heavy_imports                  # inference（全部）
    python -m scripts.check_heavy_imports --profile base   # 基盤だけ
    python -m scripts.check_heavy_imports --pins requirements-inference.txt   # 版の照合も
"""
from __future__ import annotations

import argparse
import importlib
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Callable, Optional, Sequence

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

# requirements の 1 行から「名前 == 版」を取る。extras（`name[x]`）と環境マーカー（`; ...`）は外す。
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;]+)")
# `-r other.txt` / `--requirement=other.txt`（入れ子）。
_INCLUDE_RE = re.compile(r"^(?:-r|--requirement)(?:\s+|\s*=\s*)(\S+)$")
# pip と同じく、行頭か空白の後の `#` から行末までをコメントとみなす。
_COMMENT_RE = re.compile(r"(^|\s)#.*$")


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


def normalize_name(name: str) -> str:
    """PEP 503 の正規化（`-`・`_`・`.` の連なりを `-` に、小文字に）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(path, _seen: Optional[set] = None) -> dict[str, tuple[str, str]]:
    """requirements の `==` の固定値を `{正規化した名前: (書かれた名前, 版)}` で返す（#1324）。

    `-r` は書いたファイルからの相対パスで解いてたどる（同じファイルを2回読まない＝循環しても止まる）。
    後に書いた行が勝つ（`requirements-inference.txt` が `-r requirements.txt` の後で版を上書きしても、その版で比べる）。
    `==` 以外の行（範囲指定・`-e`・`--index-url` 等）は照合の対象にしない。
    """
    p = Path(path).resolve()
    seen = set() if _seen is None else _seen
    if p in seen:
        return {}
    seen.add(p)
    pins: dict[str, tuple[str, str]] = {}
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = _COMMENT_RE.sub("", raw).strip()
        if not line:
            continue
        inc = _INCLUDE_RE.match(line)
        if inc:
            pins.update(parse_pins(p.parent / inc.group(1), seen))
            continue
        m = _PIN_RE.match(line)
        if m:
            pins[normalize_name(m.group(1))] = (m.group(1), m.group(2))
    return pins


def installed_version(name: str) -> Optional[str]:
    """入っている版。未導入なら None（`importlib.metadata` は名前の `-`/`_` の違いを吸収する）。"""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def check_pins(pins: dict[str, tuple[str, str]],
               version_of: Optional[Callable[[str], Optional[str]]] = None) -> list[str]:
    """固定値と入っている版が食い違うものを `名前: 固定値 X / 実際 Y` の形で返す（空なら一致）。"""
    version_of = version_of or installed_version
    problems: list[str] = []
    for _, (name, want) in sorted(pins.items()):
        got = version_of(name)
        if got is None:
            problems.append(f"{name}: 固定値 {want} / 未導入")
        elif got != want:
            problems.append(f"{name}: 固定値 {want} / 実際 {got}")
    return problems


def pin_failures(path) -> list[str]:
    """`--pins` の結果を出し、失敗の行を返す。固定値が取れない・ファイルが無いのも失敗にする。"""
    try:
        pins = parse_pins(path)
    except OSError as exc:
        print(f"[error] {'pins':12s} {path}: 読めない（{type(exc).__name__}: {exc}）")
        return [f"pins: {path} を読めない"]
    if not pins:
        print(f"[error] {'pins':12s} {path}: `==` の固定値が 1 件も無い（ファイルの取り違え？）")
        return [f"pins: {path} に固定値が無い"]
    problems = check_pins(pins)
    state = "error" if problems else "ok"
    print(f"[{state:5s}] {'pins':12s} {path}: {len(pins)} 件を照合・ずれ {len(problems)} 件")
    for line in problems:
        print(f"[error]   {line}")
    return [f"pins: {line}" for line in problems]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`argv=None` は引数なし扱い（pytest から呼んだとき pytest の sys.argv を読まない）。"""
    parser = argparse.ArgumentParser(description="重い依存が import できるかを確かめる")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE,
                        help="base＝基盤だけ（M-1・月次本体）／inference＝推論系と jax.devices() まで"
                             f"（既定 {DEFAULT_PROFILE}）")
    parser.add_argument("--pins", metavar="REQUIREMENTS",
                        help="入っている版を requirements の `==` の固定値と照合する（`-r` の入れ子もたどる）。"
                             "ずれ・未導入があれば失敗。WSL の venv の確認（deps_smoke_wsl / check:wsl）が使う（#1324）")
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

    pin_problems = pin_failures(args.pins) if args.pins else []

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
    if pin_problems:
        # import は通っても版が違えば、計測は固定値と別の版で走る（#1324）。SAC の話とは分けて出す。
        print("")
        print("版が固定値とずれている（または未導入）:")
        for line in pin_problems:
            print(f"  - {line}")
        print("WSL の venv なら、リポジトリ直下で "
              f"`~/finapp-venv/bin/python -m pip install -r {args.pins}` で入れ直す"
              "（docs/GOTCHAS.md「ローカルバッチの実行環境」・#1324）。")
    if failures or pin_problems:
        return 1

    print("")
    print("重い依存はすべて import できた（未評価 DLL の初回ロードはここで消化済み）"
          + ("。版も固定値どおり" if args.pins else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
