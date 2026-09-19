# ridge の α は一個抜き交差検証（LOO）で選び、行の並び順に依存させない

## Status

accepted（2026-09-19）。Issue [#697](https://github.com/kome-kome/financial_app/issues/697)
（[#626](https://github.com/kome-kome/financial_app/issues/626) の実装中に発見）。

- [ADR-0057](0057-past-gap-ratio-is-reconstructed-as-of-each-month.md) 決定4（行を edinet_code 順に
  固定する）の**理由を置き換える**。順序の固定は残すが、目的は「fold を揃える」から「浮動小数の
  加算順まで揃えて再現を完全にする」へ変わる。

## Context

`plugins/utils.py::ridge_regression` は `RidgeCV(cv=min(cv_folds, max(2, n // 5)))` で α を選んでいた。
`cv` に整数を渡すと sklearn はシャッフルなしの KFold を使うので、**どの社がどの fold に入るかは
行の並び順だけで決まる**。夜間の `sector_ols`（`NIGHTLY_PARAMS` で `regularization=ridge`）は
`_load_records` に ORDER BY を持たず、並びは PostgreSQL の返す順（物理配置・並列スキャン）に依存する。

その結果、**データが1行も変わらなくても、業種単位で α と gap_ratio が夜ごとに跳ねていた**。
2026-09-18 夜間の保存値を同じ入力で回し直すと 4業種の全社が一致せず、他の29業種は完全一致した。
エラーは出ず、どちらの値ももっともらしい乖離率なので、`/api/morning` の gap ブロックや
乖離分析の順位がデータではなく並びで動いていても気づけない。

docstring は当初から「GCV 経由で最適 α を選択」と書いており、実装（KFold）と食い違っていた。
`n < 10` のときだけ `cv=None`（LOO）になっていたので、意図は LOO だったと読める。

同じ種類の並び依存がもう1つあった。`sector_ols._prepare_fit` の `global_pred_map`（薄い業種を
全社プール予測へ縮約する表）のキーが `(edinet_code, year)` で、決算期変更で同じ年度に期末違いの
通期行を2本持つ社（2026-09 時点で12件。例: E37069 の 2025年度は 2025-02-28 と 2025-12-31）は、
後から書いた行の予測が前の行を上書きしていた。縮約の掛かる業種（社数 < `shrink_threshold`）では
どちらの予測で縮約されるかが並びで決まる。

## Decision

1. **`ridge_regression` の α 選択は `RidgeCV(cv=None)`（効率的な LOO）に固定する。** `cv_folds` 引数は
   削除し、全呼び出し経路（夜間の `sector_ols`・時点再現 `sector_gap_asof`・診断用の Fama-MacBeth ridge・
   候補比較の bakeoff）に同じ選び方を効かせる。LOO は各行を1つずつ抜いた誤差の平均なので、
   行の並びに数学的に依存しない。α の候補（`1e-3`〜`1e3` の7点）と採点（二乗誤差）は変えない
2. **`global_pred_map` のキーは `(edinet_code, year, period_end)`**（`regression_results` と
   `predict_gaps` の戻りと同じ3つ組・`sector_ols._row_key`）
3. **並び不変性をテストで縛る。** `tests/test_utils.py::test_ridge_ignores_row_order`（8通りの並べ替えで
   α・係数・予測値が一致）と `tests/test_sector_ols.py::test_gaps_ignore_row_order`（期末違いの重複キーを
   縮約業種に置き、6通りの並べ替えで gap が 0.01 以内で一致）。どちらも旧実装で落ちることを確認した

## 実測（2026-09-19・ローカル正本・本番と同じ母集団と `NIGHTLY_PARAMS`）

旧手続きは sklearn の KFold で再現した（`global_pred_map` のキーは新しい方）。保存値は
2026-09-18 夜間（`computed_at` 最大 08:50 UTC）。gap の単位は %pt。

### 並べ替えへの不変性（edinet_code 順を基準に5通りのシャッフル・3,628社・33業種）

| seed | 旧 KFold: gap が動いた社 | 旧: α が変わった業種 | 旧: Spearman | 新 LOO: 動いた社 | 新: α が変わった業種 |
|---|---|---|---|---|---|
| 1 | 2,029 | 9 | 0.9831 | **0** | **0** |
| 2 | 1,802 | 11 | 0.9530 | **0** | **0** |
| 3 | 1,003 | 9 | 0.9649 | **0** | **0** |
| 4 | 1,563 | 9 | 0.9840 | **0** | **0** |
| 5 | 361 | 7 | 0.9868 | **0** | **0** |

### 切替で本番の値がどれだけ動くか

| 比較 | gap が 0.01 超動いた社 | \|差\| 中央値 | p90 | Spearman |
|---|---|---|---|---|
| 新 LOO vs 9/18 夜間の保存値 | 793 / 3,628 | 0.00 | 13.74 | 0.9594 |
| 旧 KFold（edinet_code 順） vs 9/18 夜間の保存値 | 2,123 / 3,628 | 0.97 | 19.64 | 0.9634 |
| 新 LOO vs 旧 KFold（edinet_code 順） | 1,756 / 3,628 | 0.00 | 19.53 | 0.9613 |

**切替による一度きりの移動は、旧手続きが並びだけで毎晩起こしていた移動と同じ大きさ**
（Spearman 0.95〜0.99 の範囲）に収まる。

### 業種ごとの α（旧 KFold・edinet_code 順 → 新 LOO）

分布は 旧 `{0.001: 3, 1: 8, 10: 13, 100: 9}` → 新 `{0.001: 3, 0.1: 1, 1: 7, 10: 13, 100: 9}`。
変わったのは 33業種中 9業種:

| 業種 | 社数 | 旧 | 新 |
|---|---|---|---|
| ガラス・土石製品 | 49 | 100 | 10 |
| サービス業 | 517 | 0.001 | 10 |
| 卸売業 | 281 | 10 | 100 |
| 情報・通信業 | 592 | 100 | 10 |
| 機械 | 208 | 100 | 0.001 |
| 水産・農林業 | 12 | 10 | 0.1 |
| 証券、商品先物取引業 | 31 | 10 | 100 |
| 鉄鋼 | 37 | 1 | 10 |
| 非鉄金属 | 32 | 10 | 100 |

所要は旧 1.7秒 → 新 0.4秒（sklearn は LOO を行列分解1回で解く）。

## Consequences

- **本番の gap_ratio は並び順で動かなくなる。** マージ後の最初の夜間に一度だけ上の表の分だけ動く
  （Spearman 0.96 前後）。以後の変化はデータの変化による
- `sector_gap_asof.asof_records` の edinet_code 順の固定は残す。LOO で結果は並びに依存しなくなったが、
  浮動小数の加算順まで揃えると学習パネルがビット単位で再現する
- **ADR-0057 の実測値（割安重視 +0.0526・gap 単独 +0.1012 など）は旧手続き（KFold・edinet_code 順）で
  測った値。** 新旧の gap の Spearman は 0.96 なので結論が変わる見込みは小さいが、
  `preset_ic_gate --with-gap-ratio` は重い計算なので、#546 / #625 で昇格を判定するときに測り直す
- 診断用の Fama-MacBeth ridge（`recommend_factor_premia --estimator ridge`・`--persist` 不可）と
  bakeoff の ridge 候補（ADR-0021）は、過去の ADR に残る数値を同じコードでは再現しなくなる。
  どちらも昇格しなかった診断で、本番の値には触れない
- `PREPROCESS_VERSION` は上げない。永続化済みの重み（`build_period_panel` の z_* 7列）は gap_ratio を
  含まず、`with_gap_ratio` の既定は False のまま
- `_persist_and_rank` の `sector_rank` は gap が同値（`None` を 0 扱い）の社同士で並びに依存するが、
  保存されない表示上の順位なので本 ADR の対象外

## Considered Options

- **A. LOO へ切り替え、全経路に効かせる（採用）**
- **B. LOO を既定にし、診断・bakeoff の2経路は KFold(3) を明示で残す** → 却下。過去の診断値は再現できるが、
  並びに依存する経路がコードに残り、将来その指定を写した誰かが同じ問題を再発させる
- **C. 回帰の前に行を edinet_code 順へ並べるだけ（KFold 維持）** → 却下。最小の変更で時点再現と同じ規約に
  なるが、`ridge_regression` 自体は並び依存のまま残る。社が1社増えるだけで後続の社の fold がずれ、
  業種ごと α が切り替わりうる
- **D. KFold を `shuffle=True, random_state=固定` にする（issue の案2）** → 却下。**並び依存は消えない**。
  固定されるのは「行番号の置換」なので、入力の並びが変われば同じ社が別の fold に入る
