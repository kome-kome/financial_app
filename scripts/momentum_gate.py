"""モメンタム特徴量（`use_momentum`）を既定 ON にすべきかの OOF 実測（昇格ゲート）。

`use_momentum` は 2026-06-20 に既定 OFF で導入された。理由は2つある:

  (a) **データ制約**: 52週先リターン（未来が必要）と 12ヶ月モメンタム（過去が必要）を同時に
      要求すると、週次株価が約2年分しかない環境では両条件を満たす月が約1ヶ月の薄帯へ収縮し、
      walk-forward CV が 0 fold になった（MODELS.md §9.8-4）。
  (b) **保守ゲート**: `px_*` / `monotone` / `sector` と同じく「既定 OFF で入れ、OOF の ON/OFF
      実測で有効性を示してから既定化」する慣行（ADR-0019）。

(a) は #198 の Yahoo バックフィルで解けている（2026-08-31 実測: `stock_price_weekly` は
2019-07-29〜・1,306,610 行・4,024 社。104週以上の履歴を持つ社が 3,686＝92%）。一方 (b) は
**未消化**で、ON/OFF の実測は一度も取られていない——ADR-0021（M-6 昇格）も ADR-0022（既定
mu_source を M-6 へ）も、実測条件はすべて `use_momentum=False` だった。本スクリプトはその
欠けている実測を埋める。

判定対象は「特徴量セットにモメンタムを**足すか否か**」であってモデルの優劣ではない。よって
同一モデル・同一 fold・同一パネル入力のまま `build_snapshots` へ渡す `use_momentum` だけを
差し替えた2条件を比較する（`scripts/macro_feature_bakeoff.py` と同型。あちらが差し替えるのは
`macro_names`）。

    off … use_momentum=False（現行既定・M-2/M-6 の本番構成）
    on  … use_momentum=True, momentum_window=12（12-1 モメンタム・Jegadeesh-Titman 1993）

**母集団が動く点が macro_feature_bakeoff との決定的な違い**（本スクリプト固有の設計）:
マクロ系列の追加は M-2/M-6 が `macro_nan_ok=True` なので母集団を動かさないが、モメンタムは
`macro_snapshots._build_snapshots_impl` の `if mom is None: continue` が**行ごと落とす**ため、
ON では履歴不足の社・月が母集団から消える。さらに `min_coverage` の充足率が `c/n → (c+1)/(n+1)`
へ変わって ON 側がわずかに緩くなるので、**厳密な部分集合ですらない**。母集団差を交絡させたまま
測ると「モメンタムの効果」と「母集団が変わった効果」が分離できない（#454 の列順交絡と同型）。
そこで:

  1. 両条件のパネル規模（月数 / サンプル数 / 社数 / 特徴量数）を必ず出す
  2. **主判定は共通 (ym, ec) 域へ制限した OOF**（ADR-0015 の base-on-common と同じ発想）
  3. 生母集団のままの値も併記する（本番の運用形に対応するため）

測る手続きは書き直さない（ADR-0041 の教訓＝書き直すと本番と別物を測る）。walk-forward →
`oof_backtest` は `scripts/candidate_bakeoff.run_one`、有意差は `model_stats.
paired_ic_significance`（ADR-0018 の定常ブートストラップ）、(ym,ec) 突合は
`plugins.macro_ensemble._align` をそのまま使う。

昇格ゲート（#372 / ADR-0023 と同じ作法）:
  検定数 = 2モデル（M-2 / M-6） × 2指標（買い側 rank-IC / 売り側 short_side_spread）= 4
  → Bonferroni 補正 α = 0.05 / 4 = 0.0125。共通域の判定でどれか1つでも補正後 α を下回る
  **改善**があれば既定 ON、なければ選択肢のまま（棄却を ADR へ記録する）。
  買い側だけで決めない理由は ADR-0022 の確定知見（買い側 rank-IC の順位と売り側 spread の
  順位は一致しない）。

データは `scripts/_cache.py` 経由のローカル pickle を使う。**モメンタムは過去履歴を読む特徴量
なので、週次株価キャッシュが旧世代だと ON 条件だけが不当に不利になり判定そのものが壊れる**
（#456 と同型。mtime は当てにならない）。起点日・行数・社数を実行時に必ず印字する。

実行例（`-m` 必須・[[feedback_scripts_dir_needs_module_invocation]]）:
    python -m scripts.momentum_gate --smoke                    # 5社に1社へ間引いた経路確認
    python -m scripts.momentum_gate                            # フル実測
    python -m scripts.momentum_gate --models elasticnet        # M-6 だけ
    python -m scripts.momentum_gate --json scripts/.cache/momentum_gate.json

## 窓モード（`--windows`・#592）

上の昇格ゲートは **ON/OFF の2条件**で、窓は 12 に固定している（同時に振ると「窓を選んだこと」
自体が過剰適合になるため）。ところが**その窓選びは `tuning_search_space` の探索へ丸投げされ、
そちらには共通域制限が無い**。実際 M-1 の leaderboard は

    mw=18 → 13 fold → 0.3003 ／ mw=12 → 15 fold → 0.2846 ／ mw=6 → 17 fold → 0.2817
    ／ モメンタム無し → 19 fold → 0.2605

と、**窓が長い＝母集団が縮む＝スコアが高い**が完全に単調で、窓の効果と母集団が縮む効果が
分離できていない（`n_oof_samples` と `n_periods` の Spearman が完全一致）。`--windows` は
その分離を**この昇格ゲートと同じ手続き**（共通月 → 共通 (ym,ec) 域）で行うためのモードである:

    python -m scripts.momentum_gate --models risk_return --windows 3,6,12,18,24 --smoke
    python -m scripts.momentum_gate --models risk_return,xgb_m2 --windows 6,12,18

窓モードは既定の判定に一切影響しない（`--windows` 未指定なら条件も alpha も従来どおり）。
alpha は検定数から導出するので、窓を5本振れば 1/5 に締まる。

**`--smoke` の共通域の数値は読んではいけない**（経路確認専用）。`_thin` は各月の先頭から
stride 刻みで**並び順**に選ぶため、母集団が条件ごとに1社ずれるだけで選ばれる銘柄が総入れ替えに
なる。実測（2026-09-04・M-1）では各条件が互いに 97% 重なっているのに 6条件の交差が **483件**
＝7% しか残らなかった（97% を5回掛ければ 86% のはずで桁が合わない）。stride=1 では
**32,438件＝97.4%** が残り、期待どおりになる。**間引きは条件間の突合と両立しない。**

## マクロ軸モード（`--macro`・#604）

窓モードと同じ懸念が `use_macro` にもあった。M-1 は `build_snapshots` を
`macro_nan_ok=False`（strict）で呼ぶので、構造上は**マクロ特徴量を持つ条件のほうが母集団が
縮みうる**——`macro_risk_return.execute` の `macro_names = list(macro_features) if use_macro
else []` により、OFF ではマクロ特徴量が0個になり「1つでも欠損したら断面を破棄」の条件が
成立しない。**ただし実測では2回とも1行も動かなかった**（実データのマクロ系列は全期間
揃っていて破棄が一度も発火しない・ADR-0050）。構造から母集団効果を推論せず測る、の実例。

    python -m scripts.momentum_gate --macro                       # M-1 のマクロ ON/OFF
    python -m scripts.momentum_gate --macro --models risk_return  # 同上（明示）

**測る対象は M-1 だけ**（`MACRO_MODELS`）。M-2/M-6 は `macro_nan_ok=True` で欠損を nan として
保持するため母集団がほとんど動かず、そもそも `tuning_search_space()` で `use_macro` を
探索していない（既定固定）。M-1 の `use_macro` も **#615 で探索軸から外れ、`base_params` で
False に固定された**（効果を測り終えたため）。**つまりこの軸の ON/OFF を決めるのは、もはや
探索ではなくこのゲートである。** `macro_names_for` が本番の `use_macro` の既定を読まないのは
そのため——読むと既定 OFF の日に ON 側まで空になり、両側が同じものになる。

**基準は「マクロ無し」側**（`MACRO_BASE_COND`）。窓モードで基準をモメンタム無しに置いたのと
同じ理由で、母集団が広い側を分母にする。縮む側を分母にすると母集団効果が「改善」として
符号ごと出る。

**`--windows` と `--macro` は同時に指定できない。** 母集団を動かす軸を2つ同時に振ると、
共通域へ制限してもどちらの効果かが分離できない——それは #592/#604 が指摘している当のもので、
測る側で再現しては意味がない。

**このモードは「マクロを外せ」と言うためのものではない。** M-1 はマクロ×リスク-リターンで、
マクロを外したらモデルの前提そのものが消える（モメンタム2軸のように探索空間から落とす
選択肢が無い）。出すのは母集団を揃えても差が残るかどうかだけである。

## 行の基準モード（`--fin-rows`・#424 子3）

最新業績を「直近12か月（TTM）」の行として学習パネルへ入れるか（`plugins.macro_snapshots.
use_fin_rows`）の昇格ゲート。ADR-0051 決定9 の形そのもので、切替の両側を共通 (ym, ec) 域で比べる:

    annual   … 通期の行だけ（`financial_metrics`・本番の既定）＝基準
    with_ttm … 通期＋TTM 行（`financial_metrics_with_ttm`）

    python -m scripts.momentum_gate --fin-rows                    # M-2 / M-6（4検定・alpha 0.0125）
    python -m scripts.momentum_gate --fin-rows --models risk_return  # M-1 の参考値（別に回す）

**対象は M-2 / M-6**（`FIN_ROWS_MODELS`）。M-1 は strict でパネルを共有できないので、決定9 は
「別に参考値」としている。混ぜると検定数が変わり、昇格の alpha が決定9 と食い違う。

**基準は `annual`**（本番の構成）。**TTM は母集団を動かす**（2026-09-18 実測・M-2 パネル
stride=1: 通期のみ 95,254 → 通期＋TTM 85,533・共通 85,012・うち特徴量が変わる行 23,410）。
TTM 行は成長率などの欠けが通期より多く（前年の TTM が無い年は成長率を作らない）、M-2/M-6 は
財務列が1つでも欠けた行を**通期の行へ戻さず行ごと落とす**（`_build_snapshots_impl`）。だから
主判定は共通域で読み、raw の水準は「縮む側は有利に見える」（ADR-0045）に当たる前提で読む。

**このモードでは財務をキャッシュせずに読む。** `_load_financials` の pickle はキーが形状だけで
世代を持たないので、片方だけが古い世代だと差に「データの鮮度の差」が混ざる。同じ実行で両方を
読めば構造的に揃う（ローカルの正本からなので数秒で読める）。

**黙って同じものを比べる形を2段で止める。** どちらもエラーの出ない壊れ方で、放っておくと
「差なし」という結論だけが残る:

  1. TTM 行が0件（夜間の再構築の失敗など）→ `ttm_row_count` が 0 以下で停止
  2. TTM 行はあるのに断面へ1行も届かない（as-of の選び方の変化など）→ `count_changed_rows` が 0 で停止

判定文は他の軸と同じく中立（`FIN ROWS AXIS`）。**既定を切り替えるかは実測を見て人が決め、
ADR-0051 に記録する**（決定9「既定を切り替えるのはゲートを通ってから」）。

## 目的変数の月平均除去モード（`--demean-target`・#615）

M-1 の目的変数は市場平均を引かない素の52週先対数リターンで、BIC（二乗誤差）は全銘柄に共通する
時系列の変動（相場全体の上げ下げ）を説明するマクロ列を選ぶ。一方で評価は月内の順位である。
**学習で最適化しているものと評価しているものがずれている**という見立て（ADR-0050 の 2026-09-18
追記・未実測）を、目的変数だけを差し替えた2条件で測る:

    raw    … 素の目的変数（本番の構成）＝基準
    demean … 各月の全銘柄の算術平均を引いた目的変数

    python -m scripts.momentum_gate --demean-target               # M-1（2検定・alpha 0.025）

**変換は `_thin` の後・BIC 選択の前に掛ける**（`_build`）。選択と学習の両方に効かせないと、
「選ばれる列が変わるか」を測れない。算術平均にするのは、二乗誤差を「月の間」と「月の中」の成分に
分けたとき前者をちょうど消すのが算術平均だから（見立てそのものを測る形）。

**評価側の y は素へ戻さない。** 判定の3指標（rank-IC＝月ごとの Spearman・long_short＝top−bottom・
short_side＝期内全体平均−bottom）はどれも月の中で完結するので、y を月ごとに一定値ずらしても
値は変わらない（`test_momentum_gate_axes.py` が縛る）。変わるのは `bottom_q_return` /
`quantile_returns` の水準だけで、demean 側では「月平均からの超過」になる。

**この軸は行を1行も落とさない**（母集団は同一）。基準は本番の構成（`raw`）。**変換が断面に
届いたかを CV の前に確かめる**——raw 側の月平均が全部0（変換しても何も変わらない）か、
demean 側の月平均が0になっていなければ停止する。どちらも「差なし」だけが残る壊れ方である。

**このモードも既定を動かさない。** 本番の M-1 は素の目的変数のままで、採るなら ADR-0050 の
2026-09-19 追記に並べた変更（パラメータ契約・CV キャッシュのキー・画面の μ の意味）が要る。

## M-1 を測るときの注意

M-1 は `macro_nan_ok=False`（strict）で**母集団自体が M-2/M-6 と別物**なので、パネルを共有
できない（`MODEL_SPECS` のパネル種別で分ける）。CV の設定（min_train=6 / step=3 / embargo=12）
は M-2 と同値なので `run_one` をそのまま使えるが、**BIC 特徴量選択を挟む点だけが違う**。

出力は ASCII のみ（Windows cp932 リダイレクト対策・[[feedback_windows_cp932_stdout_symbols]]）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from plugins import get_plugin  # noqa: E402
from plugins.macro_ensemble import _align  # noqa: E402
from plugins.macro_snapshots import (  # noqa: E402
    FIN_ROW_SOURCES,
    build_snapshots,
    oof_backtest,
    preload_macro,
    use_fin_rows,
)
from plugins.utils import coerce_params  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402
from scripts.candidate_bakeoff import (  # noqa: E402
    _load_financials,
    _load_prices,
    _thin,
    run_one,
)

_OUT_DIR = Path(__file__).resolve().parent / ".cache"

# 12-1 モメンタムの標準形（Jegadeesh-Titman 1993）。窓の最適化は**既定のゲートでは**対象外＝
# まず「入れるか否か」を clean に判定する（窓も同時に振ると検定数が5倍になり、
# 「窓を選んだこと」自体が過剰適合になりうる）。`--windows` はその過剰適合を
# **わざと再現して測る**ためのモードで、既定の判定には一切影響しない（#592）。
MOM_WINDOW = 12


class Cond(NamedTuple):
    """1条件ぶんの構築パラメータ。

    **タプルではなく名前付きにするのは、軸が3つになったから**（#604 で `use_macro` を足した）。
    位置引数のタプルのままだと `(False, 12)` と `(False, 12, True)` が混在し、
    どちらが何の軸かを読み手が数えることになる。既定 `use_macro=True` は本番構成
    （M-1・M-2 とも `params_schema()` の既定が True）で、モメンタムの判定は
    従来どおりマクロ ON の下で行われる＝ADR-0045 の実測条件が動かない。

    #615 で軸が5つになった。`build_interactions` / `max_features` の既定も本番構成
    （M-1 は交互作用あり・列数はプラグインの `params_schema()` 既定）で、
    **既存3モードの測定条件は1ビットも動かない**。`max_features=None` は
    「プラグインの既定に従う」の意味で、数値を書き写さないための表現。

    `fin_rows`（#424 子3）は学習パネルが読む行の基準（`use_fin_rows`）。既定 `annual` は
    本番の構成なので、既存モードの測定条件はやはり動かない。

    `demean_target`（#615）は目的変数から月ごとの全銘柄平均を引くか。既定 False は本番の
    構成（素の52週先リターン）で、既存モードの測定条件はやはり動かない。
    """
    use_momentum: bool
    momentum_window: int
    use_macro: bool = True
    build_interactions: bool = True
    max_features: int | None = None
    fin_rows: str = "annual"
    demean_target: bool = False


CONDS: dict[str, bool] = {"off": False, "on": True}

# マクロ軸モード（`--macro`・#604）。**基準は「マクロ無し」側**——M-1 は
# `macro_nan_ok=False`（strict）なので、マクロ特徴量を持つ条件のほうが母集団が縮む。
# モメンタムの窓モードで基準を「モメンタム無し」に置いたのと同じ理由で、広い側を分母にする。
MACRO_CONDS: dict[str, bool] = {"nomacro": False, "macro": True}
MACRO_BASE_COND = "nomacro"
# マクロ軸を測る意味があるのは M-1 だけ。M-2/M-6 は `macro_nan_ok=True` で欠損を nan として
# 保持するため `use_macro` が母集団をほとんど動かさず、そもそも `tuning_search_space()` で
# 探索していない（既定固定）。M-1 だけが strict × 探索軸の組み合わせを持つ。
MACRO_MODELS = ["risk_return"]

# 交互作用モード（`--interactions`・#615）。**基準は「交互作用なし」側**——M-1 は strict
# なので、列が増えるほど「1つでも欠損したら断面を破棄」に当たりやすく母集団が縮みうる。
# 広い側を分母にする原則は窓モード・マクロ軸モードと同じ。
#
# なぜこの軸を測るのか: #604 の実測でマクロ特徴量は共通域で rank-IC を **−0.0920**
# 下げているが、`use_macro` は主効果と交差項を**同時に**動かす。M-1 は
# `build_interactions=True` で財務 × マクロの交差項を作るため列数が一気に増える
# （実測: nomacro 4列 に対し macro は max_features=20 の上限いっぱい）。
# 悪化しているのが「マクロ主効果」なのか「交差項が上限を食い尽くしたこと」なのかは、
# この軸を分けないと決まらない。
INTERACTION_CONDS: dict[str, bool] = {"nointer": False, "inter": True}
INTERACTION_BASE_COND = "nointer"
# 交互作用も列数上限も M-1 にしか無い（M-2/M-6 は `build_interactions=False` が本番構成で、
# BIC 選択も持たない）。測る意味があるのは M-1 だけ。
INTERACTION_MODELS = ["risk_return"]

# 列数モード（`--max-features`・#615）。**基準は本番値**（`params_schema()` の既定）。
# 他モードの「母集団が広い側を分母に」という原則がここでは使えない——`max_features` は
# 列を選ぶだけで**母集団を動かさない**ことが ADR-0050 の実測で確定しているため。
# 代わりに「本番から動かすとどうなるか」を見る形にする。
MAXFEAT_MODELS = ["risk_return"]
MAXFEAT_BASE_PREFIX = "mf"

# 行の基準モード（`--fin-rows`・#424 子3・ADR-0051 決定9）。条件名は切替の値そのもの
# （`FIN_ROW_SOURCES` から作り、書き写さない）。**基準は本番の構成（通期のみ）**。
# 対象は M-2 / M-6——決定9 が「M-1 は strict でパネルを共有できないので別に参考値」としている。
FIN_ROWS_CONDS: tuple[str, ...] = FIN_ROW_SOURCES
FIN_ROWS_BASE_COND = "annual"
FIN_ROWS_MODELS = ["xgb_m2", "elasticnet"]

# 目的変数の月平均除去モード（`--demean-target`・#615）。**基準は本番の構成（素の目的変数）**。
# この軸は行を1行も落とさない（母集団は同一）ので、`--max-features` / `--fin-rows` と同じく
# 「本番から動かすとどうなるか」を見る。対象は M-1——見立て（BIC が相場全体の変動を説明する
# マクロ列を選ぶ）は BIC 選択を持つ M-1 のものだから。変換そのものは種別を問わず掛かる。
DEMEAN_CONDS: dict[str, bool] = {"raw": False, "demean": True}
DEMEAN_BASE_COND = "raw"
DEMEAN_MODELS = ["risk_return"]
# 月平均除去が届いたかの許容（`demean_reach_problems`）。素の側は「全部0」だけを弾き、除去した
# 側は浮動小数の丸めを十分に上回る幅で 0 とみなす（どちらの値も実行時に出力と JSON へ出る）。
DEMEAN_RAW_MIN = 1e-12
DEMEAN_TOL = 1e-9

MODELS =["xgb_m2", "elasticnet"]
MODEL_LABELS = {"xgb_m2": "M-2(XGBoost)", "elasticnet": "M-6(ElasticNet)",
                "risk_return": "M-1(RiskReturn)"}
METRICS = (("rank_ic", "rank_ic_by_period"),
           ("short_side_spread", "short_side_spread_by_period"))
# 昇格ゲート: 2モデル × 2指標 = 4 検定を Bonferroni 補正。
N_TESTS = 4

# ── モデル → (config を持つプラグイン, run_one へ渡す推定器, パネル種別) ────────────
#
# **パネル種別が同じモデルだけがパネルを共有できる。** M-2/M-6 は `macro_enet.py:20` が
# 「`walk_forward_cv_monthly(min_train_months=6, step_months=3, embargo_months=12)` は M-2 と同値」
# と宣言しているので1枚を共有してよい。M-1 は `macro_nan_ok=False`（strict）で**母集団自体が
# 別物**なうえ `build_interactions=True`・BIC 特徴量選択が入るため、共有すると本番と違うものを
# 測る。推定器が "ols" なのは M-1 の CV が `fit_predict` を渡さない素の OLS だからで、
# `run_one` の MIN_TRAIN_MONTHS=6 / STEP_MONTHS=3 / embargo=LABEL_HORIZON_MONTHS は
# `macro_risk_return.py` の CV 呼び出しと完全に一致する（手続きを書き写していない）。
MODEL_SPECS: dict[str, tuple[str, str, str]] = {
    "xgb_m2":      ("macro_gbdt",        "xgb_m2",     "gbdt"),
    "elasticnet":  ("macro_gbdt",        "elasticnet", "gbdt"),
    "risk_return": ("macro_risk_return", "ols",        "m1"),
}
BASE_COND = "off"    # 比較の分母。窓モードでも「モメンタム無し」が基準


def maxfeat_cond_name(n: int) -> str:
    """列数モードの条件名（`mf20` 等）。名前の作り方を1箇所に閉じる。"""
    return f"{MAXFEAT_BASE_PREFIX}{n}"


def build_conditions(windows: list[int] | None = None,
                    macro: bool = False,
                    interactions: bool = False,
                    max_features: list[int] | None = None,
                    fin_rows: bool = False,
                    demean_target: bool = False) -> dict[str, Cond]:
    """条件集合 {名前: Cond} を作る。

    7つのモードがある。**同時に使えるのは1つだけ**（下記）:

      既定           … ADR-0045 の昇格ゲートと完全に同じ2条件（`off` / `on`・マクロは ON のまま）
      `windows`      … モメンタム無し ＋ 各窓（#592・ADR-0050）
      `macro`        … マクロ無し ＋ マクロ有り（#604）。モメンタムは既定 OFF に固定
      `interactions` … 交互作用なし ＋ あり（#615）。モメンタム OFF・マクロ ON に固定
      `max_features` … BIC の列数上限を振る（#615）。他の軸は本番構成に固定
      `fin_rows`     … 通期のみ ＋ 通期＋TTM（#424 子3）。他の軸は本番構成に固定
      `demean_target` … 素の目的変数 ＋ 月平均を引いた目的変数（#615）。他の軸は本番構成に固定

    **2つ以上を同時に指定できない。** 母集団を動かしうる軸を2つ同時に振ると、どちらの
    効果かが分離できない——それは共通域制限をかけても解けない（共通域は「全条件で測れる
    銘柄」に揃えるだけで、条件間の差が2軸ぶん混ざっている事実は残る）。この分離不能こそ
    #592/#604 が指摘している当のもので、測る側で再現しては意味がない。

    `max_features` は ADR-0050 の実測で「母集団を動かさない」と確定しているが、**それでも
    他モードと併用させない**——動かさないのは M-1 の現行パネルでの実測であって、
    データが伸びれば変わりうる。併用を許す形にすると、変わった日に静かに交絡する。

    窓・列数はいずれも昇順に並べ、重複は落とす（同じ値を2回測っても検定数だけが増えて
    alpha が不当に厳しくなる）。
    """
    modes = [("--windows", bool(windows)), ("--macro", macro),
             ("--interactions", interactions), ("--max-features", bool(max_features)),
             ("--fin-rows", fin_rows), ("--demean-target", demean_target)]
    picked = [name for name, on in modes if on]
    if len(picked) > 1:
        raise ValueError(
            f"{' と '.join(picked)} は同時に指定できません（母集団を動かしうる軸を2つ同時に"
            "振ると、共通域へ制限してもどちらの効果か分離できない）")
    if macro:
        return {name: Cond(False, MOM_WINDOW, use_macro)
                for name, use_macro in MACRO_CONDS.items()}
    if interactions:
        # モメンタムは OFF・マクロは ON に固定する。マクロを切ると交互作用の相手が
        # 消えて軸そのものが無くなる（`build_interactions=True` でも交差項が作られない）。
        return {name: Cond(False, MOM_WINDOW, True, build_inter)
                for name, build_inter in INTERACTION_CONDS.items()}
    if max_features:
        ns = sorted({int(n) for n in max_features})
        if any(n < 1 for n in ns):
            raise ValueError(f"列数上限は1以上の整数で指定してください: {ns}")
        return {maxfeat_cond_name(n): Cond(False, MOM_WINDOW, True, True, n) for n in ns}
    if fin_rows:
        # 行の基準だけを差し替える。他の軸はすべて本番構成（モメンタム OFF・マクロ ON・
        # 交互作用と列数はプラグインの既定）＝決定9 は「本番の断面に TTM を足すか」を問う。
        return {src: Cond(False, MOM_WINDOW, fin_rows=src) for src in FIN_ROWS_CONDS}
    if demean_target:
        # 目的変数だけを差し替える。他の軸はすべて本番構成（モメンタム OFF・マクロ ON・
        # 交互作用と列数はプラグインの既定・通期のみ）＝「本番の M-1 の学習目標を変えたら」を問う。
        return {name: Cond(False, MOM_WINDOW, demean_target=flag)
                for name, flag in DEMEAN_CONDS.items()}
    if not windows:
        return {name: Cond(use_mom, MOM_WINDOW) for name, use_mom in CONDS.items()}
    ws = sorted({int(w) for w in windows})
    if any(w < 1 for w in ws):
        raise ValueError(f"モメンタム窓は1以上の整数で指定してください: {ws}")
    return {BASE_COND: Cond(False, MOM_WINDOW), **{f"mw{w}": Cond(True, w) for w in ws}}


def base_of(conds: dict[str, Cond], default_max_features: int | None = None) -> str:
    """比較の分母になる条件名を返す。

    **多くのモードでは「母集団が最も広い条件」が分母**になる（モメンタム無し／マクロ無し／
    交互作用無し）。縮む側を分母に置くと、母集団効果が「改善」として符号ごと出てしまう。

    **列数モードだけは原則が使えない**——`max_features` は列を選ぶだけで母集団を動かさない
    （ADR-0050 の実測）。そこで分母は**本番値**（プラグインの `params_schema()` 既定）に置き、
    「本番から動かすとどうなるか」を見る形にする。本番値が条件集合に無いときは最小値へ倒す
    （比較の向きが読み手に伝わればよく、どれを選んでも母集団は同じ）。

    行の基準モードの分母も**本番の構成**（`annual`）。TTM を足したときに本番から何が変わるかを見る。
    目的変数モードの分母も**本番の構成**（`raw`）。この軸も母集団を動かさない。
    """
    for cand in (BASE_COND, MACRO_BASE_COND, INTERACTION_BASE_COND, FIN_ROWS_BASE_COND,
                 DEMEAN_BASE_COND):
        if cand in conds:
            return cand
    prod = maxfeat_cond_name(default_max_features) if default_max_features else None
    if prod and prod in conds:
        return prod
    return sorted(conds, key=lambda k: int(k[len(MAXFEAT_BASE_PREFIX):]))[0]


def bonferroni_alpha(n_models: int, n_conds: int) -> float:
    """検定数から補正後 alpha を導出する（**定数を書き写さない**）。

    検定数 = モデル数 × 指標数 × 基準以外の条件数。既定（2モデル・2条件）では
    2×2×1 = 4 となり `ALPHA` と一致する。窓を5本振れば 2×2×5 = 20 検定になり
    alpha は 1/5 に締まる——**窓を同時に振ると「窓を選んだこと」自体が過剰適合になる**
    という docstring 冒頭の懸念を、判定側でも数として現す。
    """
    n_tests = max(n_models, 1) * len(METRICS) * max(n_conds - 1, 1)
    return 0.05 / n_tests
ALPHA = 0.05 / N_TESTS


# 既定の出力先の接尾辞。同じファイルへ上書きすると、あとから JSON を見たときに
# 「どの軸を測った結果か」が中身を読むまで分からない。窓モードは既定と同じファイルを使う
# （#592 以来の挙動で、ここで変えると過去の結果の置き場所が変わる）。
MODE_SUFFIX: dict[str, str] = {"default": "", "windows": "", "macro": "_macro",
                               "interactions": "_interactions", "max_features": "_maxfeat",
                               "fin_rows": "_fin_rows", "demean_target": "_demean"}


def mode_of(windows: list[int] | None = None, macro: bool = False,
            interactions: bool = False, max_features: list[int] | None = None,
            fin_rows: bool = False, demean_target: bool = False) -> str:
    """測定モードの名前を返す（`build_conditions` と同じ引数から1箇所で導出する）。

    **#615 で足した2モードは、ここを持たないまま別モードの名前で出力されていた**——
    2026-09-15 の `--interactions` 本測定は JSON の `mode` が `"default"`、判定行が
    `REJECT (keep default use_momentum=False)` だった。測ったのは交互作用なのに、
    モメンタムの昇格ゲートの結果に見える。併用の検査は `build_conditions` が持つので、
    ここでは優先順を決めるだけでよい。
    """
    if macro:
        return "macro"
    if interactions:
        return "interactions"
    if max_features:
        return "max_features"
    if fin_rows:
        return "fin_rows"
    if demean_target:
        return "demean_target"
    return "windows" if windows else "default"


# 多条件・軸モードの判定文（見出し, 何も基準を上回らなかったときの文）。
# **どれも「既定をこう変えよ」とは言わない**——ここで出すのは母集団を揃えても差が残るかだけで、
# 既定を動かすかは実測を見てから決める。窓モードは「どの窓を既定にするか」を決める場ではなく
# （それを共通域抜きでやっていたのが #592 の指摘そのもの）、マクロ軸は「マクロを外せ」と言う場
# ではない（M-1 はマクロ×リスク-リターンで、外したらモデルの前提が消える・#604）。
_AXIS_VERDICTS: dict[str, tuple[str, str]] = {
    "macro": ("MACRO AXIS",
              "use_macro did not beat the no-macro baseline"),
    "interactions": ("INTERACTIONS AXIS",
                     "build_interactions did not beat the no-interaction baseline"),
    "max_features": ("MAX_FEATURES SCAN",
                     "no limit beat the baseline limit"),
    "windows": ("WINDOW SCAN",
                "no window beat the no-momentum baseline"),
    "fin_rows": ("FIN ROWS AXIS",
                 "with_ttm did not beat the annual-only baseline"),
    "demean_target": ("DEMEAN TARGET AXIS",
                      "the month-demeaned target did not beat the raw target"),
}


def verdict_text(mode: str, n_conds: int, passed: list[str], regressed: list[str]) -> str:
    """判定行の文言を組み立てる（出力の読み違いをテストで縛るために純関数にしてある）。

    窓モードは**窓が2本以上のときだけ** WINDOW SCAN になる（`--windows 12` は2条件で、
    既定ゲートと同じ PROMOTE/REJECT の文言になる）。これは切り出す前からの挙動で変えない。
    """
    if (mode in ("macro", "interactions", "max_features", "fin_rows", "demean_target")
            or (mode == "windows" and n_conds > 2)):
        head, none = _AXIS_VERDICTS[mode]
        verdict = (f"{head}: effects that survive the common-domain restriction: "
                   + ", ".join(passed)) if passed else (
                   f"{head}: {none} on the common (ym,ec) domain at the corrected alpha")
        if regressed:
            verdict += " | significantly WORSE: " + ", ".join(regressed)
        return verdict
    if passed:
        return "PROMOTE (default use_momentum=True): " + ", ".join(passed)
    if regressed:
        return ("REJECT (keep default use_momentum=False): no improvement passed "
                "corrected alpha; significantly WORSE on " + ", ".join(regressed))
    return "REJECT (keep default use_momentum=False): no metric passed corrected alpha"


def _num(v, nd: int = 4) -> str:
    """None 安全な数値整形（欠測は '-'）。cp932 で落ちる記号は使わない。"""
    if v is None:
        return "-"
    return f"{v:+.{nd}f}" if isinstance(v, float) else str(v)


def _restrict_months(panel: tuple, yms: set) -> tuple:
    """パネル（samples / meta / ids / feats）を指定した月集合へ制限する。

    2条件で fold の位相を揃えるために使う（`walk_forward_cv_monthly` の test 月は月リストの
    先頭からの相対位置で決まるため、開始月が違うと 3ヶ月周期の別位相になる）。
    """
    s, m, i, feats = panel
    return ({ym: v for ym, v in s.items() if ym in yms},
            {ym: v for ym, v in m.items() if ym in yms},
            {ym: v for ym, v in i.items() if ym in yms},
            feats)


# 標準出力へ出す特徴量名の件数（#615）。JSON には全件残すので、ここは「1行に収まる範囲で
# 中身の見当がつく」ことだけを狙う。M-1 の本番は max_features=20 で、全部出すと折り返す。
_FEATURE_PREVIEW = 6


def _panel_stats(samples_by_ym: dict, ids_by_ym: dict, feats: list) -> dict:
    cos = {ec for ids in ids_by_ym.values() for ec in ids}
    return {
        "months": len(samples_by_ym),
        "samples": sum(len(v) for v in samples_by_ym.values()),
        "companies": len(cos),
        "n_features": len(feats),
        "first_ym": min(samples_by_ym) if samples_by_ym else None,
        "last_ym": max(samples_by_ym) if samples_by_ym else None,
    }


def _restrict(resid_by_ym: dict, oof_meta: dict, ids_by_ym: dict, keys: set) -> tuple:
    """residuals / meta を共通 (ym, ec) 集合へ制限して同順で組み直す。

    `build_snapshots(return_stock_ids=True)` と `walk_forward_cv_monthly(return_residuals=True)`
    は samples_by_ym[ym] のサンプル順を保存する（`_align` / `build_oof_meta` が依拠する既存
    契約）ため index で 1:1 突合できる。keys は `_align` が作った集合で NaN 行を含まないので、
    NaN は自動的に落ちる。
    """
    r2: dict[str, list] = {}
    m2: dict[str, list] = {}
    for ym, pairs in resid_by_ym.items():
        ids = ids_by_ym.get(ym, [])
        metas = oof_meta.get(ym, [])
        rr, mm = [], []
        for j, (yh, y) in enumerate(pairs):
            if j >= len(ids) or (ym, ids[j]) not in keys:
                continue
            rr.append((yh, y))
            mm.append(metas[j] if j < len(metas) else (ids[j], None))
        if rr:
            r2[ym] = rr
            m2[ym] = mm
    return r2, m2


def ttm_row_count(fins: dict[str, dict]) -> int | None:
    """「通期＋TTM」側が通期側より何行多いか（＝読めた TTM 行の数）。

    `fins` は {行の基準: fin_by_co}。両方が揃っていなければ None（比べていない）。
    **0 以下は「TTM 行が1件も読めていない」**——夜間の再構築が失敗して表が空でも、VIEW の通期側は
    `financial_metrics` と同じ行を返すので、比較は例外なく走って「差なし」になる（#424 子3）。
    """
    if not all(src in fins for src in FIN_ROWS_CONDS):
        return None
    n = {src: sum(len(rows) for rows in fins[src].values()) for src in FIN_ROWS_CONDS}
    return n["with_ttm"] - n["annual"]


def _same_value(a, b) -> bool:
    """特徴量の値が同じか（nan 同士・None 同士は同じとみなす）。"""
    if a is None or b is None:
        return a is None and b is None
    if a == b:
        return True
    return isinstance(a, float) and isinstance(b, float) and a != a and b != b


def count_changed_rows(panel_a: tuple, panel_b: tuple) -> int:
    """両パネルに共通する (ym, ec) のうち、特徴量の値が1つでも違う行の数。

    パネルは `_build` の返り値 `(samples_by_ym, meta_by_ym, ids_by_ym, feats)`。特徴量は
    **列名で**突き合わせる（M-1 のように条件ごとに選ばれる列が違っても比べられる）。目的変数は
    行の基準では変わらないので見ない。

    **0 は「切替が断面に1行も届いていない」**——TTM 行が表にあっても、as-of の選び方などで
    1行も選ばれなければ両条件は同じパネルになり、「差なし」という結論だけが残る（#424 子3）。
    """
    sa, _ma, ia, fa = panel_a
    sb, _mb, ib, fb = panel_b
    changed = 0
    for ym, pairs_a in sa.items():
        pairs_b = sb.get(ym)
        if not pairs_b:
            continue
        rows_b = {ec: row for ec, (row, _tgt) in zip(ib.get(ym, []), pairs_b)}
        for ec, (row_a, _tgt) in zip(ia.get(ym, []), pairs_a):
            row_b = rows_b.get(ec)
            if row_b is None:
                continue
            va, vb = dict(zip(fa, row_a)), dict(zip(fb, row_b))
            if va.keys() != vb.keys() or not all(_same_value(va[k], vb[k]) for k in va):
                changed += 1
    return changed


def _row(o: dict) -> dict:
    q = o.get("quantile_returns") or []
    return {
        "rank_ic":           o["rank_ic"]["mean"],
        "rank_ic_std":       o["rank_ic"].get("std"),
        "rank_ic_neutral":   o.get("rank_ic_industry_neutral"),
        "short_side_spread": o.get("short_side_spread"),
        "short_side_hit":    o.get("short_side_hit_rate"),
        "bottom_q_return":   q[0] if q else None,
        "long_short_spread": o.get("long_short_spread"),
        "turnover":          o.get("effective_turnover"),
        "breakeven_bps":     o.get("breakeven_cost_bps"),
        "n_periods":         o.get("n_periods"),
    }


def _fmt_sig(sig: dict | None, alpha: float = ALPHA) -> str:
    """有意差の1行表示。**alpha は呼び出し側から渡す**（窓モードで検定数が変わるため）。"""
    if not sig:
        return "n/a (common test periods < 2)"
    p = sig.get("p_value")
    star = "SIG" if (p is not None and p < alpha) else "ns"
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={p if p is not None else float('nan'):.3f} n={sig['n_common']} "
            f"{star}(alpha={alpha:.5f})")


def macro_names_for(kind: str) -> list:
    """パネル種別が `use_macro=True` のときに使うマクロ系列名。

    **本番の `use_macro` の既定は見ない**（#615）。見ると、既定が OFF になった瞬間に
    `--macro` ゲートの `macro` 側まで空になり、**両側が同一条件になって「差なし」だけが
    残る**（ADR-0050 が繰り返し警告している「黙って同じものを比べる形」）。しかも数値は
    もっともらしく出るので、失敗として現れない。

    ON / OFF の切り分けを持つのは呼び出し側の `use_macro` 引数**だけ**である。この関数が
    本番から取るのは**系列の顔ぶれ**（`macro_features`）で、そこは既定が唯一の源のまま
    ——書き写すと本番が系列を増減したときに黙って別物を測る。
    """
    plugin_name = "macro_risk_return" if kind == "m1" else "macro_gbdt"
    params = coerce_params(get_plugin(plugin_name).params_schema(), {})
    return list(params["macro_features"])


def _build(kind: str, args, prices_by_co, fin_by_co, companies, macro_cache,
           use_momentum: bool, mom_window: int, use_macro: bool = True,
           build_interactions: bool = True, max_features: int | None = None,
           demean_target: bool = False) -> tuple:
    """種別の本番 config のまま、条件の軸だけ差し替えて構築する。

    `use_macro=False` はマクロ系列名を空にする（#604）——`macro_risk_return.execute` の
    `macro_names = list(macro_features) if use_macro else []` と同じ形。#615 で**本番の
    既定も OFF になった**ので、条件 `nomacro` のほうが本番構成と一致する。

    構造上は strict（`macro_nan_ok=False`）でこの軸が母集団を動かしうる（マクロ特徴量が
    0個なら「1つでも欠損したら断面を破棄」の条件が成立しない）。**だが実測では2回とも
    1行も動かなかった**——実データのマクロ系列は全期間揃っていて破棄が一度も発火しない
    （ADR-0050）。構造から推論せず測るための軸として、共通域で扱い続ける。

    `build_interactions=False` / `max_features` は #615 の切り分け用の軸。**どちらも M-1
    にしか効かない**——M-2/M-6 は交互作用を持たないのが本番構成で、BIC 選択も無い。
    引数が渡っても `kind != "m1"` なら無視する（種別の本番構成のほうが優先される）。

    **config は各プラグインの `params_schema()` から取り、ここへ書き写さない**（書き写すと
    本番が変わったときに黙って別物を測る）。種別ごとの差は本番コードの差そのもの:

      gbdt … `macro_nan_ok=True` / 交互作用なし（M-2・M-6 が共有）
      m1   … `macro_nan_ok=False`（strict）/ 交互作用あり / BIC 特徴量選択あり。
             `price_features` は渡さない（M-1 に px_* は無い・#446）

    M-1 の BIC 選択は**間引いた後のパネル**に対して行う。CV も同じパネルで回るので、
    「選んだ特徴量」と「評価に使う特徴量」が一致する（本番の順序と同じ）。

    `demean_target`（#615）は**間引いた後・BIC 選択の前**に掛ける。選択と学習の両方に
    効かせないと「目的変数を変えたら選ばれる列が変わるか」を測れない。月平均は学習が
    見る行（間引いた後）で取る。変換は種別を問わず掛かる（M-2/M-6 の目的変数も同じ素の
    リターン）が、既定で測るのは M-1 だけ（`DEMEAN_MODELS`）。
    """
    plugin_name = "macro_risk_return" if kind == "m1" else "macro_gbdt"
    params = coerce_params(get_plugin(plugin_name).params_schema(), {})
    macro_names = macro_names_for(kind) if use_macro else []
    extra = {} if kind == "m1" else {
        "price_features": list(params.get("price_features") or [])}
    samples_by_ym, meta_by_ym, _current, feats, ids_by_ym = build_snapshots(
        prices_by_co, fin_by_co, companies, macro_cache,
        params["fin_features"], macro_names,
        use_momentum, mom_window, params["min_coverage"],
        # M-2/M-6 は交互作用を持たないのが本番構成なので、条件が True でも入れない。
        build_interactions=(kind == "m1" and build_interactions),
        macro_nan_ok=(kind != "m1"),
        return_stock_ids=True,
        **extra,
    )
    s, m, i = _thin(samples_by_ym, meta_by_ym, ids_by_ym, args.stride)
    if demean_target:
        s = demean_target_by_month(s)
    if kind == "m1":
        # 列数上限は条件が指定したものを優先し、無ければプラグインの既定（本番値）。
        s, feats = _select_bic(s, feats, max_features or params["max_features"])
    return s, m, i, feats


def _select_bic(samples_by_ym: dict, feat_names: list, max_features: int) -> tuple:
    """M-1 の LassoLarsIC(BIC) 選択をかけ、選ばれた列だけのパネルへ絞る。

    選択そのものは `macro_risk_return._select_macro_features` を呼ぶ（`macro_snapshots.
    select_features_bic` への薄いラッパ）。**ここで BIC を書き直さない**——書き直すと
    本番の M-1 とは別のモデルを測ることになる（ADR-0041 の教訓）。

    サンプル順は保存する。`_restrict` / `_align` / `build_oof_meta` が
    `samples_by_ym[ym]` と `ids_by_ym[ym]` の index 1:1 対応に依拠しているため、
    ここで順序を崩すと共通 (ym,ec) 域の突合が静かに壊れる。
    """
    selected = get_plugin("macro_risk_return")._select_macro_features(
        samples_by_ym, feat_names, max_features=max_features)
    if not selected:
        raise SystemExit("BIC 選択で特徴量が1つも選ばれませんでした（パネルを確認）")
    idx = [feat_names.index(n) for n in selected]
    sel = {ym: [([row[i] for i in idx], tgt) for row, tgt in pairs]
           for ym, pairs in samples_by_ym.items()}
    return sel, selected


def demean_target_by_month(samples_by_ym: dict) -> dict:
    """各月の目的変数から、その月の全サンプルの算術平均を引いた**新しい**パネルを返す（#615）。

    全銘柄に共通する時系列の変動（相場全体の上げ下げ）を目的変数から消す。二乗誤差を
    「月の間」と「月の中」の成分に分けたとき、前者をちょうど消すのが算術平均である。

    **入力は書き換えない**（呼び出し側が raw の条件と同じオブジェクトを持っていても壊さない）。
    **月内の並び順と特徴量の行はそのまま保つ**——`_restrict` / `_align` / `build_oof_meta` が
    `samples_by_ym[ym]` と `ids_by_ym[ym]` の index 1:1 対応に依拠しているため。空の月は空のまま。
    """
    out: dict = {}
    for ym, pairs in samples_by_ym.items():
        if not pairs:
            out[ym] = []
            continue
        mean = sum(tgt for _row, tgt in pairs) / len(pairs)
        out[ym] = [(row, tgt - mean) for row, tgt in pairs]
    return out


def max_abs_month_mean(samples_by_ym: dict) -> float:
    """月ごとの目的変数の平均の絶対値の最大（空の月は数えない・全部空なら 0.0）。

    `--demean-target` が断面に届いたかを CV の前に確かめるための値。raw 側で 0 なら変換しても
    何も変わらず、demean 側で 0 でなければ変換が掛かっていない——どちらも「差なし」だけが残る。
    """
    means = [abs(sum(tgt for _row, tgt in pairs) / len(pairs))
             for pairs in samples_by_ym.values() if pairs]
    return max(means, default=0.0)


def demean_reach_problems(panel_means: dict[str, float], conds: dict[str, Cond]) -> list[str]:
    """`{"条件|種別": max_abs_month_mean}` から、変換が届いていないパネルを列挙する（#615）。

    空リストなら健全。**両方向を見る**——除去した側の月平均が0でなければ変換が掛かっておらず、
    素の側の月平均が全部0なら変換しても何も変わらない。どちらも両条件が同じパネルになり、
    エラーを出さずに「差なし」だけが残る（`count_changed_rows` は目的変数を見ないので拾えない）。
    """
    problems = []
    for key, v in panel_means.items():
        cond = key.split("|", 1)[0]
        if conds[cond].demean_target:
            if v > DEMEAN_TOL:
                problems.append(f"{key}: 月平均が0になっていない（max|mean|={v:.3e}）")
        elif v <= DEMEAN_RAW_MIN:
            problems.append(f"{key}: 素の目的変数の月平均が全部0（変換しても何も変わらない）")
    return problems


def main() -> None:
    # Windows cp932 では非ASCII記号でクラッシュするため UTF-8 に固定
    # （[[feedback_windows_cp932_stdout_symbols]]）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="モメンタム特徴量（use_momentum）の既定 ON/OFF 昇格ゲート実測")
    ap.add_argument("--models",
                    help=f"測るモデルをカンマ区切りで指定（既定: {','.join(MODELS)}／"
                         f"選べるのは {','.join(MODEL_SPECS)}）")
    ap.add_argument("--windows",
                    help="モメンタム窓をカンマ区切りで指定すると多条件モードになる"
                         "（例: 3,6,12,18,24）。既定は ON/OFF の2条件のまま")
    ap.add_argument("--macro", action="store_true",
                    help="マクロ軸モード（use_macro の ON/OFF を共通域で測る・#604）。"
                         f"モデル既定は {','.join(MACRO_MODELS)}。--windows とは併用不可")
    ap.add_argument("--interactions", action="store_true",
                    help="交互作用モード（build_interactions の ON/OFF を共通域で測る・#615）。"
                         "マクロの悪化が「主効果」なのか「交差項が列数上限を食い尽くしたこと」"
                         f"なのかを分ける。モデル既定は {','.join(INTERACTION_MODELS)}。他モードと併用不可")
    ap.add_argument("--max-features", dest="max_features",
                    help="列数モード（BIC の max_features をカンマ区切りで振る・#615。"
                         "例: 5,10,20,30,40）。分母は本番値。他モードと併用不可")
    ap.add_argument("--fin-rows", dest="fin_rows", action="store_true",
                    help="行の基準モード（通期のみ と 通期＋TTM を共通域で比べる・#424 子3・"
                         f"ADR-0051 決定9）。モデル既定は {','.join(FIN_ROWS_MODELS)}。"
                         "財務はキャッシュせずに読む。他モードと併用不可")
    ap.add_argument("--demean-target", dest="demean_target", action="store_true",
                    help="目的変数モード（素の目的変数 と 月ごとの全銘柄平均を引いた目的変数を"
                         "共通域で比べる・#615）。変換は BIC 選択の前に掛かる。"
                         f"モデル既定は {','.join(DEMEAN_MODELS)}。他モードと併用不可")
    ap.add_argument("--smoke", action="store_true", help="サンプルを間引いた短時間確認")
    ap.add_argument("--stride", type=int, default=1, help="各月のサンプル間引き幅")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に DB からのフルロードを許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--json", dest="json_path", help="結果 JSON の出力先")
    args = ap.parse_args()
    if args.smoke and args.stride <= 1:
        args.stride = 5
    set_refresh(args.refresh_cache)

    # M-1 専用モードの既定モデルは M-1 だけ。M-2/M-6 は `macro_nan_ok=True` で欠損を nan
    # として保持するため `use_macro` が母集団をほとんど動かさず、交互作用も BIC の列数上限も
    # 持たない（本番構成が `build_interactions=False`）＝いずれも測る動機が無い。
    if args.macro:
        default_models = MACRO_MODELS
    elif args.interactions:
        default_models = INTERACTION_MODELS
    elif args.max_features:
        default_models = MAXFEAT_MODELS
    elif args.fin_rows:
        default_models = FIN_ROWS_MODELS
    elif args.demean_target:
        default_models = DEMEAN_MODELS
    else:
        default_models = MODELS
    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if args.models else list(default_models))
    unknown = [m for m in models if m not in MODEL_SPECS]
    if unknown:
        raise SystemExit(
            f"未知のモデル: {', '.join(unknown)}（{', '.join(MODEL_SPECS)} のみ）")
    windows = ([int(w) for w in args.windows.replace(" ", "").split(",") if w]
               if args.windows else None)
    max_features = ([int(n) for n in args.max_features.replace(" ", "").split(",") if n]
                    if args.max_features else None)
    try:
        conds = build_conditions(windows, macro=args.macro, interactions=args.interactions,
                                 max_features=max_features, fin_rows=args.fin_rows,
                                 demean_target=args.demean_target)
    except ValueError as e:
        raise SystemExit(str(e))
    # 列数モードの分母は本番値（プラグイン既定）。**ここで数値を書き写さない**。
    prod_max_features = coerce_params(
        get_plugin(MODEL_SPECS[models[0]][0]).params_schema(), {}).get("max_features")
    base = base_of(conds, prod_max_features)
    # **alpha は検定数から導出する**（定数 ALPHA を窓モードへ流用すると、条件を増やした
    # ぶんの多重比較が補正されないまま「有意」が出る）。
    alpha = bonferroni_alpha(len(models), len(conds))
    n_tests = len(models) * len(METRICS) * max(len(conds) - 1, 1)
    if n_tests != N_TESTS:
        print(f"[warn] 検定数が {n_tests} です（既定のゲートは {N_TESTS}）。"
              f"alpha は {alpha:.5f} へ導出し直しました。ADR-0045 の昇格判定と"
              f"直接は比較できません。", flush=True)
    # 既定の出力先はモードで分ける（`MODE_SUFFIX`）。`mode` フィールドはあるが、
    # ファイル名で取り違えたまま比較するほうが起きやすい。
    mode = mode_of(windows, macro=args.macro, interactions=args.interactions,
                   max_features=max_features, fin_rows=args.fin_rows,
                   demean_target=args.demean_target)
    default_out = f"momentum_gate{MODE_SUFFIX[mode]}.json"
    out_path = Path(args.json_path) if args.json_path else _OUT_DIR / default_out

    db = SessionLocal()
    try:
        prices_by_co = _load_prices(args.allow_full_pull)
        # モメンタムは過去履歴を読む。キャッシュが旧世代だと ON 側だけが不当に不利になり
        # 判定が壊れるため（#456 と同型）、起点・終端・規模をここで必ず現す。
        first = min((r.trade_date for rows in prices_by_co.values() for r in rows[:1]),
                    default=None)
        last = max((rows[-1].trade_date for rows in prices_by_co.values() if rows),
                   default=None)
        n_rows = sum(len(r) for r in prices_by_co.values())
        print(f"weekly px cache: cos={len(prices_by_co)} rows={n_rows} "
              f"range={first}..{last}", flush=True)

        # 財務は**条件が使う行の基準ごと**に読む（#424 子3）。基準は `use_fin_rows` の内側で
        # 決まり、`_load_financials` はそれに従う。行の基準モードでは両方をキャッシュせずに
        # 同じ実行の中で読む（片方の pickle だけが古い世代だと、差に鮮度の差が混ざる）。
        fins: dict[str, dict] = {}
        companies: dict = {}
        for src in [x for x in FIN_ROW_SOURCES if any(c.fin_rows == x for c in conds.values())]:
            with use_fin_rows(src):
                fins[src], companies = _load_financials(db, use_cache=(mode != "fin_rows"))
            n_rows = sum(len(rows) for rows in fins[src].values())
            print(f"panel src[{src}]: fin_cos={len(fins[src])} fin_rows={n_rows} "
                  f"companies={len(companies)}", flush=True)
        ttm_rows = ttm_row_count(fins)
        if mode == "fin_rows":
            if not ttm_rows or ttm_rows <= 0:
                raise SystemExit(
                    f"中止: 通期＋TTM 側に TTM 行が読めていません（差 {ttm_rows} 行）。"
                    "このまま比べると両側が同じデータになり「差なし」だけが残る。"
                    "ttm_financial_records（夜間の rebuild_ttm_financial_records）を確認してください")
            print(f"ttm rows: {ttm_rows}", flush=True)

        # マクロは**必要な種別の和集合**を1度だけ読む（M-1 の44系列は M-2 の53系列の
        # 部分集合だが、それに寄りかからず和集合を取る＝将来どちらかが増えても壊れない）。
        kinds = sorted({MODEL_SPECS[m][2] for m in models})
        macro_names = sorted({n for k in kinds for n in macro_names_for(k)})
        mkey = hashlib.md5(",".join(macro_names).encode()).hexdigest()[:10]
        macro_cache = (cached(f"bakeoff_macro_{mkey}",
                              lambda: preload_macro(db, prices_by_co, macro_names))
                       if macro_names else {})
        db.commit()   # 以降の CPU 計算中に読取トランザクションを残さない（#411）

        # ── 1. 各条件 × 各パネル種別を構築し、母集団の差をそのまま現す ────────────
        #
        # **パネルは (条件, 種別) で持つ。** M-2/M-6 は同じ種別なので1枚を共有し（従来どおり）、
        # M-1 は strict のため別の1枚になる。ここを共有すると M-1 を M-2 の母集団で測る。
        panels: dict[tuple, tuple] = {}
        stats: dict[str, dict] = {}
        for cond, c in conds.items():
            for kind in kinds:
                s, m, i, feats = _build(kind, args, prices_by_co, fins[c.fin_rows], companies,
                                        macro_cache, c.use_momentum, c.momentum_window,
                                        c.use_macro, c.build_interactions, c.max_features,
                                        c.demean_target)
                panels[(cond, kind)] = (s, m, i, feats)
                st = _panel_stats(s, i, feats)
                # **選ばれた列名を残す**（#615）。M-1 は BIC が列を選ぶので、数だけでは
                # 「マクロ無しの4列が何か」「交差項が上限を食い尽くしているか」が分からない。
                # 標準出力は先頭だけ・JSON には全件（判断は生値で行う）。
                st["features"] = list(feats)
                stats[f"{cond}|{kind}"] = st
                head = ", ".join(feats[:_FEATURE_PREVIEW])
                more = f", +{len(feats) - _FEATURE_PREVIEW}" if len(feats) > _FEATURE_PREVIEW else ""
                print(f"[{cond}/{kind}] mw={c.momentum_window if c.use_momentum else '-'} "
                      f"rows={c.fin_rows} "
                      f"macro={'on' if c.use_macro else 'off'} "
                      f"inter={'on' if c.build_interactions else 'off'} "
                      f"maxfeat={c.max_features or '-'} "
                      f"target={'demean' if c.demean_target else 'raw'} "
                      f"months={st['months']} ({st['first_ym']}..{st['last_ym']}) "
                      f"samples={st['samples']} companies={st['companies']} "
                      f"features={st['n_features']} [{head}{more}]", flush=True)

        # 行の基準モードでは、切替が**断面まで届いたか**を確かめてから CV に入る。TTM 行が
        # 表にあっても1行も選ばれなければ両条件は同じパネルで、「差なし」だけが残る。
        changed_rows: dict[str, int] = {}
        if mode == "fin_rows":
            for cond in conds:
                if cond == base:
                    continue
                for kind in kinds:
                    n = count_changed_rows(panels[(base, kind)], panels[(cond, kind)])
                    changed_rows[f"{cond}|{kind}"] = n
                    print(f"[{cond}/{kind}] rows whose features differ from {base}: {n}",
                          flush=True)
            if not all(changed_rows.values()):
                raise SystemExit(
                    f"中止: 行の基準を切り替えても特徴量が1行も変わっていません {changed_rows}。"
                    "TTM 行が as-of の選択で1行も選ばれていない可能性がある")

        # 目的変数モードでも、変換が**断面まで届いたか**を CV の前に確かめる（#615）。
        # 両条件が同じパネルのまま走ると、fin_rows と同じく「差なし」だけが残る。
        target_month_mean: dict[str, float] = {}
        if mode == "demean_target":
            for (cond, kind), (s, _m, _i, _f) in panels.items():
                target_month_mean[f"{cond}|{kind}"] = max_abs_month_mean(s)
                print(f"[{cond}/{kind}] max |monthly mean of target| = "
                      f"{target_month_mean[f'{cond}|{kind}']:.3e}", flush=True)
            problems = demean_reach_problems(target_month_mean, conds)
            if problems:
                raise SystemExit("中止: 目的変数の月平均除去が断面に届いていません: "
                                 + " / ".join(problems))
            print("[note] bottom_q_return / quantile_returns are excess over the monthly "
                  "mean on the demean side; rank_ic / long_short / short_side are "
                  "within-month and unaffected by the shift", flush=True)

        # ── 2. 各条件 × 各モデルを走らせる（残差も受け取る）────────────────────
        results: dict[str, dict] = {}
        parts: dict[str, tuple] = {}
        for cond in conds:
            for model in models:
                estimator, kind = MODEL_SPECS[model][1], MODEL_SPECS[model][2]
                s, m, i, feats = panels[(cond, kind)]
                out = run_one(estimator, s, m, i, feats, pca=0, return_parts=True)
                parts[f"{cond}|{model}"] = out.pop("_parts")
                if out.get("error"):
                    print(f"  {model}: ERROR {out['error']}", flush=True)
                results[f"{cond}|{model}"] = out
                o = out["oof"]
                print(f"  [{cond}] {MODEL_LABELS[model]:<18} "
                      f"rank-IC={_num(o['rank_ic']['mean'])} "
                      f"(std={_num(o['rank_ic'].get('std'))}) "
                      f"short_side={_num(o.get('short_side_spread'))} "
                      f"folds={out['n_folds']} ({out['elapsed_sec']}s)", flush=True)
    finally:
        db.close()

    # ── 3. 共通月で走らせ直し、さらに共通 (ym,ec) 域へ制限する（主判定）────────
    #
    # **月を揃えないと fold の位相がずれて共通域が空になる**（初回実測で実際に踏んだ）。
    # `walk_forward_cv_monthly` は月リストの先頭から min_train_months+embargo_months を空けて
    # step_months=3 刻みで test 月を選ぶため、パネルの開始月が1ヶ月でも違うと test 月が
    # 3ヶ月周期の別位相になり **一度も一致しない**（実測: off=2019-12 起点/69ヶ月 と
    # on=2020-07 起点/62ヶ月 で共通 (ym,ec) が 0 件）。よって共通月へ制限したパネルで
    # 走らせ直す。ここまでが「fold を揃える」段で、そのあと同一 fold の中で銘柄集合を
    # 揃えるのが (ym,ec) 制限の段になる。
    # 共通月は**パネル種別ごと**に取る。M-1（strict）と M-2 の月を交差させると、
    # どちらの比較にも要らない月まで落ちて両方が不当に狭くなる（比較したいのは
    # 「同じモデルの条件間」であって「モデル間」ではない）。
    common_yms = {kind: set.intersection(*[set(panels[(c, kind)][0]) for c in conds])
                  for kind in kinds}
    for kind in kinds:
        ys = common_yms[kind]
        print(f"\n=== common months [{kind}]: {len(ys)} "
              f"({min(ys, default='-')}..{max(ys, default='-')}) ===", flush=True)
    cpanels = {(cond, kind): _restrict_months(panels[(cond, kind)], common_yms[kind])
               for cond in conds for kind in kinds}
    cparts: dict[str, tuple] = {}
    cruns: dict[str, dict] = {}
    for cond in conds:
        for kind in kinds:
            cs, _cm, ci, cfeats = cpanels[(cond, kind)]
            st = _panel_stats(cs, ci, cfeats)
            print(f"[{cond}/{kind}/common-months] months={st['months']} "
                  f"samples={st['samples']} companies={st['companies']} "
                  f"features={st['n_features']}", flush=True)
        for model in models:
            estimator, kind = MODEL_SPECS[model][1], MODEL_SPECS[model][2]
            s, m, i, feats = cpanels[(cond, kind)]
            out = run_one(estimator, s, m, i, feats, pca=0, return_parts=True)
            cparts[f"{cond}|{model}"] = out.pop("_parts")
            cruns[f"{cond}|{model}"] = out
            if out.get("error"):
                print(f"  {model}: ERROR {out['error']}", flush=True)

    print("\n=== common (ym,ec) restriction ===", flush=True)
    common_results: dict[str, dict] = {}
    common_info: dict[str, dict] = {}
    for model in models:
        kind = MODEL_SPECS[model][2]
        aligned = {cond: _align(cparts[f"{cond}|{model}"][0], cpanels[(cond, kind)][2])
                   for cond in conds}
        # **全条件の交差**を取る。2条件のときは従来と同じ off ∩ on。
        keys = set.intersection(*[set(aligned[c]) for c in conds])
        folds = {c: cruns[f"{c}|{model}"]["n_folds"] for c in conds}
        common_info[model] = {
            "n_common": len(keys),
            "n_by_cond": {c: len(aligned[c]) for c in conds},
            "n_folds": folds,
        }
        per_cond = " ".join(f"{c}={len(aligned[c])}" for c in conds)
        print(f"  {MODEL_LABELS[model]:<18} common={len(keys)} "
              f"({per_cond}) folds={folds}", flush=True)
        if len(set(folds.values())) > 1:
            print("    [warn] fold 数が一致していません（位相が揃っていない可能性）", flush=True)
        for cond in conds:
            resid, meta = cparts[f"{cond}|{model}"]
            r2, m2 = _restrict(resid, meta, cpanels[(cond, kind)][2], keys)
            bt = oof_backtest(r2, n_quantiles=5, meta_by_ym=m2, rebalance_per_year=4)
            common_results[f"{cond}|{model}"] = bt
            print(f"    [{cond}] rank-IC={_num(bt['rank_ic']['mean'])} "
                  f"(std={_num(bt['rank_ic'].get('std'))}) "
                  f"short_side={_num(bt.get('short_side_spread'))} "
                  f"periods={bt.get('n_periods')}", flush=True)

    # ── 4. 昇格ゲート判定 ──────────────────────────────────────────────────
    # 検定は common スコープだけで行う。**raw では差を検定できない**——条件ごとに
    # パネルの開始月が違うと `walk_forward_cv_monthly` の test 月が3ヶ月周期の別位相になり、
    # `paired_ic_significance` がペアリングできる共通 test 期が 0 になる（実測で 4検定すべて
    # "common test periods < 2"）。raw は各条件の**水準**としてだけ意味を持つので併記する。
    sigs: dict[str, dict] = {}
    passed: list[str] = []
    regressed: list[str] = []
    test_conds = [c for c in conds if c != base]
    print(f"\n=== cond - {base} / common [PRIMARY] "
          f"(Bonferroni alpha={alpha:.5f}, {n_tests} tests) ===", flush=True)
    for model in models:
        b = common_results[f"{base}|{model}"]
        for cond in test_conds:
            a = common_results[f"{cond}|{model}"]
            for metric, key in METRICS:
                sig = paired_ic_significance(a.get(key) or {}, b.get(key) or {})
                sigs[f"common|{cond}|{model}|{metric}"] = sig
                p = sig.get("p_value") if sig else None
                hit = bool(sig and p is not None and p < alpha)
                label = f"{MODEL_LABELS[model]}/{cond}/{metric}"
                if hit and sig["mean"] > 0:
                    passed.append(label)
                elif hit:
                    regressed.append(label)
                print(f"  {MODEL_LABELS[model]:<18} {cond:<6} {metric:<18} "
                      f"{_fmt_sig(sig, alpha)}", flush=True)

    print("\n=== raw levels (each condition's own population; NOT testable across "
          "conditions: fold phases differ) ===", flush=True)
    print(f"  {'cond':<6} {'model':<18} {'rank-IC':>9} {'IC std':>9} {'short':>9} "
          f"{'LS spread':>10} {'folds':>6} {'samples':>9}", flush=True)
    for cond in conds:
        for model in models:
            r = results[f"{cond}|{model}"]
            o = r["oof"]
            st = stats[f"{cond}|{MODEL_SPECS[model][2]}"]
            print(f"  {cond:<6} {MODEL_LABELS[model]:<18} {_num(o['rank_ic']['mean']):>9} "
                  f"{_num(o['rank_ic'].get('std')):>9} "
                  f"{_num(o.get('short_side_spread')):>9} "
                  f"{_num(o.get('long_short_spread')):>10} {r['n_folds']:>6} "
                  f"{st['samples']:>9}", flush=True)

    verdict = verdict_text(mode, len(conds), passed, regressed)
    print(f"\n=== verdict === {verdict}", flush=True)
    print("判定は common スコープで読む（同一 fold・同一 (ym,ec) 域）。raw は水準のみ。",
          flush=True)

    payload = {
        "momentum_window": MOM_WINDOW,
        # 全軸を出す。#615 の2軸が無いと、列数モードの5条件が同じ中身に見える。
        "conditions": {name: {"use_momentum": c.use_momentum,
                              "window": c.momentum_window,
                              "use_macro": c.use_macro,
                              "build_interactions": c.build_interactions,
                              "max_features": c.max_features,
                              "fin_rows": c.fin_rows,
                              "demean_target": c.demean_target}
                       for name, c in conds.items()},
        "mode": mode,
        "base_cond": base,
        "windows": windows,
        "max_features": max_features,
        # 行の基準モードの健全性（#424 子3）。他モードでは None / 空。
        "ttm_rows": ttm_rows,
        "changed_rows": changed_rows,
        # 目的変数モードの健全性（#615）。{"条件|種別": 月平均の絶対値の最大}。他モードでは空。
        "target_month_mean": target_month_mean,
        "alpha": alpha,
        "n_tests": n_tests,
        "models": models,
        "stride": args.stride,
        "panel": stats,
        "common_months": {kind: {"n": len(ys),
                                 "first": min(ys, default=None),
                                 "last": max(ys, default=None)}
                          for kind, ys in common_yms.items()},
        "common_info": common_info,
        "raw": {k: _row(v["oof"]) for k, v in results.items()},
        "common": {k: _row(v) for k, v in common_results.items()},
        "n_folds": {k: v["n_folds"] for k, v in results.items()},
        "n_folds_common_months": {k: v["n_folds"] for k, v in cruns.items()},
        "significance": sigs,
        "passed": passed,
        "regressed": regressed,
        "verdict": verdict,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
