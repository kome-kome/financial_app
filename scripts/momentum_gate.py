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

**既定で測るのは M-1 だけ**（`MACRO_MODELS`）。M-2/M-6 は `macro_nan_ok=True` で欠損を nan として
保持するため母集団はほとんど動かない見込みで、`tuning_search_space()` でも `use_macro` を
探索していない（既定固定）。**だが母集団が動かないことは成績を測らない理由にならない**（#809）。
M-6 は下の「M-6 を測るとき」の形で別に回す。M-1 の `use_macro` も **#615 で探索軸から外れ、`base_params` で
False に固定された**（効果を測り終えたため）。**つまりこの軸の ON/OFF を決めるのは、もはや
探索ではなくこのゲートである。** `macro_names_for` が本番の `use_macro` の既定を読まないのは
そのため——読むと既定 OFF の日に ON 側まで空になり、両側が同じものになる。

**M-1 の基準は「マクロ無し」側**（`MACRO_BASE_COND`）。窓モードで基準をモメンタム無しに置いたのと
同じ理由で、母集団が広い側を分母にする。縮む側を分母にすると母集団効果が「改善」として
符号ごと出る。#615 で M-1 の本番も「マクロ無し」になったので、分母は本番の構成とも一致する。

### M-6 を測るとき（#809）

    python -m scripts.momentum_gate --macro --models elasticnet

M-6 は交互作用を持たないので、マクロ値（同じ月なら全社で同じ値）は予測に「全社に同じ値を足す
定数」として入るだけで、月内の順位を直接は動かせない——M-1 で #615 が確かめた機構と同じ。
ところが ADR-0021 の昇格も CONTEXT.md の勝因の記述も、マクロ込みの構成でしか測っていない。

**分母は各モデルの本番の構成**（`macro_base_cond`・画面プラグインの `use_macro` 既定から導出）。
M-1 は `nomacro`（上と同じ）、M-6 は **`macro`**。分母が違うモデルは同時に指定すると止まるので、
M-6 は `--models elasticnet` で単独で回す。対比較のブートストラップは向きについて対称なので、
p は分母の選び方に依存しない（変わるのは差の符号と判定文だけ）。出力先は
`momentum_gate_macro_elasticnet.json`（M-1 の結果を上書きしない）。

**M-6 のパネルは M-2 の設定（`macro_gbdt`）で作る**（`MODEL_SPECS`）。系列の顔ぶれ・財務列・
充足率下限・px_* が M-6 自身の既定とずれたら黙って別物を測るので、`panel_config_mismatches` が
読み込みの前に止める。母集団が動かないとは構造から推論せず、共通域の段で条件ごとに落ちた行数を出す。

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

## リスク軸モード（`--risk-axis`・#709）

ここまでの6モードは**すべて μ̂ の順位だけを測ってきた**。ところが M-1 が画面へ出すのは
`U = μ − λR` の並びで、R は測定の外にあった。#615 でマクロ特徴量は「どの入れ方をしても月内の
順位を改善しない」と確定したが、その機構（マクロ系列は月末の週の値なので同じ月の全銘柄でほぼ
同一＝月内の順位を直接は動かせない）は**リスク軸には当たらない**——`r_macro = √(βᵀΣ_macroβ)`
の β は per-stock で、同じ月でも銘柄ごとに値が変わる。マクロが効く余地は μ 側ではなく R 側に
残っている、というのがこのモードの問い。

    python -m scripts.momentum_gate --risk-axis --risk-scale raw          # M-1 の3条件（日中枠 gate:risk-axis）
    python -m scripts.momentum_gate --risk-axis --risk-scale raw --smoke  # 経路確認（数値は読まない）

    mu_only … リスクを引かない（ここまでの6モードが測ってきたもの）
    r2      … U = μ − λ·R2（実現ボラ）。**M-1 の画面の既定＝比較の分母**
    r_macro … U = μ − λ·R_macro（√(βᵀΣ_macroβ)）

**R はパネルの構築に入らない。** 学習には一切関わらず、CV が出した μ̂ の順位を作り直すときに
だけ使う。よって3条件は `Cond` が同一で、パネルと CV を1回に畳む（`built` / `ran` / `cran`）
——**μ̂ をビット単位で一致させるため**で、速さは副産物。別々に回すと一致する保証が無い。

**パネルの `use_macro` は画面の既定から取る**（`production_use_macro`・#815）。M-1 は #615 で
OFF になったが、9/21〜10/07 は `Cond` の既定（ON）のままで、画面とは別のマクロ込みの M-1
（BIC が選ぶ列は画面の3列に対し20列）を測っていた（ADR-0050 の 2026-10-07 追記）。

**R の尺度は `--risk-scale` で選ぶ。** 既定の `z` は R を月内で z-score 標準化してから λ を掛ける
（9/21 の決定3）。`r2`（年率ボラ）と `r_macro`（√(βᵀΣβ)）はスケールが違うので、同じ強さで引いたときの
軸の質を比べるにはこちらが要る。ただし z の λ=1.0 は画面の λ≈1/sd(R)（M-1 の今買える社で R2 4.6・
R_macro 7.9）に当たり、**画面の並びを測らない**。日中枠のリスク軸ジョブは M-1 も含めて全て `raw`
（#815・ADR-0050 の 2026-10-07 追記）——画面と同じ生の R に λ を掛け、結果をスライダーの値へそのまま
結び付ける。`--lambdas` を付けなければ λ は画面の既定から導出され、条件名は上の3つのまま。raw の
`r_macro` 対 `r2` は「同じ強さで引いた軸の質の差」ではなく「画面の横軸を切り替えたときの並びの差」になる。

**`r_macro` は時点再現できない（既知の限界）。** `macro_beta_loadings` に時点列が無く、run は
2026-07 以降の数本しかないので、評価する全ての月に「その月より後のデータで推定した β」を
当てることになる。#709 はこれを**未来情報を与えた場合の上限**として測ると決めた——ここで
負ければ「採用しない」と結論でき、勝っても既定を変える根拠にはならない。副作用として
`r_macro` は銘柄ごとの定数になるため、この軸は「マクロか実現ボラか」と「静的か動的か」を
同時に動かす。分離には4つ目の条件（`r2` を銘柄ごとの定数に固定したもの）が要る。

**R が全軸で取れる行へ揃えるのは共通域を取る段**（`risk_common_keys`）。変換の後で落とすと
`r_macro` を持たない銘柄がその条件からだけ消え、母集団が条件間でずれる（ADR-0045）。

素のリターンとの順位相関だけでは**リスクを下げた効果が罰として出る**ので、U の分位ごとの
月次リターンの平均・ボラ・シャープを併記する（`quantile_risk_profile`）。**判定の主指標は
他モードと同じ `rank_ic`** で、リスク調整後は参考値。

### M-6 を測るとき（#808）

    python -m scripts.momentum_gate --risk-axis --models elasticnet --lambdas 0.1,0.3,1.0 --risk-scale raw

    mu_only     … μ̂ だけの並び。**M-6 の本番（λ=0）＝分母**
    r2@0.1 …    … U = μ̂ − λ·R2（生の実現ボラ）。λ は画面のスライダーと同じ値
    r_macro@0.1 … U = μ̂ − λ·R_macro

**λ と分母は画面プラグインの既定から取る**（`RISK_LAMBDA_PLUGIN`）。`MODEL_SPECS` の設定元から
取っていた間は、M-6 を M-2 の既定 λ=1.0 で黙って測る形だった。M-6 の既定 λ は 0 なので
`--lambdas` 無しでは止まる（λ=0 の並びは `mu_only` と同一で、差が構造的にゼロになる）。
分母は M-1 が `r2`・M-6 が `mu_only` と違うので、**両方を同時に指定すると止まる**。

**`--risk-scale raw` は画面と同じ生の R に λ を掛ける。** z-score の λ=1.0 は画面の λ≈1/sd(R)
（M-6 の R2 で約 4.7）に当たり、結果をスライダーの既定に結び付けられない。代わりに r2 と
r_macro を同じ λ で比べる意味は無くなる（どちらも分母と比べるだけ）。M-1 の事前登録（9/21 の
z-score）も、本番の解像度で走る前に #815 で raw へ置き換えた（条件・分母・検定数はそのまま）。

### M-2 を測るとき（#814）

    python -m scripts.momentum_gate --risk-axis --models xgb_m2 --lambdas 0.1,0.3,1.0 --risk-scale raw

M-2 の画面の既定は λ=1.0 × R_macro なので、`risk_base_cond` が分母を **`r_macro@1.0`** と導出する
（M-1 の `r2`・M-6 の `mu_only` のどちらとも違う＝単独で回す）。μ̂ は M-6 と同じく fold ごとの
OOF 予測で、画面の `mu_raw`（全データで学び直した最終モデル）とは単位が同じで散らばりの保証は無い
（ADR-0050 の 2026-10-05・その2 追記）。

### 分母を上書きするとき（#816）

    python -m scripts.momentum_gate --risk-axis --models xgb_m2 --lambdas 1.0 --risk-scale raw --risk-base mu_only

分母は既定では画面の既定の並びだが、「既定に勝った候補が μ̂ だけの並びに負けないか」のように
**既定ではない並びを基準に測りたい**ときは `--risk-base <条件名>` で上書きする（`resolve_risk_base`）。
条件に無い名前は止まる。判定行は「production」ではなく「base ordering」と書き、画面の既定の並びを
併記する。出力先には `_base-<分母>` が付き、既定を分母にした run の JSON を上書きしない。

## 財務の空欄モード（`--fin-nan`・#938/#916）

M-2 / M-6 で、財務特徴量が空欄の行を捨てずに NaN で学習へ通すか（`build_snapshots(fin_nan_ok=)`）を測る。
本番の既定の財務特徴量は `FIN_BASE_OPTIONS` の 25 列すべてで、どれかが空欄の行は丸ごと外れる。2026-10-13 の
実測で通期の 65% がそうで、主因は #900 の IFRS・US-GAAP の営業利益ではなく JGAAP の `rd_intensity` の空欄だった:

    drop … 財務が1つでも空欄の行を捨てる（本番の構成）＝基準
    nan  … 空欄を NaN で通す（M-2 は XGBoost が分岐で扱い、M-6 は fold 内の列平均で補完する）

    python -m scripts.momentum_gate --fin-nan                     # M-2 / M-6（4検定・alpha 0.0125）
    python -m scripts.momentum_gate --fin-nan --models xgb_m2     # M-2 だけ（2検定・alpha 0.025）

**共通域で測れるものが限られる。** `drop` 側で残る行は `nan` 側でも同じ値で残るので、`drop` 側の
パネルは `nan` 側に含まれ、共通域はほぼ `drop` 側の域になる。ここで読めるのは「空欄のある行を学習に
足すと、財務が揃った社の予測がどう変わるか」だけで、**空欄のある社にも予測が出る、という本来の狙いは
共通域の外の水準（raw）と、会計基準ごとの rank-IC でしか読めない**。判定行の見出しにもこれを書く。

そのため最後に**群ごとの内訳**を出す（#916・`blank_group_breakdown`）。各条件の OOF（自分の母集団）を
「どの列が空欄か」（`FIN_BLANK_GROUPS`）と会計基準で群に分け、群の中の rank-IC と、全体の順位の中で群を
実際より上に置いているか（placement bias）を並べる。**群は検定しない**（条件をまたいだ検定はできず、
採否は人が水準を読んで決める）。会計基準は `financial_records` から (edinet_code, period_end) で引く。

内訳は群ごとの平均しか残さないので、**行ごとの OOF を JSON の隣の `<出力名>_oof.csv` に残す**（#951・
既定は `momentum_gate_fin_nan_oof.csv`）。列は `cond, model, ym, edinet_code, yhat, y, standard,
blank_groups`（`OOF_CSV_COLUMNS`）で、`standard` は会計基準、`blank_groups` は内訳の群のラベルを `;` で
つないだもの。内訳と同じ OOF（各条件の自分の母集団）で、共通月で走らせ直した判定用の OOF ではない。
この CSV だけから内訳を計算し直せるので、規模や業種を揃えた読み直しは DB との突き合わせで済む。

**M-1 には効かせない**（交差項の OLS に NaN が入ると壊れる）。M-2 と M-6 は同じ gbdt のパネルを
共有するので、本番で「M-2 だけ」に効かせると共有が壊れる（`macro_ensemble._BUILD_KEYS`）——採るなら
共有の扱いも #916 で決める。CV の前に `nan` 側のサンプル数が `drop` 側より多いことを確かめ、
増えていなければ止める（`fin_nan_reach_problems`）。日中キューの名前は `gate:fin-nan`。

## D/E の業種除外モード（`--de-exclude`・#940/#927）

証券業・その他金融業（リース・信販など）は借入が事業の資金そのもので、#926 で D/E の分子を有利子負債へ
広げると D/E が大きく出る。この 2 業種を M-2 / M-6 の D/E の比較から外すかを測る:

    keep    … 2業種の D/E を残す（本番の構成）＝基準
    exclude … 2業種の D/E を空欄にする（業種名の正本は `database.DE_RATIO_EXCLUSION_CANDIDATES`）

    python -m scripts.momentum_gate --de-exclude                  # M-2 / M-6（4検定・alpha 0.0125）

**VIEW は変えず、読み込んだ財務の行だけを差し替える**（`null_de_ratio`）。M-2 / M-6 は生の `de_ratio` だけを
使い、他の会社の値に波及しないので、本番の VIEW の業種の条件を広げたときと同じになる。factor premia は
`z_de_ratio` を年度ごとに標準化し直す必要があるので、別の道具（#941）で測る。

**母集団が縮む。** M-2 / M-6 の既定の財務特徴量は `de_ratio` を含むので、空欄にすると財務の空欄の行を落とす
本番の規則でその社が丸ごと外れる。主判定は共通域で読み、raw は「縮む側は有利に見える」（ADR-0045）に当たる
前提で読む。#916 で財務の空欄を NaN で通す（`fin_nan_ok`）と決めた後は、社は落ちず NaN として残る＝結果が
変わるので、どちらの規則の下で測ったかを記録に残す。CV の前に、空欄にした行が0より多く、`exclude` 側の
サンプル数が `keep` 側より少ないことを確かめる（`de_exclude_reach_problems`）。日中キューの名前は
`gate:de-exclude`。**#926 のマージ後に測る**（新しい定義の D/E でないと測る意味がない）。

## 空欄を 0 で埋めるモード（`--fin-fill`・#950）

財務の空欄のうち、書類の実物で「無い」と確かめたものだけを 0 で埋める（`fill`）か、本番どおり空欄の行を
捨てる（`drop`・分母）かを比べる。無配の社は 1 株配当を「−」で載せ、R&D の無い社は「研究開発活動 該当事項は
ありません」と書く（2026-10-15 に 8 社ずつ確認・#950 のコメント）。埋めるのは `rd_intensity`（`pl_rd_expenses` が
空欄で売上 > 0）と `div_yield`（`dps` が空欄か 0 で株価 > 0）だけで、**株価が空欄の行は埋めない**（赤字・無配と
関係の無い欠けで、0 にすると別の意味になる）。差し替えは読み込んだ財務の行の写しだけで、VIEW は変えない
（`fill_fin_blanks`・判定の生の値は `load_fill_sources` が通期の行から引く）。`drop` 側は `fill` 側に含まれるので、
共通域で測れるのは「埋めた行を学習に足すと財務が揃った社の予測がどう変わるか」で、広がった社の効果は raw で
読む（`--fin-nan` と同じ形）。埋めた行が0、または `fill` 側のサンプルが増えないときは CV の前に止める
（`fin_fill_reach_problems`）。日中キューの名前は `gate:fin-fill`。PER → 益利回りの置き換えは入れていない
（`per` は分割補正係数 F を掛けた値で `stock_price` は補正しない列なので、赤字の行の E/P を作ると基準が食い違う）。

## M-6 の内側 CV の区切り方を比べるモード（`--inner-cv`・#971）

M-6 は α・l1_ratio を学習 fold の中の時系列分割 CV で選ぶ。`rows` は行の番号で区切るので
区切りが月の途中に落ち、月内の並び（社の順）だけで最終モデルの α が 165 倍動く（#964）。`months` は
月の境界でだけ区切る（`model_candidates.month_block_splits`）。#964 で本番（分母）を `months` へ切り替えた
（2026-10-17）。**パネルは2条件で同一**で、パネルの鍵から
この軸を外して1枚を共有する（`panel_cond`）＝母集団は動かず、共通域は全行になる。差し替えるのは M-6 の
CV へ渡す候補の opts だけ（`cv_opts`）。測るのは M-6 だけで（M-2・M-1 にこの軸は無い）、1モデル × 2指標
＝2検定・α 0.025。CV の後、各条件の診断値の `inner_cv` が指定どおりかを確かめ、違えば止める
（`inner_cv_reach_problems`。届かないまま走ると両条件が同じ CV になり「差なし」だけが残る）。日中キューの
名前は `gate:inner-cv`（**積むのは人**）。分母と、他のモードで M-6 を回すときの区切り方（`Cond.inner_cv`
の既定）は M-6 本体の `macro_enet._INNER_CV` から取る（書き写すと、本番を切り替えたときに全ゲートの M-6 が
黙って旧い区切り方を測る）。opts を省くかどうかは本番の値ではなく候補の既定（`_EN_INNER_CV_DEFAULT`）と比べる。

## M-2 の early stopping の検証集合の取り方を比べるモード（`--valid-split`・#976）

M-2 は学習行の末尾 2 割を early stopping の検証集合にし、各 fold の best_iteration の中央値を最終モデルの
木の本数に使う。`valid_rows`（本番・分母）は行の番号で取るので区切りが月の途中に落ち、その月のどの社が
fit 側・検証側に入るかが月内の並びで決まる（#969）。`valid_months` は末尾の月を丸ごと取る
（`macro_gbdt._tail_month_valid_rows`）。`--inner-cv` と同じ形で、**パネルは2条件で同一**（`panel_cond`）、
差し替えるのは M-2 の CV へ渡す候補の opts だけ（`cv_opts`）。測るのは M-2 だけ（種 5 個の平均・#955）で、
1モデル × 2指標 ＝2検定・α 0.025。CV の後、各条件の診断値の `valid_split` が指定どおりかを確かめ、違えば止める
（`valid_split_reach_problems`）。日中キューの名前は `gate:valid-split`（**積むのは人**）。分母と `Cond.valid_split`
の既定は M-2 本体の `macro_gbdt._VALID_SPLIT` から取り、opts を省くかどうかは関数の既定
（`_VALID_SPLIT_DEFAULT`）と比べる（#964 の教訓）。**条件名に接頭辞 `valid_` を付けるのは `--inner-cv` の
`rows` / `months` と区別するため**——同じ名前だと、分母を名前で引く `base_of` が他方の本番を返す。

## M-6 の内側 CV に空白を入れるかを比べるモード（`--inner-cv-purge`・#979）

目的変数は 52 週先リターンなので、内側 CV で学習の最後の月と検証の最初の月が隣り合うと、両者のラベル期間が
ほぼ丸ごと重なる（外側の walk-forward は `embargo_months` で外している・#968）。`nogap`（本番の `months`・分母）と、
間に `LABEL_HORIZON_MONTHS` か月を上限とする空白を空ける `gap`（`months_purged`・`model_candidates.purged_month_plan`）を
比べる。`--inner-cv` と同じ形で、**パネルは2条件で同一**（`panel_cond`）、差し替えるのは M-6 の CV へ渡す候補の
opts だけ（`cv_opts`）。測るのは M-6 だけで、1モデル × 2指標 ＝2検定・α 0.025。CV の後、各条件の診断値の
`inner_cv` が指定どおりかを `inner_cv_reach_problems` で確かめ、違えば止める。`gap` 側が実際に空けた月数（fold の
平均）は `diag.inner_cv_gap` に出る。日中キューの名前は `gate:inner-cv-purge`（**積むのは人**・既定を切り替えるかは
#968）。**分母は本番から取り、本番が `months` でなくなったら止める**（`build_conditions`）——黙って `months` を
分母にすると、本番と比べているつもりで別物を比べる。条件名を `nogap` / `gap` にするのは `--inner-cv` の `months` と
区別するため（`--valid-split` の接頭辞と同じ理由）。

## 規模で中立化した順位を比べるモード（`--size-neutral`・#970）

M-2 / M-6 は毎期、小型株を実際より高く・大型株を低く置く（#957）。予測そのまま（`raw`・本番・分母）と、
月ごとの断面で予測を log(時価総額) へ OLS で回帰した残差（`size_neutral`）を比べる。**予測の後処理で条件を
作る**のでリスク軸と同じく `Cond` は2条件で同一で、パネルも CV も1回（M-2 の種ごとの幅は `rescore_seeds` で
条件の変換を当てて測り直す）。時価総額は `financial_metrics` VIEW（分割補正済み）の通期の行から引き、その月の
断面が使う行と同じ選び方（`_find_applicable_fin` を月末で）で当てる＝先を読まない（`log_market_cap_by_ym`）。
**時価総額の無い行は変換の前に両条件から落とし**、落とした行数を出す（`size_common_keys`）。対象は M-2 / M-6・
4検定・α 0.0125。規模の十分位ごとの placement bias（#947 と同じ定義）を両条件で並べて出す（検定はしない）。
時価総額が1行も取れなければ CV の前に止める。日中キューの名前は `gate:size-neutral`（**積むのは人**・採否は #957）。

## M-2 は種を変えて平均する（`--seeds`・#955）

M-2（XGBoost）は `random_state` を固定していても、種が違えば subsample の抜き方が変わり、同じ
パネルで rank-IC が ±0.005〜0.01 動く。これは M-2 で比べてきた条件どうしの差（0.01〜0.03）と同じ
大きさで、`gate:fin-nan` の M-2 の short_side_spread は 10/14 に有意（p=0.002）、10/15 に有意でなく
（p=0.233）なった（行の並びの違い・#954）。そこで **M-2 は既定で 5 個の種（42〜46）で回し、行ごとに
予測を平均してから**指標と検定を出す（`candidate_bakeoff.run_seeds`）。下流（共通月・共通 (ym,ec)
域・ペアの検定）は無改修。測るのは「5 個の平均のモデル」で、本番の 1 個のモデルより少し安定した
成績になる。種ごとの rank-IC と short_side_spread は `[seeds]` の行と JSON の `seeds` に残す。
`--seeds 1` で従来（種 42 の 1 回）と同じになる。M-6・M-1 は種で結果が変わらないので 1 回のまま。

## M-1 を測るときの注意

M-1 は `macro_nan_ok=False`（strict）で**母集団自体が M-2/M-6 と別物**なので、パネルを共有
できない（`MODEL_SPECS` のパネル種別で分ける）。CV の設定（min_train=6 / step=3 / embargo=12）
は M-2 と同値なので `run_one` をそのまま使えるが、**BIC 特徴量選択を挟む点だけが違う**。

出力は ASCII のみ（Windows cp932 リダイレクト対策・[[feedback_windows_cp932_stdout_symbols]]）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (  # noqa: E402
    DE_RATIO_EXCLUSION_CANDIDATES,
    FinancialMetric,
    FinancialRecord,
    SessionLocal,
)
from model_stats import paired_ic_significance  # noqa: E402
from plugins import get_plugin  # noqa: E402
from plugins.macro_ensemble import _align  # noqa: E402
from plugins.macro_snapshots import (  # noqa: E402
    FIN_BASE_OPTIONS,
    FIN_ROW_SOURCES,
    _avg_ranks,
    _find_applicable_fin,
    _realized_vol,
    _spearman,
    build_snapshots,
    get_producer_scores,
    oof_backtest,
    preload_macro,
    quantile_period_ok,
    use_fin_rows,
)
from plugins.macro_enet import _INNER_CV as _M6_INNER_CV  # noqa: E402
from plugins.macro_gbdt import _VALID_SPLIT as _M2_VALID_SPLIT  # noqa: E402
from plugins.macro_gbdt import _VALID_SPLIT_CHOICES, _VALID_SPLIT_DEFAULT  # noqa: E402
from plugins.model_candidates import (  # noqa: E402
    _EN_INNER_CV_DEFAULT,
    _EN_INNER_CV_PURGED,
    _purge_gap_months,
)
from plugins.utils import coerce_params  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402
from scripts.candidate_bakeoff import (  # noqa: E402
    _load_financials,
    _load_prices,
    _thin,
    run_seeds,
)

_OUT_DIR = Path(__file__).resolve().parent / ".cache"
# M-2 を回す種の数の既定（#955）。予測を平均してから測る。1 で従来どおり種 42 の 1 回。
SEEDS_DEFAULT = 5

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
    **ただし M-1 の本番は #615（2026-09-20）で OFF になった**——この既定値を「今の本番」と
    読まない。今の本番の構成で測るモード（リスク軸）は、画面プラグインの既定から導出する
    （`production_use_macro`・#815。9/21〜10/07 はこの既定のまま M-1 をマクロ込みで測っていた）。

    #615 で軸が5つになった。`build_interactions` / `max_features` の既定も本番構成
    （M-1 は交互作用あり・列数はプラグインの `params_schema()` 既定）で、
    **既存3モードの測定条件は1ビットも動かない**。`max_features=None` は
    「プラグインの既定に従う」の意味で、数値を書き写さないための表現。

    `fin_rows`（#424 子3）は学習パネルが読む行の基準（`use_fin_rows`）。既定 `annual` は
    本番の構成なので、既存モードの測定条件はやはり動かない。

    `demean_target`（#615）は目的変数から月ごとの全銘柄平均を引くか。既定 False は本番の
    構成（素の52週先リターン）で、既存モードの測定条件はやはり動かない。

    `fin_nan_ok`（#938・#916）は財務特徴量の空欄で行を捨てずに NaN で通すか。既定 False は本番の
    構成（全モデルが空欄の行を捨てる）で、既存モードの測定条件はやはり動かない。M-1 には効かない
    （交差項の OLS に NaN が入ると壊れる・`_build`）。

    `de_null_industries`（#940・#927）は D/E を空欄にする業種。既定 `()` は本番の構成（銀行業・保険業は
    VIEW が既に空欄にしている）で、既存モードの測定条件はやはり動かない。差し替えは読み込んだ財務の行
    だけで行い、VIEW は変えない（`null_de_ratio`）。

    `fin_fill`（#950）は「無い」と確かめた空欄（無配の配当利回り・R&D の無い社の R&D 集約度）を 0 で
    埋めるか。既定 False は本番の構成（空欄の行を捨てる）で、既存モードの測定条件はやはり動かない。
    差し替えは読み込んだ財務の行だけで行い、VIEW は変えない（`fill_fin_blanks`）。

    `inner_cv`（#971）は M-6 の α・l1_ratio を選ぶ内側 CV の区切り方。既定は本番の構成
    （`macro_enet._INNER_CV`・#964 で `months`）から取る＝どのモードでも M-6 は本番の区切り方で回る。
    **この欄はパネルを動かさない**——パネルの鍵からは外し（`panel_cond`）、M-6 の CV へ渡す opts に
    だけ効く（`cv_opts`）。`--inner-cv-purge`（#979）の空白付き `months_purged` もこの欄で渡す。

    `valid_split`（#976）は M-2 の early stopping の検証集合の取り方。既定は本番の構成
    （`macro_gbdt._VALID_SPLIT`・`rows`）から取る。`inner_cv` と同じく**パネルを動かさず**、M-2 の CV へ渡す
    opts にだけ効く。
    """
    use_momentum: bool
    momentum_window: int
    use_macro: bool = True
    build_interactions: bool = True
    max_features: int | None = None
    fin_rows: str = "annual"
    demean_target: bool = False
    fin_nan_ok: bool = False
    de_null_industries: tuple[str, ...] = ()
    fin_fill: bool = False
    inner_cv: str = _M6_INNER_CV
    valid_split: str = _M2_VALID_SPLIT


CONDS: dict[str, bool] = {"off": False, "on": True}

# マクロ軸モード（`--macro`・#604）。**基準は「マクロ無し」側**——M-1 は
# `macro_nan_ok=False`（strict）なので、マクロ特徴量を持つ条件のほうが母集団が縮む。
# モメンタムの窓モードで基準を「モメンタム無し」に置いたのと同じ理由で、広い側を分母にする。
MACRO_CONDS: dict[str, bool] = {"nomacro": False, "macro": True}
MACRO_BASE_COND = "nomacro"
# 既定で測るのは M-1 だけ。M-2/M-6 は `macro_nan_ok=True` で欠損を nan として保持するため
# `use_macro` は母集団をほとんど動かさない見込みだが、**母集団の話であって成績を測らない理由
# ではない**（#809）。M-6 の本番は `macro` で、分母（`macro_base_cond`）が M-1（`nomacro`）と
# 違う＝同じ run に入れられないので、既定へ足すと `--models` を渡さない `gate:macro` が止まる。
# M-6 は `--models elasticnet` で別に回す（日中枠 `gate:macro-m6`）。
MACRO_MODELS = ["risk_return"]
# 判定文で使う、マクロ軸の各条件名（`MACRO_CONDS` のキーと一致することをテストが縛る）。
MACRO_ON_COND = "macro"
# パネルを借りるモデルが、自分の既定とずれていないか照合するパネル設定の項目（#809）。
# `_build` が設定元（`MODEL_SPECS`）から読む項目のうち、`Cond` が上書きしないものだけ。
PANEL_CONFIG_KEYS = ("fin_features", "macro_features", "min_coverage", "price_features")

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

# 財務の空欄モード（`--fin-nan`・#938/#916）。**基準は本番の構成（空欄の行を捨てる `drop`）**。
# 対象は M-2 / M-6——同じ gbdt のパネルを共有するので、パネルに効く軸は両方を測る（M-2 は XGBoost が
# NaN を分岐で扱い、M-6 は fold 内の列平均で補完する）。**`drop` 側のパネルは `nan` 側に含まれる**ので、
# 共通域で測れるのは「空欄のある行を学習に足すと、財務が揃った社の予測がどう変わるか」だけ。
# 空欄のある社にも予測が出る、という本来の狙いは共通域の外の水準（raw）で読む。
FIN_NAN_CONDS: dict[str, bool] = {"drop": False, "nan": True}
FIN_NAN_BASE_COND = "drop"
FIN_NAN_MODELS = ["xgb_m2", "elasticnet"]

# D/E の業種除外モード（`--de-exclude`・#940/#927）。**基準は本番の構成（2業種の D/E を残す `keep`）**。
# 対象は M-2 / M-6——既定の財務特徴量が `de_ratio` を含むので、空欄にすると財務の空欄の行を落とす本番の
# 規則でその社が丸ごと外れる（母集団が縮む＝共通域で比べる）。業種名の正本は database 側に1か所。
DE_EXCLUDE_CONDS: dict[str, tuple[str, ...]] = {"keep": (), "exclude": DE_RATIO_EXCLUSION_CANDIDATES}
DE_EXCLUDE_BASE_COND = "keep"
DE_EXCLUDE_MODELS = ["xgb_m2", "elasticnet"]

# 空欄を 0 で埋めるモード（`--fin-fill`・#950）。**基準は本番の構成（空欄の行を捨てる `drop`）**。
# 埋めるのは書類の実物で「無い」と確かめた空欄だけ（#950 のコメント・2026-10-15）: 無配の社は 1 株配当を
# 「−」で載せ、R&D の無い社は「研究開発活動 該当事項はありません」と書く。**株価が空欄の行は埋めない**
# （赤字・無配とは関係のない欠けで、0 にすると別の意味になる）。`drop` 側のパネルは `fill` 側に含まれる
# ので、共通域で測れるのは「埋めた行を学習に足すと、財務が揃った社の予測がどう変わるか」。広がった社の
# 予測が当たるかは raw の水準で読む（`--fin-nan` と同じ形）。
FIN_FILL_CONDS: dict[str, bool] = {"drop": False, "fill": True}
FIN_FILL_BASE_COND = "drop"
FIN_FILL_MODELS = ["xgb_m2", "elasticnet"]
# 埋める判定に使う列（VIEW の生の値）。財務の行（`_FIN_FIELDS`）には入っていないので別に引く。
FIN_FILL_SOURCE_FIELDS = ("dps", "stock_price", "pl_rd_expenses", "pl_revenue")

# M-6 の内側 CV の区切り方を比べるモード（`--inner-cv`・#971）。**基準は本番の構成**（M-6 本体の
# `macro_enet._INNER_CV`・#964 で `months`）で、書き写さずにそこから取る。条件名は候補実装の選択肢の名前そのもの。
# **条件は事前登録した `rows` / `months` の2つに固定する**（ADR-0050 の 2026-10-17 追記）——選択肢の一覧から
# 作ると、空白付きの `months_purged`（#979）を足した時点で黙って3条件になり、検定数と α が変わる。
# 2条件のパネルは同一で、M-6 の CV へ渡す候補の opts だけを差し替える。
# M-2・M-1 にこの軸は無いので測らない（同じ CV を2回回すだけになる）。
INNER_CV_CONDS: tuple[str, ...] = ("rows", "months")
INNER_CV_BASE_COND = _M6_INNER_CV
INNER_CV_MODELS = ["elasticnet"]

# M-6 の内側 CV に空白を入れるかを比べるモード（`--inner-cv-purge`・#979）。条件名は {名前: 区切り方}。
# `nogap` は本番の `months`（分母）、`gap` は `months_purged`。名前を区切り方そのものにしないのは、
# `--inner-cv` の `months` と同じ名前だと分母を名前で引く `base_of` が取り違えるため。本番が `months` で
# なくなったら `build_conditions` が止める（分母が本番でなくなる）。M-2・M-1 にこの軸は無いので測らない。
INNER_CV_PURGE_CONDS: dict[str, str] = {"nogap": "months", "gap": _EN_INNER_CV_PURGED}
INNER_CV_PURGE_BASE_COND = "nogap"
INNER_CV_PURGE_MODELS = ["elasticnet"]

# M-2 の early stopping の検証集合の取り方を比べるモード（`--valid-split`・#976）。**基準は本番の構成**（M-2 本体の
# `macro_gbdt._VALID_SPLIT`・`rows`）で、書き写さずにそこから取る。条件名は {名前: 取り方}。取り方は関数の選択肢
# そのもので、名前には接頭辞 `valid_` を付ける——`--inner-cv` の `rows` / `months` と同じ名前にすると、分母を
# 名前で引く `base_of` が `--inner-cv` の本番（`months`）を返す。M-6・M-1 にこの軸は無いので測らない。
VALID_SPLIT_CONDS: dict[str, str] = {f"valid_{s}": s for s in _VALID_SPLIT_CHOICES}
VALID_SPLIT_BASE_COND = f"valid_{_M2_VALID_SPLIT}"
VALID_SPLIT_MODELS = ["xgb_m2"]

# 規模で中立化した順位を比べるモード（`--size-neutral`・#970）。**基準は本番の構成（予測そのまま `raw`）**。
# 予測の後処理で条件を作る（リスク軸と同じ形）＝2条件は同じ `Cond` を共有し、CV は1回。`size_neutral` は
# 月ごとの断面で予測を log(時価総額) へ OLS で回帰した残差を予測として使う（#957 の 10/16 の試算の形）。
# 対象は M-2 / M-6（#957 の偏りはこの2つ）。M-1 はパネルが別物（strict）なので測らない。
SIZE_NEUTRAL_CONDS: tuple[str, ...] = ("raw", "size_neutral")
SIZE_NEUTRAL_BASE_COND = "raw"
SIZE_NEUTRAL_MODELS = ["xgb_m2", "elasticnet"]
# 規模の偏りを並べる分位（#947・#951 の「時価総額の十分位」と同じ数）。
SIZE_DECILES = 10
# 予測の後処理で条件を作るモード。CV は `Cond` ごとに1回で、種ごとの成績（変換前の μ̂ のもの）を
# 条件の変換で測り直す（#962）ので、種ごとの残差を受け取る。
SEED_RESCORED_MODES = ("risk_axis", "size_neutral")
# 月平均除去が届いたかの許容（`demean_reach_problems`）。素の側は「全部0」だけを弾き、除去した
# 側は浮動小数の丸めを十分に上回る幅で 0 とみなす（どちらの値も実行時に出力と JSON へ出る）。
DEMEAN_RAW_MIN = 1e-12
DEMEAN_TOL = 1e-9

# リスク軸モード（`--risk-axis`・#709）。**基準は本番の既定（`r2`）**——この軸は母集団も
# μ̂ も動かさない（R は学習に一切入らず、最後に `U = μ − λR` の順位を作るときだけ使う）ので、
# 「本番から動かすとどうなるか」を見る形にする（`--max-features` / `--demean-target` と同じ）。
#
# なぜこの軸を測るのか: #615 でマクロ特徴量は**どの入れ方をしても月内の順位を改善しない**と
# 確定した。機構は「マクロ系列は月末の週の値なので同じ月の全銘柄でほぼ同一＝月内の順位を
# 直接は動かせない」であり、**これはリスク軸には当たらない**（`r_macro` の β は per-stock で
# 同じ月でも銘柄ごとに値が変わる）。マクロが効く余地は μ 側ではなく R 側に残っている。
#
# `mu_only`（リスクを引かない）を分母ではなく条件として置くのは、**「そもそもリスクを引く
# ことが効いているのか」が一度も測られていない**から——既存6モードの rank-IC はすべて μ̂
# だけの順位で、本番が出している `U` の並びは測定の外にあった。同じパネル・同じ CV で測れる
# ので追加費用はほぼゼロ。
RISK_CONDS: dict[str, str | None] = {"mu_only": None, "r2": "r2", "r_macro": "r_macro"}
RISK_NO_AXIS = "mu_only"
# M-1 の本番の並び（λ=1.0・r2）。**分母そのものは `risk_base_cond` が画面プラグインの既定から
# 導出する**——この定数は M-1 の事前登録（ADR-0050 の 2026-09-21 追記・#815 で尺度だけ raw へ）を
# 読み手に示すためのもので、導出結果と一致することをテストが縛る。
RISK_BASE_COND = "r2"
# 既定のモデルは M-1 だけ。#807 で M-6 も画面に U = μ̂ − λR の並びを持ったが、**本番の λ が 0**
# なので本番の並びは `mu_only` であり、M-1（λ=1.0・r2）と分母が違う。分母が違うモデルは
# 同時に測れない（`risk_base_cond` が止める）ので、M-6 を既定へ足すと `--models` を渡さない
# `gate:risk-axis` が止まる。M-6 は `--models elasticnet --lambdas … --risk-scale raw` で別に回す（#808）。
# M-2 も分母が `r_macro@1.0`（画面の既定 λ=1.0 × R_macro）で、`--models xgb_m2` で別に回す（#814）。
RISK_MODELS = ["risk_return"]
# U を画面へ出すプラグイン（λ と横軸の既定の唯一の源）。**`MODEL_SPECS` の設定元とは別物**——
# M-6 のパネル設定は M-2（`macro_gbdt`）から借りているので、そちらから λ を取ると M-6 を
# M-2 の既定 λ=1.0 で黙って測る（#808 で見つけた。数値はもっともらしく出るので失敗として現れない）。
RISK_LAMBDA_PLUGIN: dict[str, str] = {
    "risk_return": "macro_risk_return",
    "xgb_m2":      "macro_gbdt",
    "elasticnet":  "macro_enet",
}
# λ を明示したとき（`--lambdas`）の条件名の区切り。`r2@0.3` のように軸と λ を並べる。
RISK_LAMBDA_SEP = "@"
# R の尺度（`--risk-scale`）。`z` は月内 z-score（ADR-0050 決定3）、`raw` は画面と同じ生の R
# （`static/js/analysis.js` の `_u: r.mu_raw - lambda * r[axis]`）。**日中枠のジョブは M-1 も含めて
# 全て `raw` を明示する**（#815 で M-1 の 9/21 の形を走る前に置き換えた）。`z` は軸の質を同じ強さで
# 比べたいとき用に残し、既定を変えないのは `--risk-axis` 単体の意味を黙って変えないため。
RISK_SCALES = ("z", "raw")
RISK_SCALE_DEFAULT = "z"
# リスク調整後の指標を出す分位数。`oof_backtest` の既定と揃える（別の数にすると
# 素のリターン側の分位と読み比べられない）。
RISK_QUANTILES = 5

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
                    demean_target: bool = False,
                    risk_axis: bool = False,
                    lambdas: list[float] | None = None,
                    models: list[str] | None = None,
                    fin_nan: bool = False,
                    de_exclude: bool = False,
                    fin_fill: bool = False,
                    inner_cv: bool = False,
                    size_neutral: bool = False,
                    valid_split: bool = False,
                    inner_cv_purge: bool = False) -> dict[str, Cond]:
    """条件集合 {名前: Cond} を作る。

    15のモードがある。**同時に使えるのは1つだけ**（下記）:

      既定           … ADR-0045 の昇格ゲートと完全に同じ2条件（`off` / `on`・マクロは ON のまま）
      `windows`      … モメンタム無し ＋ 各窓（#592・ADR-0050）
      `macro`        … マクロ無し ＋ マクロ有り（#604）。モメンタムは既定 OFF に固定
      `interactions` … 交互作用なし ＋ あり（#615）。モメンタム OFF・マクロ ON に固定
      `max_features` … BIC の列数上限を振る（#615）。他の軸は本番構成に固定
      `fin_rows`     … 通期のみ ＋ 通期＋TTM（#424 子3）。他の軸は本番構成に固定
      `demean_target` … 素の目的変数 ＋ 月平均を引いた目的変数（#615）。他の軸は本番構成に固定
      `risk_axis`    … U = μ − λR の R を振る（#709）。**パネルは全条件とも本番構成で同一**
      `fin_nan`      … 財務の空欄の行を捨てる ＋ NaN で通す（#938・#916）。他の軸は本番構成に固定
      `de_exclude`   … 2業種の D/E を残す ＋ 空欄にする（#940・#927）。他の軸は本番構成に固定
      `fin_fill`     … 財務の空欄の行を捨てる ＋ 「無い」空欄を 0 で埋める（#950）。他の軸は本番構成に固定
      `inner_cv`     … M-6 の内側 CV を行で区切る ＋ 月で区切る（#971）。**パネルは2条件で同一**・M-6 だけ
      `size_neutral` … 予測そのまま ＋ 月ごとに log 時価総額の効果を除いた予測（#970）。`Cond` は2条件で同一
      `valid_split`  … M-2 の early stopping の検証集合を行で取る ＋ 月の塊で取る（#976）。**パネルは2条件で同一**・M-2 だけ
      `inner_cv_purge` … M-6 の内側 CV を月の塊で区切る ＋ 学習と検証の間に空白を空ける（#979）。**パネルは2条件で同一**・M-6 だけ

    `inner_cv` / `inner_cv_purge` の `Cond` は2条件で違うが、違うのは CV へ渡す opts だけで、パネルの鍵からは外す
    （`panel_cond`）。`models` を渡したときは M-6 だけであることを確かめる（M-2・M-1 にこの軸は無い）。
    `inner_cv_purge` は本番（`macro_enet._INNER_CV`）が分母の `months` でなければ止める。

    `size_neutral` も `risk_axis` と同じく `Cond` を動かさない（予測の後処理で条件を作る）。`models` を
    渡したときは M-2 / M-6 だけであることを確かめる（M-1 はパネルが別物）。

    **`risk_axis` だけは `Cond` を動かさない。** R はモデルの学習に一切入らず、CV が出した
    μ̂ の順位を作り直すときだけ使うので、パネル構築のパラメータではない。3条件とも同じ
    `Cond` を返すのは意図どおりで、呼び出し側はこれを見て**パネルと CV を1回に畳む**
    ——別々に回すと μ̂ が完全一致する保証が無く、軸の差と混ざる（#697 と同型）。
    `lambdas` を渡すと条件は `mu_only` ＋ 軸 × λ になる（`risk_specs`・#808）。これも全て同一の `Cond`。
    リスク軸では **`models` が必須**で、パネルの `use_macro` は各モデルの画面の既定から取る
    （`production_use_macro`・#815）。画面の並びを測るモードなので、μ̂ を作るパネルも画面と同じ構成にする。

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
             ("--fin-rows", fin_rows), ("--demean-target", demean_target),
             ("--risk-axis", risk_axis), ("--fin-nan", fin_nan), ("--de-exclude", de_exclude),
             ("--fin-fill", fin_fill), ("--inner-cv", inner_cv),
             ("--size-neutral", size_neutral), ("--valid-split", valid_split),
             ("--inner-cv-purge", inner_cv_purge)]
    picked = [name for name, on in modes if on]
    if len(picked) > 1:
        raise ValueError(
            f"{' と '.join(picked)} は同時に指定できません（母集団を動かしうる軸を2つ同時に"
            "振ると、共通域へ制限してもどちらの効果か分離できない）")
    if lambdas and not risk_axis:
        raise ValueError("--lambdas は --risk-axis と一緒にしか使えません（λ はリスク軸の係数）")
    if risk_axis:
        # **全条件が本番構成の同一 `Cond`**（上の docstring を参照）。軸と λ は
        # `risk_specs` が持ち、パネルには現れない。`use_macro` は画面の既定から取る（#815）——
        # `Cond` の既定 True のままだった間は、M-1（本番は OFF）をマクロ込みのパネルで黙って測った。
        # モデルを渡さない呼び出しは止める。M-1 の既定へ倒すと、今度は M-2/M-6（本番は ON）を
        # マクロ無しで黙って測る形になる。
        if not models:
            raise ValueError("--risk-axis のパネルはモデルの画面の use_macro 既定で作る。"
                             "build_conditions に models を渡してください（#815）")
        use_macro = production_use_macro(models)
        return {name: Cond(False, MOM_WINDOW, use_macro) for name in risk_specs(lambdas)}
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
    if fin_nan:
        # 財務の空欄の扱いだけを差し替える。他の軸はすべて本番構成（モメンタム OFF・マクロ ON・
        # 通期のみ・素の目的変数）＝「本番の M-2/M-6 で空欄の社を外さずに通したら」を問う。
        return {name: Cond(False, MOM_WINDOW, fin_nan_ok=flag)
                for name, flag in FIN_NAN_CONDS.items()}
    if de_exclude:
        # D/E を空欄にする業種だけを差し替える。他の軸はすべて本番構成＝「本番の M-2/M-6 で
        # 証券業・その他金融業の D/E を比較から外したら」を問う。
        return {name: Cond(False, MOM_WINDOW, de_null_industries=inds)
                for name, inds in DE_EXCLUDE_CONDS.items()}
    if fin_fill:
        # 「無い」空欄を 0 で埋めるかだけを差し替える。他の軸はすべて本番構成＝「本番の M-2/M-6 で
        # 無配・R&D の無い社を外さずに 0 として通したら」を問う。
        return {name: Cond(False, MOM_WINDOW, fin_fill=flag)
                for name, flag in FIN_FILL_CONDS.items()}
    if valid_split:
        # M-2 の early stopping の検証集合の取り方だけを差し替える。他の軸はすべて本番構成。パネルは2条件で同一
        # （`panel_cond`）。M-6・M-1 にこの軸は無い（#976）。
        others = [m for m in (models or []) if m not in VALID_SPLIT_MODELS]
        if others:
            raise ValueError(
                f"--valid-split は M-2（{', '.join(VALID_SPLIT_MODELS)}）だけを測る。{', '.join(others)} に"
                "この軸は無く、同じ CV を2回回すだけになる（#976）")
        return {name: Cond(False, MOM_WINDOW, valid_split=split)
                for name, split in VALID_SPLIT_CONDS.items()}
    if inner_cv:
        # M-6 の内側 CV の区切り方だけを差し替える。他の軸はすべて本番構成＝「本番の M-6 で α・l1_ratio を
        # 月の境界で区切った CV で選んだら」を問う。パネルは2条件で同一（`panel_cond`）。
        others = [m for m in (models or []) if m not in INNER_CV_MODELS]
        if others:
            raise ValueError(
                f"--inner-cv は M-6（{', '.join(INNER_CV_MODELS)}）だけを測る。{', '.join(others)} に"
                "この軸は無く、同じ CV を2回回すだけになる（#971）")
        return {name: Cond(False, MOM_WINDOW, inner_cv=name) for name in INNER_CV_CONDS}
    if inner_cv_purge:
        # M-6 の内側 CV に空白を入れるかだけを差し替える。他の軸はすべて本番構成。パネルは2条件で同一
        # （`panel_cond`）。M-2・M-1 にこの軸は無い（#979）。
        others = [m for m in (models or []) if m not in INNER_CV_PURGE_MODELS]
        if others:
            raise ValueError(
                f"--inner-cv-purge は M-6（{', '.join(INNER_CV_PURGE_MODELS)}）だけを測る。{', '.join(others)} に"
                "この軸は無く、同じ CV を2回回すだけになる（#979）")
        base_split = INNER_CV_PURGE_CONDS[INNER_CV_PURGE_BASE_COND]
        if _M6_INNER_CV != base_split:
            raise ValueError(
                f"--inner-cv-purge の分母は本番の M-6 の区切り方のはずだが、本番（macro_enet._INNER_CV）は "
                f"{_M6_INNER_CV}、分母は {base_split}。本番と比べているつもりで別物を比べるので止める（#979）")
        return {name: Cond(False, MOM_WINDOW, inner_cv=split)
                for name, split in INNER_CV_PURGE_CONDS.items()}
    if size_neutral:
        # 2条件とも本番構成の同一 `Cond`（パネルも CV も1回）。差は CV の後の予測の変換だけ。
        others = [m for m in (models or []) if m not in SIZE_NEUTRAL_MODELS]
        if others:
            raise ValueError(
                f"--size-neutral は M-2 / M-6（{', '.join(SIZE_NEUTRAL_MODELS)}）だけを測る。"
                f"{', '.join(others)} はパネルが別物（#970）")
        return {name: Cond(False, MOM_WINDOW) for name in SIZE_NEUTRAL_CONDS}
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
    財務の空欄モードの分母も**本番の構成**（`drop`）。こちらは母集団を広げる側が本番ではないが、
    `drop` 側は `nan` 側に含まれる＝分母に置いた側が「母集団の狭い側」になる点に注意（共通域で揃える）。
    D/E の業種除外モードの分母も**本番の構成**（`keep`）。こちらは本番が母集団の広い側。
    空欄を 0 で埋めるモードの分母も**本番の構成**（`drop`）。`--fin-nan` と同じく分母が母集団の狭い側。
    内側 CV のモードの分母も**本番の構成**（`macro_enet._INNER_CV`・#964 で `months`）。この軸はパネルを動かさない。
    検証集合のモードの分母も**本番の構成**（`valid_` ＋ `macro_gbdt._VALID_SPLIT`）。この軸もパネルを動かさない。
    内側 CV の空白のモードの分母も**本番の構成**（`nogap`＝`months`・#979）。この軸もパネルを動かさない。
    規模の中立化のモードの分母も**本番の構成**（予測そのまま `raw`）。この軸もパネルを動かさない。
    リスク軸モードの分母も**本番の構成**だが、モデルごとに違う（M-1 は `r2`・M-6 は `mu_only`）ので
    ここでは決めない。`main` はリスク軸モードでは `risk_base_cond` を使う（#808）。
    """
    for cand in (BASE_COND, MACRO_BASE_COND, INTERACTION_BASE_COND, FIN_ROWS_BASE_COND,
                 DEMEAN_BASE_COND, RISK_BASE_COND, FIN_NAN_BASE_COND, DE_EXCLUDE_BASE_COND,
                 FIN_FILL_BASE_COND, INNER_CV_BASE_COND, SIZE_NEUTRAL_BASE_COND,
                 VALID_SPLIT_BASE_COND, INNER_CV_PURGE_BASE_COND):
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
                               "fin_rows": "_fin_rows", "demean_target": "_demean",
                               "risk_axis": "_risk_axis", "fin_nan": "_fin_nan",
                               "de_exclude": "_de_exclude", "fin_fill": "_fin_fill",
                               "inner_cv": "_inner_cv", "size_neutral": "_size_neutral",
                               "valid_split": "_valid_split", "inner_cv_purge": "_inner_cv_purge"}


def mode_of(windows: list[int] | None = None, macro: bool = False,
            interactions: bool = False, max_features: list[int] | None = None,
            fin_rows: bool = False, demean_target: bool = False,
            risk_axis: bool = False, fin_nan: bool = False, de_exclude: bool = False,
            fin_fill: bool = False, inner_cv: bool = False, size_neutral: bool = False,
            valid_split: bool = False, inner_cv_purge: bool = False) -> str:
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
    if risk_axis:
        return "risk_axis"
    if fin_nan:
        return "fin_nan"
    if de_exclude:
        return "de_exclude"
    if fin_fill:
        return "fin_fill"
    if inner_cv:
        return "inner_cv"
    if size_neutral:
        return "size_neutral"
    if valid_split:
        return "valid_split"
    if inner_cv_purge:
        return "inner_cv_purge"
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
    # 共通域は財務が揃った行だけ（drop 側 ⊂ nan 側）。空欄の社に予測が出る効果は raw の水準で読む（#938）。
    "fin_nan": ("FIN NAN AXIS [common domain = complete-financials rows only; read raw for blank rows]",
                "passing blank financials as NaN did not beat dropping them"),
    # 本番は D/E を残す側（母集団が広い側）。外した側の改善は母集団の縮みと混ざるので共通域で読む（#940）。
    "de_exclude": ("DE EXCLUDE AXIS",
                   "blanking D/E for the candidate industries did not beat keeping it"),
    # 共通域は埋める前から財務が揃っていた行だけ（drop 側 ⊂ fill 側）。広がった社の効果は raw で読む（#950）。
    "fin_fill": ("FIN FILL AXIS [common domain = rows complete before filling; read raw for filled rows]",
                 "filling no-dividend / no-R&D blanks with 0 did not beat dropping those rows"),
    # パネルは2条件で同一（母集団は動かない）。差は M-6 の α・l1_ratio の選び方だけ（#971）。
    # 分母は本番（#964 で `months`）。#964 の前は分母が `rows` で「月で区切っても行で区切るのを上回らない」と
    # 書いていたが、分母が入れ替わったので、どちらの向きでも読める形にした。
    "inner_cv": ("INNER CV AXIS [M-6 only; same panel on both sides]",
                 "the other inner CV split did not beat the production split"),
    # 分母は本番（`valid_rows`）。パネルは2条件で同一で、差は M-2 の early stopping の検証集合だけ（#976）。
    "valid_split": ("VALID SPLIT AXIS [M-2 only; same panel on both sides]",
                    "taking the early-stopping validation set as whole tail months did not beat taking "
                    "the tail rows"),
    # 分母は本番（`nogap`＝`months`）。パネルは2条件で同一で、差は M-6 の内側 CV の空白だけ（#979）。
    "inner_cv_purge": ("INNER CV PURGE AXIS [M-6 only; same panel on both sides]",
                       "leaving a label-horizon gap between the inner training and validation months "
                       "did not beat the production month blocks"),
    # CV は1回で、差は予測の変換だけ。共通域は時価総額のある行（無い行は両条件から落とす・#970）。
    "size_neutral": ("SIZE NEUTRAL AXIS [same CV; rows without market cap dropped from both sides]",
                     "removing the log market cap effect per month did not beat the raw prediction"),
    # 分母はモデルで違う（M-1 は r2・M-6 は mu_only・#808）ので名前を差し込む。
    "risk_axis": ("RISK AXIS",
                  "no risk condition beat the production ordering ({base})"),
}
# `--risk-base` で分母を上書きした run の文（#816）。「production」と書くと画面の既定と取り違える。
_RISK_OVERRIDE_NONE = "no risk condition beat the base ordering ({base})"
# マクロ軸で分母が「マクロ有り」（M-6 の本番）の run の文（#809）。M-1 の文（no-macro baseline）の
# ままだと、外した側（nomacro）を基準にしたように読める。
_MACRO_ON_BASE_NONE = "removing macro (nomacro) did not beat the production configuration (macro)"


def verdict_text(mode: str, n_conds: int, passed: list[str], regressed: list[str],
                 base: str | None = None, production: str | None = None,
                 overridden: bool = False) -> str:
    """判定行の文言を組み立てる（出力の読み違いをテストで縛るために純関数にしてある）。

    窓モードは**窓が2本以上のときだけ** WINDOW SCAN になる（`--windows 12` は2条件で、
    既定ゲートと同じ PROMOTE/REJECT の文言になる）。これは切り出す前からの挙動で変えない。
    `base` はリスク軸モードの分母名（省略時は M-1 の `RISK_BASE_COND`）と、マクロ軸モードの分母名
    （`macro` のときだけ文が変わる・#809）。他モードでは使わない。
    `overridden` は分母を `--risk-base` で上書きした run で、判定行に分母と画面の既定の並び
    （`production`・条件に無ければ None）の両方を出す（#816）。
    """
    # 軸モードは判定文の表（`_AXIS_VERDICTS`）に載っているかで決める。名前を別に並べると、足したモードを
    # 書き漏らしたときにモメンタムの PROMOTE/REJECT の文言で出力され、例外は出ない（#976 で実際に踏んだ）。
    if ((mode in _AXIS_VERDICTS and mode != "windows")
            or (mode == "windows" and n_conds > 2)):
        head, none = _AXIS_VERDICTS[mode]
        if mode == "macro" and base == MACRO_ON_COND:
            none = _MACRO_ON_BASE_NONE
        if mode == "risk_axis":
            b = base or RISK_BASE_COND
            if overridden:
                head = f"{head} [base {b} overridden; screen default {production or '-'}]"
                none = _RISK_OVERRIDE_NONE
            none = none.format(base=b)
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


def seed_spread_line(seeds: dict) -> str:
    """種ごとの rank-IC と short_side_spread の幅を 1 行にする（#955）。

    平均する前の 1 回ごとの揺れの大きさで、条件の差（diff）と見比べるための目安。
    """
    def span(vals: list) -> str:
        v = [x for x in vals if x is not None]
        if not v:
            return "-"
        return f"min={_num(min(v))} max={_num(max(v))} range={_num(max(v) - min(v))}"

    scored_on = f" (scored on {seeds['scored_on']} per seed)" if seeds.get("scored_on") else ""
    return (f"[seeds] n={seeds['n']} rank-IC by seed: {span(seeds['rank_ic_by_seed'])}"
            f" | short_side: {span(seeds['short_side_spread_by_seed'])}{scored_on}")


def risk_axis_seeds(seeds: dict, seed_parts: list, rmap: dict, lam: float,
                    standardize: bool) -> dict:
    """リスク軸の条件ごとに、種ごとの U の並びの rank-IC / short_side_spread を作り直す（#962）。

    リスク軸は μ̂ の CV を `Cond` ごとに 1 回だけ回し、条件ごとに U = μ̂ − λR へ変換する。
    `run_seeds` の `seeds` は**変換前の μ̂** の種ごとの成績なので、そのままだと全条件で同じ幅が
    出る（10/16 の `gate:risk-axis-m2` で 7 条件すべて同じ値）。種ごとの残差（`_seed_parts`）に
    同じ変換と `oof_backtest` を当て、条件の成績と同じ物差しで測り直す。`rmap` が空（`mu_only`）
    なら変換は恒等で、μ̂ の種ごとの成績をこの物差しで測ったものになる。
    """
    return rescore_seeds(
        seeds, seed_parts,
        lambda resid, meta: apply_risk_axis(resid, meta, rmap, lam, standardize=standardize)[:2],
        "U")


def rescore_seeds(seeds: dict, seed_parts: list, transform, scored_on: str) -> dict:
    """種ごとの残差に条件の変換を当て、rank-IC / short_side_spread を測り直す（#962・#970）。

    `transform(resid, meta) -> (resid, meta)` は条件の予測の後処理（リスク軸の U・規模の中立化）。
    予測の後処理で条件を作るモードは CV を `Cond` ごとに1回しか回さないので、`run_seeds` の `seeds`
    （変換前の μ̂ の種ごとの成績）のままだと全条件で同じ幅が出る。渡した dict は書き換えない。
    """
    ics, sss = [], []
    for resid, meta in seed_parts:
        rr, mm = transform(resid, meta)
        o = oof_backtest(rr, n_quantiles=RISK_QUANTILES, meta_by_ym=mm, rebalance_per_year=4)
        ics.append(o["rank_ic"]["mean"])
        sss.append(o.get("short_side_spread"))
    return {**seeds, "rank_ic_by_seed": ics, "short_side_spread_by_seed": sss,
            "scored_on": scored_on}


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


def macro_base_cond(models: list[str]) -> str:
    """マクロ軸モードの分母＝**各モデルの本番の構成**の側の条件名（#809）。

    M-1 は #615 で `use_macro=False` になったので `nomacro`（母集団が広い側＝`MACRO_BASE_COND` とも
    一致し、9/6・9/20 の形は変わらない）。M-6 の本番は `macro`。**本番の構成はモデル自身の画面
    プラグイン（`RISK_LAMBDA_PLUGIN`）から取る**——`MODEL_SPECS` の設定元は M-6 で M-2 を指すので、
    そちらから取ると M-6 の既定を M-2 の既定で黙って代用する（#808 の λ と同じ罠）。

    **分母が違うモデルは同時に測れない**（判定の向きが1本の表で混ざる）ので止める。
    """
    wanted: dict[str, str] = {}
    for model in models:
        if model not in RISK_LAMBDA_PLUGIN:
            raise ValueError(f"{model} の画面プラグインが登録されていません（RISK_LAMBDA_PLUGIN）")
        params = coerce_params(get_plugin(RISK_LAMBDA_PLUGIN[model]).params_schema(), {})
        wanted[model] = MACRO_ON_COND if params["use_macro"] else MACRO_BASE_COND
    names = set(wanted.values())
    if len(names) > 1:
        raise ValueError(
            f"分母（本番の use_macro）がモデルで違います {wanted}。--models で1つずつ別々に回してください")
    return names.pop()


def production_use_macro(models: list[str]) -> bool:
    """リスク軸モードのパネルの `use_macro`＝各モデルの画面プラグインの既定（#815）。

    リスク軸は画面が出す並びを測るので、μ̂ を作るパネルも画面と同じ構成でなければならない。
    M-1 は #615 で OFF、M-2・M-6 は ON。導出は `macro_base_cond` と同じ（`RISK_LAMBDA_PLUGIN` の
    既定）で、書き写さない。既定がモデルで違えば1枚の `Cond` に畳めないので止まる。
    """
    return MACRO_CONDS[macro_base_cond(models)]


def panel_config_mismatches(models: list[str]) -> list[str]:
    """パネルを借りるモデルの設定が、自分の画面プラグインの既定とずれている項目を列挙する（#809）。

    `_build` は種別ごとに1枚のパネルを作り、設定は `MODEL_SPECS` の設定元から読む——M-6 は
    M-2（`macro_gbdt`）の設定で作られる。ここがずれると **M-6 を M-6 ではない構成で測り、数値は
    もっともらしく出る**。空リストなら健全（今は一致している）。比べるのは `Cond` が上書きしない
    項目だけ（`PANEL_CONFIG_KEYS`）。
    """
    problems = []
    for model in models:
        source = MODEL_SPECS[model][0]
        own = RISK_LAMBDA_PLUGIN.get(model, source)
        if own == source:
            continue
        a = coerce_params(get_plugin(source).params_schema(), {})
        b = coerce_params(get_plugin(own).params_schema(), {})
        for key in PANEL_CONFIG_KEYS:
            if a.get(key) != b.get(key):
                problems.append(f"{model}: {key} が {own} の既定と {source}（パネルの設定元）で違う")
    return problems


def _build(kind: str, args, prices_by_co, fin_by_co, companies, macro_cache,
           use_momentum: bool, mom_window: int, use_macro: bool = True,
           build_interactions: bool = True, max_features: int | None = None,
           demean_target: bool = False, fin_nan_ok: bool = False) -> tuple:
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

    `fin_nan_ok`（#938）は財務の空欄を NaN で通すか。**M-1 には効かせない**——M-1 は交差項を作り
    OLS で学習するので、NaN が入ると壊れる（`build_snapshots` の注意書き）。
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
        fin_nan_ok=(kind != "m1" and fin_nan_ok),
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


def fin_nan_reach_problems(samples: dict[str, int], kinds: list[str]) -> list[str]:
    """`{"条件|種別": サンプル数}` から、財務の空欄の切り替えが届いていない種別を列挙する（#938）。

    空リストなら健全。`nan` 側は `drop` 側の行をすべて含むので、**サンプル数が増えていなければ
    切り替えが断面に届いていない**（空欄の財務を持つ行が1つも無い、または引数が渡っていない）。
    そのまま走ると両条件が同じパネルになり、エラーを出さずに「差なし」だけが残る。減っていたら
    包含関係そのものが崩れている（drop 側に無い行が nan 側にしか無い、の逆）。
    """
    problems = []
    for kind in kinds:
        n_drop = samples.get(f"{FIN_NAN_BASE_COND}|{kind}", 0)
        for cond in FIN_NAN_CONDS:
            if cond == FIN_NAN_BASE_COND:
                continue
            n_nan = samples.get(f"{cond}|{kind}", 0)
            if n_nan <= n_drop:
                problems.append(f"{cond}|{kind}: サンプル数 {n_nan} が {FIN_NAN_BASE_COND} 側の "
                                f"{n_drop} より多くない（空欄を通しても行が増えていない）")
    return problems


# ── 財務の空欄の内訳（`--fin-nan`・#916）────────────────────────────────────────
#
# 共通域は財務が揃った行だけになる（drop 側は nan 側に含まれる）ので、空欄の社の予測の質は共通域の検定では
# 読めない。各条件の OOF を「どの列が空欄か」と会計基準で群に分けて読む。
#
# 列の群は 2026-10-13 の実測（`financial_metrics` 通期・2018 年度以降 30,553 行・読み取りのみ）で決めた。
# 25 列のどれかが空欄の行は 19,958 で、最多は rd_intensity（14,055 行・うち 6,713 行はそれだけが空欄）、
# 次いで div_yield 7,792・per 6,766・成長率系 4,459〜5,751（前期の行が無い）・op_margin 1,512。
FIN_BLANK_GROUPS: dict[str, tuple[str, ...]] = {
    "rd_intensity": ("rd_intensity",),
    "div_yield": ("div_yield",),
    "per": ("per",),
    "growth": ("eps_growth", "op_growth", "rev_growth", "delta_roe", "delta_op_margin"),
    "op_margin": ("op_margin",),
}
# 会計基準が引けなかった行のラベル（`accounting_standard` が空欄、または断面が使う行が無い）
STANDARD_UNKNOWN = "unknown"


def _iso(d) -> str | None:
    """period_end を ISO 文字列へ（date でもキャッシュ由来の文字列でも同じ形にする）。"""
    if not d:
        return None
    return d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]


def fin_blank_labels(samples_by_ym: dict, ids_by_ym: dict, feats: list,
                     fin_names: list[str]) -> dict[tuple[str, str], list[str]]:
    """パネルの各 (ym, ec) に、財務の空欄の群のラベルを付ける（#916）。

    `complete` … 財務の列に空欄なし / `any_blank` … 1 つ以上空欄
    `blank:<群>` … その群の列のどれかが空欄（他の列の空欄は問わない）
    `only:<群>` … 空欄がその群の列だけ（本番で外れる理由がその群だけ）

    空欄は NaN で判定する（`build_snapshots(fin_nan_ok=True)` が空欄を NaN で入れる）。drop 側のパネルは
    全行が `complete` になる。`fin_names` に無い列（マクロ・交差項）は見ない。
    """
    idx = {n: feats.index(n) for n in fin_names if n in feats}
    out: dict[tuple[str, str], list[str]] = {}
    for ym, rows in samples_by_ym.items():
        ids = ids_by_ym.get(ym, [])
        for j, (x, _y) in enumerate(rows):
            if j >= len(ids):
                break
            blank = {n for n, k in idx.items() if x[k] != x[k]}
            labels = ["any_blank" if blank else "complete"]
            for g, cols in FIN_BLANK_GROUPS.items():
                if blank & set(cols):
                    labels.append(f"blank:{g}")
                    if blank <= set(cols):
                        labels.append(f"only:{g}")
            out[(ym, ids[j])] = labels
    return out


def load_standards(db) -> dict[tuple[str, str], str]:
    """通期の行の会計基準を {(edinet_code, period_end の ISO): 基準} で返す（#916・読み取りのみ）。

    `financial_metrics`（パネルの財務の出どころ）は `financial_records` の通期の行なので、同じ
    (edinet_code, period_end) で突き合わせられる。`_FinRow` に列を足さないのは、本番の読み込み
    （`FIN_LOAD_FIELDS`）とキャッシュの形を計測のために変えないため。
    """
    rows = (db.query(FinancialRecord.edinet_code, FinancialRecord.period_end,
                     FinancialRecord.accounting_standard)
            .filter(FinancialRecord.period_type == "annual").all())
    return {(ec, _iso(pe)): std for ec, pe, std in rows if std and pe}


def standard_labels(ids_by_ym: dict, fin_by_co: dict,
                    std_by_pe: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
    """各 (ym, ec) に、その月の断面が使う財務の行の会計基準を `std:<基準>` で付ける（#916）。

    行の選び方は `build_snapshots` と同じ `_find_applicable_fin`。断面日は月末の上限
    （`_month_end_bound`）で近似する——実際の断面日はその月の最終取引日なので、決算日＋45日が
    その間に落ちる行だけ 1 期ずれうる（群のラベルにしか使わない）。
    """
    out: dict[tuple[str, str], str] = {}
    for ym, ids in ids_by_ym.items():
        snap = _month_end_bound(ym)
        for ec in ids:
            rec = _find_applicable_fin(fin_by_co.get(ec, []), snap)
            std = std_by_pe.get((ec, _iso(rec.period_end))) if rec is not None else None
            out[(ym, ec)] = f"std:{std or STANDARD_UNKNOWN}"
    return out


def blank_group_breakdown(aligned: dict, labels: dict) -> dict[str, dict]:
    """群ごとの rank-IC と placement bias を返す（#916）。**検定はしない**。

    `aligned` は `_align` の {(ym, ec): (yhat, y)}、`labels` は {(ym, ec): [群, ...]}。

    - `rank_ic`: 期ごとに群の中だけで Spearman(yhat, y) を取った期平均。群の中で順位を当てられているか
    - `placement_bias`: 期ごとに、全体の中での予測の順位分位 − 実現の順位分位を群で平均し、その期平均。
      **正なら群を実際より上（買い側）に置いている**。空欄そのものが効いて群ごと持ち上げる・沈める形は
      群の中の rank-IC には出ず、ここに出る。`placement_bias_t` は期をまたいだ平均 ÷ 標準誤差（参考値）

    全体が 3 社未満の期は読まない。群の中が 3 社未満の期は rank-IC から外す（`oof_backtest` と同じ下限）。
    """
    by_ym: dict[str, list] = defaultdict(list)
    for (ym, ec), (yh, y) in aligned.items():
        by_ym[ym].append((ec, yh, y))
    ics: dict[str, list] = defaultdict(list)
    bias: dict[str, list] = defaultdict(list)
    n_samples: dict[str, int] = defaultdict(int)
    for ym in sorted(by_ym):
        rows = by_ym[ym]
        n = len(rows)
        if n < 3:
            continue
        r_pred = _avg_ranks([r[1] for r in rows])
        r_real = _avg_ranks([r[2] for r in rows])
        members: dict[str, list] = defaultdict(list)
        for j, (ec, _yh, _y) in enumerate(rows):
            for g in labels.get((ym, ec), ()):
                members[g].append(j)
        for g, js in members.items():
            n_samples[g] += len(js)
            bias[g].append(sum(r_pred[j] - r_real[j] for j in js) / (len(js) * n))
            ic = _spearman([rows[j][1] for j in js], [rows[j][2] for j in js])
            if ic is not None:
                ics[g].append(ic)
    out: dict[str, dict] = {}
    for g in sorted(n_samples):
        b, ic = bias[g], ics[g]
        b_mean = statistics.mean(b) if b else None
        se = statistics.stdev(b) / math.sqrt(len(b)) if len(b) > 1 else None
        out[g] = {
            "samples": n_samples[g],
            "rank_ic": statistics.mean(ic) if ic else None,
            "rank_ic_std": statistics.pstdev(ic) if len(ic) > 1 else None,
            "ic_periods": len(ic),
            "placement_bias": b_mean,
            "placement_bias_t": (b_mean / se) if se else None,
            "bias_periods": len(b),
        }
    return out


# 行ごとの OOF の CSV の列（#951）。`blank_group_breakdown` へ渡す `aligned` と `labels` を
# そのまま行にしたもので、この CSV だけから群の集計を計算し直せる。
OOF_CSV_COLUMNS = ("cond", "model", "ym", "edinet_code", "yhat", "y", "standard", "blank_groups")


def oof_rows(aligned: dict, labels: dict) -> list[tuple]:
    """群の内訳と同じ入力を、行ごとの OOF にして返す（#951）。

    1 行は `(ym, ec, yhat, y, standard, blank_groups)` で、`(ym, ec)` の順に並べる。
    `standard` は `std:<基準>` のラベルから接頭辞を外した値（無ければ `STANDARD_UNKNOWN`）、
    `blank_groups` は残りのラベル（`complete` / `any_blank` / `blank:<群>` / `only:<群>`）を
    `;` でつないだもの。群の集計（`fin_blank_breakdown`）は群ごとの平均しか残さないので、
    規模や業種を揃えて読み直すにはこの行が要る。
    """
    rows: list[tuple] = []
    for (ym, ec) in sorted(aligned):
        yh, y = aligned[(ym, ec)]
        tags = labels.get((ym, ec), ())
        std = next((t[len("std:"):] for t in tags if t.startswith("std:")), STANDARD_UNKNOWN)
        groups = ";".join(t for t in tags if not t.startswith("std:"))
        rows.append((ym, ec, yh, y, std, groups))
    return rows


def write_oof_csv(path: Path, rows_by_run: dict[str, list[tuple]]) -> int:
    """`{"条件|モデル": oof_rows の戻り値}` を CSV へ書き、書いた行数を返す（#951）。

    浮動小数は `repr` で書く——丸めると順位が同点に化け、CSV から群の集計が再現しなくなる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(OOF_CSV_COLUMNS)
        for run, rows in rows_by_run.items():
            cond, model = run.split("|", 1)
            for ym, ec, yh, y, std, groups in rows:
                w.writerow((cond, model, ym, ec, repr(float(yh)), repr(float(y)), std, groups))
                n += 1
    return n


def null_de_ratio(fin_by_co: dict, industries: tuple[str, ...]) -> tuple[dict, int]:
    """指定業種の財務の行の `de_ratio` を空欄にした写しと、空欄にした行数を返す（#940）。

    **元の dict と行は書き換えない**（他の条件が同じ財務を読むため）。M-2/M-6 は生の `de_ratio` だけを
    使い、他の会社の値に波及しないので、本番の VIEW の業種の条件を広げたときと同じになる。
    Zスコア（`z_de_ratio`）は年度ごとに標準化し直す必要があるのでここでは触らない（factor premia は #941）。
    既に空欄の行は数えない（届いたかの検査に使うため、実際に値を消した行だけを数える）。
    """
    inds = set(industries)
    out: dict = {}
    n = 0
    for ec, recs in fin_by_co.items():
        new_recs = []
        for rec in recs:
            if rec.industry in inds and rec.de_ratio is not None:
                rec = rec._replace(de_ratio=None)
                n += 1
            new_recs.append(rec)
        out[ec] = new_recs
    return out, n


def de_exclude_reach_problems(nulled_rows: int, samples: dict[str, int],
                              kinds: list[str]) -> list[str]:
    """D/E の業種除外の切り替えが断面に届いていない理由を列挙する（#940）。空リストなら健全。

    空欄にした行が0なら（業種名の表記ずれ・該当業種の D/E が元から空欄）、両条件は同じ財務で組まれる。
    `exclude` 側のサンプル数が `keep` 側より少なくなければ、空欄が行の除外まで届いていない
    （M-2/M-6 の既定の特徴量に `de_ratio` が無い、など）。どちらもエラーを出さずに「差なし」だけが残る。
    """
    problems = []
    if nulled_rows <= 0:
        problems.append("D/E を空欄にした財務の行が0（業種名の表記ずれ、または該当業種の D/E が元から空欄）")
    for kind in kinds:
        n_keep = samples.get(f"{DE_EXCLUDE_BASE_COND}|{kind}", 0)
        for cond in DE_EXCLUDE_CONDS:
            if cond == DE_EXCLUDE_BASE_COND:
                continue
            n_ex = samples.get(f"{cond}|{kind}", 0)
            if n_ex >= n_keep:
                problems.append(f"{cond}|{kind}: サンプル数 {n_ex} が {DE_EXCLUDE_BASE_COND} 側の "
                                f"{n_keep} より少なくない（空欄が行の除外まで届いていない）")
    return problems


def load_fill_sources(db) -> dict[tuple[str, str], tuple]:
    """埋める判定に使う生の値を {(edinet_code, period_end の ISO): FIN_FILL_SOURCE_FIELDS の順} で返す
    （#950・読み取りのみ）。

    `load_standards` と同じく通期の行で突き合わせる。`_FinRow` に列を足さないのは、本番の読み込みと
    キャッシュの形を計測のために変えないため。
    """
    cols = [getattr(FinancialRecord, f) for f in FIN_FILL_SOURCE_FIELDS]
    rows = (db.query(FinancialRecord.edinet_code, FinancialRecord.period_end, *cols)
            .filter(FinancialRecord.period_type == "annual").all())
    return {(r[0], _iso(r[1])): tuple(r[2:]) for r in rows if r[1]}


def fill_fin_blanks(fin_by_co: dict, sources: dict) -> tuple[dict, dict[str, int]]:
    """「無い」と確かめた空欄を 0 で埋めた財務の写しと、列ごとに埋めた行数を返す（#950）。

    - `rd_intensity`: 空欄で、`pl_rd_expenses` が空欄かつ売上 > 0 の行（R&D の無い社）
    - `div_yield`: 空欄で、`dps` が空欄か 0 かつ株価 > 0 の行（無配の社）

    **株価が空欄の行・売上が空欄か 0 の行は埋めない**（別の理由の欠けで、0 にすると意味が変わる）。
    **元の dict と行は書き換えない**（`drop` 側が同じ財務を読む）。突き合わせる生の値が無い行も埋めない。
    """
    out: dict = {}
    n = {"rd_intensity": 0, "div_yield": 0}
    for ec, recs in fin_by_co.items():
        new_recs = []
        for rec in recs:
            src = sources.get((ec, _iso(rec.period_end)))
            if src is not None:
                dps, price, rd, revenue = src
                upd = {}
                if rec.rd_intensity is None and rd is None and revenue is not None and revenue > 0:
                    upd["rd_intensity"] = 0.0
                if (rec.div_yield is None and (dps is None or dps == 0)
                        and price is not None and price > 0):
                    upd["div_yield"] = 0.0
                if upd:
                    rec = rec._replace(**upd)
                    for k in upd:
                        n[k] += 1
            new_recs.append(rec)
        out[ec] = new_recs
    return out, n


def fin_fill_reach_problems(filled: dict[str, int], samples: dict[str, int],
                            kinds: list[str]) -> list[str]:
    """空欄を 0 で埋める切り替えが断面に届いていない理由を列挙する（#950）。空リストなら健全。

    埋めた行が0なら両条件は同じ財務で組まれる。`fill` 側のサンプル数が `drop` 側より多くなければ、
    埋めた行が断面に入っていない（他の列も空欄の行だけを埋めた、など）。どちらもエラーを出さずに
    「差なし」だけが残る。
    """
    problems = []
    if sum(filled.values()) <= 0:
        problems.append("0 で埋めた財務の行が0（突き合わせる生の値が読めていない可能性）")
    for kind in kinds:
        n_drop = samples.get(f"{FIN_FILL_BASE_COND}|{kind}", 0)
        for cond in FIN_FILL_CONDS:
            if cond == FIN_FILL_BASE_COND:
                continue
            n_fill = samples.get(f"{cond}|{kind}", 0)
            if n_fill <= n_drop:
                problems.append(f"{cond}|{kind}: サンプル数 {n_fill} が {FIN_FILL_BASE_COND} 側の "
                                f"{n_drop} より多くない（埋めた行が断面に入っていない）")
    return problems


# ── M-6 の内側 CV の区切り方（`--inner-cv`・#971）─────────────────────────────

def panel_cond(c: Cond) -> Cond:
    """パネルを作る鍵（#971・#976）。CV だけに効く欄（`inner_cv`・`valid_split`）を本番の値へ戻す。

    `inner_cv` / `valid_split` の2条件は同じパネルを1枚だけ作って共有する——別々に作ると、構築の揺れが
    区切り方の差に混ざる（`--risk-axis` が同じ `Cond` を共有して μ̂ を揃えるのと同じ理由）。他のモードでは
    どちらも既定のままなので、鍵は `c` そのものと等しく、挙動は変わらない。
    """
    return c._replace(inner_cv=INNER_CV_BASE_COND, valid_split=_M2_VALID_SPLIT)


def cv_opts(c: Cond, model: str) -> dict | None:
    """条件が CV へ渡す候補の opts（M-6 の内側 CV・#971／M-2 の検証集合・#976）。候補の既定で回る条件なら None。

    **省けるかは本番の値（`INNER_CV_BASE_COND`）ではなく候補の既定（`_EN_INNER_CV_DEFAULT`）で決める**
    （#964）。本番が `months` のとき本番と比べて省くと、候補は `rows` で回り、本番の構成のつもりで
    旧い区切り方を測る——エラーは出ない。M-2 の `valid_split` も同じ規則で関数の既定（`_VALID_SPLIT_DEFAULT`）と比べる。
    """
    kind = MODEL_SPECS[model][1]
    if kind == "elasticnet" and c.inner_cv != _EN_INNER_CV_DEFAULT:
        return {"inner_cv": c.inner_cv}
    if kind == "xgb_m2" and c.valid_split != _VALID_SPLIT_DEFAULT:
        return {"valid_split": c.valid_split}
    return None


def inner_cv_reach_problems(diags: dict[str, dict], conds: dict[str, Cond]) -> list[str]:
    """内側 CV の切り替えが CV まで届いていない理由を列挙する（#971）。空リストなら健全。

    `diags` は {"条件|モデル": run の `diag`}。候補は既定（`rows`）以外のときだけ診断値に `inner_cv` を積む
    （`make_elasticnet_fit_predict`）ので、積まれていなければ候補の既定で回ったことになる（本番の値では
    ない・#964）。opts が候補まで届かないと両条件が同じ CV になり、エラーを出さずに「差なし」だけが残る。
    """
    problems = []
    for key, diag in diags.items():
        cond = key.split("|", 1)[0]
        want = conds[cond].inner_cv
        got = (diag or {}).get("inner_cv", _EN_INNER_CV_DEFAULT)
        if got != want:
            problems.append(f"{key}: 内側 CV が {got} で回った（指定は {want}）")
    return problems


def valid_split_reach_problems(diags: dict[str, dict], conds: dict[str, Cond]) -> list[str]:
    """M-2 の検証集合の切り替えが CV まで届いていない理由を列挙する（#976）。空リストなら健全。

    候補 `xgb_m2` は関数の既定（`rows`）以外のときだけ診断値に `valid_split` を積む（`_b_xgb_baseline`）ので、
    積まれていなければ関数の既定で回ったことになる。`walk_forward_cv_monthly` は fit_predict の例外を握って
    fold を捨てる（`train_groups` の渡し忘れも失敗として現れない）ので、ここで確かめる。
    """
    problems = []
    for key, diag in diags.items():
        cond = key.split("|", 1)[0]
        want = conds[cond].valid_split
        got = (diag or {}).get("valid_split", _VALID_SPLIT_DEFAULT)
        if got != want:
            problems.append(f"{key}: 検証集合が {got} で取られた（指定は {want}）")
    return problems


# ── 規模で中立化した順位（`--size-neutral`・#970）──────────────────────────────

def load_market_caps(db) -> dict[tuple[str, str], float]:
    """通期の行の時価総額を {(edinet_code, period_end の ISO): 時価総額} で返す（#970・読み取りのみ）。

    出どころは `financial_metrics` VIEW（パネルの財務の出どころと同じ通期の行）。**`financial_records` の
    生の値は使わない**——分割のあった年は遡及調整済みの株価 × 提出当時の株数で 1/F ずれ、VIEW が ×F で
    直している（ADR-0055）。`_FinRow` に列を足さないのは、本番の読み込みとキャッシュの形を計測の
    ために変えないため（`load_standards` と同じ）。
    """
    rows = db.query(FinancialMetric.edinet_code, FinancialMetric.period_end,
                    FinancialMetric.market_cap).all()
    return {(ec, _iso(pe)): float(mc) for ec, pe, mc in rows if pe and mc is not None}


def log_market_cap_by_ym(ids_by_ym: dict, fin_by_co: dict,
                         caps: dict[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    """各 (ym, ec) に、その月の断面が使う通期の行の log(時価総額) を当てる（#970）。

    行の選び方は `build_snapshots` と同じ `_find_applicable_fin` を月末の上限（`_month_end_bound`）で
    呼ぶ＝**「決算日 + FINANCIAL_LAG_DAYS ≦ 月末」の最新の行だけ**を使い、先を読まない（会計基準の群の
    `standard_labels` と同じ手順）。その行の時価総額が無い・0 以下なら当てない（両条件から落ちる）。
    """
    out: dict[tuple[str, str], float] = {}
    for ym, ids in ids_by_ym.items():
        snap = _month_end_bound(ym)
        for ec in ids:
            rec = _find_applicable_fin(fin_by_co.get(ec, []), snap)
            if rec is None:
                continue
            mc = caps.get((ec, _iso(rec.period_end)))
            if mc is not None and mc > 0:
                out[(ym, ec)] = math.log(mc)
    return out


def size_common_keys(keys: set, logcap: dict) -> set:
    """時価総額の取れる (ym, ec) だけへ絞る（#970）。**両条件に同じ集合を当てる**＝落ちる行は同じ。

    落とすのは共通域を取る段で、変換の前（`risk_common_keys` と同じ理由）。変換の後で落とすと
    `size_neutral` の条件からだけ社が消え、条件間の母集団がずれる（ADR-0045）。
    """
    return {k for k in keys if k in logcap}


def size_neutralize(resid_by_ym: dict, meta_by_ym: dict, logcap: dict) -> tuple[dict, dict, int]:
    """`yhat` を、月ごとの断面で `yhat` を [定数, log 時価総額] へ OLS で回帰した残差へ置き換える（#970）。

    戻り値は `(resid, meta, 落とした行数)`。時価総額の無い行は落とす。OLS の残差は回帰した列と
    直交するので、変換後の予測と log 時価総額の月内の相関は（数値誤差を除いて）0 になる。社が3社未満か
    log 時価総額が月内で全社同じ月は傾きを推定できないので、平均を引くだけにする（順位は変わらない）。
    残差の月平均は 0 になるが、判定の指標は月の中で完結する（月ごとの平行移動で変わらない）。
    """
    out_r: dict[str, list] = {}
    out_m: dict[str, list] = {}
    dropped = 0
    for ym, pairs in resid_by_ym.items():
        metas = meta_by_ym.get(ym, [])
        rows = []
        for j, (yh, y) in enumerate(pairs):
            meta = metas[j] if j < len(metas) else (None, None)
            x = logcap.get((ym, meta[0]))
            if x is None:
                dropped += 1
                continue
            rows.append((yh, y, meta, x))
        if not rows:
            continue
        n = len(rows)
        mx = sum(r[3] for r in rows) / n
        my = sum(r[0] for r in rows) / n
        sxx = sum((r[3] - mx) ** 2 for r in rows)
        slope = (sum((r[3] - mx) * (r[0] - my) for r in rows) / sxx
                 if n >= 3 and sxx > 0 else 0.0)
        out_r[ym] = [(yh - my - slope * (x - mx), y) for yh, y, _m, x in rows]
        out_m[ym] = [m for _yh, _y, m, _x in rows]
    return out_r, out_m, dropped


def size_decile_labels(logcap: dict, keys: set, n_bins: int = SIZE_DECILES) -> dict:
    """共通域の各 (ym, ec) に、その月の断面での log 時価総額の分位 `size_d<k>`（d0 が最小）を付ける（#970）。

    `blank_group_breakdown` へ渡して、規模の分位ごとの placement bias（#947 と同じ定義）を出すのに使う。
    同じ値の並びは社のコードで決める（実行ごとに分位が入れ替わらない）。
    """
    by_ym: dict[str, list] = defaultdict(list)
    for ym, ec in keys:
        if (ym, ec) in logcap:
            by_ym[ym].append((logcap[(ym, ec)], ec))
    out: dict[tuple[str, str], list] = {}
    for ym, rows in by_ym.items():
        rows.sort()
        m = len(rows)
        for j, (_x, ec) in enumerate(rows):
            out[(ym, ec)] = [f"size_d{j * n_bins // m}"]
    return out


def aligned_from(resid_by_ym: dict, meta_by_ym: dict) -> dict:
    """残差とメタ（同順）を {(ym, ec): (yhat, y)} へ組み直す（#970・`blank_group_breakdown` の入力）。"""
    out: dict = {}
    for ym, pairs in resid_by_ym.items():
        metas = meta_by_ym.get(ym, [])
        for j, (yh, y) in enumerate(pairs):
            if j < len(metas):
                out[(ym, metas[j][0])] = (yh, y)
    return out


# ── リスク軸（`--risk-axis`・#709）─────────────────────────────────────────────

def _month_end_bound(ym: str) -> str:
    """`"2019-08"` → `"2019-08-31"`。**日付ではなく ISO 文字列の上限**として使う。

    `_realized_vol` の絞り込みは `trade_date <= ref_date` の文字列比較なので、月の実日数を
    知らなくても「その月までの全行」を取れる（`"2019-02-28" <= "2019-02-31"` は真）。
    `calendar` を持ち込んで月末を計算すると、閏年の分岐が1つ増えるだけで結果は同じ。
    """
    return f"{ym}-31"


def normalize_lambdas(lambdas: list[float]) -> list[float]:
    """測る λ を昇順・重複なしにする。**0 以下と非有限は止める**（#808）。

    λ=0 の条件は U = μ̂ − 0·R ＝ `mu_only` と同一で、比べても差が**構造的にゼロ**になる。
    数値はもっともらしく出るので失敗として現れない（ADR-0050 の「黙って同じものを比べる形」）。
    重複を落とすのは、同じ λ を2回測っても検定数だけが増えて α が不当に締まるから（窓と同じ）。
    """
    vals = sorted({float(v) for v in lambdas})
    bad = [v for v in vals if not math.isfinite(v) or v <= 0]
    if bad:
        raise ValueError(
            f"λ は正の有限値で指定してください: {bad}（λ=0 は {RISK_NO_AXIS} と同一で、"
            "比べても差が構造的にゼロになる）")
    if not vals:
        raise ValueError("--lambdas に λ が1つもありません")
    return vals


def risk_cond_name(axis: str, lam: float) -> str:
    """λ を明示したときの条件名（`r2@0.3`）。`repr` で書くので異なる λ が同じ名前に潰れない。"""
    return f"{axis}{RISK_LAMBDA_SEP}{lam!r}"


def risk_specs(lambdas: list[float] | None = None,
               default_lam: float = 0.0) -> dict[str, tuple[str | None, float]]:
    """条件名 → `(R の軸, λ)`。**リスク軸の条件名を作るのはここだけ**（#808）。

    - `lambdas` 無し: M-1 の事前登録の形（`mu_only` / `r2` / `r_macro`）。λ は `default_lam`
      （呼び出し側が画面プラグインの既定から解決した値）で、全軸に共通。
    - `lambdas` あり: `mu_only` ＋ 軸 × λ（`r2@0.1` …）。λ は画面と同じ値を明示して測る。

    `mu_only` の λ は 0（軸が無いので変換しない）。R の算出・変換・JSON はこの表だけを読む
    ——条件名から軸を引き直すと、λ 付きの名前で `RISK_CONDS` の直読みが壊れる。
    """
    if not lambdas:
        return {name: (axis, 0.0 if axis is None else float(default_lam))
                for name, axis in RISK_CONDS.items()}
    out: dict[str, tuple[str | None, float]] = {RISK_NO_AXIS: (None, 0.0)}
    for axis in [a for a in RISK_CONDS.values() if a]:
        for lam in normalize_lambdas(lambdas):
            out[risk_cond_name(axis, lam)] = (axis, lam)
    return out


def production_risk(model: str) -> tuple[float, str]:
    """モデルの画面が既定で出す並びの `(λ, 横軸)`。**`RISK_LAMBDA_PLUGIN` からだけ取る**。"""
    if model not in RISK_LAMBDA_PLUGIN:
        raise ValueError(f"{model} は U を画面に出すプラグインが登録されていません（RISK_LAMBDA_PLUGIN）")
    params = coerce_params(get_plugin(RISK_LAMBDA_PLUGIN[model]).params_schema(), {})
    return float(params.get("lambda_risk") or 0.0), str(params.get("risk_axis") or "r2")


def resolve_risk_lambda(models: list[str]) -> float:
    """`--lambdas` 無しのときの λ（全モデルの画面の既定）。0 に解決されたら止める（#808）。

    以前は `models[0]` の設定元（`MODEL_SPECS`）から取っていたので、M-6 は M-2 の λ で、
    M-1 と並べた M-6 は M-1 の λ で黙って測られていた。
    """
    lams = {m: production_risk(m)[0] for m in models}
    vals = set(lams.values())
    if len(vals) > 1:
        raise ValueError(f"λ の既定がモデルで違います {lams}。--lambdas で測る λ を明示してください")
    lam = vals.pop()
    if not lam > 0:
        raise ValueError(
            f"λ が {lam} に解決されました {lams}。λ=0 の並びは {RISK_NO_AXIS} と同一で、比べても"
            "差が構造的にゼロになる。--lambdas で測る λ を明示してください（例: --lambdas 0.1,0.3,1.0）")
    return lam


def risk_base_cond(models: list[str], specs: dict[str, tuple[str | None, float]]) -> str:
    """リスク軸モードの分母＝**各モデルの画面が既定で出している並び**の条件名（#808）。

    M-1 は λ=1.0・r2 なので `r2`（`--lambdas` ありなら `r2@1.0`）、M-6 は λ=0 なので `mu_only`。
    **分母が違うモデルは同時に測れない**（判定の向きが1本の表で混ざる）ので止める。本番の並びが
    条件集合に無い（M-1 に `--lambdas 0.3` だけを渡した等）ときも止める——分母の無い比較になる。
    """
    wanted: dict[str, str] = {}
    for model in models:
        lam, axis = production_risk(model)
        if lam <= 0:
            wanted[model] = RISK_NO_AXIS
            continue
        hit = [name for name, (a, l) in specs.items() if a == axis and l == lam]
        if not hit:
            raise ValueError(
                f"{model} の本番の並び（{axis}・λ={lam}）が条件に無い。--lambdas に {lam} を含めてください")
        wanted[model] = hit[0]
    names = set(wanted.values())
    if len(names) > 1:
        raise ValueError(
            f"分母（本番の並び）がモデルで違います {wanted}。--models で1つずつ別々に回してください")
    return names.pop()


def resolve_risk_base(models: list[str], specs: dict[str, tuple[str | None, float]],
                      override: str | None = None) -> tuple[str, str | None]:
    """リスク軸モードの `(分母, 画面の既定の並び)`。`override` は `--risk-base`（#816）。

    上書きが無ければ分母は画面の既定の並び（`risk_base_cond`・#808）。上書きは「既に既定に勝った
    候補が μ̂ だけの並びに負けないか」のように、**既定ではない並びを基準に測りたい**ときに使う。
    上書き名が条件に無いと分母の無い比較になるので止める。画面の既定の並びが条件に無いのは
    上書き時に限って許す（その run は既定を測らない）——判定行に出すために None で返す。
    """
    if override is None:
        base = risk_base_cond(models, specs)
        return base, base
    if override not in specs:
        raise ValueError(f"--risk-base {override} は条件に無い（{', '.join(specs)}）")
    try:
        production = risk_base_cond(models, specs)
    except ValueError:
        production = None
    return override, production


def risk_by_ym(axis: str | None, prices_by_co: dict, ids_by_ym: dict,
               producer: dict) -> dict[tuple, float]:
    """`{(ym, edinet_code): R}` を返す。`axis` は `RISK_CONDS` の値（`None` は軸なし）。

    **`r2` は時点再現できる。** `_realized_vol` を月末を `ref_date` として呼ぶので、見るのは
    その月までの52週だけ＝未来を見ない。本番の M-1 と同じ関数を呼び、ボラの計算を書き写さない。

    **`r_macro` は時点再現できない。** `get_producer_scores` が返すのは
    `r_macro = √(βᵀΣ_macroβ)` で、β も Σ_macro も**単一 run のスナップショット**である
    （`macro_beta_loadings` に時点列が無く、run は 2026-07 以降の数本しかない）。したがって
    過去のどの月に当てても「その月より後のデータで推定した β」になる。#709 はこれを
    **未来情報を与えた場合の上限**として測ると決めた——ここで負ければ採用しないと結論できる。

    副作用として `r_macro` は**銘柄ごとの定数**（月で変わらない）になる。つまりこの軸は
    「マクロか実現ボラか」と「静的か動的か」を同時に動かす。分離には3つ目の条件が要り、
    #709 のスコープ外（判定文と JSON に注意書きを出す）。
    """
    if axis is None:
        return {}
    out: dict[tuple, float] = {}
    if axis == "r_macro":
        for ym, ids in ids_by_ym.items():
            for ec in ids:
                v = (producer.get(ec) or {}).get("r_macro")
                if v is not None:
                    out[(ym, ec)] = float(v)
        return out
    if axis != "r2":
        raise ValueError(f"未知のリスク軸: {axis}（{', '.join(str(a) for a in RISK_CONDS.values())}）")
    for ym, ids in ids_by_ym.items():
        bound = _month_end_bound(ym)
        for ec in ids:
            v = _realized_vol(prices_by_co.get(ec) or [], bound)
            if v is not None:
                out[(ym, ec)] = float(v)
    return out


def risk_common_keys(risk_maps: dict[str, dict], keys: set) -> set:
    """全ての軸で R が取れる `(ym, ec)` だけへ絞る（軸なしの条件は制約を課さない）。

    **落とすのは共通域を取る段**——変換の後で落とすと、`r_macro` を持たない銘柄が
    その条件だけから消えて条件間の母集団がずれる（ADR-0045 の「縮む側が有利に見える」
    そのもの）。`macro_beta` は全社に付くとは限らないので、これは実際に起きる。
    """
    out = set(keys)
    for rmap in risk_maps.values():
        if not rmap:
            continue        # 軸なし（`mu_only`）は誰も落とさない
        out &= set(rmap)
    return out


def apply_risk_axis(resid_by_ym: dict, meta_by_ym: dict, rmap: dict,
                    lam: float, standardize: bool = True) -> tuple[dict, dict, int]:
    """`yhat` を `U = yhat − λ·z(R)` へ置き換える。戻り値は `(resid, meta, 落とした行数)`。

    **R は月内で z-score 標準化する。** `r2`（年率ボラ）と `r_macro`（√(βᵀΣβ)）はスケールが
    違うので、同じ λ で比べると軸の差ではなく**スケールの差**を測ることになる。月内で揃える
    のは M-1 が月内の順位で評価されるからで、月をまたいだ水準は順位に影響しない。

    月内の R が全て同じ値なら標準偏差が 0 になる。そのときは傾けない（`z=0`）——`r_macro` は
    銘柄ごとの定数なので、1銘柄しか残らない月でこれが起きる。

    `standardize=False` は**画面と同じ生の R**で `U = yhat − λR` を作る（`--risk-scale raw`・#808）。
    λ がスライダーの値と同じ意味になる代わりに、軸の間で同じ λ を比べる意味は無くなる
    （各軸を分母と比べるだけ）。z の λ=1.0 は画面の λ≈1/sd(R) に当たり、M-6 では約 4.7。

    `rmap` が空なら恒等変換（`mu_only`）。
    """
    if not rmap:
        return resid_by_ym, meta_by_ym, 0
    out_r: dict[str, list] = {}
    out_m: dict[str, list] = {}
    dropped = 0
    for ym, pairs in resid_by_ym.items():
        metas = meta_by_ym.get(ym, [])
        rows = []
        for j, (yh, y) in enumerate(pairs):
            meta = metas[j] if j < len(metas) else (None, None)
            r = rmap.get((ym, meta[0]))
            if r is None:
                dropped += 1
                continue
            rows.append((yh, y, meta, r))
        if not rows:
            continue
        if standardize:
            rs = [r for _yh, _y, _m, r in rows]
            mu = sum(rs) / len(rs)
            sd = statistics.stdev(rs) if len(rs) > 1 else 0.0
            out_r[ym] = [(yh - lam * ((r - mu) / sd if sd else 0.0), y)
                         for yh, y, _m, r in rows]
        else:
            out_r[ym] = [(yh - lam * r, y) for yh, y, _m, r in rows]
        out_m[ym] = [m for _yh, _y, m, _r in rows]
    return out_r, out_m, dropped


def quantile_risk_profile(resid_by_ym: dict, n_quantiles: int = RISK_QUANTILES) -> list[dict]:
    """U の分位ごとの「月次リターンの平均・ボラ・シャープ」を返す（index 0 = 最低 U）。

    **素のリターンとの順位相関（rank_ic）だけでは、リスクを下げた効果が罰として出る。**
    高リスク銘柄を避ければ平均リターンは下がりうるが、ばらつきも下がる。U の並びが
    「リスク調整後に」良いかは、分位ごとに月次の系列を作って初めて読める（#709）。

    `oof_backtest` の内部には触らない（あちらは素のリターン側の正本のまま）。期内サンプルが
    `n_quantiles * 2` 未満の月と U が全社同じ月は分位を作らない——`oof_backtest` と同じ除外規則
    （`quantile_period_ok` を共有する・#977）。
    """
    series: list[list[float]] = [[] for _ in range(n_quantiles)]
    for pairs in resid_by_ym.values():
        if not quantile_period_ok(pairs, n_quantiles):
            continue
        ordered = sorted(pairs, key=lambda p: p[0])
        size = len(ordered) / n_quantiles
        for q in range(n_quantiles):
            lo, hi = int(round(q * size)), int(round((q + 1) * size))
            bucket = ordered[lo:hi]
            if bucket:
                series[q].append(sum(y for _yh, y in bucket) / len(bucket))
    out = []
    for q, vals in enumerate(series):
        mean = sum(vals) / len(vals) if vals else None
        vol = statistics.stdev(vals) if len(vals) > 1 else None
        out.append({
            "quantile": q,
            "mean_return": mean,
            "vol": vol,
            "sharpe": (mean / vol) if (mean is not None and vol) else None,
            "n_periods": len(vals),
        })
    return out


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
                         f"モデル既定は {','.join(MACRO_MODELS)}。分母は各モデルの本番の構成で、"
                         "M-6 は --models elasticnet で別に回す（#809）。--windows とは併用不可")
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
    ap.add_argument("--risk-axis", dest="risk_axis", action="store_true",
                    help="U = μ − λR の R を振る（#709）。mu_only / r2 / r_macro の3条件を"
                         "同一パネル・同一 CV で比べる。分母は各モデルの画面の既定の並び"
                         "（M-1 は r2・M-6 は mu_only）")
    ap.add_argument("--lambdas",
                    help="--risk-axis で測る λ をカンマ区切りで明示する（例: 0.1,0.3,1.0・#808）。"
                         "条件は mu_only ＋ 軸 × λ。0 以下は止まる。省略時は画面プラグインの既定 λ"
                         "（0 に解決されたら止まる）")
    ap.add_argument("--risk-scale", dest="risk_scale", choices=RISK_SCALES,
                    help=f"--risk-axis の R の尺度（既定 {RISK_SCALE_DEFAULT}）。z=月内 z-score"
                         "（ADR-0050 決定3）／raw=画面と同じ生の R（λ がスライダーと同じ意味・#808）")
    ap.add_argument("--risk-base", dest="risk_base",
                    help="--risk-axis の分母を条件名で上書きする（例: mu_only・#816）。省略時は"
                         "画面の既定の並び。条件に無い名前は止まる。出力先に _base-<分母> が付く")
    ap.add_argument("--demean-target", dest="demean_target", action="store_true",
                    help="目的変数モード（素の目的変数 と 月ごとの全銘柄平均を引いた目的変数を"
                         "共通域で比べる・#615）。変換は BIC 選択の前に掛かる。"
                         f"モデル既定は {','.join(DEMEAN_MODELS)}。他モードと併用不可")
    ap.add_argument("--fin-nan", dest="fin_nan", action="store_true",
                    help="財務の空欄モード（空欄の行を捨てる drop と NaN で通す nan を共通域で比べる・"
                         f"#938/#916）。モデル既定は {','.join(FIN_NAN_MODELS)}。共通域は財務が揃った行"
                         "だけになるので、空欄の社に予測が出る効果は raw で読む。他モードと併用不可")
    ap.add_argument("--de-exclude", dest="de_exclude", action="store_true",
                    help="D/E の業種除外モード（証券業・その他金融業の D/E を残す keep と空欄にする "
                         f"exclude を共通域で比べる・#940/#927）。モデル既定は {','.join(DE_EXCLUDE_MODELS)}。"
                         "VIEW は変えず、読み込んだ財務の行だけを差し替える。他モードと併用不可")
    ap.add_argument("--fin-fill", dest="fin_fill", action="store_true",
                    help="空欄を 0 で埋めるモード（無配の配当利回りと R&D の無い社の R&D 集約度を 0 にする "
                         f"fill と、空欄の行を捨てる drop を共通域で比べる・#950）。モデル既定は {','.join(FIN_FILL_MODELS)}。"
                         "株価が空欄の行は埋めない。VIEW は変えず、読み込んだ財務の行だけを差し替える。他モードと併用不可")
    ap.add_argument("--inner-cv", dest="inner_cv", action="store_true",
                    help="M-6 の内側 CV の区切り方のモード（α・l1_ratio を選ぶ CV を月の境界で区切る months＝本番"
                         "（#964）と、行で区切る rows を共通域で比べる・#971）。パネルは2条件で同一。"
                         f"モデルは {','.join(INNER_CV_MODELS)} だけ。他モードと併用不可")
    ap.add_argument("--valid-split", dest="valid_split", action="store_true",
                    help="M-2 の early stopping の検証集合のモード（末尾の行で取る valid_rows＝本番と、末尾の月を"
                         "丸ごと取る valid_months を共通域で比べる・#976）。パネルは2条件で同一。"
                         f"モデルは {','.join(VALID_SPLIT_MODELS)} だけ。他モードと併用不可")
    ap.add_argument("--inner-cv-purge", dest="inner_cv_purge", action="store_true",
                    help="M-6 の内側 CV の空白のモード（月の塊で区切る nogap＝本番の months と、学習の最後の月と検証の"
                         "最初の月の間に 52 週先ラベルの重なりを外す空白を空ける gap＝months_purged を共通域で比べる・"
                         f"#979）。パネルは2条件で同一。モデルは {','.join(INNER_CV_PURGE_MODELS)} だけ。他モードと併用不可")
    ap.add_argument("--size-neutral", dest="size_neutral", action="store_true",
                    help="規模の中立化のモード（予測そのまま raw＝本番と、月ごとに予測を log 時価総額へ OLS で回帰した"
                         "残差 size_neutral を共通域で比べる・#970）。CV は1回。時価総額の無い行は両条件から落とす。"
                         f"モデル既定は {','.join(SIZE_NEUTRAL_MODELS)}。他モードと併用不可")
    ap.add_argument("--smoke", action="store_true", help="サンプルを間引いた短時間確認")
    ap.add_argument("--stride", type=int, default=1, help="各月のサンプル間引き幅")
    ap.add_argument("--seeds", type=int, default=SEEDS_DEFAULT,
                    help=f"M-2 を回す種の数。予測を平均してから測る（既定 {SEEDS_DEFAULT}・"
                         "1 で従来どおり 1 回・#955）")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に DB からのフルロードを許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--json", dest="json_path", help="結果 JSON の出力先")
    args = ap.parse_args()
    if args.seeds < 1:
        raise SystemExit(f"--seeds は 1 以上（指定: {args.seeds}）")
    if args.smoke and args.stride <= 1:
        args.stride = 5
    set_refresh(args.refresh_cache)

    # M-1 専用モードの既定モデルは M-1 だけ。M-2/M-6 は交互作用も BIC の列数上限も持たない
    # （本番構成が `build_interactions=False`）＝測る動機が無い。マクロ軸は M-6 も測るが、
    # 分母が M-1 と違うので `--models elasticnet` で別に回す（#809・`macro_base_cond`）。
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
    elif args.risk_axis:
        default_models = RISK_MODELS
    elif args.fin_nan:
        default_models = FIN_NAN_MODELS
    elif args.de_exclude:
        default_models = DE_EXCLUDE_MODELS
    elif args.fin_fill:
        default_models = FIN_FILL_MODELS
    elif args.inner_cv:
        default_models = INNER_CV_MODELS
    elif args.valid_split:
        default_models = VALID_SPLIT_MODELS
    elif args.inner_cv_purge:
        default_models = INNER_CV_PURGE_MODELS
    elif args.size_neutral:
        default_models = SIZE_NEUTRAL_MODELS
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
    if args.lambdas is not None:
        lambdas = [float(x) for x in args.lambdas.replace(" ", "").split(",") if x]
        if not lambdas:
            raise SystemExit("--lambdas に λ が1つもありません")
    else:
        lambdas = None
    if args.risk_scale is not None and not args.risk_axis:
        raise SystemExit("--risk-scale は --risk-axis と一緒にしか使えません")
    if args.risk_base is not None and not args.risk_axis:
        raise SystemExit("--risk-base は --risk-axis と一緒にしか使えません")
    risk_scale = args.risk_scale or RISK_SCALE_DEFAULT
    try:
        conds = build_conditions(windows, macro=args.macro, interactions=args.interactions,
                                 max_features=max_features, fin_rows=args.fin_rows,
                                 demean_target=args.demean_target,
                                 risk_axis=args.risk_axis, lambdas=lambdas, models=models,
                                 fin_nan=args.fin_nan, de_exclude=args.de_exclude,
                                 fin_fill=args.fin_fill, inner_cv=args.inner_cv,
                                 valid_split=args.valid_split,
                                 inner_cv_purge=args.inner_cv_purge,
                                 size_neutral=args.size_neutral)
    except ValueError as e:
        raise SystemExit(str(e))
    # 列数モードの分母は本番値（プラグイン既定）。**ここで数値を書き写さない**。
    prod_max_features = coerce_params(
        get_plugin(MODEL_SPECS[models[0]][0]).params_schema(), {}).get("max_features")
    # リスク軸の λ と分母は**画面プラグインの既定から取る**（`RISK_LAMBDA_PLUGIN`・#808）。
    # 書き写すと本番が変えたときに黙って別物を測り、`MODEL_SPECS` の設定元から取ると M-6 を
    # M-2 の λ で測る。`--lambdas` 無しで λ が 0 に解決されたら止める（差が構造的にゼロ）。
    default_lam: float | None = None
    specs: dict[str, tuple[str | None, float]] = {}
    production_base: str | None = None
    try:
        if args.risk_axis:
            if lambdas is None:
                default_lam = resolve_risk_lambda(models)
            specs = risk_specs(lambdas, default_lam or 0.0)
            base, production_base = resolve_risk_base(models, specs, args.risk_base)
        elif args.macro:
            # マクロ軸の分母は各モデルの本番の構成（#809）。M-1 は `nomacro`・M-6 は `macro`。
            base = macro_base_cond(models)
        else:
            base = base_of(conds, prod_max_features)
    except ValueError as e:
        raise SystemExit(f"中止: {e}")
    # パネルを借りるモデル（M-6 は M-2 の設定で作る）が自分の既定とずれていたら、読み込む前に止める
    # （#809）。ずれたまま走ると別物を測り、数値はもっともらしく出る。
    mismatches = panel_config_mismatches(models)
    if mismatches:
        raise SystemExit("中止: パネルの設定がモデル自身の既定と違います: " + " / ".join(mismatches))
    if args.risk_base is not None:
        print(f"[risk] base={base}（--risk-base で上書き）/ 画面の既定の並び="
              f"{production_base or '条件に無い'}", flush=True)
    if args.risk_axis and len(set(conds.values())) != 1:
        # μ̂ をビット単位で一致させる前提（パネルと CV を1回に畳む）が崩れている。
        raise SystemExit("中止: リスク軸の条件が同一の Cond を共有していません（μ̂ が条件間でずれる）")
    if args.size_neutral and len(set(conds.values())) != 1:
        # 規模の中立化も同じ前提（#970）。CV が2回に割れると、変換の差に CV の揺れが混ざる。
        raise SystemExit("中止: 規模の中立化の条件が同一の Cond を共有していません（μ̂ が条件間でずれる）")
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
                   demean_target=args.demean_target, risk_axis=args.risk_axis,
                   fin_nan=args.fin_nan, de_exclude=args.de_exclude, fin_fill=args.fin_fill,
                   inner_cv=args.inner_cv, size_neutral=args.size_neutral,
                   valid_split=args.valid_split, inner_cv_purge=args.inner_cv_purge)
    default_out = f"momentum_gate{MODE_SUFFIX[mode]}.json"
    if mode == "risk_axis" and models != RISK_MODELS:
        # M-1 と M-6 は分母が違い別々に回す（#808）。同じファイルへ書くと後の run が前の結果を消す。
        default_out = f"momentum_gate{MODE_SUFFIX[mode]}_{'_'.join(models)}.json"
    if mode == "macro" and models != MACRO_MODELS:
        # M-6 は分母が M-1 と違い別に回す（#809）。同じファイルへ書くと M-1 の結果を消す。
        default_out = f"momentum_gate{MODE_SUFFIX[mode]}_{'_'.join(models)}.json"
    if args.risk_base is not None:
        # 分母を上書きした run は、画面の既定を分母にした run（#814 等）と別物。同じ名前へ
        # 書くと前の結果を消す（#816）。
        default_out = default_out[:-len(".json")] + f"_base-{base}.json"
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
        # 財務の空欄の内訳（#916）の会計基準。CPU 計算の前に読み、読取トランザクションを残さない
        std_by_pe = load_standards(db) if mode == "fin_nan" else {}
        # 規模の中立化（#970）の時価総額。同じく CPU 計算の前に読む
        market_caps = load_market_caps(db) if mode == "size_neutral" else {}
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
        # **同じ `Cond` なら同じパネルを使い回す**（#709）。リスク軸モードは3条件とも本番構成の
        # 同一 `Cond` なので、畳まないと同じ構築を3回払ううえ、**μ̂ が3条件で完全一致する保証が
        # 無くなる**——わずかでもずれると軸の差と混ざる（#697 と同型）。他モードは条件ごとに
        # `Cond` が違うので、この畳み込みは1件も当たらず挙動が変わらない。
        # D/E の業種除外モード（#940）: 条件が指定した業種の `de_ratio` だけを空欄にした財務の写しを、
        # (行の基準, 業種) ごとに1回だけ作る。元の `fins` は書き換えない（他の条件が同じ財務を読む）。
        de_nulled: dict[tuple, dict] = {}
        de_nulled_rows = 0
        # 空欄を 0 で埋めるモード（#950）: 埋めた財務の写しを1回だけ作る。元の `fins` は書き換えない。
        filled_fins: dict | None = None
        fin_filled: dict[str, int] = {}
        if any(c.fin_fill for c in conds.values()):
            filled_fins, fin_filled = fill_fin_blanks(fins[FIN_ROWS_BASE_COND], load_fill_sources(db))
            print(f"[fin_fill] financial rows filled with 0: {fin_filled}", flush=True)
        for c in conds.values():
            dkey = (c.fin_rows, c.de_null_industries)
            if c.de_null_industries and dkey not in de_nulled:
                de_nulled[dkey], n_null = null_de_ratio(fins[c.fin_rows], c.de_null_industries)
                de_nulled_rows += n_null
                print(f"[de_exclude] financial rows whose de_ratio was blanked: {n_null}", flush=True)
        built: dict[tuple, tuple] = {}
        for cond, c in conds.items():
            # 鍵は CV だけに効く欄を外した `Cond`（#971）。内側 CV のモードは2条件で1枚を共有する。
            pc = panel_cond(c)
            for kind in kinds:
                if (pc, kind) not in built:
                    fin_src = (de_nulled[(c.fin_rows, c.de_null_industries)]
                               if c.de_null_industries else fins[c.fin_rows])
                    if c.fin_fill:
                        fin_src = filled_fins
                    built[(pc, kind)] = _build(
                        kind, args, prices_by_co, fin_src, companies,
                        macro_cache, c.use_momentum, c.momentum_window,
                        c.use_macro, c.build_interactions, c.max_features,
                        c.demean_target, fin_nan_ok=c.fin_nan_ok)
                s, m, i, feats = built[(pc, kind)]
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
                      f"fin_blank={'nan' if c.fin_nan_ok else 'drop'} "
                      f"de_null_inds={len(c.de_null_industries)} "
                      f"fin_fill={'on' if c.fin_fill else 'off'} "
                      f"inner_cv={c.inner_cv} "
                      f"valid_split={c.valid_split} "
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

        # 財務の空欄モードでも、切り替えが**断面まで届いたか**を CV の前に確かめる（#938）。
        # nan 側は drop 側を含むので、サンプル数が増えていなければ同じパネルを比べることになる。
        fin_nan_samples: dict[str, int] = {}
        if mode == "fin_nan":
            fin_nan_samples = {key: st["samples"] for key, st in stats.items()}
            for key, n in fin_nan_samples.items():
                print(f"[{key}] samples={n}", flush=True)
            problems = fin_nan_reach_problems(fin_nan_samples, kinds)
            if problems:
                raise SystemExit("中止: 財務の空欄の切り替えが断面に届いていません: "
                                 + " / ".join(problems))
            print("[note] the common domain is the drop side (complete financials); the effect "
                  "of predicting companies with blank financials shows only in raw levels",
                  flush=True)

        # D/E の業種除外モードでも、切り替えが**断面まで届いたか**を CV の前に確かめる（#940）。
        de_exclude_samples: dict[str, int] = {}
        if mode == "de_exclude":
            de_exclude_samples = {key: st["samples"] for key, st in stats.items()}
            for key, n in de_exclude_samples.items():
                print(f"[{key}] samples={n}", flush=True)
            problems = de_exclude_reach_problems(de_nulled_rows, de_exclude_samples, kinds)
            if problems:
                raise SystemExit("中止: D/E の業種除外が断面に届いていません: " + " / ".join(problems))

        # 空欄を 0 で埋めるモードでも、切り替えが**断面まで届いたか**を CV の前に確かめる（#950）。
        fin_fill_samples: dict[str, int] = {}
        if mode == "fin_fill":
            fin_fill_samples = {key: st["samples"] for key, st in stats.items()}
            for key, n in fin_fill_samples.items():
                print(f"[{key}] samples={n}", flush=True)
            problems = fin_fill_reach_problems(fin_filled, fin_fill_samples, kinds)
            if problems:
                raise SystemExit("中止: 空欄の 0 埋めが断面に届いていません: " + " / ".join(problems))

        # リスク軸モードの R は CV の前に作る（軸ごとに `(ym, ec) → R`）。パネルは全条件で
        # 同一なので `base` のものを使う。**R は軸ごとに1回だけ作り**、λ 違いの条件で同じ表を
        # 共有する（条件ごとに取ると、同じ中身を λ の数だけ計算するだけ）。
        risk_maps: dict[str, dict] = {}
        if mode == "risk_axis":
            producer = get_producer_scores(db)
            ids0 = panels[(base, MODEL_SPECS[models[0]][2])][2]
            axis_maps = {axis: risk_by_ym(axis, prices_by_co, ids0, producer)
                         for axis in {a for a, _l in specs.values()}}
            risk_maps = {cond: axis_maps[specs[cond][0]] for cond in conds}
            print(f"[risk] scale={risk_scale} base={base} conds=" + ", ".join(
                f"{c}:{specs[c][0] or '-'}@{specs[c][1]:g}({len(risk_maps[c])} rows)"
                for c in conds), flush=True)
            print(f"[risk] r_macro producer companies={len(producer)} "
                  "（**時点不変**: beta と Sigma_macro は単一 run のスナップショット＝"
                  "過去の月にも 2026年推定の値を当てている。未来情報を含む上限として読む）",
                  flush=True)
            if not producer:
                raise SystemExit(
                    "中止: macro_beta の producer が空です（r_macro が全社で作れない）。"
                    "macro_beta_loadings / macro_beta_meta に live の run があるか確認してください")

        # 規模の中立化モードの log 時価総額も CV の前に作る（#970）。パネルは2条件で同一なので
        # `base` のものを使い、行の選び方は断面と同じ（`log_market_cap_by_ym`）。
        logcap: dict[tuple[str, str], float] = {}
        if mode == "size_neutral":
            ids0 = panels[(base, MODEL_SPECS[models[0]][2])][2]
            logcap = log_market_cap_by_ym(ids0, fins[conds[base].fin_rows], market_caps)
            n_ids = sum(len(v) for v in ids0.values())
            print(f"[size] panel rows with market cap: {len(logcap)} / {n_ids} "
                  f"(without: {n_ids - len(logcap)}; dropped from BOTH sides)", flush=True)
            if not logcap:
                raise SystemExit(
                    "中止: 時価総額が1行も取れません（両条件が同じ予測になる）。"
                    "financial_metrics の market_cap と、断面が使う通期の行を確認してください")

        # ── 2. 各条件 × 各モデルを走らせる（残差も受け取る）────────────────────
        results: dict[str, dict] = {}
        # パネルと同じ理由で CV も `Cond` 単位に畳む（#709）。**μ̂ をビット単位で同一にする**
        # のが目的で、速さは副産物。`_parts` は pop で消えるので memo 側に取り分けておく。
        # （この段の残差は raw 水準の表示にしか使わない。主判定が読むのは共通月で走らせ
        #  直した `cparts` のほう。）
        ran: dict[tuple, tuple] = {}
        for cond in conds:
            for model in models:
                estimator, kind = MODEL_SPECS[model][1], MODEL_SPECS[model][2]
                memo = (conds[cond], kind, model)
                if memo not in ran:
                    s, m, i, feats = panels[(cond, kind)]
                    # 種ごとの残差は予測の後処理で条件を作るモードでだけ持つ（条件ごとの種の幅を
                    # 作り直すため・#962・#970）。
                    fresh = run_seeds(estimator, s, m, i, feats, pca=0, seeds=args.seeds,
                                      return_parts=True,
                                      return_seed_parts=(mode in SEED_RESCORED_MODES),
                                      opts=cv_opts(conds[cond], model))
                    ran[memo] = (fresh, fresh.pop("_parts"), fresh.pop("_seed_parts", None))
                out, part, seed_parts = ran[memo]
                if mode == "risk_axis":
                    # 同じ CV の残差へ条件ごとの U 変換を当て、oof を作り直す。**`out` を
                    # 複製する**——memo は3条件で共有しているので、その場で書き換えると
                    # 他の条件の水準まで書き換わる。
                    rr, mm, _drop = apply_risk_axis(part[0], part[1], risk_maps[cond],
                                                    specs[cond][1],
                                                    standardize=(risk_scale == "z"))
                    out = {**out, "oof": oof_backtest(rr, n_quantiles=RISK_QUANTILES,
                                                      meta_by_ym=mm, rebalance_per_year=4)}
                    # `seeds` は変換前の μ̂ の幅なので、条件の U で測り直す（#962）。
                    if out.get("seeds") and seed_parts:
                        out["seeds"] = risk_axis_seeds(out["seeds"], seed_parts, risk_maps[cond],
                                                       specs[cond][1],
                                                       standardize=(risk_scale == "z"))
                if mode == "size_neutral" and cond != base:
                    # 同じ CV の残差へ規模の中立化を当て、oof を作り直す（#970）。`out` はリスク軸と
                    # 同じ理由で複製する。この段は raw の水準の表示用で、時価総額の無い行はここで落ちる。
                    rr, mm, _drop = size_neutralize(part[0], part[1], logcap)
                    out = {**out, "oof": oof_backtest(rr, n_quantiles=RISK_QUANTILES,
                                                      meta_by_ym=mm, rebalance_per_year=4)}
                    if out.get("seeds") and seed_parts:
                        out["seeds"] = rescore_seeds(
                            out["seeds"], seed_parts,
                            lambda r, mt: size_neutralize(r, mt, logcap)[:2], "size_neutral")
                if out.get("error"):
                    print(f"  {model}: ERROR {out['error']}", flush=True)
                results[f"{cond}|{model}"] = out
                o = out["oof"]
                print(f"  [{cond}] {MODEL_LABELS[model]:<18} "
                      f"rank-IC={_num(o['rank_ic']['mean'])} "
                      f"(std={_num(o['rank_ic'].get('std'))}) "
                      f"short_side={_num(o.get('short_side_spread'))} "
                      f"folds={out['n_folds']} ({out['elapsed_sec']}s)", flush=True)
                if out.get("seeds"):
                    print("    " + seed_spread_line(out["seeds"]), flush=True)

        # 内側 CV のモードでは、区切り方の切り替えが**CV まで届いたか**を確かめる（#971）。
        # パネルは同一なので、届かないと両条件が同じ CV になり「差なし」だけが残る。
        # 内側 CV の空白のモード（#979）も同じ確かめ方で足りる（候補は `rows` 以外の区切り方を診断値に積む）。
        if mode in ("inner_cv", "inner_cv_purge"):
            problems = inner_cv_reach_problems(
                {key: out.get("diag") for key, out in results.items()}, conds)
            if problems:
                raise SystemExit("中止: 内側 CV の切り替えが CV に届いていません: " + " / ".join(problems))
            print("[note] both sides share one panel; the only difference is how M-6 picks "
                  "alpha / l1_ratio inside each training fold", flush=True)
        if mode == "inner_cv_purge":
            # 学習月の少ない fold では空白が縮む（`purged_month_plan`）。fold の平均を出して、どれだけ入ったかを読む。
            for key, out in results.items():
                gap = (out.get("diag") or {}).get("inner_cv_gap")
                if gap is not None:
                    print(f"[note] {key}: mean inner CV gap = {gap} months "
                          f"(cap {_purge_gap_months()})", flush=True)
        # 検証集合のモードも同じ（#976）。fit_predict の例外は fold ごと捨てられるので、届かないと
        # 片方の条件の fold が黙って欠けるか、両条件が同じ取り方になる。
        if mode == "valid_split":
            problems = valid_split_reach_problems(
                {key: out.get("diag") for key, out in results.items()}, conds)
            if problems:
                raise SystemExit("中止: 検証集合の切り替えが CV に届いていません: " + " / ".join(problems))
            print("[note] both sides share one panel; the only difference is how M-2 takes "
                  "the early-stopping validation set inside each training fold", flush=True)
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
    cran: dict[tuple, tuple] = {}       # 共通月パネルでも `Cond` 単位に畳む（上と同じ理由）
    for cond in conds:
        for kind in kinds:
            cs, _cm, ci, cfeats = cpanels[(cond, kind)]
            st = _panel_stats(cs, ci, cfeats)
            print(f"[{cond}/{kind}/common-months] months={st['months']} "
                  f"samples={st['samples']} companies={st['companies']} "
                  f"features={st['n_features']}", flush=True)
        for model in models:
            estimator, kind = MODEL_SPECS[model][1], MODEL_SPECS[model][2]
            memo = (conds[cond], kind, model)
            if memo not in cran:
                s, m, i, feats = cpanels[(cond, kind)]
                fresh = run_seeds(estimator, s, m, i, feats, pca=0, seeds=args.seeds,
                                  return_parts=True, opts=cv_opts(conds[cond], model))
                cran[memo] = (fresh, fresh.pop("_parts"))
            out, part = cran[memo]
            cparts[f"{cond}|{model}"] = part
            cruns[f"{cond}|{model}"] = out
            if out.get("error"):
                print(f"  {model}: ERROR {out['error']}", flush=True)
    # 主判定が読む共通月の run でも、内側 CV の切り替えが届いたかを確かめる（#971・上と同じ）。
    if mode in ("inner_cv", "inner_cv_purge"):
        problems = inner_cv_reach_problems(
            {key: out.get("diag") for key, out in cruns.items()}, conds)
        if problems:
            raise SystemExit("中止: 内側 CV の切り替えが CV に届いていません（共通月）: "
                             + " / ".join(problems))
    if mode == "valid_split":
        problems = valid_split_reach_problems(
            {key: out.get("diag") for key, out in cruns.items()}, conds)
        if problems:
            raise SystemExit("中止: 検証集合の切り替えが CV に届いていません（共通月）: "
                             + " / ".join(problems))

    print("\n=== common (ym,ec) restriction ===", flush=True)
    common_results: dict[str, dict] = {}
    common_info: dict[str, dict] = {}
    # リスク軸モードの健全性（#709）。R が全軸で揃わず落とした行数と、U の分位ごとの
    # リスク調整後の姿。他モードでは空のまま。
    risk_dropped: dict[str, int] = {}
    risk_profiles: dict[str, list] = {}
    # 規模の中立化モード（#970）。時価総額が無くて両条件から落とした行数と、規模の十分位ごとの
    # placement bias（#947 と同じ定義）。他モードでは空のまま。
    size_dropped: dict[str, int] = {}
    size_deciles: dict[str, dict] = {}
    for model in models:
        kind = MODEL_SPECS[model][2]
        aligned = {cond: _align(cparts[f"{cond}|{model}"][0], cpanels[(cond, kind)][2])
                   for cond in conds}
        # **全条件の交差**を取る。2条件のときは従来と同じ off ∩ on。
        keys = set.intersection(*[set(aligned[c]) for c in conds])
        # **R が全軸で取れる行だけへ揃えるのは、変換の前（＝この段）**。変換の後で落とすと
        # `r_macro` を持たない銘柄がその条件からだけ消え、母集団が条件間でずれる（ADR-0045）。
        if mode == "risk_axis":
            before = len(keys)
            keys = risk_common_keys(risk_maps, keys)
            risk_dropped[model] = before - len(keys)
            print(f"  [risk] R が全軸で揃わない行を除外: {before - len(keys)} "
                  f"（{before} -> {len(keys)}）", flush=True)
            if not keys:
                raise SystemExit(
                    "中止: R が全軸で揃う (ym,ec) が 0 件です。r_macro の producer が"
                    "評価パネルの銘柄を1社も覆っていない可能性があります")
        # 時価総額の無い行も同じく変換の前に、**両条件から同じ集合で**落とす（#970）。
        if mode == "size_neutral":
            before = len(keys)
            keys = size_common_keys(keys, logcap)
            size_dropped[model] = before - len(keys)
            print(f"  [size] 時価総額の無い行を両条件から除外: {before - len(keys)} "
                  f"（{before} -> {len(keys)}）", flush=True)
            if not keys:
                raise SystemExit("中止: 時価総額のある (ym,ec) が共通域に 0 件です")
        folds = {c: cruns[f"{c}|{model}"]["n_folds"] for c in conds}
        # 共通域へ揃えるために条件ごとに落とした行数（#809）。母集団が動かないとは構造から
        # 推論せず、ここで数えて出す（`use_macro` が strict でも1行も落とさなかった #604 の逆向き）。
        dropped = {c: len(aligned[c]) - len(keys) for c in conds}
        common_info[model] = {
            "n_common": len(keys),
            "n_by_cond": {c: len(aligned[c]) for c in conds},
            "dropped_by_cond": dropped,
            "n_folds": folds,
        }
        per_cond = " ".join(f"{c}={len(aligned[c])}" for c in conds)
        print(f"  {MODEL_LABELS[model]:<18} common={len(keys)} "
              f"({per_cond}) folds={folds}", flush=True)
        print("    dropped to reach the common domain: "
              + " ".join(f"{c}={n}" for c, n in dropped.items()), flush=True)
        if len(set(folds.values())) > 1:
            print("    [warn] fold 数が一致していません（位相が揃っていない可能性）", flush=True)
        # 規模の十分位は共通域で1回だけ作り、両条件に同じラベルを当てる（#970）
        size_labels = size_decile_labels(logcap, keys) if mode == "size_neutral" else {}
        for cond in conds:
            resid, meta = cparts[f"{cond}|{model}"]
            r2, m2 = _restrict(resid, meta, cpanels[(cond, kind)][2], keys)
            if mode == "risk_axis":
                r2, m2, _d = apply_risk_axis(r2, m2, risk_maps[cond], specs[cond][1],
                                             standardize=(risk_scale == "z"))
                risk_profiles[f"{cond}|{model}"] = quantile_risk_profile(r2)
            if mode == "size_neutral":
                if cond != base:
                    r2, m2, _d = size_neutralize(r2, m2, logcap)
                size_deciles.setdefault(model, {})[cond] = blank_group_breakdown(
                    aligned_from(r2, m2), size_labels)
            bt = oof_backtest(r2, n_quantiles=5, meta_by_ym=m2, rebalance_per_year=4)
            common_results[f"{cond}|{model}"] = bt
            print(f"    [{cond}] rank-IC={_num(bt['rank_ic']['mean'])} "
                  f"(std={_num(bt['rank_ic'].get('std'))}) "
                  f"short_side={_num(bt.get('short_side_spread'))} "
                  f"periods={bt.get('n_periods')}", flush=True)
        if mode == "size_neutral":
            # 規模の十分位ごとに、断面全体での予測の順位百分位 − 実現の順位百分位（#947 と同じ定義）。
            # 正なら実際より上（買い側）に置いている。検定はしない（並べて読む・#957）。
            print(f"    size decile placement bias (d0 = smallest; {' / '.join(conds)}):",
                  flush=True)
            for g in sorted(size_deciles[model][base]):
                cells = " ".join(
                    f"{c}={_num((size_deciles[model][c].get(g) or {}).get('placement_bias'))}"
                    for c in conds)
                print(f"      {g:<8} {cells}", flush=True)

    # ── 4. 昇格ゲート判定 ──────────────────────────────────────────────────
    # 検定は common スコープだけで行う。**raw では差を検定できない**——条件ごとに
    # パネルの開始月が違うと `walk_forward_cv_monthly` の test 月が3ヶ月周期の別位相になり、
    # `paired_ic_significance` がペアリングできる共通 test 期が 0 になる（実測で 4検定すべて
    # "common test periods < 2"）。raw は各条件の**水準**としてだけ意味を持つので併記する。
    sigs: dict[str, dict] = {}
    passed: list[str] = []
    regressed: list[str] = []
    test_conds = [c for c in conds if c != base]
    cw = max(6, *(len(c) for c in conds))    # 条件名の列幅（`r_macro@0.3` は6字に収まらない）
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
                print(f"  {MODEL_LABELS[model]:<18} {cond:<{cw}} {metric:<18} "
                      f"{_fmt_sig(sig, alpha)}", flush=True)

    print("\n=== raw levels (each condition's own population; NOT testable across "
          "conditions: fold phases differ) ===", flush=True)
    print(f"  {'cond':<{cw}} {'model':<18} {'rank-IC':>9} {'IC std':>9} {'short':>9} "
          f"{'LS spread':>10} {'folds':>6} {'samples':>9}", flush=True)
    for cond in conds:
        for model in models:
            r = results[f"{cond}|{model}"]
            o = r["oof"]
            st = stats[f"{cond}|{MODEL_SPECS[model][2]}"]
            print(f"  {cond:<{cw}} {MODEL_LABELS[model]:<18} {_num(o['rank_ic']['mean']):>9} "
                  f"{_num(o['rank_ic'].get('std')):>9} "
                  f"{_num(o.get('short_side_spread')):>9} "
                  f"{_num(o.get('long_short_spread')):>10} {r['n_folds']:>6} "
                  f"{st['samples']:>9}", flush=True)

    # 財務の空欄の内訳（#916）。共通域の外＝空欄の社の予測の質は、各条件の OOF を群に分けて読む。
    fin_blank_breakdown: dict[str, dict] = {}
    # 行ごとの OOF（#951）。内訳と同じ `aligned` と `labels` から作り、JSON の隣の CSV へ残す。
    fin_oof: dict[str, list[tuple]] = {}
    fin_oof_path: Path | None = None
    fin_oof_rows = 0
    if mode == "fin_nan":
        fin_names = [o["value"] for o in FIN_BASE_OPTIONS]
        print("\n=== blank-financials breakdown (each condition's own OOF; groups are NOT "
              "tested) ===", flush=True)
        print("[note] rank-IC is within the group. bias = mean(pred rank pct - realized rank pct) "
              "of the group in the whole cross-section (+ = placed higher than realized).",
              flush=True)
        # 会計基準の群の bias は規模の交絡だった（#951・ADR-0050 の 2026-10-16 追記）。同じ数字が毎回出るので隣に書く。
        print("[note] group bias is NOT adjusted for size or sector. On the drop side the std:IFRS "
              "bias vanished within market-cap deciles: IFRS firms cluster in the largest decile, "
              "which the models under-rank (#951, #957).", flush=True)
        group_labels: dict[tuple, dict] = {}
        for cond in conds:
            for model in models:
                kind = MODEL_SPECS[model][2]
                s, _m, ids, feats = panels[(cond, kind)]
                if (cond, kind) not in group_labels:
                    labels = fin_blank_labels(s, ids, feats, fin_names)
                    for key, std in standard_labels(ids, fins[conds[cond].fin_rows],
                                                    std_by_pe).items():
                        labels.setdefault(key, []).append(std)
                    group_labels[(cond, kind)] = labels
                part = ran[(conds[cond], kind, model)][1]
                aligned_run = _align(part[0], ids)
                bd = blank_group_breakdown(aligned_run, group_labels[(cond, kind)])
                fin_blank_breakdown[f"{cond}|{model}"] = bd
                fin_oof[f"{cond}|{model}"] = oof_rows(aligned_run, group_labels[(cond, kind)])
                print(f"  [{cond}] {MODEL_LABELS[model]}", flush=True)
                print(f"    {'group':<20} {'samples':>8} {'rank-IC':>9} {'IC std':>9} "
                      f"{'periods':>8} {'bias':>9} {'bias t':>8}", flush=True)
                for g, row in bd.items():
                    print(f"    {g:<20} {row['samples']:>8} {_num(row['rank_ic']):>9} "
                          f"{_num(row['rank_ic_std']):>9} {row['ic_periods']:>8} "
                          f"{_num(row['placement_bias']):>9} "
                          f"{_num(row['placement_bias_t'], 2):>8}", flush=True)
        # JSON の隣に置く（`--json` で出力先を変えても対になる）。
        fin_oof_path = out_path.with_name(out_path.stem + "_oof.csv")
        fin_oof_rows = write_oof_csv(fin_oof_path, fin_oof)
        print(f"wrote {fin_oof_path} ({fin_oof_rows} rows)", flush=True)

    if mode == "risk_axis":
        # **素のリターンだけでは、リスクを下げた効果が罰として出る**（#709）。判定の主指標は
        # 上の rank_ic のままで、ここは読み手が符号を解釈するための参考値。
        print("\n=== risk-adjusted profile of the U ordering "
              "(quantile 0 = lowest U, monthly series) ===", flush=True)
        print(f"  {'cond':<{max(cw, 8)}} {'q':<3} {'mean ret':>10} {'vol':>10} {'sharpe':>10} "
              f"{'periods':>8}", flush=True)
        for cond in conds:
            for model in models:
                for row in risk_profiles.get(f"{cond}|{model}", []):
                    print(f"  {cond:<{max(cw, 8)}} {row['quantile']:<3} "
                          f"{_num(row['mean_return']):>10} {_num(row['vol']):>10} "
                          f"{_num(row['sharpe']):>10} {row['n_periods']:>8}", flush=True)
        print("[note] r_macro is TIME-INVARIANT per company (beta and Sigma_macro come from a "
              "single 2026 snapshot), so this axis moves 'macro vs realized vol' and "
              "'static vs dynamic' at the same time. A win here is an UPPER BOUND that "
              "includes look-ahead and is NOT grounds for changing the default (#709).",
              flush=True)

    verdict = verdict_text(mode, len(conds), passed, regressed, base=base,
                           production=production_base, overridden=args.risk_base is not None)
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
                              "demean_target": c.demean_target,
                              "fin_nan_ok": c.fin_nan_ok,
                              "de_null_industries": list(c.de_null_industries),
                              "fin_fill": c.fin_fill,
                              "inner_cv": c.inner_cv,
                              "valid_split": c.valid_split}
                       for name, c in conds.items()},
        "mode": mode,
        "base_cond": base,
        # 分母を `--risk-base` で上書きしたか（#816）と、そのときの画面の既定の並び（条件に無ければ None）。
        "risk_base_override": args.risk_base is not None,
        "production_base": production_base,
        "windows": windows,
        "max_features": max_features,
        # 行の基準モードの健全性（#424 子3）。他モードでは None / 空。
        "ttm_rows": ttm_rows,
        "changed_rows": changed_rows,
        "fin_nan_samples": fin_nan_samples,
        # 財務の空欄の内訳（#916）。{"条件|モデル": {群: 指標}}。他モードでは空。
        "fin_blank_groups": FIN_BLANK_GROUPS if mode == "fin_nan" else {},
        "fin_blank_breakdown": fin_blank_breakdown,
        # 行ごとの OOF の CSV（#951・列は `OOF_CSV_COLUMNS`）。他モードでは None / 0。
        "fin_oof_path": str(fin_oof_path) if fin_oof_path else None,
        "fin_oof_rows": fin_oof_rows,
        "de_exclude": {"nulled_rows": de_nulled_rows, "samples": de_exclude_samples},
        # 規模の中立化モード（#970）。時価総額の取れたパネルの行数・共通域で両条件から落とした行数・
        # 規模の十分位ごとの {モデル: {条件: {size_d<k>: 指標}}}。他モードでは 0 / 空。
        "size_neutral": {"panel_rows_with_cap": len(logcap), "dropped_rows": size_dropped,
                         "deciles": size_deciles},
        "fin_fill": {"filled_rows": fin_filled, "samples": fin_fill_samples},
        # 目的変数モードの健全性（#615）。{"条件|種別": 月平均の絶対値の最大}。他モードでは空。
        "target_month_mean": target_month_mean,
        # リスク軸モード（#709）。他モードでは None / 空。
        "risk_axes": {name: specs[name][0] for name in conds} if mode == "risk_axis" else {},
        # `--lambdas` 無しのときの共通 λ（画面プラグインの既定）。明示したときは None で、
        # 条件ごとの λ は `risk_specs` が持つ（#808）。
        "lambda_risk": default_lam,
        "risk_specs": ({name: {"axis": a, "lambda": lam} for name, (a, lam) in specs.items()}
                       if mode == "risk_axis" else {}),
        "risk_scale": risk_scale if mode == "risk_axis" else None,
        "risk_dropped_rows": risk_dropped,
        "risk_profiles": risk_profiles,
        # **勝っても既定を変える根拠にならない**ことを JSON 側にも残す（標準出力は流れる）。
        "risk_axis_caveat": (
            "r_macro is time-invariant per company (beta and Sigma_macro come from a single "
            "2026 snapshot; macro_beta_loadings has no as-of column). Every evaluated month "
            "uses beta estimated from later data, so this is an upper bound that includes "
            "look-ahead, and the axis moves 'macro vs realized vol' and 'static vs dynamic' "
            "together." if mode == "risk_axis" else None),
        "alpha": alpha,
        "n_tests": n_tests,
        "models": models,
        "stride": args.stride,
        "seeds": args.seeds,
        "seeds_by_run": {k: v["seeds"] for k, v in results.items() if v.get("seeds")},
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
